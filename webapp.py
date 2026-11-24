"""
Browser front-end for Atmos Track Splitter (Debian Docker edition).

The Flask app reuses the repository's existing extraction engine. Browser
users must authenticate; the source directory is read-only in Docker.
"""

from __future__ import annotations

import hmac
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

import auto_titles
import extractor
import settings
import qbittorrent
import iso_support
import poster_art

RIPS_ROOT = Path(os.environ.get("RIPS_DIR", "/rips")).resolve()
OUTPUT_ROOT = Path(os.environ.get("OUTPUT_DIR", "/output")).resolve()
STAGING_ROOT = Path(os.environ.get("STAGING_DIR", "/staging")).resolve()
AUTH_USERNAME = os.environ.get("AUTH_USERNAME", "").strip()
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_cancel_events: dict[str, threading.Event] = {}
_scan_cache: dict[tuple[str, str], tuple[extractor.Playlist, list[extractor.Chapter]]] = {}


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _disc_path(raw: str, allow_staging: bool = False) -> Path:
    value = str(raw or "").strip()
    candidate = Path(value).expanduser()
    p = (candidate if candidate.is_absolute() else RIPS_ROOT / candidate).resolve()
    allowed = _inside(p, RIPS_ROOT) or (allow_staging and _inside(p, STAGING_ROOT))
    if not allowed or not p.is_dir():
        raise ValueError("Folder is outside the permitted read-only rips or staging directories")
    return p


def _rips_source_file(raw: str) -> Path:
    value = str(raw or "").strip()
    candidate = Path(value).expanduser()
    p = (candidate if candidate.is_absolute() else RIPS_ROOT / candidate).resolve()
    if not _inside(p, RIPS_ROOT) or not p.is_file():
        raise ValueError("Source file is outside the read-only rips directory or doesn't exist")
    return p


def _sidecar_folder(raw: str | None, fallback: Path) -> Path:
    if not raw:
        return fallback
    return _disc_path(raw, allow_staging=True)


def _album_label(path: Path, hint: str = "") -> str:
    label = Path(hint).name if hint.strip() else path.name
    return extractor.derive_album_folder_name(Path(label))


def _discogs_token() -> str:
    return os.environ.get("DISCOGS_TOKEN", "").strip() or settings.load().get("discogs_token", "").strip()


def _fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return ""
    value = int(round(seconds))
    return (
        f"{value // 3600}:{value % 3600 // 60:02d}:{value % 60:02d}"
        if value >= 3600 else f"{value // 60}:{value % 60:02d}"
    )


def _err(message: str, code: int = 400):
    return jsonify({"error": message}), code


@app.before_request
def _require_authentication():
    # Health checks reveal no application or filesystem information.
    if request.endpoint == "healthz":
        return None
    if not AUTH_USERNAME or not AUTH_PASSWORD:
        return _err("Application login is not configured. Set AUTH_USERNAME and AUTH_PASSWORD.", 503)

    credentials = request.authorization
    user_ok = bool(credentials and hmac.compare_digest(credentials.username or "", AUTH_USERNAME))
    pass_ok = bool(credentials and hmac.compare_digest(credentials.password or "", AUTH_PASSWORD))
    if not (user_ok and pass_ok):
        response = jsonify({"error": "Login required"})
        response.status_code = 401
        response.headers["WWW-Authenticate"] = 'Basic realm="Atmos Track Splitter"'
        response.headers["Cache-Control"] = "no-store"
        return response
    return None


@app.after_request
def _security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


def _load_playlist(path: Path, playlist_file: str):
    # Do not accept paths or separators from the client in this field.
    if Path(playlist_file).name != playlist_file or not playlist_file.lower().endswith(".mpls"):
        raise ValueError("Invalid playlist filename")
    key = (str(path), playlist_file)
    if key in _scan_cache:
        return _scan_cache[key]
    mpls = path / "BDMV" / "PLAYLIST" / playlist_file
    if not mpls.is_file():
        raise ValueError(f"Playlist {playlist_file} not found")
    pl = extractor.inspect_playlist(mpls)
    chapters = extractor.read_chapters_from_source(mpls)
    _scan_cache[key] = (pl, chapters)
    return pl, chapters


