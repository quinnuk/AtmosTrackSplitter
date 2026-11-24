from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path

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


def test_unattended_auto_endpoint_is_not_exposed():
    response = client.post("/api/auto", json={"path": str(RIPS)}, headers=_HEADERS)
    assert response.status_code == 404
