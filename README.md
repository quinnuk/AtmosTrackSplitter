<div align="center">

# 🎧 Atmos Track Splitter

**Split a ripped Blu-ray concert/music disc into individual, chapter-named Dolby Atmos song files — without ever re-encoding the audio.**

![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)
![Python](https://img.shields.io/badge/python-3.10%2B-3776AB)
![License](https://img.shields.io/badge/license-MIT-green)

[![Atmos Track Splitter Screenshot](https://github.com/quinnuk/AtmosTrackSplitter/raw/main/screenshot.png)](https://github.com/quinnuk/AtmosTrackSplitter/blob/main/screenshot.png)

<a href="https://buymeacoffee.com/quinnuk" target="_blank">
  <img src="https://cdn.buymeacoffee.com/buttons/default-orange.png" alt="Buy Me A Coffee" height="60" width="217">
</a>

<sub>If Atmos Track Splitter saves you time, a coffee is always appreciated ☕</sub>

</div>

---

## Is this for you?

You've ripped a Blu-ray Audio disc — a concert film, a live album, or one of the growing number of **Dolby Atmos remaster/reissue Blu-rays** (SDE's Surround Series, immersive audio reissues, etc.) — and you want it in your **Plex / Jellyfin / Kodi** library as individual, correctly-named song files instead of one giant feature-length file. If that's you, this tool exists for exactly that job, and nothing else.

It is **not** a ripping tool, a re-encoder, or a general-purpose media converter — see [Notes & known limitations](#notes--known-limitations) for what it deliberately doesn't do.

## Table of Contents

- [Why this exists](#why-this-exists)
- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Usage](#usage)
- [Building a standalone .exe](#building-a-standalone-exe)
- [Project layout](#project-layout)
- [Recovering from an interrupted run](#recovering-from-an-interrupted-run)
- [Notes & known limitations](#notes--known-limitations)
- [Roadmap](#roadmap)
- [Reporting Issues](#reporting-issues)
- [Support This Project](#support-this-project)
- [License](#license)

## Why this exists

Concert Blu-rays and Atmos reissue discs are usually one giant file with chapter markers per song. Most tools either:
- flatten/transcode the audio (losing the Atmos object metadata), or
- give you the whole disc as one file with no easy way to split it, or
- require you to hand-build `mkvmerge`/`ffmpeg` commands per chapter.

Atmos Track Splitter automates the whole thing: point it at the ripped disc folder, let it find the right Atmos playlist, name the tracks (typed, pasted, imported, or looked up), and get one clean, bit-exact file per song.

## Features

- 🔍 **Auto-detects the right playlist** — scans every `.mpls` playlist and scores each one (Atmos track present, chapter count, video track, duration) rather than just grabbing the first Atmos match, and explains its reasoning so you can sanity-check or override the pick. Near-identical playlists (alternate angles, region variants) are automatically flagged as likely duplicates instead of shown as separate top candidates.
- 📝 **Four ways to name tracks** — paste a plain list and click Fill; import a tracklist file (`.txt`, `.nfo`, `.cue`, or a saved `.tracklist.json`); pull in whatever chapter names are already embedded in the disc; or look the album up on **MusicBrainz** and match its tracklist to your chapters automatically. Every method shows a review screen before anything is applied — matched chapters pre-checked, mismatches flagged, nothing silently overwritten.
- 📂 **Finds sidecar tracklists for you** — if a `.txt`/`.nfo`/`.cue` tracklist is already sitting in the disc rip folder, the app tells you so you don't have to go looking.
- ⏸️ **Resumable, cancel-safe jobs** — extraction progress is checkpointed to a manifest, so a crash, a cancel, or a closed app mid-job doesn't mean starting over; relaunching against the same output folder offers to resume exactly where it left off.
- 📋 **Per-job extraction log** — every run writes an `extraction.log` you can open with one click, showing exactly what each external tool was told to do — the single most useful thing to attach to a bug report.
- ⚠️ **Checks for MKVToolNix/ffmpeg on startup** — if either is missing, you get a dialog with direct download links and a Browse button that verifies and remembers a custom path, instead of a confusing failure mid-extraction.
- ✂️ **No re-encoding** — stream-copies video + Atmos audio only; the object-based Atmos mix stays bit-exact.
- 📺 **Keeps the video track** — so playback on a TV shows the concert, not a black screen.
- 📁 **Clean output, never overwritten silently** — a sensibly-named album folder, one file per song, and a confirmation before any existing output file is replaced.
- 💾 **Remembers your settings** — last-used folders and tool paths persist between runs.
- 🪟 **Runs standalone** — build a windowed `.exe` with no attached console, so closing the terminal (or just double-clicking it) never kills a mid-extraction job.
- ❓ **Built-in Help menu** — tips and troubleshooting live in the app itself (Help → Tips & Troubleshooting), alongside quick links to this README and the issue tracker.

## How it works

1. Point the app at a folder containing a ripped disc — it must have the standard, unmodified `BDMV/PLAYLIST/*.mpls` structure (not a flattened single MKV).
2. It inspects every playlist via `mkvmerge -i` and scores each as a candidate for the Atmos feature, pre-selecting the best match and showing its reasoning.
3. You name each chapter — paste, import a file, pull in embedded names, or look it up on MusicBrainz — and review the proposed matches before accepting them.
4. It extracts the video track plus just the Atmos audio (stream-copy, no transcoding) via `mkvmerge`, dropping the other audio tracks (AC-3, DTS-HD, etc). It then reads the exact chapter timestamps and splits the result into one file per song with `ffmpeg`, checkpointing progress as it goes.

## Requirements

**External tools** (must be installed and either on your `PATH`, or their paths set via the app's missing-tools dialog / `settings.py`):

| Tool | Provides | Link |
|---|---|---|
| MKVToolNix | `mkvmerge`, `mkvextract` | https://mkvtoolnix.download/ |
| ffmpeg | `ffmpeg`, `ffprobe` | https://ffmpeg.org/ |

**Python packages:**

```bash
pip install -r requirements.txt
```

## Usage

```bash
python main.py
```

> **Note:** if you launch it from PowerShell/cmd, the app is a child process of that terminal — closing the terminal window kills the app too, even mid-extraction. Run it with `pythonw main.py` (fully detached, no console) instead, or build the standalone `.exe` below.

## Building a standalone .exe

```bash
pip install pyinstaller
build_exe.bat
```

This produces `AtmosTrackSplitter.exe` — a windowed exe with no attached console, so it survives closing the terminal it was launched from (or just double-click it directly).

> The exe bundles the Python app itself, but **not** `mkvmerge`, `mkvextract`, `ffmpeg`, or `ffprobe` — those still need to be installed separately and reachable on `PATH`.

## Project layout

```
AtmosTrackSplitter/
├── main.py                    GUI (customtkinter): scanning, naming, extraction, Help menu
├── extractor.py                Core logic: scanning, scoring, chapters, tracklist matching,
│                                MusicBrainz lookup, splitting, resume/manifest handling
├── settings.py                 Persisted settings (tool paths, last-used folders)
├── split_now.py                 CLI: split an already-extracted Atmos MKV by hand
├── AtmosTrackSplitter.spec     PyInstaller build spec
├── build_exe.bat               One-click .exe build script
└── requirements.txt
```

`extractor.py` has no GUI dependencies, so it can be imported and used on its own (e.g. from a script or a future CLI).

## Recovering from an interrupted run

If the app is closed, crashes, or a job is cancelled partway through, just point it at the same source and output folders again — it'll detect the in-progress manifest and offer to **resume** from wherever it left off, skipping whatever was already completed.

If you'd rather handle it by hand (or the GUI resume path doesn't apply — e.g. you deleted the manifest, or extraction succeeded but you want to split into different names), the intermediate `_atmos_extracted.mkv` is left in the work folder rather than being cleaned up automatically on failure. Split it directly:

```bash
python split_now.py "path\to\_atmos_extracted.mkv" "path\to\output folder" --names-file tracks.txt
```

`tracks.txt` is just one track name per line, in chapter order — the same tracklist you'd paste into the GUI. Leading numbering (`1.`, `01 -`) is stripped automatically. Add `--list-only` (and skip `--names-file`) to just see how many chapters a file has before committing to names.

## Notes & known limitations

- Assumes chapters map 1:1 to songs. This holds for most concert/live-album Blu-rays, but always sanity-check the chapter count against the actual tracklist before running the paste-to-fill step.
- If a disc has no Atmos track, or the Atmos track isn't chaptered per song, this tool won't help — it depends on both being true.
- **MusicBrainz coverage is best for mainstream releases.** Small-run audiophile Blu-ray Pure Audio / Surround Series discs and other boutique/mail-order-only editions are often missing from MusicBrainz entirely, or only indexed under a different parent release with a different track count. If the number of tracks MusicBrainz returns looks nothing like your chapter count, that's almost always why — use Import Tracklist with a sidecar file instead.
- Output format defaults to `.mkv` (TrueHD Atmos in an MKV container) to preserve the object-based Atmos metadata — converting to a plain audio container like FLAC would discard it.
- Folder-in, folder-out only — no CLI or disc-drive/ripping support. This tool works on discs you've **already ripped yourself**; it doesn't rip or decrypt anything.

## Roadmap

- [ ] Folder-watch mode (auto-detect new rips dropped into a monitored folder and prompt for naming)
- [ ] Full settings dialog for tool paths (currently only settable via the startup missing-tools dialog or by hand-editing `settings.json`)
- [x] ~~MusicBrainz lookup as a fallback for track names~~ — shipped

## Reporting Issues

Found a bug or have a suggestion? Please open an issue rather than a Reddit comment, so it doesn't get lost:

**[Open an issue](https://github.com/quinnuk/AtmosTrackSplitter/issues/new/choose)**

The most useful things to include: the `mkvmerge -i` output for the affected playlist, and the `extraction.log` from the job (Open Log button in the app, or the file directly in your output folder) — between the two, that's normally enough to track down what's actually going wrong.

## Support This Project

Atmos Track Splitter is free and built in my spare time — if it's useful to you, consider buying me a coffee.

<p align="center">
  <a href="https://buymeacoffee.com/quinnuk">
    <img src="https://cdn.buymeacoffee.com/buttons/default-orange.png" alt="Buy Me A Coffee">
  </a>
</p>

## License

Released under the [MIT License](LICENSE). You are free to use, modify, and share this software.
