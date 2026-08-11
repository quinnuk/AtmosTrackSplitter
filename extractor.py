diff --git a/extractor.py b/extractor.py
index 8f9ea64..560d25c 100644
--- a/extractor.py
+++ b/extractor.py
@@ -15,9 +15,11 @@ imported by main.py.
 
 from __future__ import annotations
 
+import os
 import re
 import shutil
 import subprocess
+import uuid
 import xml.etree.ElementTree as ET
 from dataclasses import dataclass, field
 from pathlib import Path
@@ -329,10 +331,116 @@ def probe_duration_seconds(media_path: Path) -> float:
 
 _INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]')
 
+# Windows reserves these as device names - "CON.mkv" etc silently fails or
+# hits the device instead of creating a file, even though the name looks
+# fine on Linux/macOS where this tool is often developed/tested.
+_WINDOWS_RESERVED_NAMES = {
+    "CON", "PRN", "AUX", "NUL",
+    *(f"COM{i}" for i in range(1, 10)),
+    *(f"LPT{i}" for i in range(1, 10)),
+}
+
 
 def sanitize_filename(name: str) -> str:
     name = _INVALID_FILENAME_CHARS.sub("", name).strip()
-    return name or "Untitled"
+    # Windows silently strips trailing dots/spaces from filenames, which
+    # means "Track. " and "Track" collide without ever showing a warning -
+    # strip them ourselves so what we display matches what actually lands
+    # on disk.
+    name = name.rstrip(" .")
+    if not name:
+        name = "Untitled"
+    if name.upper() in _WINDOWS_RESERVED_NAMES:
+        name = f"_{name}"
+    return name
+
+
+@dataclass
+class PlannedOutput:
+    chapter: "Chapter"
+    path: Path
+    exists: bool = False
+    duplicate_name: bool = False  # another chapter sanitises to the same track name
+
+
+def plan_output_files(
+    chapters: list[Chapter],
+    output_folder: Path,
+    container: str = "mkv",
+) -> list[PlannedOutput]:
+    """
+    Compute the final output filename for each chapter without touching
+    the filesystem beyond an existence check.
+
+    Every filename is prefixed with its chapter index ("01 - ...",
+    "02 - ..."), so two chapters can never actually collide with each
+    other on disk even if they share a name - but two chapters sharing a
+    name is usually a sign the tracklist got pasted wrong (e.g. duplicated
+    a line), so those are flagged via duplicate_name for the caller to
+    show as a warning, not silently renamed.
+
+    Separately, each planned path is checked against what's already on
+    disk, so the caller can get user confirmation *before* running the
+    (slow) extraction step, rather than discovering the collision partway
+    through splitting.
+    """
+    name_counts: dict[str, int] = {}
+    for ch in chapters:
+        base = sanitize_filename(ch.name or f"Track {ch.index:02d}").lower()
+        name_counts[base] = name_counts.get(base, 0) + 1
+
+    planned: list[PlannedOutput] = []
+    for ch in chapters:
+        track_name = ch.name or f"Track {ch.index:02d}"
+        base = sanitize_filename(track_name)
+        path = output_folder / f"{ch.index:02d} - {base}.{container}"
+        planned.append(
+            PlannedOutput(
+                chapter=ch,
+                path=path,
+                exists=path.exists(),
+                duplicate_name=name_counts[base.lower()] > 1,
+            )
+        )
+
+    return planned
+
+
+def preflight_check(
+    output_folder: Path,
+    chapter_count: int,
+    track_names: dict[int, str],
+    container: str = "mkv",
+) -> list[PlannedOutput]:
+    """
+    Compute planned output filenames and existence collisions using just
+    the chapter count and the names the user has already typed in -
+    before the slow mkvmerge extraction step even starts. Chapter start
+    times aren't known yet at this point and aren't needed for filename
+    planning, so this uses placeholder timestamps.
+    """
+    fake_chapters = [
+        Chapter(index=i, start_seconds=0.0, name=track_names.get(i, ""))
+        for i in range(1, chapter_count + 1)
+    ]
+    return plan_output_files(fake_chapters, output_folder, container=container)
+
+
+class OutputCollisionError(RuntimeError):
+    """
+    Raised when one or more planned output files already exist on disk and
+    weren't explicitly approved for overwrite. Nothing is ever overwritten
+    silently - the caller (GUI or CLI) must resolve this before splitting
+    can proceed, typically via preflight_check() + user confirmation.
+    """
+
+    def __init__(self, colliding: list[PlannedOutput]):
+        self.colliding = colliding
+        names = ", ".join(p.path.name for p in colliding)
+        super().__init__(
+            f"{len(colliding)} output file(s) already exist and were not "
+            f"approved for overwrite: {names}"
+        )
 
 
 _TRAILING_BRACKET_RE = re.compile(r"\s*[\[\(][^\[\]\(\)]*[\]\)]\s*$")
