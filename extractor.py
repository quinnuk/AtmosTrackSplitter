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

import dataclasses
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    Check whether each configured tool is actually runnable and actually
    the right tool. See verify_tool_at_path() for why - existence alone
    doesn't prove much, and neither does exit code alone.

    Returns {tool_name: True/False}.
    """
    return {name: verify_tool_at_path(path, name)[0] for name, path in TOOL_PATHS.items()}


# ffmpeg/ffprobe use a single-dash "-version" - "--version" (GNU style) is
# NOT a recognised option for them. mkvmerge/mkvextract do use GNU-style
# "--version". Using the wrong flag doesn't necessarily fail cleanly: ffmpeg
# prints its full version banner to stderr unconditionally at startup,
# *before* it even parses arguments, so an unrecognised "--version" still
# produces a perfectly legible banner followed by an "Unrecognized option"
# error and a nonzero exit - which looks exactly like a real failure if
# you're only checking the exit code.
_VERSION_FLAGS = {
    "mkvmerge": "--version",
    "mkvextract": "--version",
    "ffmpeg": "-version",
    "ffprobe": "-version",
}

# Text each tool prints about itself, used to confirm a binary's actual
# identity rather than just trusting that "something ran successfully".
_IDENTIFYING_TEXT = {
    "mkvmerge": "mkvmerge v",
    "mkvextract": "mkvextract v",
    "ffmpeg": "ffmpeg version",
    "ffprobe": "ffprobe version",
}


def verify_tool_at_path(path: str, tool_name: str, timeout: float = 10.0) -> tuple[bool, str]:
    """
    Check whether a specific path is actually a working copy of tool_name -
    usable directly against a path the user just picked in a file browser,
    before it's saved to settings at all.

    This checks the tool's own self-identifying banner text in its output
    (e.g. "ffmpeg version") rather than trusting the exit code alone, for
    two reasons that both showed up in practice:

    - ffmpeg/ffprobe print their version banner to stderr unconditionally
      at startup, before parsing arguments - so a genuinely-working binary
      given the wrong flag can still exit nonzero after having already
      printed a perfectly valid banner. Checking only the exit code would
      wrongly reject it and show that banner back as if it were an error.
    - A file that runs fine and exits 0 isn't necessarily the *right*
      tool - ffmpeg.exe and ffprobe.exe sit right next to each other in
      every install, and it's an easy misclick to pick one while browsing
      for the other. Checking identity catches that with a clear message
      instead of a false pass.

    Returns (True, first line of version output) on success, or
    (False, a short human-readable reason - naming what it actually looks
    like, if it's recognisably one of our *other* tools) on failure.
    """
    flag = _VERSION_FLAGS.get(tool_name, "--version")
    try:
        result = _run([path, flag], timeout=timeout)
    except (FileNotFoundError, OSError) as exc:
        return False, f"Could not run this file: {exc}"
    except RuntimeError as exc:
        # _run's own timeout wrapper - already a clear message.
        return False, str(exc)

    combined = f"{result.stdout or ''}\n{result.stderr or ''}"
    combined_lower = combined.lower()

    expected = _IDENTIFYING_TEXT.get(tool_name, tool_name)
    if expected.lower() in combined_lower:
        first_line = next((ln.strip() for ln in combined.splitlines() if ln.strip()), "OK")
        return True, first_line

    # Not the expected tool - check whether it's recognisably one of our
    # *other* tools, so a mixed-up file gets a specific, actionable answer
    # ("this looks like ffprobe, not ffmpeg") instead of a raw dump.
    for other_name, other_text in _IDENTIFYING_TEXT.items():
        if other_name != tool_name and other_text.lower() in combined_lower:
            return False, f"This looks like {other_name}, not {tool_name}."

    if result.returncode != 0:
        detail_lines = [ln.strip() for ln in combined.splitlines() if ln.strip()]
        detail = detail_lines[0] if detail_lines else f"exited with code {result.returncode}"
        return False, detail

    return False, "Ran, but didn't produce recognisable version output."


# Tools that are always installed together in the same folder, so once the
# user locates one we can offer to auto-detect the other instead of making
# them browse twice for what's really one install.
SIBLING_TOOL_NAMES = {
    "ffmpeg": "ffprobe",
    "ffprobe": "ffmpeg",
    "mkvmerge": "mkvextract",
    "mkvextract": "mkvmerge",
}


def guess_sibling_tool_path(chosen_path: str, tool_name: str) -> Optional[str]:
    """
    Given a path the user just picked for one tool (e.g. .../bin/ffmpeg.exe),
    guess the path for its usual companion in the same install (ffprobe
    next to ffmpeg, mkvextract next to mkvmerge - both pairs are always
    shipped together in the same bin/ folder). Only replaces the filename
    itself, not anything matching the tool name elsewhere in the path
    (e.g. a parent folder called "ffmpeg-8.1.2-full_build" is left alone).

    Returns the guessed path only if a file actually exists there - the
    caller still has to run it through verify_tool_at_path() before
    trusting it, since a same-named file isn't proof it's a working,
    matching-architecture binary.
    """
    sibling_name = SIBLING_TOOL_NAMES.get(tool_name)
    if sibling_name is None:
        return None

    chosen = Path(chosen_path)
    candidate = chosen.with_name(chosen.name.replace(tool_name, sibling_name))
    if candidate != chosen and candidate.is_file():
        return str(candidate)
    return None


def _tool_versions() -> dict[str, str]:
    """First line of each configured tool's version output, for the job manifest."""
    versions: dict[str, str] = {}
    for name, configured_path in TOOL_PATHS.items():
        ok, message = verify_tool_at_path(configured_path, name)
        versions[name] = message if ok else "unknown"
    return versions


# ---------------------------------------------------------------------------
# Job manifest - lets an interrupted job be resumed instead of redone from
# scratch, and gives "Open log" / crash diagnosis something concrete to
# point at.
# ---------------------------------------------------------------------------

MANIFEST_FILENAME = "manifest.json"


@dataclass
class JobManifest:
    source_playlist: str
    atmos_track_id: int
    video_track_id: Optional[int]
    output_folder: str
    container: str
    track_names: dict[str, str]      # chapter index (as string, for JSON) -> name
    tool_versions: dict[str, str]
    created_at: str
    status: str = "pending"          # pending -> extracting -> splitting -> complete | failed | cancelled
    atmos_extracted: bool = False    # True once extract_atmos_mkv has succeeded - independent of status,
                                      # since status becomes "failed"/"cancelled" regardless of which stage failed
    chapters: list[dict] = field(default_factory=list)             # [{index, start_seconds, end_seconds, name}]
    completed_outputs: list[str] = field(default_factory=list)     # output paths already written, in order

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "JobManifest":
        data = json.loads(text)
        known_fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known_fields})