def _parent_artist(path: Path) -> str:
    parent = path.parent
    return parent.name if parent != RIPS_ROOT and _inside(parent, RIPS_ROOT) else ""


def _output_dir(output_name: str) -> Path:
    name = extractor.sanitize_filename(str(output_name or ""))
    out = (OUTPUT_ROOT / name).resolve()
    if not _inside(out, OUTPUT_ROOT) or out == OUTPUT_ROOT:
        raise ValueError("Bad output folder name")
    return out


@app.get("/healthz")
def healthz():
    return jsonify(ok=True)


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/config")
def api_config():
    return jsonify(
        rips_root=str(RIPS_ROOT),
        output_root=str(OUTPUT_ROOT),
        has_discogs=bool(_discogs_token()),
        has_qbittorrent=bool(os.environ.get("QBITTORRENT_URL")),
        tools=extractor.check_tools(),
        unattended_splitting=False,
    )


@app.get("/api/completed-downloads")
def api_completed_downloads():
    try:
        downloads = qbittorrent.list_completed_disc_downloads()
    except Exception as exc:
        return _err(str(exc), 502)
    return jsonify(downloads=[
        {
            "name": item.name,
            "kind": item.kind,
            "path": str(item.source_path),
            "download_root": str(item.download_root),
            "torrent_hash": item.torrent_hash,
            "size_bytes": item.size_bytes,
        }
        for item in downloads
    ])


@app.post("/api/select-download")
def api_select_download():
    data = request.get_json(silent=True) or {}
    try:
        source = _rips_source_file(data.get("path", "")) if str(data.get("path", "")).lower().endswith(".iso") else _disc_path(data.get("path", ""))
        root = _disc_path(data.get("download_root", str(source.parent)))
        album_hint = str(data.get("name") or (source.stem if source.is_file() else source.name)).strip()

        if source.is_file() and source.suffix.lower() == ".iso":
            disc = iso_support.prepare_iso(source, STAGING_ROOT)
        else:
            disc = iso_support.find_disc_folder(source)
            if disc is None:
                raise ValueError("No BDMV/PLAYLIST folder found in this completed download.")
        return jsonify(
            path=str(disc),
            sidecar_path=str(root),
            album_name_hint=album_hint,
            is_iso=source.suffix.lower() == ".iso",
        )
    except (ValueError, RuntimeError, OSError) as exc:
        return _err(str(exc))


@app.get("/api/browse")
def api_browse():
    try:
        folder = _disc_path(request.args.get("path", str(RIPS_ROOT)))
    except ValueError as exc:
        return _err(str(exc))
    rows = []
    try:
        for child in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
            if child.is_dir() and not child.name.startswith("."):
                rows.append({
                    "name": child.name,
                    "path": str(child),
                    "is_disc": (child / "BDMV" / "PLAYLIST").is_dir(),
                })
    except OSError as exc:
        return _err(f"Can't read folder: {exc}")
    parent = str(folder.parent) if folder != RIPS_ROOT else None
    return jsonify(
        path=str(folder),
        parent=parent,
        is_disc=(folder / "BDMV" / "PLAYLIST").is_dir(),
        entries=rows,
    )


@app.post("/api/settings")
def api_settings():
    data = request.get_json(silent=True) or {}
    if "discogs_token" in data:
        settings.update(discogs_token=str(data["discogs_token"]).strip())
    return jsonify(ok=True, has_discogs=bool(_discogs_token()))