@@ -363,10 +471,17 @@ def split_chapters(
     output_folder: Path,
     container: str = "mkv",
     progress_cb: Optional[Callable[[str], None]] = None,
+    overwrite: Optional[set[Path]] = None,
 ) -> list[Path]:
     """
     Split the Atmos MKV into one file per chapter using ffmpeg stream-copy
     (no re-encoding - Atmos must stay bit-exact).
+
+    overwrite: set of specific output paths the caller has already gotten
+    user approval to overwrite (normally via preflight_check() plus a
+    confirmation dialog). Any planned path that already exists and isn't
+    in this set raises OutputCollisionError instead of overwriting it -
+    ffmpeg's own -y flag is never allowed to make that decision silently.
     """
     if not chapters:
         raise ValueError(
@@ -376,15 +491,32 @@ def split_chapters(
 
     output_folder.mkdir(parents=True, exist_ok=True)
 
+    # Check for collisions before the (comparatively expensive) duration
+    # probe below, so a caller that skipped preflight_check still fails
+    # fast instead of waiting on ffprobe first.
+    planned = plan_output_files(chapters, output_folder, container=container)
+    overwrite = overwrite or set()
+    colliding = [p for p in planned if p.exists and p.path not in overwrite]
+    if colliding:
+        raise OutputCollisionError(colliding)
+
     if chapters[-1].end_seconds is None:
         chapters[-1].end_seconds = probe_duration_seconds(atmos_mkv)
 
     output_paths: list[Path] = []
 
-    for ch in chapters:
+    for item in planned:
+        ch = item.chapter
+        out_path = item.path
         track_name = ch.name or f"Track {ch.index:02d}"
-        filename = f"{ch.index:02d} - {sanitize_filename(track_name)}.{container}"
-        out_path = output_folder / filename
+
+        # Write to a temp file in the same folder (so the final rename is
+        # on the same filesystem and therefore atomic) and only move it to
+        # the real name once ffmpeg has produced a non-empty file. A crash,
+        # Ctrl+C, or killed process mid-chapter then leaves a stray
+        # ".partial-*" file instead of a truncated file sitting at the
+        # real track name looking like a finished, playable song.
+        tmp_path = out_path.with_name(f".partial-{uuid.uuid4().hex}-{out_path.name}")
 
         # -ss before -i does a fast keyframe seek in the input instead of
         # decoding from the start of the file on every single chapter
@@ -394,7 +526,7 @@ def split_chapters(
         # is dramatically faster, especially for later chapters in a long file.
         args = [
             TOOL_PATHS["ffmpeg"],
-            "-y",
+            "-y",  # only ever overwrites our own freshly-named temp file
             "-ss", str(ch.start_seconds),
             "-i", str(atmos_mkv),
         ]
@@ -404,14 +536,24 @@ def split_chapters(
             # comes before -i - so we need the chapter's duration, not its
             # absolute end time, to get the correct cut point.
             args += ["-t", str(ch.end_seconds - ch.start_seconds)]
-        args += ["-c", "copy", str(out_path)]
+        args += ["-c", "copy", str(tmp_path)]
 
         if progress_cb:
             progress_cb(f"Splitting chapter {ch.index}: {track_name}")
 
-        result = _run(args, timeout=1800)  # 30 min per chapter - stream-copy is fast, this is just a safety net
-        if result.returncode != 0:
-            raise RuntimeError(f"ffmpeg failed on chapter {ch.index}:\n{result.stderr}")
+        try:
+            result = _run(args, timeout=1800)  # 30 min per chapter - stream-copy is fast, this is just a safety net
+            if result.returncode != 0:
+                raise RuntimeError(f"ffmpeg failed on chapter {ch.index}:\n{result.stderr}")
+            if not tmp_path.is_file() or tmp_path.stat().st_size == 0:
+                raise RuntimeError(
+                    f"ffmpeg reported success on chapter {ch.index} ({track_name}) "
+                    f"but produced no output file, or an empty one."
+                )
+            os.replace(tmp_path, out_path)
+        except Exception:
+            tmp_path.unlink(missing_ok=True)
+            raise
 
         output_paths.append(out_path)
 
@@ -430,6 +572,7 @@ def run_full_pipeline(
     container: str = "mkv",
     progress_cb: Optional[Callable[[str], None]] = None,
     cleanup_work_folder: bool = True,
+    overwrite: Optional[set[Path]] = None,
 ) -> list[Path]:
     """
     track_names: maps chapter index (1-based) -> song title.
@@ -440,6 +583,12 @@ def run_full_pipeline(
     finished successfully. If splitting raises, the work folder is left
     in place so the extraction doesn't have to be redone. Set to False to
     always keep the intermediate file around (e.g. for debugging).
+
+    overwrite: paths pre-approved for overwrite by the caller (see
+    preflight_check()). Extraction still runs even if this ends up wrong
+    (e.g. the disk changed since preflight) - the collision is caught by
+    split_chapters afterwards rather than skipped, so nothing gets
+    silently overwritten either way.
     """
     atmos_mkv = work_folder / "_atmos_extracted.mkv"
     extract_atmos_mkv(playlist, atmos_mkv, progress_cb=progress_cb)
@@ -450,7 +599,8 @@ def run_full_pipeline(
             ch.name = track_names[ch.index]
 
     results = split_chapters(
-        atmos_mkv, chapters, output_folder, container=container, progress_cb=progress_cb
+        atmos_mkv, chapters, output_folder, container=container,
+        progress_cb=progress_cb, overwrite=overwrite,
     )
 
     if cleanup_work_folder:
diff --git a/main.py b/main.py
index 1cf4b52..e93709c 100644
--- a/main.py
+++ b/main.py
@@ -438,6 +438,11 @@ class AtmosTrackSplitterApp(ctk.CTk):
             if var.get().strip()
         }
 
+        resolution = self._resolve_output_collisions(output_folder, track_names)
+        if resolution is None:
+            return  # user cancelled out of the collision dialog
+        output_folder, overwrite_paths = resolution
+
         self.extract_button.configure(state="disabled", text="Working...")
         self.set_status(f"Starting extraction -> {output_folder}")
 
@@ -454,13 +459,141 @@ class AtmosTrackSplitterApp(ctk.CTk):
                     output_folder=output_folder,
                     container=self.cfg.get("output_container", "mkv"),
                     progress_cb=progress,
+                    overwrite=overwrite_paths,
                 )
                 self.after(0, lambda: self._on_extraction_complete(results))
+            except extractor.OutputCollisionError as exc:
+                # Rare - preflight already checked - but the output folder
+                # could change on disk between preflight and the actual
+                # write (another process, another run). Handled the same
+                # as any other extraction failure: nothing overwritten,
+                # user told exactly what collided.
+                self.after(0, lambda: self._on_extraction_failed(exc))
             except Exception as exc:  # noqa: BLE001
                 self.after(0, lambda: self._on_extraction_failed(exc))
 
         threading.Thread(target=work, daemon=True).start()
 
+    def _resolve_output_collisions(
+        self, output_folder: Path, track_names: dict[int, str]
+    ) -> tuple[Path, set[Path]] | None:
+        """
+        Run the fast filename-planning check before starting the slow
+        extraction pipeline, and get explicit user confirmation for any
+        output file that would already exist. Returns
+        (output_folder, approved_overwrite_paths), where output_folder may
+        have been changed if the user picked a different one; or None if
+        the user cancelled.
+        """
+        chapter_count = self.selected_playlist.chapter_count
+        container = self.cfg.get("output_container", "mkv")
+
+        warned_duplicates = False
+
+        while True:
+            planned = extractor.preflight_check(
+                output_folder, chapter_count, track_names, container=container
+            )
+
+            duplicates = [p for p in planned if p.duplicate_name]
+            if duplicates and not warned_duplicates:
+                names = ", ".join(sorted({p.chapter.name for p in duplicates}))
+                proceed = messagebox.askyesno(
+                    "Duplicate track names",
+                    f"More than one chapter is named the same thing ({names}). "
+                    "This usually means a pasted tracklist got mis-aligned - "
+                    "the chapter numbers stay separate either way, but you may "
+                    "want to double check the names before continuing.\n\n"
+                    "Continue anyway?",
+                )
+                if not proceed:
+                    return None
+                warned_duplicates = True  # don't re-ask if they loop back (e.g. after choosing a folder)
+
+            colliding = [p for p in planned if p.exists]
+            if not colliding:
+                return output_folder, set()
+
+            choice = self._show_collision_dialog(output_folder, colliding)
+            if choice == "cancel":
+                return None
+            if choice == "choose_folder":
+                new_library_folder = filedialog.askdirectory(
+                    title="Select a different output folder"
+                )
+                if not new_library_folder:
+                    continue  # back to the same dialog, nothing changed
+                source_folder = Path(self.source_entry.get().strip())
+                album_name = extractor.derive_album_folder_name(source_folder)
+                output_folder = Path(new_library_folder) / album_name
+                self.output_entry.delete(0, "end")
+                self.output_entry.insert(0, new_library_folder)
+                continue
+            if choice == "overwrite":
+                return output_folder, {p.path for p in colliding}
+
+    def _show_collision_dialog(self, output_folder: Path, colliding: list) -> str:
+        """
+        Modal dialog listing output files that already exist. Nothing is
+        ever overwritten without the user explicitly choosing to here.
+        Returns "cancel", "choose_folder", or "overwrite".
+        """
+        result = {"choice": "cancel"}
+
+        dialog = ctk.CTkToplevel(self)
+        dialog.title("Output files already exist")
+        dialog.geometry("480x380")
+        dialog.transient(self)
+        dialog.grab_set()
+
+        ctk.CTkLabel(
+            dialog,
+            text=f"{len(colliding)} file(s) already exist in \"{output_folder.name}\":",
+            font=ctk.CTkFont(weight="bold"),
+            wraplength=440,
+            justify="left",
+        ).pack(padx=16, pady=(16, 8), anchor="w")
+
+        listbox = ctk.CTkTextbox(dialog, height=180)
+        listbox.pack(fill="both", expand=True, padx=16, pady=(0, 8))
+        listbox.insert("1.0", "\n".join(p.path.name for p in colliding))
+        listbox.configure(state="disabled")
+
+        ctk.CTkLabel(
+            dialog,
+            text="Nothing is overwritten automatically. Choose how to proceed:",
+            wraplength=440,
+            justify="left",
+        ).pack(padx=16, pady=(0, 8), anchor="w")
+
+        button_row = ctk.CTkFrame(dialog, fg_color="transparent")
+        button_row.pack(pady=(0, 16))
+
+        def pick(choice: str) -> None:
+            result["choice"] = choice
+            dialog.destroy()
+
+        ctk.CTkButton(
+            button_row, text="Cancel", width=100, command=lambda: pick("cancel")
+        ).pack(side="left", padx=6)
+        ctk.CTkButton(
+            button_row,
+            text="Choose another folder",
+            width=170,
+            command=lambda: pick("choose_folder"),
+        ).pack(side="left", padx=6)
+        ctk.CTkButton(
+            button_row,
+            text="Overwrite these files",
+            width=170,
+            fg_color="#a33",
+            hover_color="#822",
+            command=lambda: pick("overwrite"),
+        ).pack(side="left", padx=6)
+
+        dialog.wait_window()
+        return result["choice"]
+
     def _on_extraction_complete(self, results: list[Path]) -> None:
         self.extract_button.configure(state="normal", text="Extract && Split")
         self.set_status(f"Done. Wrote {len(results)} files to {self.output_entry.get()}")
