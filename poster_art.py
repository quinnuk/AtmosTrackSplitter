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

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

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


def create_portrait_poster(source_image: bytes | Path, output_path: Path) -> Path:
    """Create a 1000x1500 portrait JPEG without stretching or cropping the source cover.

    A softly blurred, darkened full-bleed version of the cover fills the
    portrait canvas. The original artwork is then placed intact in the centre.
    """
    if isinstance(source_image, Path):
        image = Image.open(source_image)
    else:
        image = Image.open(io.BytesIO(source_image))
    with image:
        cover = ImageOps.exif_transpose(image).convert("RGB")
        if cover.width < 100 or cover.height < 100:
            raise ValueError("The album cover is too small to make a clean poster image.")

        # Fill the portrait canvas with a subdued, blurred crop while keeping
        # the complete original artwork visible and undistorted in front.
        background = ImageOps.fit(
            cover, POSTER_SIZE, method=Image.Resampling.LANCZOS
        ).filter(ImageFilter.GaussianBlur(radius=38))
        background = ImageEnhance.Brightness(background).enhance(0.38)
        poster = background.convert("RGBA")

        foreground = ImageOps.contain(
            cover, (900, 1200), method=Image.Resampling.LANCZOS
        ).convert("RGBA")
        x = (POSTER_SIZE[0] - foreground.width) // 2
        y = (POSTER_SIZE[1] - foreground.height) // 2

        shadow = Image.new("RGBA", POSTER_SIZE, (0, 0, 0, 0))
        draw = ImageDraw.Draw(shadow)
        draw.rounded_rectangle(
            (x - 12, y - 12, x + foreground.width + 12, y + foreground.height + 12),
            radius=8,
            fill=(0, 0, 0, 190),
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=24))
        poster = Image.alpha_composite(poster, shadow)

        draw = ImageDraw.Draw(poster)
        draw.rectangle(
            (x - 2, y - 2, x + foreground.width + 1, y + foreground.height + 1),
            fill=(238, 238, 238, 255),
        )
        poster.alpha_composite(foreground, (x, y))

        output_path.parent.mkdir(parents=True, exist_ok=True)
        poster.convert("RGB").save(
            output_path, format="JPEG", quality=94, optimize=True, subsampling=0
        )
    return output_path


def create_album_poster(
    source_folder: Path,
    output_folder: Path,
    artist: str,
    album: str,
    year: str = "",
    musicbrainz_release_id: str = "",
) -> Optional[Path]:
    """Create Plex-ready 2:3 portrait artwork as folder.jpg."""
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

    create_portrait_poster(image_source, output_path)
    return output_path
