"""
settings.py

Small JSON-backed settings store: remembers tool paths (mkvmerge,
mkvextract, ffmpeg) and the last-used source/output folders so the user
doesn't have to re-browse every time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SETTINGS_PATH = Path.home() / ".atmos_track_splitter" / "settings.json"

DEFAULTS: dict[str, Any] = {
    "mkvmerge_path": "mkvmerge",
    "mkvextract_path": "mkvextract",
    "ffmpeg_path": "ffmpeg",
    "ffprobe_path": "ffprobe",
    "last_source_folder": "",
    "last_output_folder": "",
    "output_container": "mkv",   # or "m4a" / "flac" if a re-encode step is ever added
    "watch_folder_enabled": False,
    "watch_folder_path": "",
}


def load() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return dict(DEFAULTS)
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)
    merged = dict(DEFAULTS)
    merged.update(data)
    return merged


def save(settings: dict[str, Any]) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def update(**kwargs: Any) -> dict[str, Any]:
    current = load()
    current.update(kwargs)
    save(current)
    return current