def _scan(
    path: Path,
    playlist_file: str | None = None,
    sidecar_path: Path | None = None,
    album_name_hint: str = "",
) -> dict:
    playlists = extractor.scan_disc_folder(path)
    if not playlists:
        raise ValueError("No BDMV/PLAYLIST/*.mpls found - is this an unmodified disc rip folder?")
    scored = extractor.score_playlists(playlists)
    atmos = [score for score in scored if score.playlist.has_atmos and not score.duplicate_of]
    if not atmos:
        raise ValueError("No Dolby Atmos / TrueHD playlist found on this disc")
    chosen = next((score for score in atmos if score.playlist.path.name == playlist_file), atmos[0])
    pl, chapters = _load_playlist(path, chosen.playlist.path.name)

    durations = auto_titles.chapter_durations(chapters, pl.duration_seconds)
    album = _album_label(path, album_name_hint)
    out_dir = _output_dir(album)
    naming_source = Path(album_name_hint) if album_name_hint else path
    guess = extractor.guess_artist_album_year(naming_source)
    local_root = sidecar_path or path
    manifest = extractor.read_manifest(out_dir / "_work")
    return {
        "path": str(path),
        "sidecar_path": str(local_root),
        "album_name_hint": album_name_hint,
        "playlist": pl.path.name,
        "playlists": [
            {
                "file": score.playlist.path.name,
                "chapters": score.playlist.chapter_count,
                "minutes": round(score.playlist.duration_seconds / 60),
                "score": round(score.score, 1),
                "reasons": score.reasons,
            }
            for score in atmos
        ],
        "chapters": [
            {
                "index": chapter.index,
                "start": _fmt_time(chapter.start_seconds),
                "length": _fmt_time(duration),
                "embedded_name": (
                    "" if extractor.is_generic_chapter_name(chapter.embedded_name, chapter.index)
                    else chapter.embedded_name
                ),
            }
            for chapter, duration in zip(chapters, durations)
        ],
        "default_output": album,
        "resumable": bool(
            manifest and manifest.status in ("cancelled", "failed", "extracting", "splitting")
        ),
        "guess": dict(zip(("artist", "album", "year"), guess)),
    }


def _resolve(
    path: Path,
    playlist_file: str,
    override=None,
    offline=False,
    log=None,
    sidecar_folder: Path | None = None,
) -> dict:
    playlist, chapters = _load_playlist(path, playlist_file)
    result = auto_titles.resolve_titles(
        path,
        chapters,
        total_seconds=playlist.duration_seconds,
        discogs_token=_discogs_token(),
        parent_artist=_parent_artist(path),
        override=override,
        offline=offline,
        log=log,
        sidecar_folder=sidecar_folder,
    )
    return result.to_dict()


@app.post("/api/scan")
def api_scan():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(_scan(
            _disc_path(data.get("path", ""), allow_staging=True),
            data.get("playlist"),
            _sidecar_folder(data.get("sidecar_path"), _disc_path(data.get("path", ""), allow_staging=True)),
            str(data.get("album_name_hint") or ""),
        ))
    except (ValueError, RuntimeError, OSError) as exc:
        return _err(str(exc))


@app.post("/api/resolve")
def api_resolve():
    data = request.get_json(silent=True) or {}
    try:
        path = _disc_path(data.get("path", ""), allow_staging=True)
        sidecar_folder = _sidecar_folder(data.get("sidecar_path"), path)
        playlist = str(data["playlist"])
        override = None
        if data.get("artist") or data.get("album"):
            year = int(data["year"]) if str(data.get("year") or "").isdigit() else None
            override = (str(data.get("artist", "")), str(data.get("album", "")), year)
        return jsonify(_resolve(
            path, playlist, override, bool(data.get("offline")),
            sidecar_folder=sidecar_folder,
        ))
    except (ValueError, RuntimeError, KeyError, OSError) as exc:
        return _err(str(exc))


def _new_job(kind: str) -> dict:
    job = {
        "id": uuid.uuid4().hex[:10],
        "kind": kind,
        "status": "running",
        "log": [],
        "error": "",
        "outputs": [],
        "started": time.time(),
    }
    with _jobs_lock:
        _jobs[job["id"]] = job
        _cancel_events[job["id"]] = threading.Event()
    return job


def _jlog(job: dict, message: str) -> None:
    job["log"].append(message)


