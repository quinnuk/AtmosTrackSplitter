"""Safe, read-only preparation of Blu-ray ISO files for the splitter.

7-Zip can read the UDF filesystem used by Blu-ray images. We extract only the
BDMV tree into a dedicated staging directory, so the rest of the application
can continue using its normal MPLS/STREAM folder workflow. The original ISO
is never modified.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


def find_disc_folder(path: Path, max_depth: int = 5) -> Path | None:
    """Find one unambiguous folder containing BDMV/PLAYLIST."""
    path = path.expanduser()
    if path.is_file():
        return None
    if (path / "BDMV" / "PLAYLIST").is_dir():
        return path
    if path.name.casefold() == "bdmv" and (path / "PLAYLIST").is_dir():
        return path.parent

    matches: list[Path] = []
    for current_text, dirs, _files in os.walk(path, followlinks=False):
        current = Path(current_text)
        depth = len(current.relative_to(path).parts)
        dirs[:] = [d for d in dirs if d != ".atmos-staging" and not (current / d).is_symlink()]
        if depth >= max_depth:
            dirs[:] = []
        if (current / "BDMV" / "PLAYLIST").is_dir():
            matches.append(current)
            dirs[:] = []
    if len(matches) > 1:
        raise RuntimeError(
            "More than one Blu-ray folder was found in this download. "
            "Choose the specific disc folder rather than the parent download folder."
        )
    return matches[0] if matches else None


def _seven_zip() -> str:
    from shutil import which
    tool = which("7z") or which("7zz")
    if not tool:
        raise RuntimeError("The 7-Zip command was not found. Rebuild the Docker image with the 7zip package.")
    return tool


def _iso_identity(iso_path: Path) -> dict[str, object]:
    stat = iso_path.stat()
    return {
        "source": str(iso_path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def prepare_iso(iso_path: Path, staging_root: Path) -> Path:
    """Extract the BDMV tree from a Blu-ray ISO to a reusable staging folder."""
    iso_path = iso_path.expanduser().resolve()
    if not iso_path.is_file() or iso_path.suffix.lower() != ".iso":
        raise RuntimeError(f"Not a readable ISO file: {iso_path}")

    identity = _iso_identity(iso_path)
    key = hashlib.sha256(
        f"{identity['source']}|{identity['size']}|{identity['mtime_ns']}".encode("utf-8")
    ).hexdigest()[:12]
    safe_stem = "".join(c if c.isalnum() or c in "-_ ." else "_" for c in iso_path.stem).strip(" .")[:70]
    stage = staging_root / f"{safe_stem or 'bluray'}-{key}"
    marker = stage / ".atmos-iso-source.json"

    if marker.is_file():
        try:
            if json.loads(marker.read_text(encoding="utf-8")) == identity:
                found = find_disc_folder(stage)
                if found is not None:
                    return found
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    if stage.exists():
        shutil.rmtree(stage)
    staging_root.mkdir(parents=True, exist_ok=True)

    free_bytes = shutil.disk_usage(staging_root).free
    required_bytes = iso_path.stat().st_size + 512 * 1024 * 1024
    if free_bytes < required_bytes:
        raise RuntimeError(
            f"Not enough free space to prepare this ISO. The image is "
            f"{iso_path.stat().st_size / (1024 ** 3):.1f} GiB and the staging "
            f"drive has only {free_bytes / (1024 ** 3):.1f} GiB free. "
            "The ISO is left untouched."
        )

    tool = _seven_zip()
    listing = subprocess.run(
        [tool, "l", "-slt", str(iso_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, timeout=180,
    )
    if listing.returncode not in (0, 1):
        raise RuntimeError(
            "7-Zip could not read this ISO. It may be incomplete, damaged, or use an unsupported image format.\n"
            + (listing.stderr or listing.stdout)[-3000:]
        )

    bdmv_roots: set[str] = set()
    for line in listing.stdout.splitlines():
        if not line.startswith("Path = "):
            continue
        value = line[7:].replace("\\", "/").lstrip("/")
        parts = Path(value).parts
        if parts and parts[0].casefold() == "bdmv":
            bdmv_roots.add(parts[0])
    if not bdmv_roots:
        raise RuntimeError("No BDMV folder was found inside this ISO. It may not be a Blu-ray disc image.")

    stage.mkdir(parents=True, exist_ok=True)
    command = [tool, "x", "-y", f"-o{stage}", str(iso_path), *sorted(bdmv_roots)]
    try:
        extracted = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=4 * 60 * 60,
        )
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(stage, ignore_errors=True)
        raise RuntimeError("Extracting the Blu-ray ISO timed out after four hours.") from exc

    found = find_disc_folder(stage)
    if extracted.returncode not in (0, 1) or found is None:
        detail = (extracted.stderr or extracted.stdout)[-4000:]
        shutil.rmtree(stage, ignore_errors=True)
        raise RuntimeError(
            "7-Zip could not extract a usable BDMV/PLAYLIST structure from this ISO.\n"
            + detail
        )
    marker.write_text(json.dumps(identity, indent=2), encoding="utf-8")
    return found


def stage_directory_for(iso_path: Path, staging_root: Path) -> Path:
    """Return the deterministic staging directory used for an ISO."""
    identity = _iso_identity(iso_path.resolve())
    key = hashlib.sha256(
        f"{identity['source']}|{identity['size']}|{identity['mtime_ns']}".encode("utf-8")
    ).hexdigest()[:12]
    safe_stem = "".join(c if c.isalnum() or c in "-_ ." else "_" for c in iso_path.stem).strip(" .")[:70]
    return staging_root / f"{safe_stem or 'bluray'}-{key}"
