"""Read-only discovery of completed Blu-ray downloads from qBittorrent.

Only the Web API's torrent-list endpoint is used. The splitter never pauses,
moves, renames, deletes, or otherwise changes a torrent.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import HTTPCookieProcessor, Request, build_opener


@dataclass(frozen=True)
class CompletedDownload:
    name: str
    torrent_hash: str
    source_path: Path
    download_root: Path
    state: str
    size_bytes: int

    @property
    def display_name(self) -> str:
        return self.name if self.source_path.name == self.name else f"{self.name} — {self.source_path.name}"


def _api_request(opener, base_url: str, endpoint: str, data: dict[str, str] | None = None) -> bytes:
    url = urljoin(base_url.rstrip("/") + "/", endpoint.lstrip("/"))
    payload = urlencode(data).encode("utf-8") if data is not None else None
    request = Request(url, data=payload, headers={
        "User-Agent": "AtmosTrackSplitter/1.0",
        "Referer": base_url.rstrip("/") + "/",
    })
    try:
        with opener.open(request, timeout=20) as response:
            return response.read(10 * 1024 * 1024 + 1)
    except HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", errors="replace")
        if exc.code in (401, 403):
            raise RuntimeError(
                "qBittorrent rejected the login or API request. Check its Web UI username/password "
                "and confirm that the Web UI is enabled."
            ) from exc
        raise RuntimeError(f"qBittorrent API returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(
            f"Could not connect to qBittorrent at {base_url}: {exc.reason}. "
            "Check the Web UI address, port, and Docker network access."
        ) from exc


def _candidate_sources(path: Path, max_depth: int = 5) -> list[Path]:
    """Return BDMV roots and ISO files under a completed torrent's content path."""
    if path.is_file():
        return [path] if path.suffix.lower() == ".iso" else []
    if not path.is_dir():
        return []

    found: list[Path] = []
    seen: set[Path] = set()
    for current_text, dirs, files in os.walk(path, followlinks=False):
        current = Path(current_text)
        try:
            depth = len(current.relative_to(path).parts)
        except ValueError:
            dirs[:] = []
            continue
        dirs[:] = [
            d for d in dirs
            if d != ".atmos-staging" and not (current / d).is_symlink()
        ]
        if depth >= max_depth:
            dirs[:] = []

        if (current / "BDMV" / "PLAYLIST").is_dir():
            resolved = current.resolve()
            if resolved not in seen:
                seen.add(resolved)
                found.append(current)
            # This is already a disc root; don't report nested content.
            dirs[:] = []

        for filename in files:
            candidate = current / filename
            if candidate.suffix.lower() == ".iso" and not candidate.is_symlink():
                resolved = candidate.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(candidate)
    return sorted(found, key=lambda item: (item.name.casefold(), str(item).casefold()))


def _map_content_path(remote_path: str, content_root: str, local_root: Path) -> Path:
    remote = PurePosixPath(remote_path)
    root = PurePosixPath(content_root)
    try:
        relative = remote.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"qBittorrent returned '{remote_path}', which is outside the configured "
            f"QBITTORRENT_CONTENT_ROOT '{content_root}'. Set that value to the path "
            "qBittorrent uses for this category."
        ) from exc
    if any(part in ("..", "") for part in relative.parts):
        raise ValueError("qBittorrent returned an unsafe content path.")
    local = (local_root / Path(*relative.parts)).resolve()
    root_resolved = local_root.resolve()
    if not local.is_relative_to(root_resolved):
        raise ValueError("The mapped download path escapes the read-only /rips mount.")
    return local


def list_completed_disc_downloads(
    *,
    base_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    category: str | None = None,
    content_root: str | None = None,
    local_root: Path | str | None = None,
) -> list[CompletedDownload]:
    """Fetch completed torrents in one category and map their content into /rips."""
    base_url = (base_url or os.environ.get("QBITTORRENT_URL", "")).strip()
    username = username if username is not None else os.environ.get("QBITTORRENT_USERNAME", "")
    password = password if password is not None else os.environ.get("QBITTORRENT_PASSWORD", "")
    category = (category or os.environ.get("QBITTORRENT_CATEGORY", "audio-bluray")).strip()
    content_root = (content_root or os.environ.get("QBITTORRENT_CONTENT_ROOT", "")).strip()
    local_root = Path(local_root or os.environ.get("ATMOS_RIPS_DIR", "/rips"))

    if not base_url:
        raise RuntimeError("qBittorrent is not configured. Set QBITTORRENT_URL in the Docker .env file.")
    if not username or not password:
        raise RuntimeError("Set QBITTORRENT_USERNAME and QBITTORRENT_PASSWORD in the Docker .env file.")
    if not category:
        raise RuntimeError("QBITTORRENT_CATEGORY cannot be empty.")
    if not content_root:
        raise RuntimeError(
            "Set QBITTORRENT_CONTENT_ROOT to the absolute download path as seen inside qBittorrent."
        )
    if not base_url.startswith(("http://", "https://")):
        raise RuntimeError("QBITTORRENT_URL must start with http:// or https://.")

    cookie_jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))
    login = _api_request(
        opener, base_url, "/api/v2/auth/login",
        {"username": username, "password": password},
    ).decode("utf-8", errors="replace").strip()
    if login.lower() != "ok.":
        raise RuntimeError(
            "qBittorrent login did not succeed. Check the Web UI credentials and API access settings."
        )

    query = urlencode({"filter": "completed", "category": category, "sort": "completion_on", "reverse": "true"})
    payload = _api_request(opener, base_url, f"/api/v2/torrents/info?{query}")
    if len(payload) > 10 * 1024 * 1024:
        raise RuntimeError("qBittorrent returned an unexpectedly large torrent list.")
    try:
        torrents: Any = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("qBittorrent returned invalid JSON from its torrent-list API.") from exc
    if not isinstance(torrents, list):
        raise RuntimeError("qBittorrent returned an unexpected torrent-list response.")

    results: list[CompletedDownload] = []
    mapping_errors: list[str] = []
    for torrent in torrents:
        if not isinstance(torrent, dict):
            continue
        # Keep the completion check explicit even though the API request is filtered.
        if float(torrent.get("progress") or 0) < 0.999999 or int(torrent.get("amount_left") or 0) > 0:
            continue
        remote_path = str(torrent.get("content_path") or "").strip()
        if not remote_path:
            save_path = str(torrent.get("save_path") or "").rstrip("/")
            name = str(torrent.get("name") or "")
            remote_path = f"{save_path}/{name}" if save_path and name else ""
        if not remote_path:
            continue
        try:
            local_path = _map_content_path(remote_path, content_root, local_root)
        except ValueError as exc:
            mapping_errors.append(str(exc))
            continue
        sources = _candidate_sources(local_path)
        for source in sources:
            results.append(CompletedDownload(
                name=str(torrent.get("name") or source.name),
                torrent_hash=str(torrent.get("hash") or ""),
                source_path=source,
                download_root=local_path if local_path.is_dir() else local_path.parent,
                state=str(torrent.get("state") or "completed"),
                size_bytes=int(torrent.get("size") or 0),
            ))

    if not results and mapping_errors:
        raise RuntimeError(mapping_errors[0])
    return results
