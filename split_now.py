r"""
split_now.py

CLI tool: splits an already-extracted Atmos MKV (the intermediate file
the GUI app normally deletes after a successful run) into named track
files, without going through the GUI. Uses the same extractor.py logic
the app itself uses, so behaviour matches exactly.

Track names come from a text file, one name per line, in chapter order.
Leading numbering like "1.", "01 -", "1)" is stripped automatically, so
you can paste a tracklist straight from wherever you found it.

Usage:
    python split_now.py ATMOS_MKV OUTPUT_FOLDER --names-file tracks.txt

Example:
    python split_now.py "E:\Music Videos\Album\_work\_atmos_extracted.mkv" ^
        "E:\Music Videos\Album" --names-file tracks.txt

If you just want to see the chapters found in a file without splitting
anything yet, pass --list-only and skip --names-file.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import extractor


def _strip_leading_number(line: str) -> str:
    return re.sub(r"^\s*\d+[\.\)\-]?\s*", "", line).strip()


def read_track_names(names_file: Path) -> dict[int, str]:
    lines = names_file.read_text(encoding="utf-8").splitlines()
    names: dict[int, str] = {}
    chapter_index = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        chapter_index += 1
        names[chapter_index] = _strip_leading_number(line)
    return names


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Split an already-extracted Atmos MKV into named track files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "atmos_mkv",
        type=Path,
        help="Path to the extracted Atmos MKV (video + Atmos audio, with chapters).",
    )
    parser.add_argument(
        "output_folder",
        type=Path,
        nargs="?",
        help="Folder to write the split track files into. Required unless --list-only.",
    )
    parser.add_argument(
        "--names-file",
        type=Path,
        help="Text file with one track name per line, in chapter order. "
        "Leading numbering (e.g. '1.', '01 -') is stripped automatically.",
    )
    parser.add_argument(
        "--container",
        default="mkv",
        help="Output container extension (default: mkv).",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Just print the chapters found in the file and exit, without splitting.",
    )

    args = parser.parse_args(argv)

    if not args.list_only:
        if args.output_folder is None:
            parser.error("output_folder is required unless --list-only is given")
        if args.names_file is None:
            parser.error("--names-file is required unless --list-only is given")

    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    missing = [name for name, ok in extractor.check_tools().items() if not ok]
    if missing:
        print("Warning: the following required tools were not found on PATH:", file=sys.stderr)
        for name in missing:
            url = extractor.TOOL_DOWNLOAD_URLS.get(name, "")
            print(f"  - {name}  ({url})", file=sys.stderr)
        print("Continuing anyway - this will fail if they're actually needed.\n", file=sys.stderr)

    if not args.atmos_mkv.is_file():
        print(f"Error: {args.atmos_mkv} does not exist.", file=sys.stderr)
        return 1

    print(f"Reading chapters from {args.atmos_mkv.name} ...")
    chapters = extractor.read_chapters(args.atmos_mkv)
    print(f"Found {len(chapters)} chapters.")

    if args.list_only:
        for ch in chapters:
            print(f"  Chapter {ch.index}: starts at {ch.start_seconds:.1f}s")
        return 0

    track_names = read_track_names(args.names_file)
    if len(track_names) != len(chapters):
        print(
            f"Warning: {args.names_file.name} has {len(track_names)} name(s) "
            f"but the file has {len(chapters)} chapter(s). Unmatched chapters "
            f"will use a default 'Track NN' name.",
            file=sys.stderr,
        )

    for ch in chapters:
        if ch.index in track_names:
            ch.name = track_names[ch.index]

    print(f"Splitting into {args.output_folder} ...")
    results = extractor.split_chapters(
        args.atmos_mkv,
        chapters,
        args.output_folder,
        container=args.container,
        progress_cb=print,
    )

    print(f"\nDone. Wrote {len(results)} files:")
    for p in results:
        print(f"  {p}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