def write_manifest(work_folder: Path, manifest: JobManifest) -> None:
    """Write the manifest atomically - a crash mid-write must never leave a corrupt manifest behind."""
    work_folder.mkdir(parents=True, exist_ok=True)
    path = work_folder / MANIFEST_FILENAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(manifest.to_json(), encoding="utf-8")
    os.replace(tmp, path)


def read_manifest(work_folder: Path) -> Optional[JobManifest]:
    """
    Read a previous job's manifest from work_folder, or None if there
    isn't one (fresh job) or it's unreadable (treated the same as "no
    manifest" - a corrupt manifest shouldn't block starting a new job,
    it just means resume isn't available for whatever was there before).
    """
    path = work_folder / MANIFEST_FILENAME
    if not path.is_file():
        return None
    try:
        return JobManifest.from_json(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError, KeyError, OSError):
        return None


class JobCancelled(Exception):
    """Raised when a running job is stopped via a cancel_event."""


def _run(
    args: list[str],
    timeout: Optional[float] = None,
    cancel_event: Optional[threading.Event] = None,
) -> subprocess.CompletedProcess:
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

    cancel_event: if given, checked roughly twice a second while the
    process runs. If set, the process is terminated (SIGTERM, then
    SIGKILL if it hasn't exited within 5s) and JobCancelled is raised
    instead of waiting for it to finish naturally. Omitted entirely for
    calls that don't need to be cancellable (quick --version checks etc),
    which keeps using the simpler non-polling subprocess.run path.
    """
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    if cancel_event is None:
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

    # Cancellable path: subprocess.run() blocks until the process exits
    # with no way to interrupt it early, so use Popen + a short-timeout
    # communicate() loop instead, checking cancel_event between polls.
    # Retrying communicate() after a TimeoutExpired is explicitly
    # supported and doesn't lose any output (per the subprocess docs).
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    started = time.monotonic()
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=0.5)
            return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            if cancel_event.is_set():
                proc.terminate()
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                raise JobCancelled("Cancelled by user.")
            if timeout is not None and (time.monotonic() - started) > timeout:
                proc.kill()
                proc.communicate()
                tool = Path(args[0]).name if args else "process"
                raise RuntimeError(
                    f"{tool} timed out after {timeout} seconds and was killed. "
                    f"Command: {' '.join(args)}"
                )


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
    cancel_event: Optional[threading.Event] = None,
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

    result = _run(args, timeout=7200, cancel_event=cancel_event)  # 2 hours - full-disc extraction can be slow
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


_LEADING_NUMBER_RE = re.compile(r"^\s*\d+\s*[\.\)\-]?\s*")


def strip_leading_number(line: str) -> str:
    """Strip a leading '1.', '01 -', '1)' etc from a pasted or imported tracklist line."""
    return _LEADING_NUMBER_RE.sub("", line).strip()


# ---------------------------------------------------------------------------
# Track-name discovery: disc-local sidecar files and user-imported tracklists
# ---------------------------------------------------------------------------

@dataclass
class TracklistEntry:
    name: str
    index: Optional[int] = None
    start_seconds: Optional[float] = None
    duration_seconds: Optional[float] = None


def find_sidecar_tracklist_files(disc_folder: Path) -> list[Path]:
    """
    Look for disc-local files that might carry track names: a plain text
    or NFO tracklist dropped next to the disc, a CUE sheet, a previously
    saved project mapping (*.tracklist.json), or (best-effort) BDMV disc
    metadata. Only the top level of disc_folder is searched for the
    common formats - not recursively - since a deep search of the whole
    BDMV structure would surface a lot of files that have nothing to do
    with track names.

    Never applied automatically - this only finds candidates. Parsing
    happens via load_sidecar_tracklist(), and the result still has to go
    through match_tracklist_to_chapters() and user confirmation before it
    touches any naming field.
    """
    candidates: list[Path] = []
    for pattern in ("*.nfo", "*.cue", "*.txt", "*.tracklist.json"):
        candidates.extend(sorted(disc_folder.glob(pattern)))

    meta_dl = disc_folder / "BDMV" / "META" / "DL"
    if meta_dl.is_dir():
        candidates.extend(sorted(meta_dl.glob("*.xml")))

    return candidates


def parse_plain_text_tracklist(text: str) -> list[TracklistEntry]:
    """One track name per line - the common shape for a .txt/.nfo tracklist or a pasted list."""
    entries: list[TracklistEntry] = []
    for i, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        entries.append(TracklistEntry(index=i, name=strip_leading_number(line)))
    return entries


_CUE_TRACK_RE = re.compile(r'^\s*TRACK\s+(\d+)\s+AUDIO', re.IGNORECASE)
_CUE_TITLE_RE = re.compile(r'^\s*TITLE\s+"(.*)"', re.IGNORECASE)
_CUE_INDEX01_RE = re.compile(r'^\s*INDEX\s+01\s+(\d+):(\d+):(\d+)', re.IGNORECASE)


def parse_cue_tracklist(text: str) -> list[TracklistEntry]:
    """
    Parse a CUE sheet's TRACK/TITLE/INDEX 01 entries. INDEX 01 timestamps
    are mm:ss:ff (ff = frames, 75 frames/second in the Red Book standard
    CUE sheets use), which gives each track a real start time - unlike a
    plain text list, this lets match_tracklist_to_chapters() align by
    actual position instead of assuming track order matches chapter order.
    """
    entries: list[TracklistEntry] = []
    current_index: Optional[int] = None
    current_title = ""
    current_start: Optional[float] = None

    def flush() -> None:
        if current_index is not None and current_title:
            entries.append(
                TracklistEntry(index=current_index, name=current_title, start_seconds=current_start)
            )

    for line in text.splitlines():
        m = _CUE_TRACK_RE.match(line)
        if m:
            flush()
            current_index = int(m.group(1))
            current_title = ""
            current_start = None
            continue

        # Only a TITLE line *after* a TRACK line is a song title - a
        # TITLE line before the first TRACK is the album title.
        m = _CUE_TITLE_RE.match(line)
        if m and current_index is not None:
            current_title = m.group(1).strip()
            continue

        m = _CUE_INDEX01_RE.match(line)
        if m and current_index is not None:
            mm, ss, ff = m.groups()
            current_start = int(mm) * 60 + int(ss) + int(ff) / 75.0
            continue

    flush()

    for i, entry in enumerate(entries):
        if (
            i + 1 < len(entries)
            and entry.start_seconds is not None
            and entries[i + 1].start_seconds is not None
        ):
            entry.duration_seconds = entries[i + 1].start_seconds - entry.start_seconds

    return entries


def parse_json_tracklist(text: str) -> list[TracklistEntry]:
    """
    Supports a few JSON shapes:
      - a list of plain strings: ["Song One", "Song Two"]
      - a list of objects: [{"name": "...", "start_seconds": ..., "duration_seconds": ...}, ...]
      - a dict mapping chapter index -> name: {"1": "Song One", "2": "Song Two"}
        (the shape a previously saved project mapping would use)
    """
    data = json.loads(text)
    entries: list[TracklistEntry] = []

    if isinstance(data, list):
        for i, item in enumerate(data, start=1):
            if isinstance(item, str):
                if item.strip():
                    entries.append(TracklistEntry(index=i, name=item.strip()))
            elif isinstance(item, dict):
                name = str(item.get("name") or item.get("title") or "").strip()
                if not name:
                    continue
                entries.append(
                    TracklistEntry(
                        index=item.get("index", i),
                        name=name,
                        start_seconds=item.get("start_seconds"),
                        duration_seconds=item.get("duration_seconds"),
                    )
                )
    elif isinstance(data, dict):
        for k, v in data.items():
            try:
                idx = int(k)
            except (TypeError, ValueError):
                continue
            if isinstance(v, str) and v.strip():
                entries.append(TracklistEntry(index=idx, name=v.strip()))
        entries.sort(key=lambda e: e.index if e.index is not None else 0)

    return entries


def parse_disc_meta_tracklist(text: str) -> list[TracklistEntry]:
    """
    Best-effort parse of a BDMV/META/DL/*.xml disc metadata file. These
    aren't a standardised song-tracklist format - they're disc/menu
    metadata (title, thumbnails; a chapter/mark title list only shows up
    depending on the authoring tool) - so this only looks for a handful
    of common tag shapes (<chapter>/<track>/<item> with a <title> or
    <name> child) and returns an empty list if none match, rather than
    guessing at a schema that may not apply. A result from here goes
    through the same match/review step as every other source either way.
    """
    try:
        root = ET.fromstring(text.lstrip("\ufeff"))
    except ET.ParseError:
        return []

    entries: list[TracklistEntry] = []
    for tag in ("chapter", "Chapter", "track", "Track", "item", "Item"):
        for i, el in enumerate(root.iter(tag), start=1):
            title_el = (
                el.find("title") or el.find("Title") or el.find("name") or el.find("Name")
            )
            name = (title_el.text or "").strip() if title_el is not None else ""
            if name:
                entries.append(TracklistEntry(index=i, name=name))
        if entries:
            break

    return entries


def load_sidecar_tracklist(path: Path) -> list[TracklistEntry]:
    """
    Parse a sidecar tracklist file based on its extension. Raises
    ValueError with a clear, file-specific message on anything
    unparseable or empty - this is user-supplied data (someone's own
    .nfo/.cue/.txt/.json file), so a bad or unexpected file is expected
    occasionally and should read as a normal, specific error rather than
    a raw parser traceback.
    """
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise ValueError(f"Could not read {path.name}: {exc}") from exc

    suffix = path.suffix.lower()
    try:
        if suffix == ".cue":
            entries = parse_cue_tracklist(text)
        elif suffix == ".json" or path.name.lower().endswith(".tracklist.json"):
            entries = parse_json_tracklist(text)
        elif suffix == ".xml":
            entries = parse_disc_meta_tracklist(text)
        else:
            # .nfo, .txt, and anything unrecognised - treat as plain text.
            entries = parse_plain_text_tracklist(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Could not parse {path.name} as JSON: {exc}") from exc

    if not entries:
        raise ValueError(f"{path.name} didn't contain anything recognisable as track names.")

    return entries


@dataclass
class ChapterMatch:
    chapter_index: int
    name: str = ""
    confidence: str = "no_match"   # "matched" | "duration_mismatch" | "no_match"
    detail: str = ""


@dataclass
class TracklistMatchResult:
    matches: list[ChapterMatch]
    matched_count: int
    total_chapters: int
    summary: str


def match_tracklist_to_chapters(
    chapters: list[Chapter],
    entries: list[TracklistEntry],
    start_tolerance_seconds: float = 8.0,
    duration_tolerance_seconds: float = 20.0,
) -> TracklistMatchResult:
    """
    Propose a mapping from an imported tracklist onto actual chapters.
    Never applied automatically - this only produces per-chapter
    confidence for the caller (GUI) to show and let the user accept,
    adjust, or reject before it touches any naming field.

    Two alignment strategies, chosen by what the tracklist provides:

    - If every entry has a start_seconds (from a CUE sheet's INDEX 01
      timestamps), align each chapter to whichever unused entry's start
      time is closest, within start_tolerance_seconds. This tolerates
      chapters the tracklist doesn't cover and tracklist entries that
      don't correspond to any chapter - intros, encores, spoken
      sections, bonus features - rather than assuming a strict 1:1
      position match.

    - Otherwise (plain text/NFO/JSON names with no timing), align
      positionally: tracklist entry i -> chapter i. There's no timing
      information to align by in that case, and straight position order
      is what a pasted "one song per line" list means.

    Where an entry does carry a duration_seconds (from a CUE sheet), a
    matched chapter whose actual duration differs by more than
    duration_tolerance_seconds is flagged "duration_mismatch" rather than
    a plain match, since that usually means the tracklist is for a
    different edition/cut even though the position lined up.
    """
    matches: list[ChapterMatch] = []
    has_timing = bool(entries) and all(e.start_seconds is not None for e in entries)

    if has_timing:
        used: set[int] = set()
        for ch in chapters:
            best_i, best_diff = None, None
            for i, entry in enumerate(entries):
                if i in used:
                    continue
                diff = abs(entry.start_seconds - ch.start_seconds)
                if best_diff is None or diff < best_diff:
                    best_i, best_diff = i, diff

            if best_i is not None and best_diff is not None and best_diff <= start_tolerance_seconds:
                entry = entries[best_i]
                used.add(best_i)
                confidence, detail = "matched", ""
                if entry.duration_seconds is not None and ch.end_seconds is not None:
                    actual_duration = ch.end_seconds - ch.start_seconds
                    duration_diff = abs(actual_duration - entry.duration_seconds)
                    if duration_diff > duration_tolerance_seconds:
                        confidence = "duration_mismatch"
                        detail = f"Track {ch.index} differs by {duration_diff:.0f} seconds"
                matches.append(
                    ChapterMatch(chapter_index=ch.index, name=entry.name, confidence=confidence, detail=detail)
                )
            else:
                matches.append(ChapterMatch(chapter_index=ch.index, confidence="no_match"))
    else:
        for i, ch in enumerate(chapters):
            if i < len(entries) and entries[i].name:
                matches.append(ChapterMatch(chapter_index=ch.index, name=entries[i].name, confidence="matched"))
            else:
                matches.append(ChapterMatch(chapter_index=ch.index, confidence="no_match"))

    matched_count = sum(1 for m in matches if m.confidence in ("matched", "duration_mismatch"))
    summary = f"{matched_count} of {len(chapters)} chapter(s) matched"

    # A large gap between tracklist length and chapter count - well beyond
    # what a few bonus tracks/intros would explain - is a sign this
    # tracklist may belong to a different edition of the release entirely,
    # not just a partial match. This is a hint, not a hard rule.
    if entries and abs(len(entries) - len(chapters)) >= max(2, len(chapters) // 3):
        summary += " - candidate may be a different live edition (track counts differ significantly)"

    return TracklistMatchResult(
        matches=matches, matched_count=matched_count, total_chapters=len(chapters), summary=summary
    )


# ---------------------------------------------------------------------------
# MusicBrainz lookup - an optional, online track-name source. Never applied
# automatically: search -> pick a release -> fetch its tracklist -> the
# same match_tracklist_to_chapters()/review flow as any other source.
# ---------------------------------------------------------------------------

MUSICBRAINZ_BASE_URL = "https://musicbrainz.org/ws/2"
# MusicBrainz's API policy requires a descriptive User-Agent identifying
# the application and a contact point - requests without one are liable
# to be blocked outright. See https://musicbrainz.org/doc/MusicBrainz_API
MUSICBRAINZ_USER_AGENT = (
    "AtmosTrackSplitter/0.1 (+https://github.com/quinnuk/AtmosTrackSplitter)"
)

_musicbrainz_lock = threading.Lock()
_musicbrainz_last_request = 0.0


def _musicbrainz_request(path: str, params: dict[str, str], timeout: float = 15.0) -> dict:
    """
    GET a MusicBrainz API endpoint. Enforces MusicBrainz's rate limit
    (documented as roughly one request per second) via a module-level
    lock so overlapping calls from different threads still queue up
    properly, rather than each independently waiting and bursting.
    """
    global _musicbrainz_last_request
    with _musicbrainz_lock:
        wait = 1.0 - (time.monotonic() - _musicbrainz_last_request)
        if wait > 0:
            time.sleep(wait)

        query = urllib.parse.urlencode({**params, "fmt": "json"})
        url = f"{MUSICBRAINZ_BASE_URL}/{path}?{query}"
        request = urllib.request.Request(url, headers={"User-Agent": MUSICBRAINZ_USER_AGENT})

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"MusicBrainz returned an error ({exc.code}): {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach MusicBrainz: {exc.reason}") from exc
        finally:
            _musicbrainz_last_request = time.monotonic()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"MusicBrainz returned unexpected data: {exc}") from exc


_YEAR_RE = re.compile(r"\((\d{4})\)")


def guess_artist_album_year(source_folder: Path) -> tuple[str, str, Optional[int]]:
    """
    Best-effort guess at artist/album/year from a disc-rip folder name,
    to prefill the MusicBrainz search form - e.g. "Fleetwood Mac -
    Fleetwood Mac (1975) [Blu-ray]" splits on the first " - " into
    artist/album and pulls the year out separately. Purely a starting
    point for a form the user reviews and can edit before anything is
    sent to MusicBrainz - if the folder name doesn't fit the pattern,
    this just returns an empty artist and the whole cleaned name as the
    album guess.
    """
    name = derive_album_folder_name(source_folder)
    year_match = _YEAR_RE.search(name)
    year = int(year_match.group(1)) if year_match else None
    if year_match:
        name = name[: year_match.start()].strip()

    if " - " in name:
        artist, _, album = name.partition(" - ")
        return artist.strip(), album.strip(), year
    return "", name.strip(), year


@dataclass
class MusicBrainzCandidate:
    release_id: str
    title: str
    artist: str
    date: str = ""
    track_count: int = 0
    format_hint: str = ""   # e.g. "Blu-ray", "Digital Media" - the release's MusicBrainz format, if any
    score: int = 0          # MusicBrainz's own relevance score (0-100) for this search, not our playlist score


def search_musicbrainz_releases(
    artist: str,
    album_title: str,
    year: Optional[int] = None,
    limit: int = 8,
) -> list[MusicBrainzCandidate]:
    """
    Search MusicBrainz for release candidates matching an artist/album
    (and optionally year). Returns MusicBrainz's own ranked candidates -
    this is a search, not a lookup, so more than one release can come
    back (different editions, live vs studio versions, reissues). Nothing
    is fetched or applied here; use fetch_musicbrainz_tracklist() on
    whichever candidate the caller (user) picks.
    """
    if not artist.strip() or not album_title.strip():
        raise ValueError("Both artist and album title are needed to search MusicBrainz.")

    query_parts = [f'artist:"{artist.strip()}"', f'release:"{album_title.strip()}"']
    if year:
        query_parts.append(f"date:{year}")
    query = " AND ".join(query_parts)

    data = _musicbrainz_request("release", {"query": query, "limit": str(limit)})

    candidates: list[MusicBrainzCandidate] = []
    for r in data.get("releases", []):
        artist_credit = r.get("artist-credit") or []
        artist_name = artist_credit[0].get("name", "") if artist_credit else ""
        media = r.get("media") or []
        track_count = sum(m.get("track-count", 0) for m in media)
        format_hint = media[0].get("format") or "" if media else ""
        candidates.append(
            MusicBrainzCandidate(
                release_id=r.get("id", ""),
                title=r.get("title", ""),
                artist=artist_name,
                date=r.get("date", ""),
                track_count=track_count,
                format_hint=format_hint,
                score=r.get("score", 0),
            )
        )
    return candidates


def fetch_musicbrainz_tracklist(release_id: str) -> list[TracklistEntry]:
    """
    Fetch the full tracklist (with per-track lengths, where MusicBrainz
    has them) for a specific release, as TracklistEntry objects ready for
    match_tracklist_to_chapters(). Only the first medium (disc) is used -
    MusicBrainz supports multi-disc releases, but one Atmos Blu-ray
    playlist corresponds to a single continuous chapter set.

    start_seconds is only filled in for a run of tracks whose lengths are
    all known from the start - a length-less track partway through the
    list means every start time after it would be a guess, so those are
    left as None instead. match_tracklist_to_chapters() already falls
    back to positional alignment whenever any entry lacks a start time,
    so a partial gap here just means a partial fallback there.
    """
    data = _musicbrainz_request(f"release/{release_id}", {"inc": "recordings"})
    media = data.get("media") or []
    if not media:
        return []

    entries: list[TracklistEntry] = []
    cumulative_start = 0.0
    timing_still_reliable = True

    for track in media[0].get("tracks", []):
        title = (track.get("title") or "").strip()
        if not title:
            continue
        length_ms = track.get("length")
        duration_seconds = (length_ms / 1000.0) if length_ms else None

        entries.append(
            TracklistEntry(
                index=track.get("position", len(entries) + 1),
                name=title,
                start_seconds=cumulative_start if timing_still_reliable else None,
                duration_seconds=duration_seconds,
            )
        )
        if duration_seconds is not None and timing_still_reliable:
            cumulative_start += duration_seconds
        else:
            timing_still_reliable = False

    return entries


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
    cancel_event: Optional[threading.Event] = None,
    skip_paths: Optional[set[Path]] = None,
    on_output_written: Optional[Callable[[Path], None]] = None,
) -> list[Path]:
    """
    Split the Atmos MKV into one file per chapter using ffmpeg stream-copy
    (no re-encoding - Atmos must stay bit-exact).

    overwrite: set of specific output paths the caller has already gotten
    user approval to overwrite (normally via preflight_check() plus a
    confirmation dialog). Any planned path that already exists and isn't
    in this set raises OutputCollisionError instead of overwriting it -
    ffmpeg's own -y flag is never allowed to make that decision silently.

    cancel_event: checked before each chapter and passed down into the
    ffmpeg call itself, so cancelling actually stops the in-progress
    chapter rather than only stopping between chapters.

    skip_paths: output paths already known-complete from a previous run
    of this same job (see run_full_pipeline's resume support) - these are
    left untouched and reported as already done, rather than re-split.

    on_output_written: called with each output path immediately after
    it's finalised, so a caller can persist progress (e.g. to the job
    manifest) incrementally instead of only at the very end.
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
    skip_paths = skip_paths or set()
    colliding = [
        p for p in planned if p.exists and p.path not in overwrite and p.path not in skip_paths
    ]
    if colliding:
        raise OutputCollisionError(colliding)

    if chapters[-1].end_seconds is None:
        chapters[-1].end_seconds = probe_duration_seconds(atmos_mkv)

    output_paths: list[Path] = []

    for item in planned:
        ch = item.chapter
        out_path = item.path
        track_name = ch.name or f"Track {ch.index:02d}"

        if out_path in skip_paths and out_path.is_file():
            if progress_cb:
                progress_cb(f"Chapter {ch.index} ({track_name}) already done - skipping.")
            output_paths.append(out_path)
            continue

        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled("Cancelled by user.")

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
            # 30 min per chapter - stream-copy is fast, this is just a safety net
            result = _run(args, timeout=1800, cancel_event=cancel_event)
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
        if on_output_written:
            on_output_written(out_path)

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
    cancel_event: Optional[threading.Event] = None,
    resume: bool = False,
    log_path: Optional[Path] = None,
) -> list[Path]:
    """
    track_names: maps chapter index (1-based) -> song title.
    Returns list of final output file paths.

    cleanup_work_folder: if True (default), deletes work_folder (and the
    large intermediate _atmos_extracted.mkv inside it, and the job
    manifest) once splitting has finished successfully. If splitting
    raises - including cancellation - the work folder and manifest are
    left in place so the job can be resumed. Set to False to always keep
    the intermediate file around (e.g. for debugging).

    overwrite: paths pre-approved for overwrite by the caller (see
    preflight_check()). Extraction still runs even if this ends up wrong
    (e.g. the disk changed since preflight) - the collision is caught by
    split_chapters afterwards rather than skipped, so nothing gets
    silently overwritten either way.

    cancel_event: if set while running, stops as soon as the current
    external-tool call can be safely terminated and raises JobCancelled.
    The manifest is updated to status "cancelled" first, so the job shows
    up as resumable rather than merely failed.

    resume: if True and work_folder already has a manifest from a
    previous run of this same job, reuse whatever it recorded as already
    done - skip re-extracting the intermediate Atmos file if it's already
    there, and skip re-splitting any chapter whose output file is already
    on disk. This folds "resume" and "retry failed chapters" into one
    behaviour: since progress is tracked by which output files actually
    exist, there's nothing left to distinguish a plain resume from a
    retry-what-failed - both mean "don't redo what's already done".

    log_path: if given, every progress message is also appended to this
    file (with a UTC timestamp), independent of progress_cb - this is
    what an "Open log" button in the UI can point at, and it survives
    even if the caller passes cleanup_work_folder=True.
    """
    def log(message: str) -> None:
        if progress_cb:
            progress_cb(message)
        if log_path:
            timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"{timestamp}  {message}\n")

    atmos_mkv = work_folder / "_atmos_extracted.mkv"

    manifest = read_manifest(work_folder) if resume else None
    if manifest is None:
        manifest = JobManifest(
            source_playlist=str(playlist.path),
            atmos_track_id=playlist.atmos_track.track_id if playlist.atmos_track else -1,
            video_track_id=playlist.video_track.track_id if playlist.video_track else None,
            output_folder=str(output_folder),
            container=container,
            track_names={str(k): v for k, v in track_names.items()},
            tool_versions=_tool_versions(),
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            status="extracting",
        )
        write_manifest(work_folder, manifest)

    try:
        if resume and manifest.atmos_extracted and atmos_mkv.is_file():
            log("Resuming: Atmos file already extracted, skipping re-extraction.")
        else:
            manifest.status = "extracting"
            manifest.atmos_extracted = False
            write_manifest(work_folder, manifest)
            extract_atmos_mkv(playlist, atmos_mkv, progress_cb=log, cancel_event=cancel_event)
            manifest.atmos_extracted = True
            write_manifest(work_folder, manifest)

        chapters = read_chapters(atmos_mkv)
        for ch in chapters:
            if ch.index in track_names:
                ch.name = track_names[ch.index]

        manifest.chapters = [
            {
                "index": ch.index,
                "start_seconds": ch.start_seconds,
                "end_seconds": ch.end_seconds,
                "name": ch.name,
            }
            for ch in chapters
        ]
        manifest.status = "splitting"
        write_manifest(work_folder, manifest)

        already_done = {Path(p) for p in manifest.completed_outputs} if resume else set()
        if already_done:
            log(f"Resuming: {len(already_done)} chapter(s) already split, skipping those.")

        def on_output_written(path: Path) -> None:
            manifest.completed_outputs.append(str(path))
            write_manifest(work_folder, manifest)

        results = split_chapters(
            atmos_mkv, chapters, output_folder, container=container,
            progress_cb=log, overwrite=overwrite, cancel_event=cancel_event,
            skip_paths=already_done, on_output_written=on_output_written,
        )

        manifest.status = "complete"
        write_manifest(work_folder, manifest)

    except JobCancelled:
        manifest.status = "cancelled"
        write_manifest(work_folder, manifest)
        log("Cancelled.")
        raise
    except Exception as exc:
        manifest.status = "failed"
        write_manifest(work_folder, manifest)
        log(f"Failed: {exc}")
        raise

    if cleanup_work_folder:
        log("Cleaning up temporary files...")
        shutil.rmtree(work_folder, ignore_errors=True)

    return results
