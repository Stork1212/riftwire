"""A saved URL becomes a correctly linked recording (url_download.acquire), run as a
durable ACQUIRE job (queue.enqueue_acquire, handlers.handle_acquire).

The platform downloader is replaced by a stand-in that writes a synthetic MP4 made by
FFmpeg; every other command (ffprobe, ffmpeg validation) runs for real. A live YouTube or
Twitch download is not exercised here."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.errors import RightsRefused
from app.main import create_app
from app.models import DiskReservation, Job, ProvenanceEvent, Recording, SystemEvent
from app.services import evidence, media, rights, url_download
from tests.conftest import requires_ffmpeg
from tests.helpers import import_file, legacy_public_web
from tests.test_ingest import drain
from tests.test_rights import ATTEST, FULL_SCOPE, World

REAL_WHICH = shutil.which
URL_SCOPE = {**FULL_SCOPE, "acquisition_methods": ["url_fetch", "creator_file"]}


class FakeDownloader:
    """Stands in for yt-dlp only; delegates every other command to the real runner."""

    def __init__(self, monkeypatch, clip: Path | None, *, returncode: int = 0,
                 after=None):
        self.calls: list[list[str]] = []
        self.clip, self.returncode, self.after = clip, returncode, after
        self.real = media.run_command
        monkeypatch.setattr(url_download.shutil, "which",
                            lambda b: f"/fake/{b}" if b in ("yt-dlp", "node")
                            else REAL_WHICH(b))
        monkeypatch.setattr(media, "run_command", self)

    @property
    def downloads(self) -> list[list[str]]:
        return [a for a in self.calls if "--version" not in a]

    def __call__(self, args, timeout, **kw):
        if not args[0].endswith("yt-dlp"):
            return self.real(args, timeout, **kw)
        self.calls.append(list(args))
        if "--version" in args:
            return media.CommandResult(0, b"2026.11.23\n", "")
        if self.clip is not None and self.returncode == 0:
            shutil.copyfile(self.clip, args[args.index("-o") + 1])
        if self.after:
            self.after()
        return media.CommandResult(self.returncode, b"", "")


@pytest.fixture
def w(db):
    return World(db)


@pytest.fixture
def client(db, settings):
    with TestClient(create_app(settings)) as c:
        yield c


def _permitted(w, db, settings, scope=URL_SCOPE) -> str:
    rid = w.licence(scope=scope)
    evidence.add_evidence(db, settings, rid, kind="platform_permission",
                          reference="Synthetic test permission")
    settings.url_acquisition_enabled = True
    return rid


def _nothing_stored(db, settings) -> None:
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Recording)) == 0
        assert s.scalar(select(func.count()).select_from(DiskReservation)) == 0
    assert list(settings.temp_dir.glob("source-download-*")) == []


def _acquire(db, settings, sid: str):
    return url_download.acquire(db, settings, sid)


def _source_detail(client, sid: str) -> dict:
    r = client.get(f"/api/v1/sources/{sid}")
    assert r.status_code == 200, r.text
    return r.json()


def _jobs(db) -> list[Job]:
    with db.session() as s:
        return list(s.scalars(select(Job).order_by(Job.created_at)))


# ------------------------------------------------------------------ refusals at the door

def test_refused_before_any_download_or_job(w, db, settings, client, monkeypatch,
                                            media_dir):
    fake = FakeDownloader(monkeypatch, media_dir / "basic.mp4")
    sid = w.source()
    url = f"/api/v1/sources/{sid}/acquire"

    settings.url_acquisition_enabled = False               # switched off
    r = client.post(url)
    assert r.status_code == 501 and r.json()["error"]["code"] == "acquisition_unavailable"
    settings.url_acquisition_enabled = True                # on, but nothing permits it
    r = client.post(url)
    assert r.status_code == 403
    assert r.json()["error"]["details"]["reason"] == rights.NO_RIGHTS_RECORD
    with db.session() as s:                                # a legacy public_web row
        legacy_public_web(s, sid)
    r = client.post(url)
    assert r.status_code == 403
    assert r.json()["error"]["details"]["reason"] == rights.CREATOR_PERMISSION_NEEDED
    local = w.source(kind="local", url=None, creator_channel_id=w.local)
    r = client.post(f"/api/v1/sources/{local}/acquire")    # nothing to fetch
    assert r.status_code == 422
    assert fake.calls == [] and _jobs(db) == []
    _nothing_stored(db, settings)


# ------------------------------------------------------------ the service (patch 1 proof)

@requires_ffmpeg
def test_saved_url_becomes_a_linked_recording(w, db, settings, monkeypatch, media_dir):
    _permitted(w, db, settings)
    sid = w.source()
    fake = FakeDownloader(monkeypatch, media_dir / "basic.mp4")

    res = _acquire(db, settings, sid)
    assert res.source_id == sid and res.already_acquired is False and res.job_id
    (args,) = fake.downloads                               # one download, canonical URL
    assert args[-1] == "https://clips.twitch.tv/BigHeistMoment-AbC_123"
    assert "--no-playlist" in args and "!is_live" in args
    drain(db, settings)                                    # INGEST, METADATA, THUMBNAIL
    with db.session() as s:
        rec = s.get(Recording, res.recording_id)
        assert rec.status == "READY" and rec.source_id == sid
        assert rec.acquisition_method == "url_fetch"
        assert rec.original_filename == "twitch-BigHeistMoment-AbC_123.mp4"
        assert rec.sha256 == _sha(media_dir / "basic.mp4")
        ev = {(e.entity, e.action): json.loads(e.details_json) for e in s.scalars(
            select(ProvenanceEvent).where(ProvenanceEvent.entity_id.in_([rec.id, sid])))}
        assert ev[("recording", "attached_to_source")]["method"] == "url_fetch"
        assert ev[("source", "media_attached")]["recording_id"] == rec.id
        got = ev[("source", "acquired_by_url")]
        assert got["url"] == "https://clips.twitch.tv/BigHeistMoment-AbC_123"
        assert got["downloader"] == "yt-dlp" and got["downloader_version"] == "2026.11.23"
        assert got["sha256"] == rec.sha256 and got["recording_id"] == rec.id
        assert rights.evaluate_recording(s, rec.id, "analyze").allowed
        assert s.scalar(select(func.count()).select_from(DiskReservation)) == 0
    assert list(settings.temp_dir.glob("source-download-*")) == []
    again = _acquire(db, settings, sid)                    # idempotent, no second download
    assert again.recording_id == res.recording_id and again.already_acquired
    assert len(fake.downloads) == 1


@requires_ffmpeg
def test_rights_revoked_during_download_discards_it(w, db, settings, monkeypatch, media_dir):
    rid = _permitted(w, db, settings)
    sid = w.source()

    def revoke():
        with db.session() as s:
            rights.revoke(s, rid, "withdrawn during download")
    FakeDownloader(monkeypatch, media_dir / "basic.mp4", after=revoke)
    with pytest.raises(RightsRefused) as e:
        _acquire(db, settings, sid)
    assert e.value.details["reason"] == rights.RIGHTS_CHANGED
    _nothing_stored(db, settings)


@requires_ffmpeg
def test_licence_without_url_fetch_refuses_analysis_of_fetched_media(
        w, db, settings, monkeypatch, media_dir):
    rid = _permitted(w, db, settings)
    sid = w.source()
    FakeDownloader(monkeypatch, media_dir / "basic.mp4")
    rec_id = _acquire(db, settings, sid).recording_id
    with db.session() as s:
        rights.supersede(s, rid, scope={**URL_SCOPE, "acquisition_methods": ["creator_file"]},
                         attestation_text=ATTEST)
    with db.session() as s:
        d = rights.evaluate_recording(s, rec_id, "analyze")
        assert not d.allowed and d.reason == rights.METHOD_NOT_PERMITTED


@requires_ffmpeg
def test_earlier_unlinked_import_of_the_same_bytes_is_linked_as_fetched(
        w, db, settings, monkeypatch, media_dir):
    _permitted(w, db, settings)
    sid = w.source()
    earlier = import_file(db, settings, media_dir / "basic.mp4").recording
    FakeDownloader(monkeypatch, media_dir / "basic.mp4")
    res = _acquire(db, settings, sid)
    assert res.recording_id == earlier.id and res.already_acquired
    with db.session() as s:
        rec = s.get(Recording, earlier.id)
        assert rec.source_id == sid and rec.acquisition_method == "url_fetch"
        assert s.scalar(select(func.count()).select_from(Recording)) == 1


# ------------------------------------------------------------------ the queue (patch 2)

@requires_ffmpeg
def test_acquisition_runs_as_a_queued_job(w, db, settings, client, monkeypatch, media_dir):
    _permitted(w, db, settings)
    sid = w.source()
    fake = FakeDownloader(monkeypatch, media_dir / "basic.mp4")

    r = client.post(f"/api/v1/sources/{sid}/acquire")
    assert r.status_code == 202, r.text
    job = r.json()["job"]
    assert job["job_type"] == "ACQUIRE" and job["status"] == "PENDING"
    assert job["source_id"] == sid and job["recording_id"] is None
    assert fake.downloads == []                            # nothing happens in the request
    again = client.post(f"/api/v1/sources/{sid}/acquire").json()
    assert again["job"]["id"] == job["id"]                 # one ACQUIRE job per source
    listed = client.get("/api/v1/jobs").json()["items"]
    assert [j["id"] for j in listed] == [job["id"]]        # visible before a recording exists
    assert _source_detail(client, sid)["acquire_job"]["status"] == "PENDING"

    drain(db, settings)                                    # ACQUIRE, INGEST, METADATA, THUMBNAIL
    detail = _source_detail(client, sid)
    acq = detail["acquire_job"]
    assert acq["status"] == "COMPLETED" and acq["progress_percent"] == 100
    (rec,) = detail["recordings"]
    assert acq["recording_id"] == rec["id"] and rec["acquisition_method"] == "url_fetch"
    assert rec["status"] == "READY"
    types = {j.job_type: j.status for j in _jobs(db)}
    assert types == {"ACQUIRE": "COMPLETED", "INGEST": "COMPLETED", "METADATA": "COMPLETED",
                     "THUMBNAIL": "COMPLETED"}
    done = client.post(f"/api/v1/sources/{sid}/acquire").json()
    assert done["recording"]["id"] == rec["id"] and len(fake.downloads) == 1
    with db.session() as s:
        evs = [e.event_type for e in s.scalars(select(SystemEvent))]
    assert "acquire_queued" in evs


@requires_ffmpeg
def test_network_failures_retry_then_succeed(w, db, settings, client, monkeypatch,
                                             media_dir):
    _permitted(w, db, settings)
    sid = w.source()
    fake = FakeDownloader(monkeypatch, media_dir / "basic.mp4", returncode=1)
    job_id = client.post(f"/api/v1/sources/{sid}/acquire").json()["job"]["id"]
    drain(db, settings)
    with db.session() as s:
        j = s.get(Job, job_id)
        assert j.status == "FAILED" and j.attempt_count == settings.job_max_attempts
        assert "download_unavailable" in j.last_error or "downloadable MP4" in j.last_error
    assert len(fake.downloads) == settings.job_max_attempts   # retried, not given up early
    _nothing_stored(db, settings)
    fake.returncode = 0                                    # the platform is back
    assert client.post(f"/api/v1/jobs/{job_id}/retry").status_code == 200
    drain(db, settings)
    with db.session() as s:
        j = s.get(Job, job_id)
        assert j.status == "COMPLETED" and j.recording_id is not None
        assert s.get(Recording, j.recording_id).acquisition_method == "url_fetch"


@requires_ffmpeg
def test_rights_revoked_while_queued_fails_permanently_with_provenance(
        w, db, settings, client, monkeypatch, media_dir):
    rid = _permitted(w, db, settings)
    sid = w.source()
    fake = FakeDownloader(monkeypatch, media_dir / "basic.mp4")
    job_id = client.post(f"/api/v1/sources/{sid}/acquire").json()["job"]["id"]
    with db.session() as s:
        rights.revoke(s, rid, "withdrawn while queued")
    drain(db, settings)
    with db.session() as s:
        j = s.get(Job, job_id)
        assert j.status == "FAILED" and j.attempt_count == 1      # never retried
        assert "RightsRefused" in j.last_error
        assert s.scalars(select(ProvenanceEvent).where(
            ProvenanceEvent.entity_id == job_id,
            ProvenanceEvent.action == "rights_refused")).one()
    assert fake.downloads == []                            # refused before downloading
    _nothing_stored(db, settings)


def test_missing_downloader_fails_permanently(w, db, settings, client, monkeypatch):
    _permitted(w, db, settings)
    sid = w.source()
    monkeypatch.setattr(url_download.shutil, "which",
                        lambda b: None if b == "yt-dlp" else REAL_WHICH(b))
    job_id = client.post(f"/api/v1/sources/{sid}/acquire").json()["job"]["id"]
    drain(db, settings)
    with db.session() as s:
        j = s.get(Job, job_id)
        assert j.status == "FAILED" and j.attempt_count == 1
        assert "downloader_missing" in j.last_error
    _nothing_stored(db, settings)


@requires_ffmpeg
def test_crashed_attempt_is_recovered_without_duplicates(w, db, settings, client,
                                                         monkeypatch, media_dir):
    """A previous attempt crashed after importing the bytes but before linking them, and
    left its workspace behind. The next attempt clears the workspace, recognises the
    identical bytes and links that recording: one recording, correctly attributed."""
    _permitted(w, db, settings)
    sid = w.source()
    job_id = client.post(f"/api/v1/sources/{sid}/acquire").json()["job"]["id"]
    stale = settings.temp_dir / f"source-download-{job_id}"
    stale.mkdir()
    (stale / "video.mp4.part").write_bytes(b"half a download")
    orphan = url_download._import(db, settings, media_dir / "basic.mp4",
                                  "twitch-BigHeistMoment-AbC_123.mp4").recording
    fake = FakeDownloader(monkeypatch, media_dir / "basic.mp4")
    drain(db, settings)
    with db.session() as s:
        j = s.get(Job, job_id)
        assert j.status == "COMPLETED" and j.recording_id == orphan.id
        assert s.scalar(select(func.count()).select_from(Recording)) == 1
        rec = s.get(Recording, orphan.id)
        assert rec.source_id == sid and rec.acquisition_method == "url_fetch"
    assert len(fake.downloads) == 1 and not stale.exists()


# ------------------------------------------------------------------- method integrity

def test_uploads_cannot_claim_url_fetch(w, db, settings, client):
    _permitted(w, db, settings)
    sid = w.source()
    r = client.post("/api/v1/recordings/import", content=b"x",
                    params={"filename": "x.mp4", "source_id": sid,
                            "acquisition_method": "url_fetch"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "url_fetch_reserved"
    methods = {m["method"]: m["available"] for m in
               client.get("/api/v1/acquisition-methods").json()}
    assert methods["url_fetch"] is True                    # switched on in this test
    settings.url_acquisition_enabled = False
    methods = {m["method"]: m["available"] for m in
               client.get("/api/v1/acquisition-methods").json()}
    assert methods["url_fetch"] is False


@requires_ffmpeg
def test_attach_endpoint_cannot_label_an_upload_as_fetched(w, db, settings, client,
                                                          media_dir):
    _permitted(w, db, settings)
    sid = w.source()
    rec = import_file(db, settings, media_dir / "basic.mp4").recording
    r = client.post(f"/api/v1/sources/{sid}/recordings",
                    json={"recording_id": rec.id, "acquisition_method": "url_fetch"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "url_fetch_reserved"
    with db.session() as s:
        assert s.get(Recording, rec.id).source_id is None


def _sha(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()
