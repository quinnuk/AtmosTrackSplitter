"""Read-only completed-download discovery via the qBittorrent Web API."""
from __future__ import annotations

import http.cookiejar
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import HTTPCookieProcessor, Request, build_opener


@dataclass(frozen=True)
class CompletedDownload:
    name: str
    torrent_hash: str
    source_path: Path
    download_root: Path
    size_bytes: int

    @property
    def kind(self) -> str:
        return "ISO image" if self.source_path.suffix.lower() == ".iso" else "Blu-ray folder"


def _request(opener, base_url: str, endpoint: str, data: dict[str, str] | None = None) -> bytes:
    url = urljoin(base_url.rstrip("/") + "/", endpoint.lstrip("/"))
    payload = urlencode(data).encode("utf-8") if data is not None else None
    req = Request(url, data=payload, headers={
        "User-Agent": "AtmosTrackSplitter/1.0",
        "Referer": base_url.rstrip("/") + "/",
    })
    try:
        with opener.open(req, timeout=20) as response:
            body = response.read(10 * 1024 * 1024 + 1)
    except HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", errors="replace")
        if exc.code in (401, 403):
            raise RuntimeError(
                "qBittorrent rejected the login or API request. Check the Web UI credentials "
                "and confirm that the Web UI is enabled."
            ) from exc
        raise RuntimeError(f"qBittorrent returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(
            f"Could not connect to qBittorrent at {base_url}: {exc.reason}. "
            "Check the Web UI address, port and network access."
        ) from exc
    if len(body) > 10 * 1024 * 1024:
        raise RuntimeError("qBittorrent returned an unexpectedly large response.")
    return body


def _map_content_path(remote_path: str, content_root: str, local_root: Path) -> Path:
    remote = PurePosixPath(remote_path)
    remote_root = PurePosixPath(content_root)
    try:
        relative = remote.relative_to(remote_root)
    except ValueError as exc:
        raise ValueError(
            f"qBittorrent returned '{remote_path}', outside QBITTORRENT_CONTENT_ROOT "
            f"'{content_root}'. Set that variable to the category path as seen inside qBittorrent."
        ) from exc
    if any(part in ("..", "") for part in relative.parts):
        raise ValueError("qBittorrent returned an unsafe content path.")
    mapped = (local_root / Path(*relative.parts)).resolve()
    root = local_root.resolve()
    if not mapped.is_relative_to(root):
        raise ValueError("The mapped path escapes the read-only rips mount.")
    return mapped


def _find_sources(path: Path, max_depth: int = 5) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix.lower() == ".iso" else []
    if not path.is_dir():
        return []
    found: list[Path] = []
    seen: set[Path] = set()
    for current_text, dirs, files in os.walk(path, followlinks=False):
        current = Path(current_text)
        depth = len(current.relative_to(path).parts)
        dirs[:] = [name for name in dirs if name != ".atmos-staging" and not (current / name).is_symlink()]
        if depth >= max_depth:
            dirs[:] = []
        if (current / "BDMV" / "PLAYLIST").is_dir():
            resolved = current.resolve()
            if resolved not in seen:
                seen.add(resolved)
                found.append(current)
            dirs[:] = []
        for filename in files:
            candidate = current / filename
            if candidate.suffix.lower() == ".iso" and not candidate.is_symlink():
                resolved = candidate.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(candidate)
    return sorted(found, key=lambda item: (item.name.casefold(), str(item).casefold()))


def list_completed_disc_downloads(
    *,
    base_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    category: str | None = None,
    content_root: str | None = None,
    local_root: Path | str | None = None,
) -> list[CompletedDownload]:
    base_url = (base_url or os.environ.get("QBITTORRENT_URL", "")).strip()
    username = username if username is not None else os.environ.get("QBITTORRENT_USERNAME", "")
    password = password if password is not None else os.environ.get("QBITTORRENT_PASSWORD", "")
    category = (category or os.environ.get("QBITTORRENT_CATEGORY", "audio-bluray")).strip()
    content_root = (content_root or os.environ.get("QBITTORRENT_CONTENT_ROOT", "")).strip()
    local_root = Path(local_root or os.environ.get("RIPS_DIR", "/rips"))

    if not base_url or not base_url.startswith(("http://", "https://")):
        raise RuntimeError("Set QBITTORRENT_URL to the qBittorrent Web UI address reachable from this container.")
    if not username or not password:
        raise RuntimeError("Set QBITTORRENT_USERNAME and QBITTORRENT_PASSWORD in the Docker .env file.")
    if not category:
        raise RuntimeError("QBITTORRENT_CATEGORY cannot be empty.")
    if not content_root:
        raise RuntimeError(
            "Set QBITTORRENT_CONTENT_ROOT to the absolute download path as seen inside qBittorrent."
        )

    opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    login = _request(
        opener, base_url, "/api/v2/auth/login",
        {"username": username, "password": password},
    ).decode("utf-8", errors="replace").strip()
    # qBittorrent Web API 2.15 (qBittorrent 5.2+) returns HTTP 204
    # with an empty body on successful login. Older versions return "Ok.".
    # _request raises HTTPError for non-success HTTP statuses, including the
    # 401 used by current qBittorrent versions for invalid credentials.
    if login and login.lower() != "ok.":
        raise RuntimeError("qBittorrent login failed. Check the Web UI username and password.")

    query = urlencode({
        "filter": "completed",
        "category": category,
        "sort": "completion_on",
        "reverse": "true",
    })
    payload = _request(opener, base_url, f"/api/v2/torrents/info?{query}")
    try:
        torrents = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("qBittorrent returned invalid JSON for its torrent list.") from exc
    if not isinstance(torrents, list):
        raise RuntimeError("qBittorrent returned an unexpected torrent-list response.")

    results: list[CompletedDownload] = []
    path_errors: list[str] = []
    for torrent in torrents:
        if not isinstance(torrent, dict):
            continue
        if float(torrent.get("progress") or 0) < 0.999999 or int(torrent.get("amount_left") or 0) > 0:
            continue
        remote = str(torrent.get("content_path") or "").strip()
        if not remote:
            continue
        try:
            local = _map_content_path(remote, content_root, local_root)
        except ValueError as exc:
            path_errors.append(str(exc))
            continue
        for source in _find_sources(local):
            results.append(CompletedDownload(
                name=str(torrent.get("name") or source.name),
                torrent_hash=str(torrent.get("hash") or ""),
                source_path=source,
                download_root=local if local.is_dir() else local.parent,
                size_bytes=int(torrent.get("size") or 0),
            ))
    if not results and path_errors:
        raise RuntimeError(path_errors[0])
    return results
