from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import iso_support


class IsoSupportTests(unittest.TestCase):
    def test_find_disc_folder_direct_and_nested(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            direct = root / "direct"
            (direct / "BDMV" / "PLAYLIST").mkdir(parents=True)
            self.assertEqual(iso_support.find_disc_folder(direct), direct)

            nested = root / "torrent"
            nested_disc = nested / "disc-a"
            (nested_disc / "BDMV" / "PLAYLIST").mkdir(parents=True)
            self.assertEqual(iso_support.find_disc_folder(nested), nested_disc)

    def test_multiple_disc_folders_are_reported_as_ambiguous(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("disc-a", "disc-b"):
                (root / name / "BDMV" / "PLAYLIST").mkdir(parents=True)
            with self.assertRaisesRegex(RuntimeError, "More than one Blu-ray"):
                iso_support.find_disc_folder(root)

    def test_iso_path_requires_a_real_iso_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            not_iso = root / "not-an-iso.mkv"
            not_iso.touch()
            with self.assertRaisesRegex(RuntimeError, "Not a readable ISO"):
                iso_support.prepare_iso(not_iso, root / "staging")


if __name__ == "__main__":
    unittest.main()
