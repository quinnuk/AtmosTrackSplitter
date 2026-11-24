from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_TMP = Path(tempfile.mkdtemp(prefix="atmos-web-tests-"))
RIPS = _TMP / "rips"
OUTPUT = _TMP / "out"
STAGING = _TMP / "staging"
(RIPS / "Band" / "Live (2020)" / "BDMV" / "PLAYLIST").mkdir(parents=True)
OUTPUT.mkdir()
STAGING.mkdir()

os.environ["RIPS_DIR"] = str(RIPS)
os.environ["OUTPUT_DIR"] = str(OUTPUT)
os.environ["STAGING_DIR"] = str(STAGING)
os.environ["AUTH_USERNAME"] = "test-user"
os.environ["AUTH_PASSWORD"] = "test-password"
os.environ["HOME"] = str(_TMP)

import webapp  # noqa: E402

client = webapp.app.test_client()
_AUTH = "Basic " + base64.b64encode(b"test-user:test-password").decode("ascii")
_HEADERS = {"Authorization": _AUTH}


def test_health_endpoint_does_not_require_credentials():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


def test_application_requires_basic_authentication():
    response = client.get("/api/config")
    assert response.status_code == 401
    assert "Basic" in response.headers["WWW-Authenticate"]


def test_explicit_lan_mode_allows_browser_use_without_credentials():
    with patch.object(webapp, "AUTH_DISABLED", True):
        response = client.get("/api/config")
    assert response.status_code == 200


def test_authenticated_browse_and_path_jail():
    response = client.get("/api/browse", headers=_HEADERS)
    assert response.status_code == 200
    assert response.get_json()["entries"][0]["name"] == "Band"

    outside = client.get("/api/browse?path=%2Fetc", headers=_HEADERS)
    assert outside.status_code == 400

    traversal = client.get(
        "/api/browse?path=" + str(RIPS / ".." / "out"),
        headers=_HEADERS,
    )
    assert traversal.status_code == 400


def test_output_name_cannot_escape():
    assert webapp._output_dir("..").name == "Untitled"
    assert webapp._output_dir("../../etc").parent == webapp.OUTPUT_ROOT


def test_browser_index_is_served_after_login():
    response = client.get("/", headers=_HEADERS)
    assert response.status_code == 200
    assert b"Atmos Track Splitter" in response.data
    assert b"Suggested titles are not applied automatically" in response.data



def test_browser_templates_use_javascript_interpolation():
    html = (Path(webapp.app.static_folder) / "index.html").read_text(encoding="utf-8")
    assert r"\${" not in html
    assert "${esc(e.name)}" in html
    assert "${esc(d.name)}" in html


def test_unattended_auto_endpoint_is_not_exposed():
    response = client.post("/api/auto", json={"path": str(RIPS)}, headers=_HEADERS)
    assert response.status_code == 404

def test_completed_download_list_is_read_only_and_displayed():
    item = SimpleNamespace(
        name="Test Artist - Test Album",
        kind="Blu-ray folder",
        source_path=RIPS / "Band" / "Live (2020)",
        download_root=RIPS / "Band" / "Live (2020)",
        torrent_hash="a" * 40,
        size_bytes=123456,
    )
    with patch.object(webapp.qbittorrent, "list_completed_disc_downloads", return_value=[item]):
        response = client.get("/api/completed-downloads", headers=_HEADERS)
    assert response.status_code == 200
    assert response.get_json()["downloads"][0]["name"] == item.name


def test_completed_bdmv_folder_can_be_selected():
    response = client.post(
        "/api/select-download",
        headers=_HEADERS,
        json={
            "path": str(RIPS / "Band" / "Live (2020)"),
            "download_root": str(RIPS / "Band" / "Live (2020)"),
            "name": "Band - Live (2020)",
        },
    )
    assert response.status_code == 200
    result = response.get_json()
    assert result["path"] == str(RIPS / "Band" / "Live (2020)")
    assert result["album_name_hint"] == "Band - Live (2020)"


def test_completed_download_selection_cannot_escape_rips():
    response = client.post(
        "/api/select-download",
        headers=_HEADERS,
        json={
            "path": "/etc/passwd.iso",
            "download_root": str(RIPS),
            "name": "bad",
        },
    )
    assert response.status_code == 400

def test_iso_selection_returns_background_preparation_job():
    iso_file = RIPS / "Test Album.iso"
    iso_file.write_bytes(b"fake ISO bytes")
    prepared = STAGING / "Test Album-staged"
    prepared.mkdir()
    def run_inline(job, fn):
        fn()
        if job["status"] == "running":
            job["status"] = "done"

    with patch.object(webapp.iso_support, "prepare_iso", return_value=prepared), \
         patch.object(webapp, "_thread", side_effect=run_inline):
        response = client.post(
            "/api/select-download",
            headers=_HEADERS,
            json={
                "path": str(iso_file),
                "download_root": str(RIPS),
                "name": "Test Artist - Test Album.iso",
            },
        )
    assert response.status_code == 200
    result = response.get_json()
    assert result["preparing"] is True
    job = client.get("/api/jobs/" + result["job_id"], headers=_HEADERS).get_json()
    assert job["status"] == "done"
    assert job["prepared_download"]["path"] == str(prepared)
    assert job["prepared_download"]["album_name_hint"] == "Test Artist - Test Album.iso"
