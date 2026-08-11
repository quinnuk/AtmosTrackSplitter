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

import json
import os
import re
import shutil
import subprocess
import uuid
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
    kind: str           # "video" | "audio" | "subtitles"
    codec: str           # human-readable codec string from mkvmerge JSON, e.g. "TrueHD Atmos"
    codec_id: str = ""   # internal codec id, e.g. "A_TRUEHD", "V_MPEG4/ISO/AVC"
    language: str = ""   # ISO 639-2 code, e.g. "eng" - empty if not set on the track
    channels: Optional[int] = None   # audio channel count, None for non-audio tracks
    title: str = ""       # track name/title embedded in the container, if any

    @property
    def is_atmos(self) -> bool:
        return "atmos" in self.codec.lower()


@dataclass
class Playlist:
    path: Path
    tracks: list[Track] = field(default_factory=list)
    chapter_count: int = 0
    duration_seconds: float = 0.0

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
    name: str = ""                        # final song title used for the output filename
    embedded_name: str = ""               # ChapterString read from the source, if any
    language: str = ""                    # ChapterLanguage of the embedded name, if any


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
    Check whether each configured tool is actually runnable by invoking
    `<tool> --version`, rather than merely checking that a file exists at
    the configured path or that something by that name is on PATH.
    Existence alone doesn't prove much: a stale/broken shim, a
    wrong-architecture binary, a permissions problem, or an unrelated file
    that happens to share the name would all pass a mere existence check
    but fail here - and fail here in the same way they'd fail when the
    app actually tries to use them, so problems surface at startup
    instead of mid-extraction.

    Returns {tool_name: True/False}.
    """
    found: dict[str, bool] = {}
    for name, configured_path in TOOL_PATHS.items():
        try:
            result = _run([configured_path, "--version"], timeout=10)
            found[name] = result.returncode == 0
        except (FileNotFoundError, OSError, RuntimeError):
            # FileNotFoundError/OSError: nothing runnable at that path/name.
            # RuntimeError: _run's own timeout wrapper - a tool that hangs
            # on --version isn't usable either.
            found[name] = False
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


def find_playlists(disc_folder: Path) -> list[Path]:
    """Find .mpls playlist files under BDMV/PLAYLIST (ignores BACKUP)."""
    playlist_dir = disc_folder / "BDMV" / "PLAYLIST"
    if not playlist_dir.is_dir():
        return []
    return sorted(playlist_dir.glob("*.mpls"))


def inspect_playlist(playlist_path: Path) -> Playlist:
    """
    Run `mkvmerge -J` on a playlist and parse the resulting JSON for
    tracks and chapter count.

    JSON (rather than the human-readable `mkvmerge -i` text output) is
    used deliberately: the text format isn't a stable interface - its
    wording, spacing, and line layout can shift between MKVToolNix
    versions or with locale settings, which is exactly the kind of thing
    that quietly breaks a regex without any obvious error. The JSON
    schema is the format MKVToolNix documents and maintains for tooling.
    """
    result = _run([TOOL_PATHS["mkvmerge"], "-J", str(playlist_path)], timeout=60)
    # mkvmerge returns 0 for a clean read and 1 for warnings (still usable
    # output), but 2 means it couldn't read the file at all - in that case
    # stdout won't have useful track/chapter info, so surface the real
    # error instead of silently reporting "no Atmos track".
    if result.returncode >= 2:
        raise RuntimeError(
            f"mkvmerge could not read {playlist_path.name}:\n{result.stderr or result.stdout}"
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"mkvmerge produced output that wasn't valid JSON for "
            f"{playlist_path.name}: {exc}\n"
            f"First 2000 chars of output:\n{result.stdout[:2000]}"
        ) from exc

    pl = Playlist(path=playlist_path)

    for t in data.get("tracks", []):
        props = t.get("properties") or {}
        track_id = t.get("id")
        if track_id is None:
            continue  # malformed entry - skip rather than crash on a bad track_id
        pl.tracks.append(
            Track(
                track_id=track_id,
                kind=t.get("type", ""),
                codec=t.get("codec", ""),
                codec_id=props.get("codec_id", "") or "",
                language=props.get("language", "") or "",
                channels=props.get("audio_channels"),
                title=props.get("track_name", "") or "",
            )
        )

    chapters = data.get("chapters") or []
    if chapters:
        # mkvmerge -J groups chapters into one "edition"; a Blu-ray
        # playlist normally has exactly one, so take the first.
        pl.chapter_count = chapters[0].get("num_entries", 0)

    duration_ns = ((data.get("container") or {}).get("properties") or {}).get("duration")
    if duration_ns:
        pl.duration_seconds = duration_ns / 1_000_000_000

    return pl


def scan_disc_folder(disc_folder: Path) -> list[Playlist]:
    """Inspect every playlist in a disc folder, return list of Playlist."""
    return [inspect_playlist(p) for p in find_playlists(disc_folder)]


# ---------------------------------------------------------------------------
# Playlist scoring
# ---------------------------------------------------------------------------

@dataclass
class PlaylistScore:
    playlist: Playlist
    score: float
    reasons: list[str] = field(default_factory=list)
    duplicate_of: Optional[Path] = None  # set if this looks like a duplicate/alternate angle of a higher-scored playlist


def score_playlists(
    playlists: list[Playlist],
    expected_chapter_count: Optional[int] = None,
    expected_duration_seconds: Optional[float] = None,
) -> list[PlaylistScore]:
    """
    Score every scanned playlist as a candidate for "the" Atmos concert
    feature, replacing the old heuristic of silently picking whichever
    Atmos playlist happened to have the most chapters. Returns
    PlaylistScore objects sorted highest-score first, each carrying the
    reasons behind its score so the UI can show its work and the user can
    confirm (or override) the pick, instead of a single silent choice.

    expected_chapter_count / expected_duration_seconds: optional values
    from an external source (e.g. a user-imported tracklist). When given,
    playlists matching them closely get a bonus. Both are unused for now -
    they exist so a future tracklist-import feature can feed into playlist
    selection without changing this function's shape.
    """
    scored: list[PlaylistScore] = []

    for pl in playlists:
        score = 0.0
        reasons: list[str] = []

        if pl.has_atmos:
            score += 100
            atmos = pl.atmos_track
            reasons.append(f"Has a Dolby Atmos/TrueHD track (ID {atmos.track_id})")
        else:
            reasons.append("No Atmos/TrueHD track - very unlikely to be the right playlist")

        if pl.chapter_count > 0:
            score += min(pl.chapter_count * 2, 40)
            reasons.append(f"{pl.chapter_count} chapters")
        else:
            reasons.append("No chapters - can't be split into songs even if selected")

        if pl.video_track is not None:
            score += 10
            reasons.append(f"Includes a video track ({pl.video_track.codec})")
        else:
            reasons.append("No video track - audio-only playlist")

        if pl.duration_seconds > 0:
            minutes = pl.duration_seconds / 60
            # A real concert feature usually runs from roughly 20 minutes
            # to a few hours. Duration mainly helps rule out menus,
            # trailers, and short bonus clips rather than reward length
            # for its own sake, so this is capped and lightly weighted
            # rather than dominating the score.
            score += min(minutes, 180) * 0.15
            reasons.append(f"Runs {minutes:.0f} minutes")

        # Playlist number is a weak signal - naming conventions vary by
        # studio/authoring tool - so it only nudges close ties, never
        # dominates the score on its own.
        try:
            score -= int(pl.path.stem) * 0.01
        except ValueError:
            pass

        if expected_chapter_count is not None and pl.chapter_count == expected_chapter_count:
            score += 15
            reasons.append(f"Chapter count matches the expected tracklist ({expected_chapter_count})")

        if expected_duration_seconds is not None and pl.duration_seconds > 0:
            if abs(pl.duration_seconds - expected_duration_seconds) < 30:
                score += 15
                reasons.append("Duration matches the expected tracklist closely")

        scored.append(PlaylistScore(playlist=pl, score=score, reasons=reasons))

    _flag_duplicate_angles(scored)

    scored.sort(key=lambda s: s.score, reverse=True)
    return scored


def _flag_duplicate_angles(scored: list[PlaylistScore]) -> None:
    """
    Blu-rays sometimes expose the same underlying content as several
    playlists - alternate angles, region variants, a "clean" vs
    "with-recap" cut. These share duration, chapter count, and track
    layout almost exactly, so group candidates by that signature and mark
    every playlist but the highest-scored one in each group as a likely
    duplicate, rather than presenting near-identical entries as separate
    top candidates.
    """
    def signature(pl: Playlist) -> tuple:
        track_sig = tuple(sorted((t.kind, t.codec) for t in pl.tracks))
        return (pl.chapter_count, round(pl.duration_seconds), track_sig)

    groups: dict[tuple, list[PlaylistScore]] = {}
    for s in scored:
        groups.setdefault(signature(s.playlist), []).append(s)

    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda s: s.score, reverse=True)
        primary = group[0]
        for dup in group[1:]:
            dup.duplicate_of = primary.playlist.path
            dup.score -= 50
            dup.reasons.append(
                f"Same duration/chapters/tracks as {primary.playlist.path.name} "
                f"- likely a duplicate or alternate angle"
            )


def find_best_atmos_playlist(playlists: list[Playlist]) -> Optional[Playlist]:
    """
    Deprecated - kept only for backwards compatibility with any external
    scripts built against the old API. Prefer score_playlists(), which
    exposes the reasoning behind the pick and every other candidate
    instead of returning a single silent choice.
    """
    scored = [s for s in score_playlists(playlists) if s.playlist.has_atmos]
    return scored[0].playlist if scored else None


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


def read_chapters(mkv_path: Path, preferred_language: str = "eng") -> list[Chapter]:
    """
    Extract chapter markers from an MKV (or any mkvextract-readable
    source, including a Blu-ray .mpls playlist directly) via
    `mkvextract chapters -`.

    Also reads each chapter's embedded name, if the source has one: a
    ChapterAtom can carry multiple <ChapterDisplay> blocks (one per
    language) via <ChapterString>/<ChapterLanguage> - when there's more
    than one, the one matching preferred_language is used, falling back
    to the first display block present.
    """
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

        embedded_name, language = _pick_chapter_display(atom, preferred_language)

        chapters.append(
            Chapter(
                index=i,
                start_seconds=_timecode_to_seconds(start_el.text),
                embedded_name=embedded_name,
                language=language,
            )
        )

    # Fill in end times from the next chapter's start.
    for i, ch in enumerate(chapters):
        if i + 1 < len(chapters):
            ch.end_seconds = chapters[i + 1].start_seconds
        else:
            ch.end_seconds = None  # last chapter runs to end of file

    return chapters


def _pick_chapter_display(atom: ET.Element, preferred_language: str) -> tuple[str, str]:
    """
    A ChapterAtom can have several <ChapterDisplay> blocks (e.g. one per
    language track on the disc). Prefer the one whose <ChapterLanguage>
    matches preferred_language; otherwise use the first display block
    present. Returns (name, language), both "" if there's no display
    block or no <ChapterString> text at all.
    """
    displays = atom.findall("ChapterDisplay")
    if not displays:
        return "", ""

    def display_name_lang(display: ET.Element) -> tuple[str, str]:
        string_el = display.find("ChapterString")
        lang_el = display.find("ChapterLanguage")
        name = (string_el.text or "").strip() if string_el is not None else ""
        language = (lang_el.text or "").strip() if lang_el is not None else ""
        return name, language

    for display in displays:
        name, language = display_name_lang(display)
        if language == preferred_language and name:
            return name, language

    # No match for preferred_language (or none of them had a name) - fall
    # back to the first display block that actually has a name.
    for display in displays:
        name, language = display_name_lang(display)
        if name:
            return name, language

    return "", ""


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

# Windows reserves these as device names - "CON.mkv" etc silently fails or
# hits the device instead of creating a file, even though the name looks
# fine on Linux/macOS where this tool is often developed/tested.
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str) -> str:
    name = _INVALID_FILENAME_CHARS.sub("", name).strip()
    # Windows silently strips trailing dots/spaces from filenames, which
    # means "Track. " and "Track" collide without ever showing a warning -
    # strip them ourselves so what we display matches what actually lands
    # on disk.
    name = name.rstrip(" .")
    if not name:
        name = "Untitled"
    if name.upper() in _WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    return name


@dataclass
class PlannedOutput:
    chapter: "Chapter"
    path: Path
    exists: bool = False
    duplicate_name: bool = False  # another chapter sanitises to the same track name


def plan_output_files(
    chapters: list[Chapter],
    output_folder: Path,
    container: str = "mkv",
) -> list[PlannedOutput]:
    """
    Compute the final output filename for each chapter without touching
    the filesystem beyond an existence check.

    Every filename is prefixed with its chapter index ("01 - ...",
    "02 - ..."), so two chapters can never actually collide with each
    other on disk even if they share a name - but two chapters sharing a
    name is usually a sign the tracklist got pasted wrong (e.g. duplicated
    a line), so those are flagged via duplicate_name for the caller to
    show as a warning, not silently renamed.

    Separately, each planned path is checked against what's already on
    disk, so the caller can get user confirmation *before* running the
    (slow) extraction step, rather than discovering the collision partway
    through splitting.
    """
    name_counts: dict[str, int] = {}
    for ch in chapters:
        base = sanitize_filename(ch.name or f"Track {ch.index:02d}").lower()
        name_counts[base] = name_counts.get(base, 0) + 1

    planned: list[PlannedOutput] = []
    for ch in chapters:
        track_name = ch.name or f"Track {ch.index:02d}"
        base = sanitize_filename(track_name)
        path = output_folder / f"{ch.index:02d} - {base}.{container}"
        planned.append(
            PlannedOutput(
                chapter=ch,
                path=path,
                exists=path.exists(),
                duplicate_name=name_counts[base.lower()] > 1,
            )
        )

    return planned


def preflight_check(
    output_folder: Path,
    chapter_count: int,
    track_names: dict[int, str],
    container: str = "mkv",
) -> list[PlannedOutput]:
    """
    Compute planned output filenames and existence collisions using just
    the chapter count and the names the user has already typed in -
    before the slow mkvmerge extraction step even starts. Chapter start
    times aren't known yet at this point and aren't needed for filename
    planning, so this uses placeholder timestamps.
    """
    fake_chapters = [
        Chapter(index=i, start_seconds=0.0, name=track_names.get(i, ""))
        for i in range(1, chapter_count + 1)
    ]
    return plan_output_files(fake_chapters, output_folder, container=container)


class OutputCollisionError(RuntimeError):
    """
    Raised when one or more planned output files already exist on disk and
    weren't explicitly approved for overwrite. Nothing is ever overwritten
    silently - the caller (GUI or CLI) must resolve this before splitting
    can proceed, typically via preflight_check() + user confirmation.
    """

    def __init__(self, colliding: list[PlannedOutput]):
        self.colliding = colliding
        names = ", ".join(p.path.name for p in colliding)
        super().__init__(
            f"{len(colliding)} output file(s) already exist and were not "
            f"approved for overwrite: {names}"
        )


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
    overwrite: Optional[set[Path]] = None,
) -> list[Path]:
    """
    Split the Atmos MKV into one file per chapter using ffmpeg stream-copy
    (no re-encoding - Atmos must stay bit-exact).

    overwrite: set of specific output paths the caller has already gotten
    user approval to overwrite (normally via preflight_check() plus a
    confirmation dialog). Any planned path that already exists and isn't
    in this set raises OutputCollisionError instead of overwriting it -
    ffmpeg's own -y flag is never allowed to make that decision silently.
    """
    if not chapters:
        raise ValueError(
            "No chapters found - this playlist has an Atmos track but no "
            "chapter markers, so it can't be split into individual songs."
        )

    output_folder.mkdir(parents=True, exist_ok=True)

    # Check for collisions before the (comparatively expensive) duration
    # probe below, so a caller that skipped preflight_check still fails
    # fast instead of waiting on ffprobe first.
    planned = plan_output_files(chapters, output_folder, container=container)
    overwrite = overwrite or set()
    colliding = [p for p in planned if p.exists and p.path not in overwrite]
    if colliding:
        raise OutputCollisionError(colliding)

    if chapters[-1].end_seconds is None:
        chapters[-1].end_seconds = probe_duration_seconds(atmos_mkv)

    output_paths: list[Path] = []

    for item in planned:
        ch = item.chapter
        out_path = item.path
        track_name = ch.name or f"Track {ch.index:02d}"

        # Write to a temp file in the same folder (so the final rename is
        # on the same filesystem and therefore atomic) and only move it to
        # the real name once ffmpeg has produced a non-empty file. A crash,
        # Ctrl+C, or killed process mid-chapter then leaves a stray
        # ".partial-*" file instead of a truncated file sitting at the
        # real track name looking like a finished, playable song.
        tmp_path = out_path.with_name(f".partial-{uuid.uuid4().hex}-{out_path.name}")

        # -ss before -i does a fast keyframe seek in the input instead of
        # decoding from the start of the file on every single chapter
        # (which is what -ss after -i would do). Since we're stream-copying,
        # the cut still snaps to the nearest keyframe either way - you can't
        # cut mid-GOP without re-encoding - so accuracy is the same, but this
        # is dramatically faster, especially for later chapters in a long file.
        args = [
            TOOL_PATHS["ffmpeg"],
            "-y",  # only ever overwrites our own freshly-named temp file
            "-ss", str(ch.start_seconds),
            "-i", str(atmos_mkv),
        ]
        if ch.end_seconds is not None:
            # -to is relative to the original input timeline when placed
            # after -i, but ffmpeg re-bases it to the seek point when -ss
            # comes before -i - so we need the chapter's duration, not its
            # absolute end time, to get the correct cut point.
            args += ["-t", str(ch.end_seconds - ch.start_seconds)]
        args += ["-c", "copy", str(tmp_path)]

        if progress_cb:
            progress_cb(f"Splitting chapter {ch.index}: {track_name}")

        try:
            result = _run(args, timeout=1800)  # 30 min per chapter - stream-copy is fast, this is just a safety net
            if result.returncode != 0:
                raise RuntimeError(f"ffmpeg failed on chapter {ch.index}:\n{result.stderr}")
            if not tmp_path.is_file() or tmp_path.stat().st_size == 0:
                raise RuntimeError(
                    f"ffmpeg reported success on chapter {ch.index} ({track_name}) "
                    f"but produced no output file, or an empty one."
                )
            os.replace(tmp_path, out_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

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
    overwrite: Optional[set[Path]] = None,
) -> list[Path]:
    """
    track_names: maps chapter index (1-based) -> song title.
    Returns list of final output file paths.

    cleanup_work_folder: if True (default), deletes work_folder (and the
    large intermediate _atmos_extracted.mkv inside it) once splitting has
    finished successfully. If splitting raises, the work folder is left
    in place so the extraction doesn't have to be redone. Set to False to
    always keep the intermediate file around (e.g. for debugging).

    overwrite: paths pre-approved for overwrite by the caller (see
    preflight_check()). Extraction still runs even if this ends up wrong
    (e.g. the disk changed since preflight) - the collision is caught by
    split_chapters afterwards rather than skipped, so nothing gets
    silently overwritten either way.
    """
    atmos_mkv = work_folder / "_atmos_extracted.mkv"
    extract_atmos_mkv(playlist, atmos_mkv, progress_cb=progress_cb)

    chapters = read_chapters(atmos_mkv)
    for ch in chapters:
        if ch.index in track_names:
            ch.name = track_names[ch.index]

    results = split_chapters(
        atmos_mkv, chapters, output_folder, container=container,
        progress_cb=progress_cb, overwrite=overwrite,
    )

    if cleanup_work_folder:
        if progress_cb:
            progress_cb("Cleaning up temporary files...")
        shutil.rmtree(work_folder, ignore_errors=True)

    return results
