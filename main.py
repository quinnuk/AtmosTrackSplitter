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

import re
import threading
import tkinter as tk
import tkinter.filedialog as filedialog
import tkinter.messagebox as messagebox
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
        self.selected_playlist: extractor.Playlist | None = None
        self.chapter_name_vars: dict[int, ctk.StringVar] = {}

        self._build_layout()

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

        self.playlist_info_label = ctk.CTkLabel(playlist_frame, text="", justify="left")
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

        labels = [
            f"{p.path.name}  (chapters: {p.chapter_count}, atmos: {'yes' if p.has_atmos else 'no'})"
            for p in playlists
        ]
        self.playlist_option.configure(values=labels)

        best = extractor.find_best_atmos_playlist(playlists)
        if best:
            idx = playlists.index(best)
            self.playlist_option.set(labels[idx])
            self.on_playlist_selected(labels[idx])
            self.set_status(f"Found Atmos track in {best.path.name}. Ready to name tracks.")
        else:
            self.playlist_option.set(labels[0])
            self.on_playlist_selected(labels[0])
            self.set_status("No Atmos track found in any playlist.")

    def on_playlist_selected(self, label: str) -> None:
        idx = self.playlist_option.cget("values").index(label)
        self.selected_playlist = self.playlists[idx]
        pl = self.selected_playlist

        atmos = pl.atmos_track
        info = f"{len(pl.tracks)} tracks, {pl.chapter_count} chapters. "
        if atmos:
            info += f"Atmos track ID {atmos.track_id}"
            info += f" + video track ID {pl.video_track.track_id}." if pl.video_track else " (no video track found)."
        else:
            info += "No Atmos track in this playlist."
        self.playlist_info_label.configure(text=info)

        self._rebuild_chapter_table(pl.chapter_count)

    # ------------------------------------------------------------------
    # Chapter naming table
    # ------------------------------------------------------------------

    def _rebuild_chapter_table(self, chapter_count: int) -> None:
        for widget in self.chapter_scroll.winfo_children():
            widget.destroy()
        self.chapter_name_vars.clear()

        for i in range(1, chapter_count + 1):
            ctk.CTkLabel(self.chapter_scroll, text=f"Chapter {i:02d}", width=90).grid(
                row=i - 1, column=0, padx=(4, 8), pady=4, sticky="w"
            )
            var = ctk.StringVar()
            entry = ctk.CTkEntry(
                self.chapter_scroll, textvariable=var, placeholder_text=f"Track {i:02d}"
            )
            entry.grid(row=i - 1, column=1, padx=(0, 8), pady=4, sticky="ew")
            enable_clipboard(entry)
            self.chapter_name_vars[i] = var

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

        if len(lines) != len(self.chapter_name_vars):
            self.set_status(
                f"Pasted {len(lines)} lines but there are {len(self.chapter_name_vars)} chapters - check alignment."
            )

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

        self.extract_button.configure(state="disabled", text="Working...")
        self.set_status(f"Starting extraction -> {output_folder}")

        def progress(msg: str) -> None:
            self.after(0, lambda: self.set_status(msg))

        def work() -> None:
            try:
                work_folder = output_folder / "_work"
                results = extractor.run_full_pipeline(
                    self.selected_playlist,
                    track_names,
                    work_folder=work_folder,
                    output_folder=output_folder,
                    container=self.cfg.get("output_container", "mkv"),
                    progress_cb=progress,
                )
                self.after(0, lambda: self._on_extraction_complete(results))
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: self._on_extraction_failed(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_extraction_complete(self, results: list[Path]) -> None:
        self.extract_button.configure(state="normal", text="Extract && Split")
        self.set_status(f"Done. Wrote {len(results)} files to {self.output_entry.get()}")
        messagebox.showinfo("Done", f"Wrote {len(results)} track files.")

    def _on_extraction_failed(self, exc: Exception) -> None:
        self.extract_button.configure(state="normal", text="Extract && Split")
        self.set_status("Failed - see error dialog.")
        messagebox.showerror("Extraction failed", str(exc))

    # ------------------------------------------------------------------

    def set_status(self, text: str) -> None:
        self.status_label.configure(text=text)


def _strip_leading_number(line: str) -> str:
    return re.sub(r"^\s*\d+[\.\)\-]?\s*", "", line).strip()


if __name__ == "__main__":
    app = AtmosTrackSplitterApp()
    app.mainloop()
