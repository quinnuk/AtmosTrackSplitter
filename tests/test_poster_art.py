from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from PIL import Image

import poster_art


class PosterArtTests(unittest.TestCase):
    def test_creates_square_folder_jpg_from_local_cover(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "rip"
            output = root / "music-videos" / "Artist - Album"
            source.mkdir()
            Image.new("RGB", (1200, 800), (30, 80, 120)).save(source / "cover.jpg")
            result = poster_art.create_album_poster(source, output, "Artist", "Album")
            self.assertIsNotNone(result)
            self.assertEqual(result.name, "folder.jpg")
            with Image.open(result) as image:
                self.assertEqual(image.size, (1000, 1000))
                self.assertEqual(image.format, "JPEG")

    def test_existing_folder_art_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "rip"
            output = root / "music-videos" / "Artist - Album"
            source.mkdir()
            output.mkdir(parents=True)
            existing = output / "folder.jpg"
            Image.new("RGB", (500, 500), (200, 10, 10)).save(existing)
            original = existing.read_bytes()
            Image.new("RGB", (800, 800), (10, 200, 10)).save(source / "cover.jpg")
            result = poster_art.create_album_poster(source, output, "Artist", "Album")
            self.assertEqual(result, existing)
            self.assertEqual(existing.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
