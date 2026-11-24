from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import queue
from app.services.system import clear_tool_cache
from tests.conftest import requires_ffmpeg
from tests.test_ingest import drain

pytestmark = requires_ffmpeg


@pytest.fixture
def client(db, settings):
    clear_tool_cache()
    with TestClient(create_app(settings)) as c:
        yield c


def upload(client, path, name=None):
    return client.post("/api/v1/recordings/import", params={"filename": name or path.name},
                       content=path.read_bytes(),
                       headers={"Content-Type": "application/octet-stream"})


def assert_error(r, status, code=None):
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == {"error"} and {"code", "message", "details"} <= set(body["error"])
    if code:
        assert body["error"]["code"] == code
    return body["error"]


# ------------------------------------------------------------------ health

def test_health_liveness(client):
    assert client.get("/health").json()["status"] == "ok"


def test_readiness_ok(client):
    r = client.get("/health/ready")
    assert r.status_code == 200 and r.json()["ready"] is True
    assert set(r.json()["checks"]) == {"database", "ffmpeg", "ffprobe", "data_dirs"}


def test_readiness_unmigrated_is_503_but_liveness_ok(settings):
    with TestClient(create_app(settings)) as c:
        assert c.get("/health").status_code == 200
        r = c.get("/health/ready")
        assert r.status_code == 503 and r.json()["checks"]["database"]["ok"] is False
        assert_error(c.get("/api/v1/recordings"), 503, "schema_not_ready")


def test_readiness_reports_missing_ffmpeg(db, settings):
    settings.ffmpeg_path = "/nonexistent/ffmpeg"
    clear_tool_cache()
    with TestClient(create_app(settings)) as c:
        r = c.get("/health/ready")
        assert r.status_code == 503 and r.json()["checks"]["ffmpeg"]["ok"] is False
    clear_tool_cache()


# ------------------------------------------------------------------ import

def test_import_flow_and_detail(client, db, settings, media_dir):
    r = upload(client, media_dir / "basic.mp4", "clip one.mp4")
    assert r.status_code == 201, r.text
    rec = r.json()["recording"]
    assert rec["status"] == "STAGED" and rec["original_filename"] == "clip one.mp4"
    assert "file_path" not in rec and "/" not in rec["stored_filename"]

    # Thumbnail placeholder before processing.
    t = client.get(rec["thumbnail_url"])
    assert t.status_code == 200 and t.headers["content-type"].startswith("image/svg")

    drain(db, settings)
    d = client.get(f"/api/v1/recordings/{rec['id']}").json()
    assert d["status"] == "READY" and d["fps_rational"] == "30000/1001"
    assert d["width"] == 320 and d["has_audio"] is True
    assert [j["status"] for j in d["jobs"]] == ["COMPLETED"] * 3
    t = client.get(rec["thumbnail_url"])
    assert t.headers["content-type"] == "image/jpeg" and t.content[:3] == b"\xff\xd8\xff"


def test_import_duplicate_409(client, media_dir):
    first = upload(client, media_dir / "basic.mp4").json()["recording"]["id"]
    err = assert_error(upload(client, media_dir / "basic.mp4", "again.mp4"), 409,
                       "duplicate_recording")
    assert err["details"]["existing_recording_id"] == first


@pytest.mark.parametrize("name,status,code", [
    ("garbage.mp4", 422, "invalid_media"),
    ("truncated.mp4", 422, "invalid_media"),
    ("audio_only.mp4", 422, "invalid_media"),
    ("image.png", 415, "unsupported_media"),
])
def test_import_rejections(client, settings, media_dir, name, status, code):
    err = assert_error(upload(client, media_dir / name), status, code)
    assert err["message"]
    assert str(settings.data_dir) not in err["message"]  # no server paths leaked
    assert "/tmp" not in err["message"] and ".part" not in err["message"]
    assert list(settings.temp_dir.iterdir()) == []


def test_import_too_large(client, settings, media_dir):
    settings.max_upload_bytes = 100
    assert_error(upload(client, media_dir / "basic.mp4"), 413, "payload_too_large")


def test_import_insufficient_space(client, settings, media_dir):
    settings.min_free_disk_bytes = 10**18
    err = assert_error(upload(client, media_dir / "basic.mp4"), 507, "insufficient_storage")
    assert "GiB" in err["message"]


