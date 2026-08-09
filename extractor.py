"""
extractor.py

Core logic for scanning Blu-ray disc folders, finding Dolby Atmos audio
tracks, reading chapter markers, and splitting the Atmos stream into
individual named song files.

Requires on PATH (or configured via settings.py):
    - mkvmerge / mkvextract  (MKVToolNix)
    - ffmpeg / ffprobe

This module has no GUI dependencies - it can be used standalone or
imported by main.py.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Track:
    track_id: int
    kind: str          # "video" | "audio" | "subtitles"
    codec: str          # raw string from mkvmerge, e.g. "TrueHD Atmos"

    @property
    def is_atmos(self) -> bool:
        return "atmos" in self.codec.lower()


@dataclass
class Playlist:
    path: Path
    tracks: list[Track] = field(default_factory=list)
    chapter_count: int = 0

    @property
    def atmos_track(self) -> Optional[Track]:
        for t in self.tracks:
            if t.is_atmos:
                return t
        return None

    @property
    def video_track(self) -> Optional[Track]:
        for t in self.tracks:
            if t.kind == "video":
                return t
        return None

    @property
    def has_atmos(self) -> bool:
        return self.atmos_track is not None


@dataclass
class Chapter:
    index: int
    start_seconds: float
    end_seconds: Optional[float] = None   # filled in after all chapters read
    name: str = ""                        # user-assigned song title


# ---------------------------------------------------------------------------
# Tool paths - overridden by settings.py if the user configures custom paths
# ---------------------------------------------------------------------------

TOOL_PATHS = {
    "mkvmerge": "mkvmerge",
    "mkvextract": "mkvextract",
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffprobe",
}

# Where to point people if a required tool isn't found. mkvmerge/mkvextract
# both ship in the same MKVToolNix install; ffmpeg/ffprobe both ship in the
# same ffmpeg download.
TOOL_DOWNLOAD_URLS = {
    "mkvmerge": "https://mkvtoolnix.download/downloads.html",
    "mkvextract": "https://mkvtoolnix.download/downloads.html",
    "ffmpeg": "https://ffmpeg.org/download.html",
    "ffprobe": "https://ffmpeg.org/download.html",
}


def set_tool_path(tool: str, path: str) -> None:
    if tool not in TOOL_PATHS:
        raise KeyError(f"Unknown tool '{tool}'")
    TOOL_PATHS[tool] = path


def check_tools() -> dict[str, bool]:
    """
    Check whether each configured tool is actually runnable: found on PATH
    if it's a bare command name (e.g. "mkvmerge"), or exists at that exact
    location if it's been set to a specific file path.

    Returns {tool_name: True/False}.
    """
    found: dict[str, bool] = {}
    for name, configured_path in TOOL_PATHS.items():
        looks_like_path = "/" in configured_path or "\\" in configured_path
        if looks_like_path:
            found[name] = Path(configured_path).is_file()
        else:
            found[name] = shutil.which(configured_path) is not None
    return found


def _run(args: list[str], timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    """
    Run a subprocess, hiding the console window on Windows.

    stdin is explicitly set to DEVNULL. When this app is running as a
    windowed/no-console exe (PyInstaller --windowed), there is no valid
    console handle for the process to inherit as stdin. A child process
    that inherits a broken/invalid stdin handle can hang indefinitely
    waiting on it even after the child itself has exited - this was the
    root cause of the app appearing stuck on "Working..." after mkvmerge
    had already finished/died. Explicitly redirecting stdin from DEVNULL
    avoids that inheritance entirely, regardless of how the app is launched.

    A timeout is also supported (default: no timeout) so that a genuinely
    hung external tool doesn't wedge the app forever with no feedback.
    """
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        tool = Path(args[0]).name if args else "process"
        raise RuntimeError(
            f"{tool} timed out after {timeout} seconds and was killed. "
            f"Command: {' '.join(args)}"
        ) from exc


# ---------------------------------------------------------------------------
# Disc / folder scanning
# ---------------------------------------------------------------------------

_TRACK_LINE_RE = re.compile(
    r"Track ID (\d+): (video|audio|subtitles) \((.+)\)"
)
_CHAPTERS_LINE_RE = re.compile(r"Chapters: (\d+) entries")


def find_playlists(disc_folder: Path) -> list[Path]:
    """Find .mpls playlist files under BDMV/PLAYLIST (ignores BACKUP)."""
    playlist_dir = disc_folder / "BDMV" / "PLAYLIST"
    if not playlist_dir.is_dir():
        return []
    return sorted(playlist_dir.glob("*.mpls"))


def inspect_playlist(playlist_path: Path) -> Playlist:
    """Run `mkvmerge -i` on a playlist and parse tracks + chapter count."""
    result = _run([TOOL_PATHS["mkvmerge"], "-i", str(playlist_path)], timeout=60)
    # mkvmerge -i returns 0 for a clean read and 1 for warnings (still
    # usable output), but 2 means it couldn't read the file at all - in
    # that case stdout won't have useful track/chapter info, so surface
    # the real error instead of silently reporting "no Atmos track".
    if result.returncode >= 2:
        raise RuntimeError(
            f"mkvmerge could not read {playlist_path.name}:\n{result.stderr or result.stdout}"
        )
    pl = Playlist(path=playlist_path)

    for line in result.stdout.splitlines():
        m = _TRACK_LINE_RE.search(line)
        if m:
            pl.tracks.append(
                Track(track_id=int(m.group(1)), kind=m.group(2), codec=m.group(3))
            )
            continue
        m = _CHAPTERS_LINE_RE.search(line)
        if m:
            pl.chapter_count = int(m.group(1))

    return pl


def scan_disc_folder(disc_folder: Path) -> list[Playlist]:
    """Inspect every playlist in a disc folder, return list of Playlist."""
    return [inspect_playlist(p) for p in find_playlists(disc_folder)]


def find_best_atmos_playlist(playlists: list[Playlist]) -> Optional[Playlist]:
    """Heuristic: prefer the Atmos playlist with the most chapters."""
    candidates = [p for p in playlists if p.has_atmos]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.chapter_count)


# ---------------------------------------------------------------------------
# Extraction: playlist -> standalone Atmos MKV (with chapters)
# ---------------------------------------------------------------------------

def extract_atmos_mkv(
    playlist: Playlist,
    output_path: Path,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> Path:
    """
    Remux the video track + just the Atmos audio track (+ chapters) out of
    a playlist. Keeping video means playback isn't a black/no-signal
    screen on a TV - the other audio tracks (AC-3, DTS-HD) are dropped.
    """
    if not playlist.has_atmos:
        raise ValueError("Playlist has no Atmos track")

    audio_track = playlist.atmos_track
    video_track = playlist.video_track
    output_path.parent.mkdir(parents=True, exist_ok=True)

    args = [TOOL_PATHS["mkvmerge"], "-o", str(output_path)]

    if video_track is not None:
        args += ["-d", str(video_track.track_id)]
    else:
        args += ["--no-video"]

    args += ["--no-subtitles", "-a", str(audio_track.track_id), str(playlist.path)]

    if progress_cb:
        target = f"video track {video_track.track_id} + " if video_track else ""
        progress_cb(
            f"Extracting {target}Atmos audio track {audio_track.track_id} -> {output_path.name}"
        )

    result = _run(args, timeout=7200)  # 2 hours - full-disc extraction can be slow
    if result.returncode != 0:
        raise RuntimeError(f"mkvmerge failed:\n{result.stdout}\n{result.stderr}")

    return output_path


# ---------------------------------------------------------------------------
# Chapters
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(r"(\d+):(\d+):(\d+(?:\.\d+)?)")


def _timecode_to_seconds(tc: str) -> float:
    m = _TIME_RE.match(tc)
    if not m:
        raise ValueError(f"Unrecognised timecode: {tc}")
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


def read_chapters(mkv_path: Path) -> list[Chapter]:
    """Extract chapter markers from an MKV via `mkvextract chapters -`."""
    result = _run([TOOL_PATHS["mkvextract"], str(mkv_path), "chapters", "-"], timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"mkvextract failed:\n{result.stderr}")

    xml_text = result.stdout
    # Defensive cleanup: strip a UTF-8 BOM (now correctly decoded thanks to
    # explicit encoding="utf-8" in _run) and drop anything before the
    # opening "<" in case a tool ever emits stray leading bytes/whitespace.
    xml_text = xml_text.lstrip("\ufeff").strip()
    lt_index = xml_text.find("<")
    if lt_index > 0:
        xml_text = xml_text[lt_index:]
    root = ET.fromstring(xml_text)

    chapters: list[Chapter] = []
    for i, atom in enumerate(root.iter("ChapterAtom"), start=1):
        start_el = atom.find("ChapterTimeStart")
        if start_el is None or start_el.text is None:
            continue
        chapters.append(
            Chapter(index=i, start_seconds=_timecode_to_seconds(start_el.text))
        )

    # Fill in end times from the next chapter's start.
    for i, ch in enumerate(chapters):
        if i + 1 < len(chapters):
            ch.end_seconds = chapters[i + 1].start_seconds
        else:
            ch.end_seconds = None  # last chapter runs to end of file

    return chapters


def probe_duration_seconds(media_path: Path) -> float:
    """Get total duration of a media file via ffprobe (used for the last chapter)."""
    args = [
        TOOL_PATHS["ffprobe"],
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(media_path),
    ]
    result = _run(args, timeout=60)
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise RuntimeError(f"Could not determine duration of {media_path}")


# ---------------------------------------------------------------------------
# Splitting: Atmos MKV + chapters -> individual named files
# ---------------------------------------------------------------------------

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]')


def sanitize_filename(name: str) -> str:
    name = _INVALID_FILENAME_CHARS.sub("", name).strip()
    return name or "Untitled"


_TRAILING_BRACKET_RE = re.compile(r"\s*[\[\(][^\[\]\(\)]*[\]\)]\s*$")


def derive_album_folder_name(source_folder: Path) -> str:
    """
    Turn a disc-rip folder name into a clean album folder name, e.g.
    "Fleetwood Mac - Fleetwood Mac (1975) [Blu-ray]" -> keeps the year,
    only strips a trailing "[Blu-ray]"-style disc/format tag.
    """
    name = source_folder.name
    # Repeatedly strip trailing bracketed tags if they look like disc/format
    # labels rather than a release year - years are 4 digits in parens and
    # should be kept (e.g. "(1975)"). Handles multiple stacked tags like
    # "Album (2020) [Blu-ray] [1080p]".
    while True:
        m = _TRAILING_BRACKET_RE.search(name)
        if not m or re.fullmatch(r"[\(]\d{4}[\)]", m.group(0).strip()):
            break
        name = name[: m.start()].strip()
    return sanitize_filename(name) or source_folder.name


def split_chapters(
    atmos_mkv: Path,
    chapters: list[Chapter],
    output_folder: Path,
    container: str = "mkv",
    progress_cb: Optional[Callable[[str], None]] = None,
) -> list[Path]:
    """
    Split the Atmos MKV into one file per chapter using ffmpeg stream-copy
    (no re-encoding - Atmos must stay bit-exact).
    """
    if not chapters:
        raise ValueError(
            "No chapters found - this playlist has an Atmos track but no "
            "chapter markers, so it can't be split into individual songs."
        )

    output_folder.mkdir(parents=True, exist_ok=True)

    if chapters[-1].end_seconds is None:
        chapters[-1].end_seconds = probe_duration_seconds(atmos_mkv)

    output_paths: list[Path] = []

    for ch in chapters:
        track_name = ch.name or f"Track {ch.index:02d}"
        filename = f"{ch.index:02d} - {sanitize_filename(track_name)}.{container}"
        out_path = output_folder / filename

        # -ss before -i does a fast keyframe seek in the input instead of
        # decoding from the start of the file on every single chapter
        # (which is what -ss after -i would do). Since we're stream-copying,
        # the cut still snaps to the nearest keyframe either way - you can't
        # cut mid-GOP without re-encoding - so accuracy is the same, but this
        # is dramatically faster, especially for later chapters in a long file.
        args = [
            TOOL_PATHS["ffmpeg"],
            "-y",
            "-ss", str(ch.start_seconds),
            "-i", str(atmos_mkv),
        ]
        if ch.end_seconds is not None:
            # -to is relative to the original input timeline when placed
            # after -i, but ffmpeg re-bases it to the seek point when -ss
            # comes before -i - so we need the chapter's duration, not its
            # absolute end time, to get the correct cut point.
            args += ["-t", str(ch.end_seconds - ch.start_seconds)]
        args += ["-c", "copy", str(out_path)]

        if progress_cb:
            progress_cb(f"Splitting chapter {ch.index}: {track_name}")

        result = _run(args, timeout=1800)  # 30 min per chapter - stream-copy is fast, this is just a safety net
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed on chapter {ch.index}:\n{result.stderr}")

        output_paths.append(out_path)

    return output_paths


# ---------------------------------------------------------------------------
# Convenience: run the whole pipeline in one call
# ---------------------------------------------------------------------------

def run_full_pipeline(
    playlist: Playlist,
    track_names: dict[int, str],
    work_folder: Path,
    output_folder: Path,
    container: str = "mkv",
    progress_cb: Optional[Callable[[str], None]] = None,
    cleanup_work_folder: bool = True,
) -> list[Path]:
    """
    track_names: maps chapter index (1-based) -> song title.
    Returns list of final output file paths.

    cleanup_work_folder: if True (default), deletes work_folder (and the
    large intermediate _atmos_extracted.mkv inside it) once splitting has
    finished successfully. If splitting raises, the work folder is left
    in place so the extraction doesn't have to be redone. Set to False to
    always keep the intermediate file around (e.g. for debugging).
    """
    atmos_mkv = work_folder / "_atmos_extracted.mkv"
    extract_atmos_mkv(playlist, atmos_mkv, progress_cb=progress_cb)

    chapters = read_chapters(atmos_mkv)
    for ch in chapters:
        if ch.index in track_names:
            ch.name = track_names[ch.index]

    results = split_chapters(
        atmos_mkv, chapters, output_folder, container=container, progress_cb=progress_cb
    )

    if cleanup_work_folder:
        if progress_cb:
            progress_cb("Cleaning up temporary files...")
        shutil.rmtree(work_folder, ignore_errors=True)

    return results
