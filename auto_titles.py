"""
auto_titles.py

Automatic track-title resolution for Atmos Track Splitter.

Collects embedded chapter names, local sidecars, MusicBrainz and Discogs
tracklists, then aligns candidates to real chapters by duration. Matches are
returned for review; this browser edition does not split unattended.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import extractor
from extractor import Chapter, TracklistEntry

# Kept as a scoring signal for the review UI only. Automatic splitting is
# intentionally disabled until the resolver has been validated on real discs.
AUTO_ACCEPT = 0.85
MIN_USEFUL = 0.40

_SKIP_CHAPTER = 0.55
_SKIP_ENTRY = 0.55
_UNKNOWN_DURATION = 0.35
MAX_MB_RELEASES = 8


@dataclass
class Candidate:
    source: str
    label: str
    entries: list[TracklistEntry]
    trust_bonus: float = 0.0
    release_id: str = ""


@dataclass
class Alignment:
    source: str
    label: str
    confidence: float
    names: dict[int, str]
    matched: int
    total_chapters: int
    entry_count: int
    notes: list[str] = field(default_factory=list)
    release_id: str = ""

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "label": self.label,
            "confidence": round(self.confidence, 3),
            "names": {str(k): v for k, v in self.names.items()},
            "matched": self.matched,
            "total_chapters": self.total_chapters,
            "entry_count": self.entry_count,
            "notes": self.notes,
            "release_id": self.release_id,
        }


@dataclass
class ResolveResult:
    status: str
    best: Optional[Alignment]
    alternatives: list[Alignment]
    hints: list[tuple[str, str, Optional[int]]]
    messages: list[str]

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "best": self.best.to_dict() if self.best else None,
            "alternatives": [a.to_dict() for a in self.alternatives],
            "hints": [{"artist": a, "album": b, "year": y} for a, b, y in self.hints],
            "messages": self.messages,
        }


def clean_title(title: str) -> str:
    t = html.unescape(title or "")
    t = re.sub(r"\s+", " ", t).strip()
    t = t.replace(":", " -").replace("/", "-")
    t = re.sub(r"\s+-\s+-", " -", t)
    return re.sub(r"\s+", " ", t).strip()


def chapter_durations(chapters: list[Chapter], total_seconds: float = 0.0) -> list[Optional[float]]:
    out: list[Optional[float]] = []
    for ch in chapters:
        end = ch.end_seconds
        if end is None and total_seconds > ch.start_seconds:
            end = total_seconds
        out.append((end - ch.start_seconds) if end is not None else None)
    return out


def _pair_cost(chapter_dur: Optional[float], entry: TracklistEntry) -> float:
    if chapter_dur is None or entry.duration_seconds is None:
        return _UNKNOWN_DURATION
    target = entry.duration_seconds
    diff = abs(chapter_dur - target)
    allowed = max(10.0, 0.08 * target)
    excess = max(0.0, diff - allowed)
    return min(excess / max(target, 30.0) * 4.0, 1.0)


def _dp_align(durs: list[Optional[float]], entries: list[TracklistEntry]) -> list[tuple[int, int, float]]:
    """Monotonic alignment; returns [(chapter_pos, entry_pos, cost), ...]."""
    n, m = len(durs), len(entries)
    inf = float("inf")
    dp = [[inf] * (m + 1) for _ in range(n + 1)]
    back: list[list[Optional[tuple[int, int, str, float]]]] = [
        [None] * (m + 1) for _ in range(n + 1)
    ]
    dp[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            cur = dp[i][j]
            if cur == inf:
                continue
            if i < n and j < m:
                cost = _pair_cost(durs[i], entries[j])
                if cur + cost < dp[i + 1][j + 1] - 1e-9:
                    dp[i + 1][j + 1] = cur + cost
                    back[i + 1][j + 1] = (i, j, "m", cost)
            if i < n and cur + _SKIP_CHAPTER < dp[i + 1][j] - 1e-9:
                dp[i + 1][j] = cur + _SKIP_CHAPTER
                back[i + 1][j] = (i, j, "c", 0.0)
            if j < m and cur + _SKIP_ENTRY < dp[i][j + 1] - 1e-9:
                dp[i][j + 1] = cur + _SKIP_ENTRY
                back[i][j + 1] = (i, j, "e", 0.0)

    pairs: list[tuple[int, int, float]] = []
    i, j = n, m
    while (i, j) != (0, 0):
        step = back[i][j]
        if step is None:
            break
        pi, pj, kind, cost = step
        if kind == "m":
            pairs.append((pi, pj, cost))
        i, j = pi, pj
    pairs.reverse()
    return pairs


def align_candidate(
    chapters: list[Chapter],
    durs: list[Optional[float]],
    cand: Candidate,
) -> Alignment:
    n, m = len(chapters), len(cand.entries)
    notes: list[str] = []
    if n == 0 or m == 0:
        return Alignment(cand.source, cand.label, 0.0, {}, 0, n, m, ["Empty tracklist"], cand.release_id)

    timed_entries = sum(1 for entry in cand.entries if entry.duration_seconds is not None)
    has_timing = timed_entries >= max(2, m // 2) and any(duration is not None for duration in durs)

    names: dict[int, str] = {}
    if not has_timing:
        # Count-only sources are useful suggestions but cannot establish that
        # the release edition is correct; the caller must always review them.
        pairs = [(i, i, 0.0) for i in range(min(n, m))]
        for i, j, _ in pairs:
            names[chapters[i].index] = clean_title(cand.entries[j].name)
        if n == m:
            confidence = 0.92
            notes.append("Matched by position only; no usable track durations to verify this edition")
        else:
            confidence = max(0.0, 0.85 - 0.12 * abs(n - m))
            notes.append(f"Tracklist has {m} entries but disc has {n} chapters; matched by position")
        confidence = min(1.0, confidence + cand.trust_bonus)
        return Alignment(cand.source, cand.label, confidence, names, len(pairs), n, m, notes, cand.release_id)

    pairs = _dp_align(durs, cand.entries)
    for i, j, _ in pairs:
        names[chapters[i].index] = clean_title(cand.entries[j].name)
    matched = len(pairs)
    if matched == 0:
        return Alignment(cand.source, cand.label, 0.0, {}, 0, n, m, ["Nothing lined up"], cand.release_id)

    quality = sum(1.0 - min(cost, 1.0) for _, _, cost in pairs) / matched
    coverage = matched / n
    entry_coverage = matched / m
    timed_fraction = sum(
        1 for i, j, _ in pairs
        if durs[i] is not None and cand.entries[j].duration_seconds is not None
    ) / matched
    base = 0.5 * quality + 0.3 * coverage + 0.2 * entry_coverage
    confidence = min(1.0, base * (0.75 + 0.25 * timed_fraction) + cand.trust_bonus)

    if matched < n:
        notes.append(f"{n - matched} chapter(s) had no matching song (intro/applause/bonus?)")
    if matched < m:
        notes.append(f"{m - matched} tracklist entr{'y' if m - matched == 1 else 'ies'} not found on this disc")
    return Alignment(cand.source, cand.label, confidence, names, matched, n, m, notes, cand.release_id)


def read_disc_title(disc_folder: Path) -> str:
    """Best-effort disc title from BDMV/META/DL/*.xml (<di:name>)."""
    meta = disc_folder / "BDMV" / "META" / "DL"
    if not meta.is_dir():
        return ""
    for xml in sorted(meta.glob("bdmt_*.xml")):
        try:
            text = xml.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        match = re.search(r"<(?:\w+:)?name>\s*([^<]+?)\s*</(?:\w+:)?name>", text)
        if match:
            return html.unescape(match.group(1)).strip()
    return ""


def build_hints(
    source_folder: Path,
    parent_artist: str = "",
    override: Optional[tuple[str, str, Optional[int]]] = None,
) -> list[tuple[str, str, Optional[int]]]:
    """Ordered, de-duplicated (artist, album, year) search variants."""
    hints: list[tuple[str, str, Optional[int]]] = []

    def add(artist: str, album: str, year: Optional[int]) -> None:
        item = (artist.strip(), album.strip(), year)
        if item[1] and item not in hints:
            hints.append(item)

    if override and (override[0] or override[1]):
        add(*override)

    artist, album, year = extractor.guess_artist_album_year(source_folder)
    if artist:
        add(artist, album, year)
        add(album, artist, year)
    elif parent_artist:
        add(parent_artist, album, year)
    add(artist, album, year)

    title = read_disc_title(source_folder)
    if title:
        if " - " in title:
            a, _, b = title.partition(" - ")
            add(a, b, year)
        else:
            add(artist or parent_artist, title, year)
    return hints


def local_candidates(
    source_folder: Path,
    chapters: list[Chapter],
    sidecar_folder: Optional[Path] = None,
) -> list[Candidate]:
    candidates: list[Candidate] = []

    embedded = [ch.embedded_name for ch in chapters]
    real = [
        name for chapter, name in zip(chapters, embedded)
        if not extractor.is_generic_chapter_name(name, chapter.index)
    ]
    if chapters and len(real) >= max(2, int(0.8 * len(chapters))):
        entries = [
            TracklistEntry(index=ch.index, name=ch.embedded_name or "")
            for ch in chapters
        ]
        candidates.append(Candidate("embedded", "Names embedded in the disc's chapters", entries, 0.08))

    folders = [source_folder]
    if sidecar_folder is not None and sidecar_folder != source_folder:
        folders.append(sidecar_folder)
    seen: set[Path] = set()
    for folder in folders:
        for sidecar in extractor.find_sidecar_tracklist_files(folder):
            if sidecar in seen:
                continue
            seen.add(sidecar)
            try:
            entries = extractor.load_sidecar_tracklist(sidecar)
            except ValueError:
                continue
            if len(entries) >= 2:
                candidates.append(Candidate("sidecar", f"File: {sidecar.name}", entries, 0.05))
    return candidates


def _lucene_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').strip()


def _mb_search_releases(
    hints: list[tuple[str, str, Optional[int]]],
    log: Callable[[str], None],
) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for artist, album, year in hints:
        for title, search_year in extractor._tracklist_search_attempts(album, year):
            parts = [f'release:"{_lucene_escape(title)}"']
            if artist:
                parts.insert(0, f'artist:"{_lucene_escape(artist)}"')
            if search_year:
                parts.append(f"date:{search_year}")
            query = " AND ".join(parts)
            try:
                data = extractor._musicbrainz_request("release", {"query": query, "limit": "10"})
            except Exception as exc:
                log(f"MusicBrainz search failed: {exc}")
                return found
            releases = data.get("releases", [])
            if releases:
                log(f"MusicBrainz: {len(releases)} release(s) for '{artist or '?'} / {title}'")
                for release in releases:
                    release_id = release.get("id")
                    if release_id and (
                        release_id not in found
                        or release.get("score", 0) > found[release_id].get("score", 0)
                    ):
                        found[release_id] = release
                break
    return found


def _mb_candidates(
    chapters: list[Chapter],
    hints: list[tuple[str, str, Optional[int]]],
    log: Callable[[str], None],
) -> list[Candidate]:
    chapter_count = len(chapters)
    found = _mb_search_releases(hints, log)
    if not found:
        return []

    def closeness(release: dict) -> tuple[int, int]:
        counts = [medium.get("track-count", 0) for medium in (release.get("media") or [])] or [0]
        return min(abs(count - chapter_count) for count in counts), -int(release.get("score", 0))

    chosen = sorted(found.values(), key=closeness)[:MAX_MB_RELEASES]
    candidates: list[Candidate] = []
    for release in chosen:
        release_id = release["id"]
        try:
            data = extractor._musicbrainz_request(f"release/{release_id}", {"inc": "recordings"})
        except Exception as exc:
            log(f"MusicBrainz fetch failed for {release_id}: {exc}")
            continue
        credit = (release.get("artist-credit") or [{}])[0].get("name", "")
        for medium_index, medium in enumerate(data.get("media") or [], start=1):
            entries: list[TracklistEntry] = []
            for track in medium.get("tracks") or []:
                title = (track.get("title") or "").strip()
                if not title:
                    continue
                length = track.get("length") or (track.get("recording") or {}).get("length")
                entries.append(
                    TracklistEntry(
                        index=len(entries) + 1,
                        name=title,
                        duration_seconds=(length / 1000.0) if length else None,
                    )
                )
            if len(entries) >= 2:
                fmt = medium.get("format") or "medium"
                label = (
                    f"MusicBrainz: {credit} - {release.get('title', '')} "
                    f"({release.get('date', '?')}) - {fmt} {medium_index}, {len(entries)} tracks"
                )
                candidates.append(Candidate("musicbrainz", label, entries, 0.0, release_id))
    return candidates


def _discogs_candidates(
    chapters: list[Chapter],
    hints: list[tuple[str, str, Optional[int]]],
    token: str,
    log: Callable[[str], None],
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for artist, album, year in hints:
        if not artist:
            continue
        try:
            found = extractor.search_discogs_releases(
                artist, album, year, token, expected_track_count=len(chapters), limit=6
            )
        except Exception as exc:
            log(f"Discogs search failed: {exc}")
            return candidates
        if found:
            log(f"Discogs: {len(found)} release(s) for '{artist} / {album}'")
            for candidate in found:
                candidates.append(
                    Candidate(
                        "discogs",
                        f"Discogs: {candidate.artist} - {candidate.title} "
                        f"({candidate.date or '?'}) - {candidate.format_hint}, {candidate.track_count} tracks",
                        candidate.entries,
                    )
                )
            break
    return candidates


def resolve_titles(
    source_folder: Path,
    chapters: list[Chapter],
    total_seconds: float = 0.0,
    discogs_token: str = "",
    parent_artist: str = "",
    override: Optional[tuple[str, str, Optional[int]]] = None,
    offline: bool = False,
    log: Optional[Callable[[str], None]] = None,
    sidecar_folder: Optional[Path] = None,
) -> ResolveResult:
    messages: list[str] = []

    def say(message: str) -> None:
        messages.append(message)
        if log:
            log(message)

    durations = chapter_durations(chapters, total_seconds)
    hints = build_hints(source_folder, parent_artist, override)
    alignments: list[Alignment] = []

    def score(candidates: list[Candidate]) -> None:
        for candidate in candidates:
            alignments.append(align_candidate(chapters, durations, candidate))
        alignments.sort(key=lambda alignment: alignment.confidence, reverse=True)

    def duration_verified_best() -> bool:
        """Only stop online fallback when a high-scoring match used real durations."""
        if not alignments or alignments[0].confidence < AUTO_ACCEPT:
            return False
        best = alignments[0]
        return best.source in ("musicbrainz", "discogs") and "no usable track durations" not in " ".join(best.notes).lower()

    local = local_candidates(source_folder, chapters, sidecar_folder)
    if local:
        say(f"Found {len(local)} local tracklist source(s)")
    score(local)

    if not duration_verified_best() and not offline and hints:
        say("Searching MusicBrainz...")
        score(_mb_candidates(chapters, hints, say))
        if not duration_verified_best() and discogs_token.strip():
            say("Searching Discogs...")
            score(_discogs_candidates(chapters, hints, discogs_token, say))
    elif not hints and not local:
        say("Couldn't work out an artist/album from the folder name; enter them to search")

    useful = [alignment for alignment in alignments if alignment.confidence >= MIN_USEFUL]
    if not useful:
        say("No confident match found")
        return ResolveResult("none", None, alignments[:5], hints, messages)

    best = useful[0]
    say(
        f"Best suggestion ({int(best.confidence * 100)}% scoring confidence): {best.label}. "
        "Review the chapter assignments before splitting."
    )
    # Always require an explicit user click to start the extraction.
    return ResolveResult("review", best, useful[:5], hints, messages)