def test_import_requires_filename(client, media_dir):
    r = client.post("/api/v1/recordings/import", content=b"x")
    assert_error(r, 422, "request_validation")


def test_import_empty_body(client):
    r = client.post("/api/v1/recordings/import", params={"filename": "a.mp4"}, content=b"")
    assert r.status_code == 422


# ------------------------------------------------------------------ lists / pagination

def test_recordings_pagination_and_search(client, media_dir):
    for n in ("basic.mp4", "basic.mov", "basic.mkv", "noaudio.mp4", "short.mp4"):
        assert upload(client, media_dir / n, f"gta-{n}").status_code == 201
    p1 = client.get("/api/v1/recordings", params={"limit": 2}).json()
    p3 = client.get("/api/v1/recordings", params={"limit": 2, "offset": 4}).json()
    assert p1["total"] == 5 and len(p1["items"]) == 2 and len(p3["items"]) == 1
    ids = {i["id"] for i in p1["items"]} | {i["id"] for i in p3["items"]}
    assert len(ids) == 3
    s = client.get("/api/v1/recordings", params={"q": "noaudio"}).json()
    assert s["total"] == 1 and s["items"][0]["original_filename"] == "gta-noaudio.mp4"
    assert client.get("/api/v1/recordings", params={"status": "staged"}).json()["total"] == 5


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 1000}, {"offset": -1},
                                    {"limit": "abc"}])
def test_pagination_validation(client, params):
    assert_error(client.get("/api/v1/recordings", params=params), 422, "request_validation")
    assert_error(client.get("/api/v1/jobs", params=params), 422)
    assert_error(client.get("/api/v1/events", params=params), 422)


def test_jobs_list_filter_and_detail(client, media_dir):
    upload(client, media_dir / "basic.mp4")
    jobs = client.get("/api/v1/jobs").json()
    assert jobs["total"] == 1 and jobs["items"][0]["job_type"] == "INGEST"
    assert jobs["items"][0]["recording_filename"] == "basic.mp4"
    jid = jobs["items"][0]["id"]
    assert client.get(f"/api/v1/jobs/{jid}").json()["status"] == "PENDING"
    assert client.get("/api/v1/jobs", params={"status": "failed"}).json()["total"] == 0


def test_job_retry_endpoint(client, db, media_dir):
    upload(client, media_dir / "basic.mp4")
    jid = client.get("/api/v1/jobs").json()["items"][0]["id"]
    assert_error(client.post(f"/api/v1/jobs/{jid}/retry"), 409, "not_retryable")
    c = queue.claim_next(db, "w", 30)
    queue.fail(db, c, "x", permanent=True, backoff_base=0, backoff_cap=0)
    j = client.get(f"/api/v1/jobs/{jid}").json()
    assert j["status"] == "FAILED" and j["retryable"] is True
    r = client.post(f"/api/v1/jobs/{jid}/retry")
    assert r.status_code == 200 and r.json()["status"] == "PENDING"


def test_not_found(client):
    assert_error(client.get("/api/v1/recordings/nope"), 404, "not_found")
    assert_error(client.get("/api/v1/jobs/nope"), 404, "not_found")
    assert_error(client.get("/api/v1/recordings/nope/thumbnail"), 404, "not_found")
    assert_error(client.post("/api/v1/jobs/nope/retry"), 404, "not_found")
    assert_error(client.get("/api/v1/does-not-exist"), 404)


def test_events_and_system_status(client, media_dir):
    upload(client, media_dir / "basic.mp4")
    upload(client, media_dir / "basic.mp4")  # duplicate is not an event, still fine
    ev = client.get("/api/v1/events").json()
    assert ev["total"] >= 2  # api_started + recording_imported
    assert {e["event_type"] for e in ev["items"]} >= {"api_started", "recording_imported"}
    st = client.get("/api/v1/system/status").json()
    assert st["version"] == "0.1.0" and st["backend"]["status"] == "ok"
    assert st["ffmpeg"]["available"] is True
    assert st["counts"]["recordings"] == 1 and st["counts"]["jobs"]["PENDING"] == 1
    assert st["worker"]["state"] == "NEVER_STARTED"
    assert st["storage"]["free_bytes"] > 0 and "importable_bytes" in st["storage"]


def test_cors_restricted(client):
    ok = client.options("/api/v1/recordings", headers={
        "Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    bad = client.get("/api/v1/recordings", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in bad.headers
