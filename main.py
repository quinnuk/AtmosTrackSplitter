"""
main.py

Atmos Track Splitter - GUI entry point.

Workflow:
    1. Pick (or watch) a folder containing a ripped Blu-ray disc structure
       (BDMV/PLAYLIST/*.mpls).
    2. Scan playlists, auto-select the one with a Dolby Atmos track.
    3. Enter/paste song names for each chapter.
    4. Extract Atmos audio + split into individually named files.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import tkinter as tk
import tkinter.filedialog as filedialog
import tkinter.messagebox as messagebox
import webbrowser
from pathlib import Path

import customtkinter as ctk

import extractor
import settings

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


def enable_clipboard(widget: ctk.CTkEntry | ctk.CTkTextbox) -> None:
    """
    customtkinter's Entry/Textbox widgets don't reliably inherit the OS's
    default copy/cut/paste keyboard or right-click behaviour on every
    platform/version. This adds both explicitly so Ctrl+V and right-click
    -> Paste always work.
    """
    # The actual tkinter widget underneath is .entry for CTkEntry-like
    # widgets, or the CTkTextbox itself acts as a Text widget directly.
    target = getattr(widget, "_entry", None) or getattr(widget, "_textbox", None) or widget

    def paste(_event=None) -> str:
        try:
            clipboard_text = widget.clipboard_get()
        except tk.TclError:
            return "break"
        try:
            if isinstance(widget, ctk.CTkTextbox):
                widget.insert("insert", clipboard_text)
            else:
                widget.insert("insert", clipboard_text)
        except Exception:
            pass
        return "break"

    def copy(_event=None) -> str:
        try:
            if isinstance(widget, ctk.CTkTextbox):
                selected = widget.get("sel.first", "sel.last")
            else:
                selected = widget.get()
            widget.clipboard_clear()
            widget.clipboard_append(selected)
        except Exception:
            pass
        return "break"

    def cut(_event=None) -> str:
        copy(_event)
        try:
            if isinstance(widget, ctk.CTkTextbox):
                widget.delete("sel.first", "sel.last")
            else:
                widget.delete(0, "end")
        except Exception:
            pass
        return "break"

    def select_all(_event=None) -> str:
        try:
            if isinstance(widget, ctk.CTkTextbox):
                widget.tag_add("sel", "1.0", "end")
            else:
                widget.select_range(0, "end")
        except Exception:
            pass
        return "break"

    for seq in ("<Control-v>", "<Control-V>"):
        widget.bind(seq, paste)
    for seq in ("<Control-c>", "<Control-C>"):
        widget.bind(seq, copy)
    for seq in ("<Control-x>", "<Control-X>"):
        widget.bind(seq, cut)
    for seq in ("<Control-a>", "<Control-A>"):
        widget.bind(seq, select_all)

    menu = tk.Menu(widget, tearoff=0)
    menu.add_command(label="Cut", command=cut)
    menu.add_command(label="Copy", command=copy)
    menu.add_command(label="Paste", command=paste)
    menu.add_separator()
    menu.add_command(label="Select All", command=select_all)

    def show_menu(event) -> None:
        try:
            widget.focus_set()
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    widget.bind("<Button-3>", show_menu)


class AtmosTrackSplitterApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        self.title("Atmos Track Splitter")
        self.geometry("820x640")
        self.minsize(700, 560)

        self.cfg = settings.load()
        self.playlists: list[extractor.Playlist] = []
        self.playlist_scores: list[extractor.PlaylistScore] = []
        self.selected_playlist: extractor.Playlist | None = None
        self.selected_playlist_score: extractor.PlaylistScore | None = None
        self.selected_playlist_chapters: list[extractor.Chapter] = []
        self.disc_folder: Path | None = None
        self.chapter_name_vars: dict[int, ctk.StringVar] = {}
        self.chapter_source_labels: dict[int, ctk.CTkLabel] = {}
        self.cancel_event: threading.Event | None = None
        self.current_log_path: Path | None = None

        self._apply_tool_paths()
        self._build_layout()
        self.after(200, self._check_tools_on_startup)

    def _apply_tool_paths(self) -> None:
        """
        Push any custom tool paths from settings.json into extractor.py.
        Without this, a custom mkvmerge_path/ffmpeg_path etc set in settings
        would be silently ignored and the bare command name used instead.
        """
        extractor.set_tool_path("mkvmerge", self.cfg.get("mkvmerge_path", "mkvmerge"))
        extractor.set_tool_path("mkvextract", self.cfg.get("mkvextract_path", "mkvextract"))
        extractor.set_tool_path("ffmpeg", self.cfg.get("ffmpeg_path", "ffmpeg"))
        extractor.set_tool_path("ffprobe", self.cfg.get("ffprobe_path", "ffprobe"))

    def _check_tools_on_startup(self) -> None:
        found = extractor.check_tools()
        missing = [name for name, ok in found.items() if not ok]
        if missing:
            self._show_missing_tools_dialog(missing)

    def _show_missing_tools_dialog(self, missing: list[str]) -> None:
        remaining = set(missing)

        dialog = ctk.CTkToplevel(self)
        dialog.title("Missing required tools")
        dialog.geometry("520x360")
        dialog.transient(self)
        dialog.grab_set()

        heading = ctk.CTkLabel(
            dialog,
            text="These required tools weren't found on your PATH:",
            font=ctk.CTkFont(weight="bold"),
            wraplength=480,
            justify="left",
        )
        heading.pack(padx=16, pady=(16, 8), anchor="w")

        rows_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        rows_frame.pack(fill="x", padx=16)

        row_widgets: dict[str, dict] = {}

        def mark_resolved(name: str, version_text: str) -> None:
            remaining.discard(name)
            widgets = row_widgets[name]
            widgets["status"].configure(text=f"OK ({version_text})", text_color="#4a4")
            widgets["browse_btn"].configure(state="disabled")
            widgets["download_btn"].configure(state="disabled")
            if not remaining:
                heading.configure(text="All required tools are now available.")

        def browse_for(name: str) -> None:
            path_str = filedialog.askopenfilename(
                title=f"Locate {name}",
                filetypes=[
                    ("Executable", "*.exe"),
                    ("All files", "*.*"),
                ],
            )
            if not path_str:
                return

            ok, message = extractor.verify_tool_at_path(path_str, name)
            if not ok:
                messagebox.showerror(
                    "Not a working tool",
                    f"This doesn't look like a working {name}:\n\n{message}",
                    parent=dialog,
                )
                return

            extractor.set_tool_path(name, path_str)
            settings.update(**{f"{name}_path": path_str})
            mark_resolved(name, message)
            self.set_status(f"Using {name} at {path_str}")

            # ffmpeg/ffprobe and mkvmerge/mkvextract always ship together in
            # the same folder - if the other half of this pair is also
            # still missing, check right there before asking the user to
            # browse a second time for what's really one install.
            sibling_name = extractor.SIBLING_TOOL_NAMES.get(name)
            sibling_path = extractor.guess_sibling_tool_path(path_str, name)
            if sibling_path and sibling_name and sibling_name in remaining:
                sib_ok, sib_message = extractor.verify_tool_at_path(sibling_path, sibling_name)
                if sib_ok:
                    extractor.set_tool_path(sibling_name, sibling_path)
                    settings.update(**{f"{sibling_name}_path": sibling_path})
                    mark_resolved(sibling_name, sib_message)
                    self.set_status(f"Also found {sibling_name} alongside it at {sibling_path}")

        for i, name in enumerate(missing):
            row = ctk.CTkFrame(rows_frame, fg_color="transparent")
            row.pack(fill="x", pady=4)

            ctk.CTkLabel(row, text=f"•  {name}", width=110, anchor="w").pack(side="left")

            status_label = ctk.CTkLabel(row, text="not found", width=110, anchor="w", text_color="gray60")
            status_label.pack(side="left")

            url = extractor.TOOL_DOWNLOAD_URLS.get(name, "")
            download_btn = ctk.CTkButton(
                row, text="Download", width=90, command=lambda u=url: webbrowser.open(u)
            )
            download_btn.pack(side="left", padx=(4, 4))

            browse_btn = ctk.CTkButton(
                row, text="Browse...", width=90, command=lambda n=name: browse_for(n)
            )
            browse_btn.pack(side="left")

            row_widgets[name] = {
                "status": status_label, "browse_btn": browse_btn, "download_btn": download_btn,
            }

        ctk.CTkLabel(
            dialog,
            text=(
                "Already have these installed? Click Browse and point at the "
                "actual .exe - it's checked and saved automatically, no need "
                "to edit settings.json by hand. Otherwise, install and make "
                "sure they're on your system PATH, then restart the app."
            ),
            wraplength=480,
            justify="left",
        ).pack(padx=16, pady=(12, 16), anchor="w")

        ctk.CTkButton(dialog, text="Continue anyway", command=dialog.destroy).pack(
            pady=(0, 16)
        )

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # --- Source folder row ---
        source_frame = ctk.CTkFrame(self)
        source_frame.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
        source_frame.grid_columnconfigure(0, weight=1)

        self.source_entry = ctk.CTkEntry(
            source_frame, placeholder_text="Path to ripped Blu-ray folder..."
        )
        self.source_entry.grid(row=0, column=0, sticky="ew", padx=(8, 8), pady=8)
        if self.cfg.get("last_source_folder"):
            self.source_entry.insert(0, self.cfg["last_source_folder"])
        enable_clipboard(self.source_entry)

        ctk.CTkButton(source_frame, text="Browse...", width=100, command=self.browse_source).grid(
            row=0, column=1, padx=(0, 8), pady=8
        )
        ctk.CTkButton(source_frame, text="Scan", width=100, command=self.scan_folder).grid(
            row=0, column=2, padx=(0, 8), pady=8
        )

        # --- Playlist selection row ---
        playlist_frame = ctk.CTkFrame(self)
        playlist_frame.grid(row=1, column=0, sticky="ew", padx=16, pady=8)
        playlist_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(playlist_frame, text="Playlist:").grid(row=0, column=0, padx=8, pady=8)
        self.playlist_option = ctk.CTkOptionMenu(
            playlist_frame, values=["(scan a folder first)"], command=self.on_playlist_selected
        )
        self.playlist_option.grid(row=0, column=1, sticky="ew", padx=8, pady=8)

        self.playlist_info_label = ctk.CTkLabel(playlist_frame, text="", justify="left", wraplength=740)
        self.playlist_info_label.grid(row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 8))

        # --- Paste tracklist row ---
        paste_frame = ctk.CTkFrame(self)
        paste_frame.grid(row=2, column=0, sticky="ew", padx=16, pady=8)
        paste_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            paste_frame,
            text="Paste tracklist (one song per line, in order) then click Fill:",
        ).grid(row=0, column=0, sticky="w", padx=8, pady=(8, 0))

        paste_row = ctk.CTkFrame(paste_frame, fg_color="transparent")
        paste_row.grid(row=1, column=0, sticky="ew", padx=8, pady=(4, 8))
        paste_row.grid_columnconfigure(0, weight=1)

        self.paste_textbox = ctk.CTkTextbox(paste_row, height=70)
        self.paste_textbox.grid(row=0, column=0, sticky="ew")
        enable_clipboard(self.paste_textbox)

        ctk.CTkButton(paste_row, text="Fill", width=80, command=self.fill_from_paste).grid(
            row=0, column=1, padx=(8, 0)
        )
        ctk.CTkButton(
            paste_row, text="Import Tracklist...", width=150, command=self.import_tracklist
        ).grid(row=0, column=2, padx=(8, 0))
        ctk.CTkButton(
            paste_row, text="Search Online...", width=140, command=self.search_online_tracklist
        ).grid(row=0, column=3, padx=(8, 0))

        # --- Chapter/track name table (scrollable) ---
        self.chapter_scroll = ctk.CTkScrollableFrame(self, label_text="Chapters / Track Names")
        self.chapter_scroll.grid(row=3, column=0, sticky="nsew", padx=16, pady=8)
        self.chapter_scroll.grid_columnconfigure(1, weight=1)
        # --- Output folder + run row ---
        bottom_frame = ctk.CTkFrame(self)
        bottom_frame.grid(row=4, column=0, sticky="ew", padx=16, pady=(8, 16))
        bottom_frame.grid_columnconfigure(0, weight=1)

        self.output_entry = ctk.CTkEntry(
            bottom_frame,
            placeholder_text="Music library folder (an album subfolder is created automatically)...",
        )
        self.output_entry.grid(row=0, column=0, sticky="ew", padx=(8, 8), pady=8)
        if self.cfg.get("last_output_folder"):
            self.output_entry.insert(0, self.cfg["last_output_folder"])
        enable_clipboard(self.output_entry)

        ctk.CTkButton(bottom_frame, text="Browse...", width=100, command=self.browse_output).grid(
            row=0, column=1, padx=(0, 8), pady=8
        )
        self.extract_button = ctk.CTkButton(
            bottom_frame, text="Extract && Split", width=140, command=self.start_extraction
        )
        self.extract_button.grid(row=0, column=2, padx=(0, 8), pady=8)

        self.cancel_button = ctk.CTkButton(
            bottom_frame, text="Cancel", width=90, fg_color="#a33", hover_color="#822",
            command=self.cancel_extraction, state="disabled",
        )
        self.cancel_button.grid(row=0, column=3, padx=(0, 8), pady=8)

        self.open_log_button = ctk.CTkButton(
            bottom_frame, text="Open Log", width=100, command=self.open_log, state="disabled",
        )
        self.open_log_button.grid(row=0, column=4, padx=(0, 8), pady=8)

        self.status_label = ctk.CTkLabel(self, text="Ready.", anchor="w")
        self.status_label.grid(row=5, column=0, sticky="ew", padx=16, pady=(0, 12))

    # ------------------------------------------------------------------
    # Source folder / scanning
    # ------------------------------------------------------------------

    def browse_source(self) -> None:
        folder = filedialog.askdirectory(title="Select ripped Blu-ray disc folder")
        if folder:
            self.source_entry.delete(0, "end")
            self.source_entry.insert(0, folder)

    def scan_folder(self) -> None:
        folder_str = self.source_entry.get().strip()
        if not folder_str:
            messagebox.showwarning("No folder", "Pick a folder first.")
            return

        folder = Path(folder_str)
        if not (folder / "BDMV" / "PLAYLIST").is_dir():
            messagebox.showerror(
                "Not a disc folder", "No BDMV/PLAYLIST found in that folder."
            )
            return

        self.disc_folder = folder
        self.set_status(f"Scanning playlists in {folder.name}...")
        settings.update(last_source_folder=str(folder))

        def work() -> None:
            try:
                playlists = extractor.scan_disc_folder(folder)
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: self._on_scan_failed(exc))
                return
            self.after(0, lambda: self._on_scan_complete(playlists))

        threading.Thread(target=work, daemon=True).start()

    def _on_scan_failed(self, exc: Exception) -> None:
        self.set_status("Scan failed - see error dialog.")
        messagebox.showerror(
            "Scan failed",
            f"{exc}\n\nCheck that mkvmerge is installed and on PATH "
            "(or its path is set correctly in settings).",
        )

    def _on_scan_complete(self, playlists: list[extractor.Playlist]) -> None:
        self.playlists = playlists
        if not playlists:
            self.set_status("No playlists found.")
            return

        # Score every playlist as a candidate instead of silently picking
        # whichever Atmos playlist has the most chapters - the dropdown
        # is sorted best-first and the reasons behind each score are
        # shown below it so the choice can actually be reviewed, not just
        # accepted on faith.
        self.playlist_scores = extractor.score_playlists(playlists)
        self.playlists = [s.playlist for s in self.playlist_scores]

        labels = [self._playlist_label(s) for s in self.playlist_scores]
        self.playlist_option.configure(values=labels)
        self.playlist_option.set(labels[0])
        self.on_playlist_selected(labels[0])

        top = self.playlist_scores[0]
        if top.playlist.has_atmos:
            status = f"Best candidate: {top.playlist.path.name} (score {top.score:.0f}) - review below."
        else:
            status = "No Atmos track found in any playlist."

        if self.disc_folder is not None:
            sidecar_files = extractor.find_sidecar_tracklist_files(self.disc_folder)
            if sidecar_files:
                names = ", ".join(f.name for f in sidecar_files[:3])
                if len(sidecar_files) > 3:
                    names += f", +{len(sidecar_files) - 3} more"
                status += f" Possible tracklist file(s) found in the disc folder: {names} - use Import Tracklist to review."

        self.set_status(status)

    @staticmethod
    def _playlist_label(score: extractor.PlaylistScore) -> str:
        tag = " [possible duplicate]" if score.duplicate_of else ""
        return (
            f"{score.playlist.path.name}  -  score {score.score:.0f}  "
            f"(chapters: {score.playlist.chapter_count}, atmos: "
            f"{'yes' if score.playlist.has_atmos else 'no'}){tag}"
        )

    def on_playlist_selected(self, label: str) -> None:
        idx = self.playlist_option.cget("values").index(label)
        score = self.playlist_scores[idx]
        self.selected_playlist_score = score
        self.selected_playlist = score.playlist
        pl = self.selected_playlist

        info_lines = [f"{len(pl.tracks)} tracks, {pl.chapter_count} chapters."]
        if pl.duration_seconds:
            info_lines[0] += f" Runs {pl.duration_seconds / 60:.0f} minutes."
        info_lines.append("Why this ranking:")
        info_lines.extend(f"  - {r}" for r in score.reasons)
        self.playlist_info_label.configure(text="\n".join(info_lines))

        # Preserve anything already typed for chapter numbers that still
        # exist in the new playlist, so switching between candidate
        # playlists (e.g. a different angle/cut with matching chapter
        # positions) doesn't throw away names entered by hand.
        preserved = {
            i: var.get() for i, var in self.chapter_name_vars.items() if var.get().strip()
        }
        self._rebuild_chapter_table(pl.chapter_count, preserve=preserved)
        self.selected_playlist_chapters = []  # stale until _load_embedded_chapter_names finishes for this playlist
        self._load_embedded_chapter_names(pl)

    # ------------------------------------------------------------------
    # Chapter naming table
    # ------------------------------------------------------------------

    def _rebuild_chapter_table(
        self, chapter_count: int, preserve: dict[int, str] | None = None
    ) -> None:
        preserve = preserve or {}
        for widget in self.chapter_scroll.winfo_children():
            widget.destroy()
        self.chapter_name_vars.clear()
        self.chapter_source_labels.clear()

        for i in range(1, chapter_count + 1):
            ctk.CTkLabel(self.chapter_scroll, text=f"Chapter {i:02d}", width=90).grid(
                row=i - 1, column=0, padx=(4, 8), pady=4, sticky="w"
            )
            var = ctk.StringVar()
            if i in preserve:
                var.set(preserve[i])
            entry = ctk.CTkEntry(
                self.chapter_scroll, textvariable=var, placeholder_text=f"Track {i:02d}"
            )
            entry.grid(row=i - 1, column=1, padx=(0, 8), pady=4, sticky="ew")
            enable_clipboard(entry)
            self.chapter_name_vars[i] = var

            # Shows where the current name came from - "from disc" once
            # prefilled from an embedded chapter title, flipping to
            # "edited" the moment the user changes it, blank if the user
            # typed the name themselves and nothing was ever auto-filled.
            source_label = ctk.CTkLabel(
                self.chapter_scroll, text="", width=90, text_color="gray60"
            )
            source_label.grid(row=i - 1, column=2, padx=(0, 4), pady=4, sticky="w")
            self.chapter_source_labels[i] = source_label

    def _load_embedded_chapter_names(self, pl: extractor.Playlist) -> None:
        """
        Read chapter names embedded in the playlist itself, if any, and
        use them to prefill blank naming fields. Runs off the UI thread:
        mkvextract can't read a .mpls playlist directly (only mkvmerge
        can), so this goes through read_chapters_from_source(), which
        remuxes just the chapter data via mkvmerge first - it isn't
        copying the video or Atmos audio streams, so it's normally quick,
        but a slow/network drive could still make it worth not blocking
        the UI for.
        """
        def work() -> None:
            try:
                chapters = extractor.read_chapters_from_source(pl.path)
            except Exception:
                return  # no embedded chapter names available - not fatal, nothing to prefill
            self.after(0, lambda: self._apply_embedded_chapter_names(pl, chapters))

        threading.Thread(target=work, daemon=True).start()

    def _apply_embedded_chapter_names(
        self, pl: extractor.Playlist, chapters: list[extractor.Chapter]
    ) -> None:
        if self.selected_playlist is not pl:
            return  # user already moved on to a different playlist selection
        self.selected_playlist_chapters = chapters
        for ch in chapters:
            if ch.index not in self.chapter_name_vars or not ch.embedded_name:
                continue
            var = self.chapter_name_vars[ch.index]
            if var.get().strip():
                continue  # already has a name (preserved or hand-typed) - don't clobber it
            var.set(ch.embedded_name)
            self._mark_chapter_source(ch.index, ch.embedded_name)

    def _mark_chapter_source(self, index: int, baseline_value: str) -> None:
        """Label a chapter's name as auto-filled, and flip the label to 'edited' if the user changes it."""
        label = self.chapter_source_labels.get(index)
        var = self.chapter_name_vars.get(index)
        if label is None or var is None:
            return
        label.configure(text="from disc")

        def on_change(*_args, baseline=baseline_value, lbl=label, v=var) -> None:
            lbl.configure(text="edited" if v.get() != baseline else "from disc")

        var.trace_add("write", on_change)

    def fill_from_paste(self) -> None:
        text = self.paste_textbox.get("1.0", "end").strip()
        if not text:
            return
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

        for i, line in enumerate(lines, start=1):
            if i in self.chapter_name_vars:
                # Strip a leading "1.", "01 -", "1)" etc if present.
                cleaned = _strip_leading_number(line)
                self.chapter_name_vars[i].set(cleaned)
                # A pasted tracklist is an explicit user action - it should
                # read as user-entered, not linger as "from disc"/"edited"
                # from whatever was there before.
                label = self.chapter_source_labels.get(i)
                if label is not None:
                    label.configure(text="")

        if len(lines) != len(self.chapter_name_vars):
            self.set_status(
                f"Pasted {len(lines)} lines but there are {len(self.chapter_name_vars)} chapters - check alignment."
            )

    def import_tracklist(self) -> None:
        if not self.selected_playlist:
            messagebox.showwarning("No playlist selected", "Select a playlist first.")
            return

        initial_dir = str(self.disc_folder) if self.disc_folder else None
        path_str = filedialog.askopenfilename(
            title="Import tracklist",
            initialdir=initial_dir,
            filetypes=[
                ("Tracklist files", "*.txt *.nfo *.cue *.json"),
                ("All files", "*.*"),
            ],
        )
        if not path_str:
            return
        path = Path(path_str)

        try:
            entries = extractor.load_sidecar_tracklist(path)
        except ValueError as exc:
            messagebox.showerror("Could not read tracklist", str(exc))
            return

        chapters = self.selected_playlist_chapters
        if not chapters:
            # Embedded-chapter prefill hasn't finished loading yet - read
            # directly rather than making the user wait and retry.
            try:
                chapters = extractor.read_chapters_from_source(self.selected_playlist.path)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Could not read chapters", str(exc))
                return

        result = extractor.match_tracklist_to_chapters(chapters, entries)
        accepted = self._show_tracklist_review_dialog(path.name, result)
        if not accepted:
            return

        for index, name in accepted.items():
            if index not in self.chapter_name_vars:
                continue
            self.chapter_name_vars[index].set(name)
            label = self.chapter_source_labels.get(index)
            if label is not None:
                label.configure(text="imported")

        self.set_status(f"Imported {len(accepted)} track name(s) from {path.name}.")

    def _show_tracklist_review_dialog(
        self, source_name: str, result: extractor.TracklistMatchResult
    ) -> dict[int, str] | None:
        """
        Show the proposed chapter -> name mapping before anything is
        applied. Matched rows are pre-checked, mismatched rows are shown
        but unchecked so a duration mismatch has to be consciously
        accepted, and unmatched chapters can't be checked at all since
        there's no name to apply. Returns {chapter_index: name} for
        whatever the user accepts, or None if they cancel.
        """
        outcome: dict[str, dict[int, str] | None] = {"accepted": None}

        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Review tracklist - {source_name}")
        dialog.geometry("560x480")
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(
            dialog, text=result.summary, font=ctk.CTkFont(weight="bold"),
            wraplength=520, justify="left",
        ).pack(padx=16, pady=(16, 8), anchor="w")

        scroll = ctk.CTkScrollableFrame(dialog)
        scroll.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        row_vars: dict[int, tuple[tk.BooleanVar, str]] = {}
        icon_for = {"matched": "OK", "duration_mismatch": "!", "no_match": "-"}

        for m in result.matches:
            row = ctk.CTkFrame(scroll, fg_color="transparent")
            row.pack(fill="x", pady=2)

            var = tk.BooleanVar(value=(m.confidence == "matched"))
            checkbox = ctk.CTkCheckBox(row, text="", variable=var, width=20)
            checkbox.pack(side="left", padx=(0, 8))
            if m.confidence == "no_match":
                checkbox.configure(state="disabled")

            label_text = f"[{icon_for[m.confidence]}] Chapter {m.chapter_index:02d}: {m.name or '(no match)'}"
            if m.detail:
                label_text += f"  -  {m.detail}"
            ctk.CTkLabel(row, text=label_text, anchor="w").pack(side="left", fill="x", expand=True)

            row_vars[m.chapter_index] = (var, m.name)

        button_row = ctk.CTkFrame(dialog, fg_color="transparent")
        button_row.pack(pady=(0, 16))

        def accept_all() -> None:
            outcome["accepted"] = {i: name for i, (_var, name) in row_vars.items() if name}
            dialog.destroy()

        def accept_selected() -> None:
            outcome["accepted"] = {i: name for i, (var, name) in row_vars.items() if var.get() and name}
            dialog.destroy()

        def cancel() -> None:
            dialog.destroy()

        ctk.CTkButton(button_row, text="Cancel", width=100, command=cancel).pack(side="left", padx=6)
        ctk.CTkButton(
            button_row, text="Accept Selected", width=140, command=accept_selected
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            button_row, text="Accept All Matched", width=160, command=accept_all
        ).pack(side="left", padx=6)

        dialog.wait_window()
        return outcome["accepted"]

    def search_online_tracklist(self) -> None:
        if not self.selected_playlist:
            messagebox.showwarning("No playlist selected", "Select a playlist first.")
            return

        artist_guess, album_guess, year_guess = "", "", None
        if self.disc_folder is not None:
            artist_guess, album_guess, year_guess = extractor.guess_artist_album_year(self.disc_folder)

        search_input = self._show_musicbrainz_search_dialog(artist_guess, album_guess, year_guess)
        if search_input is None:
            return
        artist, album, year = search_input

        self.set_status(f"Searching MusicBrainz for {artist} - {album}...")

        def work() -> None:
            try:
                candidates = extractor.search_musicbrainz_releases(artist, album, year)
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: self._on_musicbrainz_error(exc))
                return
            self.after(0, lambda: self._on_musicbrainz_search_complete(candidates))

        threading.Thread(target=work, daemon=True).start()

    def _on_musicbrainz_error(self, exc: Exception) -> None:
        self.set_status("MusicBrainz lookup failed.")
        messagebox.showerror("MusicBrainz lookup failed", str(exc))

    def _on_musicbrainz_search_complete(self, candidates: list[extractor.MusicBrainzCandidate]) -> None:
        if not candidates:
            self.set_status("No MusicBrainz results found.")
            messagebox.showinfo("No results", "No matching releases found on MusicBrainz.")
            return

        chosen = self._show_musicbrainz_results_dialog(candidates)
        if chosen is None:
            return

        self.set_status(f"Fetching tracklist for {chosen.title}...")

        def work() -> None:
            try:
                entries = extractor.fetch_musicbrainz_tracklist(chosen.release_id)
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: self._on_musicbrainz_error(exc))
                return
            self.after(0, lambda: self._on_musicbrainz_tracklist_fetched(chosen, entries))

        threading.Thread(target=work, daemon=True).start()

    def _on_musicbrainz_tracklist_fetched(
        self, candidate: extractor.MusicBrainzCandidate, entries: list[extractor.TracklistEntry]
    ) -> None:
        if not entries:
            messagebox.showinfo(
                "Empty tracklist", f"MusicBrainz has no track titles for '{candidate.title}'."
            )
            self.set_status("Ready.")
            return

        chapters = self.selected_playlist_chapters
        if not chapters:
            try:
                chapters = extractor.read_chapters_from_source(self.selected_playlist.path)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Could not read chapters", str(exc))
                return

        # MusicBrainz's track count is summed across every medium on the
        # release, but fetch_musicbrainz_tracklist() only pulls the first
        # medium's tracks (a single Atmos playlist is one continuous
        # chapter set). A very different track count from this playlist's
        # chapter count usually means the wrong release/edition was
        # picked - flag it up front rather than letting a silent
        # mismatch fall through to the review dialog.
        if abs(len(entries) - len(chapters)) > max(2, len(chapters) // 4):
            proceed = messagebox.askyesno(
                "Track count mismatch",
                f"This playlist has {len(chapters)} chapter(s), but the "
                f"selected MusicBrainz release has {len(entries)} track(s) "
                f"on its first disc.\n\n"
                f"This usually means a different edition/release was "
                f"picked. Continue anyway and review the proposed match?",
            )
            if not proceed:
                self.set_status("Ready.")
                return

        result = extractor.match_tracklist_to_chapters(chapters, entries)
        accepted = self._show_tracklist_review_dialog(f"MusicBrainz: {candidate.title}", result)
        if not accepted:
            self.set_status("Ready.")
            return

        for index, name in accepted.items():
            if index not in self.chapter_name_vars:
                continue
            self.chapter_name_vars[index].set(name)
            label = self.chapter_source_labels.get(index)
            if label is not None:
                label.configure(text="MusicBrainz")

        self.set_status(f"Imported {len(accepted)} track name(s) from MusicBrainz.")

    def _show_musicbrainz_search_dialog(
        self, artist_guess: str, album_guess: str, year_guess: int | None
    ) -> tuple[str, str, int | None] | None:
        outcome: dict[str, tuple[str, str, int | None] | None] = {"result": None}

        dialog = ctk.CTkToplevel(self)
        dialog.title("Search MusicBrainz")
        dialog.geometry("420x240")
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(dialog, text="Artist:").grid(row=0, column=0, sticky="w", padx=16, pady=(16, 4))
        artist_var = ctk.StringVar(value=artist_guess)
        artist_entry = ctk.CTkEntry(dialog, textvariable=artist_var, width=260)
        artist_entry.grid(row=0, column=1, padx=(0, 16), pady=(16, 4))
        enable_clipboard(artist_entry)

        ctk.CTkLabel(dialog, text="Album:").grid(row=1, column=0, sticky="w", padx=16, pady=4)
        album_var = ctk.StringVar(value=album_guess)
        album_entry = ctk.CTkEntry(dialog, textvariable=album_var, width=260)
        album_entry.grid(row=1, column=1, padx=(0, 16), pady=4)
        enable_clipboard(album_entry)

        ctk.CTkLabel(dialog, text="Year (optional):").grid(row=2, column=0, sticky="w", padx=16, pady=4)
        year_var = ctk.StringVar(value=str(year_guess) if year_guess else "")
        year_entry = ctk.CTkEntry(dialog, textvariable=year_var, width=100)
        year_entry.grid(row=2, column=1, sticky="w", padx=(0, 16), pady=4)
        enable_clipboard(year_entry)

        ctk.CTkLabel(
            dialog, text="Searches musicbrainz.org - nothing is applied until you review results.",
            text_color="gray60", wraplength=380, justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=16, pady=(4, 0))

        button_row = ctk.CTkFrame(dialog, fg_color="transparent")
        button_row.grid(row=4, column=0, columnspan=2, pady=16)

        def do_search() -> None:
            artist = artist_var.get().strip()
            album = album_var.get().strip()
            if not artist or not album:
                messagebox.showwarning(
                    "Missing info", "Both artist and album are needed to search.", parent=dialog
                )
                return
            year_text = year_var.get().strip()
            year = int(year_text) if year_text.isdigit() else None
            outcome["result"] = (artist, album, year)
            dialog.destroy()

        def cancel() -> None:
            dialog.destroy()

        ctk.CTkButton(button_row, text="Cancel", width=100, command=cancel).pack(side="left", padx=6)
        ctk.CTkButton(button_row, text="Search", width=100, command=do_search).pack(side="left", padx=6)

        dialog.wait_window()
        return outcome["result"]

    def _show_musicbrainz_results_dialog(
        self, candidates: list[extractor.MusicBrainzCandidate]
    ) -> extractor.MusicBrainzCandidate | None:
        outcome: dict[str, extractor.MusicBrainzCandidate | None] = {"chosen": None}

        dialog = ctk.CTkToplevel(self)
        dialog.title("MusicBrainz results")
        dialog.geometry("560x420")
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(
            dialog, text=f"{len(candidates)} release(s) found - pick one:",
            font=ctk.CTkFont(weight="bold"),
        ).pack(padx=16, pady=(16, 8), anchor="w")

        scroll = ctk.CTkScrollableFrame(dialog)
        scroll.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        selected_index = tk.IntVar(value=0)
        for i, c in enumerate(candidates):
            label = f"{c.artist} - {c.title}"
            if c.date:
                label += f" ({c.date})"
            if c.track_count:
                label += f", {c.track_count} tracks"
            if c.format_hint:
                label += f" [{c.format_hint}]"
            ctk.CTkRadioButton(scroll, text=label, variable=selected_index, value=i).pack(
                anchor="w", pady=4, padx=4
            )

        button_row = ctk.CTkFrame(dialog, fg_color="transparent")
        button_row.pack(pady=(0, 16))

        def use_selected() -> None:
            outcome["chosen"] = candidates[selected_index.get()]
            dialog.destroy()

        def cancel() -> None:
            dialog.destroy()

        ctk.CTkButton(button_row, text="Cancel", width=100, command=cancel).pack(side="left", padx=6)
        ctk.CTkButton(
            button_row, text="Use Selected", width=140, command=use_selected
        ).pack(side="left", padx=6)

        dialog.wait_window()
        return outcome["chosen"]

    # ------------------------------------------------------------------
    # Output folder / extraction
    # ------------------------------------------------------------------

    def browse_output(self) -> None:
        folder = filedialog.askdirectory(title="Select output folder")
        if folder:
            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, folder)

    def start_extraction(self) -> None:
        if not self.selected_playlist or not self.selected_playlist.has_atmos:
            messagebox.showerror("No Atmos track", "Selected playlist has no Atmos track.")
            return

        if self.selected_playlist_score and self.selected_playlist_score.duplicate_of:
            proceed = messagebox.askyesno(
                "Possible duplicate playlist",
                f"{self.selected_playlist.path.name} looks like a duplicate or "
                f"alternate angle of {self.selected_playlist_score.duplicate_of.name} "
                "(same duration, chapter count, and tracks).\n\n"
                "Continue with this playlist anyway?",
            )
            if not proceed:
                return

        output_str = self.output_entry.get().strip()
        if not output_str:
            messagebox.showwarning("No output folder", "Pick an output folder first.")
            return

        library_folder = Path(output_str)
        settings.update(last_output_folder=str(library_folder))

        source_folder = Path(self.source_entry.get().strip())
        album_name = extractor.derive_album_folder_name(source_folder)
        output_folder = library_folder / album_name

        track_names = {
            idx: var.get().strip()
            for idx, var in self.chapter_name_vars.items()
            if var.get().strip()
        }

        resolution = self._resolve_output_collisions(output_folder, track_names)
        if resolution is None:
            return  # user cancelled out of the collision dialog
        output_folder, overwrite_paths = resolution

        work_folder = output_folder / "_work"
        resume_choice = self._resolve_resume_choice(work_folder)
        if resume_choice is None:
            return  # user backed out of the resume/clean-up prompt
        resume, do_cleanup_first = resume_choice
        if do_cleanup_first:
            shutil.rmtree(work_folder, ignore_errors=True)

        self.cancel_event = threading.Event()
        self.current_log_path = output_folder / "extraction.log"
        self.extract_button.configure(state="disabled", text="Working...")
        self.cancel_button.configure(state="normal", text="Cancel")
        self.open_log_button.configure(state="normal")
        self.set_status(f"Starting extraction -> {output_folder}")

        def progress(msg: str) -> None:
            self.after(0, lambda: self.set_status(msg))

        def work() -> None:
            try:
                results = extractor.run_full_pipeline(
                    self.selected_playlist,
                    track_names,
                    work_folder=work_folder,
                    output_folder=output_folder,
                    container=self.cfg.get("output_container", "mkv"),
                    progress_cb=progress,
                    overwrite=overwrite_paths,
                    cancel_event=self.cancel_event,
                    resume=resume,
                    log_path=self.current_log_path,
                )
                self.after(0, lambda: self._on_extraction_complete(results))
            except extractor.JobCancelled:
                self.after(0, self._on_extraction_cancelled)
            except extractor.OutputCollisionError as exc:
                # Rare - preflight already checked - but the output folder
                # could change on disk between preflight and the actual
                # write (another process, another run). Handled the same
                # as any other extraction failure: nothing overwritten,
                # user told exactly what collided.
                self.after(0, lambda: self._on_extraction_failed(exc))
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: self._on_extraction_failed(exc))

        threading.Thread(target=work, daemon=True).start()

    def cancel_extraction(self) -> None:
        if self.cancel_event is None:
            return
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled", text="Cancelling...")
        self.set_status("Cancelling - waiting for the current step to stop...")

    def open_log(self) -> None:
        if self.current_log_path is None or not self.current_log_path.is_file():
            messagebox.showinfo("No log yet", "No log file is available yet.")
            return
        if hasattr(os, "startfile"):
            os.startfile(self.current_log_path)  # type: ignore[attr-defined]
        else:
            webbrowser.open(self.current_log_path.as_uri())

    def _resolve_resume_choice(self, work_folder: Path) -> tuple[bool, bool] | None:
        """
        Check whether work_folder holds a manifest from a previous job
        that didn't finish (crashed, was cancelled, or failed), and if so
        ask how to proceed. Returns (resume, clean_up_first):
          - (False, False): nothing to resume, proceed as a normal fresh job
          - (True, False):  resume - skip whatever the manifest says is already done
          - (False, True):  clean up the leftover _work folder, then start fresh
          - None:            user backed out - don't start any job

        Resume and "retry failed tracks" are the same action here: progress
        is tracked by which output files actually exist on disk, so there's
        nothing left to redo differently between "resume where it left off"
        and "retry whatever didn't finish" - both just mean "don't redo
        what's already done".
        """
        manifest = extractor.read_manifest(work_folder)
        if manifest is None or manifest.status == "complete":
            return False, False

        choice = messagebox.askyesnocancel(
            "Resume previous job?",
            f"A previous job for this output folder didn't finish "
            f"(status: {manifest.status}).\n\n"
            f"Yes = Resume - skip whatever was already completed\n"
            f"No = Clean up and start this job fresh\n"
            f"Cancel = Don't start a job right now",
        )
        if choice is None:
            return None
        return (True, False) if choice else (False, True)

    def _on_extraction_cancelled(self) -> None:
        self.extract_button.configure(state="normal", text="Extract && Split")
        self.cancel_button.configure(state="disabled", text="Cancel")
        self.cancel_event = None
        self.set_status("Cancelled. Extracting again to the same folder will offer to resume.")

    def _resolve_output_collisions(
        self, output_folder: Path, track_names: dict[int, str]
    ) -> tuple[Path, set[Path]] | None:
        """
        Run the fast filename-planning check before starting the slow
        extraction pipeline, and get explicit user confirmation for any
        output file that would already exist. Returns
        (output_folder, approved_overwrite_paths), where output_folder may
        have been changed if the user picked a different one; or None if
        the user cancelled.
        """
        chapter_count = self.selected_playlist.chapter_count
        container = self.cfg.get("output_container", "mkv")

        warned_duplicates = False

        while True:
            planned = extractor.preflight_check(
                output_folder, chapter_count, track_names, container=container
            )

            duplicates = [p for p in planned if p.duplicate_name]
            if duplicates and not warned_duplicates:
                names = ", ".join(sorted({p.chapter.name for p in duplicates}))
                proceed = messagebox.askyesno(
                    "Duplicate track names",
                    f"More than one chapter is named the same thing ({names}). "
                    "This usually means a pasted tracklist got mis-aligned - "
                    "the chapter numbers stay separate either way, but you may "
                    "want to double check the names before continuing.\n\n"
                    "Continue anyway?",
                )
                if not proceed:
                    return None
                warned_duplicates = True  # don't re-ask if they loop back (e.g. after choosing a folder)

            colliding = [p for p in planned if p.exists]
            if not colliding:
                return output_folder, set()

            choice = self._show_collision_dialog(output_folder, colliding)
            if choice == "cancel":
                return None
            if choice == "choose_folder":
                new_library_folder = filedialog.askdirectory(
                    title="Select a different output folder"
                )
                if not new_library_folder:
                    continue  # back to the same dialog, nothing changed
                source_folder = Path(self.source_entry.get().strip())
                album_name = extractor.derive_album_folder_name(source_folder)
                output_folder = Path(new_library_folder) / album_name
                self.output_entry.delete(0, "end")
                self.output_entry.insert(0, new_library_folder)
                continue
            if choice == "overwrite":
                return output_folder, {p.path for p in colliding}

    def _show_collision_dialog(self, output_folder: Path, colliding: list) -> str:
        """
        Modal dialog listing output files that already exist. Nothing is
        ever overwritten without the user explicitly choosing to here.
        Returns "cancel", "choose_folder", or "overwrite".
        """
        result = {"choice": "cancel"}

        dialog = ctk.CTkToplevel(self)
        dialog.title("Output files already exist")
        dialog.geometry("480x380")
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(
            dialog,
            text=f"{len(colliding)} file(s) already exist in \"{output_folder.name}\":",
            font=ctk.CTkFont(weight="bold"),
            wraplength=440,
            justify="left",
        ).pack(padx=16, pady=(16, 8), anchor="w")

        listbox = ctk.CTkTextbox(dialog, height=180)
        listbox.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        listbox.insert("1.0", "\n".join(p.path.name for p in colliding))
        listbox.configure(state="disabled")

        ctk.CTkLabel(
            dialog,
            text="Nothing is overwritten automatically. Choose how to proceed:",
            wraplength=440,
            justify="left",
        ).pack(padx=16, pady=(0, 8), anchor="w")

        button_row = ctk.CTkFrame(dialog, fg_color="transparent")
        button_row.pack(pady=(0, 16))

        def pick(choice: str) -> None:
            result["choice"] = choice
            dialog.destroy()

        ctk.CTkButton(
            button_row, text="Cancel", width=100, command=lambda: pick("cancel")
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            button_row,
            text="Choose another folder",
            width=170,
            command=lambda: pick("choose_folder"),
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            button_row,
            text="Overwrite these files",
            width=170,
            fg_color="#a33",
            hover_color="#822",
            command=lambda: pick("overwrite"),
        ).pack(side="left", padx=6)

        dialog.wait_window()
        return result["choice"]

    def _on_extraction_complete(self, results: list[Path]) -> None:
        self.extract_button.configure(state="normal", text="Extract && Split")
        self.cancel_button.configure(state="disabled", text="Cancel")
        self.cancel_event = None
        self.set_status(f"Done. Wrote {len(results)} files to {self.output_entry.get()}")
        messagebox.showinfo("Done", f"Wrote {len(results)} track files.")

    def _on_extraction_failed(self, exc: Exception) -> None:
        self.extract_button.configure(state="normal", text="Extract && Split")
        self.cancel_button.configure(state="disabled", text="Cancel")
        self.cancel_event = None
        self.set_status("Failed - see error dialog. Extracting again to the same folder will offer to resume.")
        messagebox.showerror("Extraction failed", str(exc))

    # ------------------------------------------------------------------

    def set_status(self, text: str) -> None:
        self.status_label.configure(text=text)


def _strip_leading_number(line: str) -> str:
    return extractor.strip_leading_number(line)


if __name__ == "__main__":
    app = AtmosTrackSplitterApp()
    app.mainloop()
