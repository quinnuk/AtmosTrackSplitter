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


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                pass
    return ImageFont.load_default()


def _fit_text(draw: ImageDraw.ImageDraw, text: str, max_width: int, start_size: int,
              bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    size = start_size
    while size > 18:
        font = _font(size, bold)
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
            return font
        size -= 2
    return _font(size, bold)


def create_plex_poster(
    source_image: bytes | Path,
    output_path: Path,
    artist: str,
    album: str,
    year: str = "",
) -> Path:
    """Create a 1000x1500 JPEG poster while preserving the whole source cover."""
    if isinstance(source_image, Path):
        image = Image.open(source_image)
    else:
        image = Image.open(io.BytesIO(source_image))
    with image:
        cover = ImageOps.exif_transpose(image).convert("RGB")
        if cover.width < 100 or cover.height < 100:
            raise ValueError("The album cover is too small to make a clean poster.")

        canvas = ImageOps.fit(cover, POSTER_SIZE, method=Image.Resampling.LANCZOS)
        canvas = canvas.filter(ImageFilter.GaussianBlur(radius=34))
        shade = Image.new("RGBA", POSTER_SIZE, (8, 10, 16, 150))
        canvas = Image.alpha_composite(canvas.convert("RGBA"), shade)
        draw = ImageDraw.Draw(canvas)

        artist_text = (artist or "Unknown Artist").strip().upper()
        artist_font = _fit_text(draw, artist_text, 880, 48, bold=True)
        artist_box = draw.textbbox((0, 0), artist_text, font=artist_font)
        artist_y = 112
        draw.text(((1000 - (artist_box[2] - artist_box[0])) / 2, artist_y),
                  artist_text, font=artist_font, fill=(255, 255, 255, 245))

        # Display the original cover whole; never stretch or crop it.
        cover.thumbnail((860, 860), Image.Resampling.LANCZOS)
        cover_x = (1000 - cover.width) // 2
        cover_y = 300 + (860 - cover.height) // 2

        shadow_layer = Image.new("RGBA", POSTER_SIZE, (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow_layer)
        shadow_draw.rounded_rectangle(
            (cover_x - 14, cover_y - 14, cover_x + cover.width + 14, cover_y + cover.height + 14),
            radius=10, fill=(0, 0, 0, 150)
        )
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(22))
        canvas = Image.alpha_composite(canvas, shadow_layer)
        canvas.alpha_composite(cover.convert("RGBA"), (cover_x, cover_y))

        draw = ImageDraw.Draw(canvas)
        album_text = (album or "Album").strip()
        album_font = _fit_text(draw, album_text, 900, 56, bold=True)
        album_box = draw.textbbox((0, 0), album_text, font=album_font)
        draw.text(((1000 - (album_box[2] - album_box[0])) / 2, 1215),
                  album_text, font=album_font, fill=(255, 255, 255, 255))

        footer = "DOLBY ATMOS  •  BLU-RAY AUDIO"
        if year:
            footer = f"{year}  •  {footer}"
        footer_font = _font(25, bold=False)
        footer_box = draw.textbbox((0, 0), footer, font=footer_font)
        draw.text(((1000 - (footer_box[2] - footer_box[0])) / 2, 1370),
                  footer, font=footer_font, fill=(235, 238, 244, 235))

        output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.convert("RGB").save(output_path, format="JPEG", quality=94, optimize=True, subsampling=0)
    return output_path


def create_square_cover(source_image: bytes | Path, output_path: Path) -> Path:
    """Create a 1000x1000 JPEG cover for Plex Music album artwork."""
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
    """Prefer rip-local cover art, then the exact MusicBrainz release's front cover."""
    output_path = output_folder / "poster.jpg"
    square_path = output_folder / "cover.jpg"
    if output_path.exists() and square_path.exists():
        return output_path

    local_cover = find_local_cover(source_folder)
    image_source: bytes | Path | None = local_cover
    if image_source is None:
        image_source = fetch_musicbrainz_front_cover(musicbrainz_release_id)
    if image_source is None:
        return output_path if output_path.exists() else None

    output_folder.mkdir(parents=True, exist_ok=True)
    if not square_path.exists():
        create_square_cover(image_source, square_path)
    if not output_path.exists():
        create_plex_poster(image_source, output_path, artist, album, year)
    return output_path
