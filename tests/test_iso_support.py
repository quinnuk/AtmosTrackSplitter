from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

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

    def test_iso_extracts_bdmv_and_reuses_completed_staging(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            iso = root / "Test Album.iso"
            iso.write_bytes(b"fake image data")
            staging = root / "staging"

            def fake_run(command, **_kwargs):
                if command[1] == "l":
                    return CompletedProcess(
                        command, 0,
                        stdout=(
                            "Path = BDMV/PLAYLIST/00000.mpls\n"
                            "Path = BDMV/PLAYLIST/00001.mpls\n"
                        ),
                        stderr="",
                    )
                stage_arg = next(item for item in command if item.startswith("-o"))
                stage_folder = Path(stage_arg[2:])
                playlist = stage_folder / "BDMV" / "PLAYLIST"
                playlist.mkdir(parents=True)
                (playlist / "00000.mpls").write_bytes(b"playlist")
                return CompletedProcess(command, 0, stdout="", stderr="")

            with patch.object(iso_support, "_seven_zip", return_value="7z"), patch(
                "iso_support.subprocess.run", side_effect=fake_run
            ) as run:
                prepared = iso_support.prepare_iso(iso, staging)
                self.assertEqual(prepared, staging / iso_support.stage_directory_for(iso, staging).name)
                self.assertTrue((prepared / "BDMV" / "PLAYLIST" / "00000.mpls").is_file())
                run.reset_mock()
                reused = iso_support.prepare_iso(iso, staging)
                self.assertEqual(reused, prepared)
                run.assert_not_called()

    def test_iso_path_requires_a_real_iso_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            not_iso = root / "not-an-iso.mkv"
            not_iso.touch()
            with self.assertRaisesRegex(RuntimeError, "Not a readable ISO"):
                iso_support.prepare_iso(not_iso, root / "staging")


if __name__ == "__main__":
    unittest.main()