def _start_pipeline(
    job: dict,
    path: Path,
    playlist_file: str,
    names: dict[int, str],
    output_name: str,
    overwrite: bool,
    resume: bool,
    sidecar_folder: Path,
    artist: str,
    album: str,
    year: str,
    release_id: str,
) -> bool:
    """Runs inside a job thread. Returns False if it stopped for user input."""
    playlist, chapters = _load_playlist(path, playlist_file)
    out_dir = _output_dir(output_name)
    work = out_dir / "_work"
    out_dir.mkdir(parents=True, exist_ok=True)

    planned = extractor.preflight_check(out_dir, len(chapters), names)
    colliding = [item for item in planned if item.exists]
    if colliding and not overwrite and not resume:
        job["status"] = "needs_confirm"
        job["error"] = f"{len(colliding)} output file(s) already exist. Tick 'Overwrite' to replace them."
        return False

    if not resume and extractor.read_manifest(work) is not None:
        shutil.rmtree(work, ignore_errors=True)

    results = extractor.run_full_pipeline(
        playlist,
        names,
        work,
        out_dir,
        progress_cb=lambda message: _jlog(job, message),
        overwrite={item.path for item in colliding} if overwrite else None,
        cancel_event=_cancel_events[job["id"]],
        resume=resume,
        log_path=out_dir / "extraction.log",
    )
    job["outputs"] = [str(result) for result in results]
    try:
        artwork = poster_art.create_album_poster(
            sidecar_folder if sidecar_folder.is_dir() else path,
            out_dir,
            artist,
            album,
            year,
            musicbrainz_release_id=release_id,
        )
        if artwork is not None:
            _jlog(job, f"Plex artwork saved as {artwork.name} (1000 x 1000).")
        else:
            _jlog(job, "No verified front-cover image found; artwork skipped.")
    except Exception as exc:
        _jlog(job, f"Tracks split successfully, but artwork creation failed: {exc}")

    # Only remove a deterministic ISO staging folder after a successful split.
    if path.parent == STAGING_ROOT and path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
        _jlog(job, "Temporary ISO staging files removed.")
    return True


def _thread(job: dict, fn) -> None:
    def run():
        try:
            fn()
            if job["status"] == "running":
                job["status"] = "done"
        except extractor.JobCancelled:
            job["status"] = "cancelled"
        except Exception as exc:  # surfaced in the UI log
            job["status"] = "failed"
            job["error"] = str(exc)
            _jlog(job, f"Failed: {exc}")
    threading.Thread(target=run, daemon=True).start()


@app.post("/api/jobs")
def api_start_job():
    data = request.get_json(silent=True) or {}
    try:
        path = _disc_path(data.get("path", ""), allow_staging=True)
        sidecar_folder = _sidecar_folder(data.get("sidecar_path"), path)
        playlist_file = str(data["playlist"])
        _load_playlist(path, playlist_file)
        names = {
            int(key): str(value).strip()
            for key, value in (data.get("names") or {}).items()
            if str(value).strip()
        }
        output_name = str(data.get("output_name") or _album_label(path, str(data.get("album_name_hint") or "")))
        _output_dir(output_name)
        artist = str(data.get("artist") or "")
        album = str(data.get("album") or "")
        year = str(data.get("year") or "")
        release_id = str(data.get("release_id") or "")
    except (ValueError, RuntimeError, KeyError, OSError, TypeError) as exc:
        return _err(str(exc))

    job = _new_job("run")
    _thread(
        job,
        lambda: _start_pipeline(
            job,
            path,
            playlist_file,
            names,
            output_name,
            bool(data.get("overwrite")),
            bool(data.get("resume")),
            sidecar_folder,
            artist,
            album,
            year,
            release_id,
        ),
    )
    return jsonify(job_id=job["id"])


@app.get("/api/jobs/<job_id>")
def api_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return _err("Unknown job", 404)
        return jsonify({**job, "log": job["log"][-200:]})


@app.post("/api/jobs/<job_id>/cancel")
def api_cancel(job_id: str):
    with _jobs_lock:
        event = _cancel_events.get(job_id)
    if not event:
        return _err("Unknown job", 404)
    event.set()
    return jsonify(ok=True)


if __name__ == "__main__":
    if not AUTH_USERNAME or not AUTH_PASSWORD:
        raise SystemExit("Set AUTH_USERNAME and AUTH_PASSWORD before starting the web application.")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8765")))
