"""
Poster artwork helpers for the Atmos Track Splitter.

The source cover is kept intact. A portrait Plex poster is composed around it
rather than stretching or cropping the original album artwork.
"""
from __future__ import annotations

import io
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

POSTER_SIZE = (1000, 1500)
USER_AGENT = "AtmosTrackSplitter/1.0 (+https://github.com/quinnuk/AtmosTrackSplitter)"
LOCAL_COVER_NAMES = (
    "cover.jpg", "cover.jpeg", "cover.png",
    "folder.jpg", "folder.jpeg", "folder.png",
    "front.jpg", "front.jpeg", "front.png",
    "album.jpg", "album.jpeg", "album.png",
)


def find_local_cover(source_folder: Path) -> Optional[Path]:
    """Find a likely front-cover image in the rip's top-level folder."""
    for name in LOCAL_COVER_NAMES:
        candidate = source_folder / name
        if candidate.is_file():
            return candidate
    return None


def fetch_musicbrainz_front_cover(release_id: str) -> Optional[bytes]:
    """Download the community-selected front image for an exact MusicBrainz release."""
    if not release_id:
        return None
    url = f"https://coverartarchive.org/release/{release_id}/front"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            payload = response.read(30 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError(f"Cover Art Archive returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Cover Art Archive: {exc.reason}") from exc

    if len(payload) > 30 * 1024 * 1024:
        raise RuntimeError("The album artwork was unexpectedly large (over 30 MB).")
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image.verify()
    except Exception as exc:
        raise RuntimeError("Cover Art Archive returned an invalid image.") from exc
    return payload


def create_square_cover(source_image: bytes | Path, output_path: Path) -> Path:
    """Create a 1000x1000 JPEG, matching Plex's square album-art convention."""
    if isinstance(source_image, Path):
        image = Image.open(source_image)
    else:
        image = Image.open(io.BytesIO(source_image))
    with image:
        cover = ImageOps.exif_transpose(image).convert("RGB")
        if cover.width < 100 or cover.height < 100:
            raise ValueError("The album cover is too small to make a clean cover image.")
        square = ImageOps.fit(cover, (1000, 1000), method=Image.Resampling.LANCZOS)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        square.save(output_path, format="JPEG", quality=94, optimize=True, subsampling=0)
    return output_path


def create_album_poster(
    source_folder: Path,
    output_folder: Path,
    artist: str,
    album: str,
    year: str = "",
    musicbrainz_release_id: str = "",
) -> Optional[Path]:
    """Create Plex-ready square album artwork as folder.jpg."""
    output_path = output_folder / "folder.jpg"

    # Never replace artwork that is already present in the destination.
    if output_path.exists():
        return output_path

    local_cover = find_local_cover(source_folder)
    image_source: bytes | Path | None = local_cover
    if image_source is None:
        image_source = fetch_musicbrainz_front_cover(musicbrainz_release_id)
    if image_source is None:
        return None

    create_square_cover(image_source, output_path)
    return output_path
