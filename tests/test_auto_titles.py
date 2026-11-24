from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import auto_titles as at
from extractor import Chapter, TracklistEntry


def make_chapters(durations):
    chapters, start = [], 0.0
    for index, duration in enumerate(durations, start=1):
        chapters.append(Chapter(index=index, start_seconds=start, end_seconds=start + duration))
        start += duration
    return chapters


def entries(durations, prefix="Song"):
    return [
        TracklistEntry(index=index, name=f"{prefix} {index}", duration_seconds=duration)
        for index, duration in enumerate(durations, start=1)
    ]


def run(chapter_durations, track_durations):
    chapters = make_chapters(chapter_durations)
    candidate = at.Candidate("musicbrainz", "test", entries(track_durations))
    return at.align_candidate(chapters, at.chapter_durations(chapters), candidate)


def test_perfect_duration_match_scores_high():
    durations = [240, 200, 310, 180, 260]
    alignment = run(durations, durations)
    assert alignment.confidence >= at.AUTO_ACCEPT
    assert alignment.matched == 5


def test_applause_padding_still_scores_high():
    tracks = [240, 200, 310, 180, 260]
    chapters = [duration + 15 for duration in tracks]
    assert run(chapters, tracks).confidence >= at.AUTO_ACCEPT


def test_extra_intro_chapter_is_skipped_not_shifted():
    tracks = [240, 200, 310, 180, 260]
    chapters = [45] + tracks
    alignment = run(chapters, tracks)
    assert 1 not in alignment.names
    assert alignment.names[2] == "Song 1"
    assert alignment.names[6] == "Song 5"


def test_encore_missing_from_tracklist():
    tracks = [240, 200, 310, 180]
    alignment = run(tracks + [275], tracks)
    assert alignment.names[4] == "Song 4"
    assert 5 not in alignment.names


def test_wrong_edition_scores_low():
    chapters = [240, 200, 310, 180, 260]
    tracks = [95, 410, 130, 520, 75, 300, 220]
    assert run(chapters, tracks).confidence < at.AUTO_ACCEPT


def test_count_only_equal_counts_are_flagged_unverified():
    chapters = make_chapters([100] * 4)
    candidate = at.Candidate(
        "sidecar",
        "plain text file",
        [TracklistEntry(name=f"Track {i}") for i in range(4)],
        0.05,
    )
    alignment = at.align_candidate(chapters, at.chapter_durations(chapters), candidate)
    assert alignment.confidence >= at.AUTO_ACCEPT
    assert "no usable track durations" in alignment.notes[0].lower()


def test_title_cleanup():
    assert at.clean_title("Rock &amp; Roll:  Part 1/2") == "Rock & Roll - Part 1-2"


def test_resolver_always_requires_manual_review():
    chapters = make_chapters([100, 120, 140])
    candidate = at.Candidate(
        "sidecar",
        "test list",
        [TracklistEntry(name=f"Track {i}") for i in range(3)],
        0.0,
    )
    with tempfile.TemporaryDirectory() as temp, \
         patch.object(at, "local_candidates", return_value=[candidate]), \
         patch.object(at, "_mb_candidates", return_value=[]):
        result = at.resolve_titles(Path(temp), chapters, offline=True)
    assert result.status == "review"
    assert result.best is not None
    assert "Review the chapter assignments" in result.messages[-1]
