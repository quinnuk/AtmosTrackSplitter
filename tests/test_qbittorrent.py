from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import qbittorrent


class _FakeQbittorrentHandler(BaseHTTPRequestHandler):
    observed_referer = ""
    observed_query = {}

    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.__class__.observed_referer = self.headers.get("Referer", "")
        length = int(self.headers.get("Content-Length", "0"))
        body = parse_qs(self.rfile.read(length).decode("utf-8"))
        if self.path == "/api/v2/auth/login" and body.get("username") == ["test"] and body.get("password") == ["secret"]:
            self.send_response(200)
            self.send_header("Set-Cookie", "SID=test-session; Path=/")
            self.end_headers()
            self.wfile.write(b"Ok.")
            return
        self.send_error(403)

    def do_GET(self):
        self.__class__.observed_referer = self.headers.get("Referer", "")
        parsed = urlparse(self.path)
        if parsed.path != "/api/v2/torrents/info" or "SID=test-session" not in self.headers.get("Cookie", ""):
            self.send_error(403)
            return
        self.__class__.observed_query = parse_qs(parsed.query)
        payload = json.dumps([{
            "name": "Test Artist - Test Album",
            "hash": "a" * 40,
            "content_path": "/downloads/audio-bluray/Test Artist - Test Album",
            "progress": 1.0,
            "amount_left": 0,
            "state": "uploading",
            "size": 123456,
        }]).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class QbittorrentTests(unittest.TestCase):
    def test_completed_downloads_are_mapped_to_read_only_rips_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            disc = root / "Test Artist - Test Album" / "Disc 1"
            playlist = disc / "BDMV" / "PLAYLIST"
            playlist.mkdir(parents=True)
            (playlist / "00000.mpls").write_bytes(b"test")

            server = HTTPServer(("127.0.0.1", 0), _FakeQbittorrentHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                result = qbittorrent.list_completed_disc_downloads(
                    base_url=url,
                    username="test",
                    password="secret",
                    category="audio-bluray",
                    content_root="/downloads/audio-bluray",
                    local_root=root,
                )
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].name, "Test Artist - Test Album")
            self.assertEqual(result[0].source_path, disc)
            self.assertEqual(result[0].download_root, root / "Test Artist - Test Album")
            self.assertEqual(_FakeQbittorrentHandler.observed_query.get("filter"), ["completed"])
            self.assertEqual(_FakeQbittorrentHandler.observed_query.get("category"), ["audio-bluray"])
            self.assertEqual(_FakeQbittorrentHandler.observed_referer, url + "/")

    def test_content_path_cannot_escape_mapped_root(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                qbittorrent._map_content_path(
                    "/downloads/other/secret.iso",
                    "/downloads/audio-bluray",
                    Path(temp),
                )

    def test_candidate_scan_finds_iso_and_nested_disc_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "concert.iso").touch()
            disc = root / "disc" / "rip"
            (disc / "BDMV" / "PLAYLIST").mkdir(parents=True)
            found = qbittorrent._find_sources(root)
            self.assertIn(root / "concert.iso", found)
            self.assertIn(disc, found)


if __name__ == "__main__":
    unittest.main()
