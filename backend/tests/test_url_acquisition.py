"""A saved URL becomes a correctly linked recording (url_download.acquire).

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

from app.main import create_app
from app.models import DiskReservation, Job, JobType, ProvenanceEvent, Recording
from app.services import evidence, media, rights, url_download
from tests.conftest import requires_ffmpeg
from tests.helpers import import_file
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


# ------------------------------------------------------------------------------ refusals

def test_refused_before_any_download(w, db, settings, client, monkeypatch, media_dir):
    fake = FakeDownloader(monkeypatch, media_dir / "basic.mp4")
    rid = w.licence(scope=URL_SCOPE)
    sid = w.source()
    url = f"/api/v1/sources/{sid}/acquire"

    r = client.post(url)                                   # switch off
    assert r.status_code == 501 and r.json()["error"]["code"] == "acquisition_unavailable"
    settings.url_acquisition_enabled = True
    r = client.post(url)                                   # no written platform permission
    assert r.status_code == 403
    assert r.json()["error"]["details"]["reason"] == rights.PLATFORM_PERMISSION_MISSING
    evidence.add_evidence(db, settings, rid, kind="platform_permission", reference="test")
    with db.session() as s:                                # licence without url_fetch
        rights.supersede(s, rid, scope=FULL_SCOPE, attestation_text=ATTEST)
    r = client.post(url)
    assert r.status_code == 403
    assert r.json()["error"]["details"]["reason"] == rights.METHOD_NOT_PERMITTED
    local = w.source(kind="local", url=None, creator_channel_id=w.local)
    r = client.post(f"/api/v1/sources/{local}/acquire")    # nothing to fetch
    assert r.status_code == 422
    assert fake.calls == []
    _nothing_stored(db, settings)


def test_missing_downloader_is_reported_and_nothing_is_stored(w, db, settings, client,
                                                             monkeypatch):
    _permitted(w, db, settings)
    sid = w.source()
    monkeypatch.setattr(url_download.shutil, "which",
                        lambda b: None if b == "yt-dlp" else REAL_WHICH(b))
    r = client.post(f"/api/v1/sources/{sid}/acquire")
    assert r.status_code == 503 and r.json()["error"]["code"] == "downloader_missing"
    _nothing_stored(db, settings)


@requires_ffmpeg
def test_failed_download_leaves_nothing(w, db, settings, client, monkeypatch, media_dir):
    _permitted(w, db, settings)
    sid = w.source()
    FakeDownloader(monkeypatch, media_dir / "basic.mp4", returncode=1)
    r = client.post(f"/api/v1/sources/{sid}/acquire")
    assert r.status_code == 503 and r.json()["error"]["code"] == "download_unavailable"
    _nothing_stored(db, settings)


@requires_ffmpeg
def test_rights_revoked_during_download_discards_it(w, db, settings, client, monkeypatch,
                                                   media_dir):
    rid = _permitted(w, db, settings)
    sid = w.source()

    def revoke():
        with db.session() as s:
            rights.revoke(s, rid, "withdrawn during download")
    FakeDownloader(monkeypatch, media_dir / "basic.mp4", after=revoke)
    r = client.post(f"/api/v1/sources/{sid}/acquire")
    assert r.status_code == 403
    assert r.json()["error"]["details"]["reason"] == rights.RIGHTS_CHANGED
    _nothing_stored(db, settings)


# ------------------------------------------------------------------------------ the proof

@requires_ffmpeg
def test_saved_url_becomes_a_linked_recording(w, db, settings, client, monkeypatch,
                                              media_dir):
    _permitted(w, db, settings)
    sid = w.source()
    fake = FakeDownloader(monkeypatch, media_dir / "basic.mp4")

    r = client.post(f"/api/v1/sources/{sid}/acquire")
    assert r.status_code == 200, r.text
    body = r.json()
    rec_id = body["recording"]["id"]
    assert body["source_id"] == sid and body["already_acquired"] is False and body["job_id"]
    # exactly one download, of the canonical URL, within the limits
    (args,) = fake.downloads
    assert args[-1] == "https://clips.twitch.tv/BigHeistMoment-AbC_123"
    assert "--no-playlist" in args and "!is_live" in args

    drain(db, settings)                                    # INGEST, METADATA, THUMBNAIL
    with db.session() as s:
        rec = s.get(Recording, rec_id)
        assert rec.status == "READY" and rec.source_id == sid
        assert rec.acquisition_method == "url_fetch"
        assert rec.original_filename == "twitch-BigHeistMoment-AbC_123.mp4"
        assert rec.sha256 == _sha(media_dir / "basic.mp4")
        jobs = {j.job_type: j.status for j in s.scalars(select(Job).where(
            Job.recording_id == rec_id))}
        assert jobs == {JobType.INGEST.value: "COMPLETED", JobType.METADATA.value: "COMPLETED",
                        JobType.THUMBNAIL.value: "COMPLETED"}
        # provenance: the recording, the source, and how it was obtained
        ev = {(e.entity, e.action): json.loads(e.details_json) for e in s.scalars(
            select(ProvenanceEvent).where(ProvenanceEvent.entity_id.in_([rec_id, sid])))}
        assert ev[("recording", "attached_to_source")]["method"] == "url_fetch"
        assert ev[("source", "media_attached")]["recording_id"] == rec_id
        got = ev[("source", "acquired_by_url")]
        assert got["url"] == "https://clips.twitch.tv/BigHeistMoment-AbC_123"
        assert got["downloader"] == "yt-dlp" and got["downloader_version"] == "2026.11.23"
        assert got["sha256"] == rec.sha256 and got["recording_id"] == rec_id
        # downstream rights are checked against the actual method
        assert rights.evaluate_recording(s, rec_id, "analyze").allowed
        assert s.scalar(select(func.count()).select_from(DiskReservation)) == 0
    assert list(settings.temp_dir.glob("source-download-*")) == []
    src = client.get(f"/api/v1/sources/{sid}").json()
    assert [x["id"] for x in src["recordings"]] == [rec_id]
    assert src["recordings"][0]["acquisition_method"] == "url_fetch"

    # idempotent: a second request returns the same recording without downloading again
    again = client.post(f"/api/v1/sources/{sid}/acquire").json()
    assert again["recording"]["id"] == rec_id and again["already_acquired"] is True
    assert len(fake.downloads) == 1


@requires_ffmpeg
def test_licence_without_url_fetch_refuses_analysis_of_fetched_media(
        w, db, settings, client, monkeypatch, media_dir):
    rid = _permitted(w, db, settings)
    sid = w.source()
    FakeDownloader(monkeypatch, media_dir / "basic.mp4")
    rec_id = client.post(f"/api/v1/sources/{sid}/acquire").json()["recording"]["id"]
    with db.session() as s:
        rights.supersede(s, rid, scope={**URL_SCOPE, "acquisition_methods": ["creator_file"]},
                         attestation_text=ATTEST)
    with db.session() as s:
        d = rights.evaluate_recording(s, rec_id, "analyze")
        assert not d.allowed and d.reason == rights.METHOD_NOT_PERMITTED


@requires_ffmpeg
def test_earlier_unlinked_import_of_the_same_bytes_is_linked_as_fetched(
        w, db, settings, client, monkeypatch, media_dir):
    _permitted(w, db, settings)
    sid = w.source()
    earlier = import_file(db, settings, media_dir / "basic.mp4").recording
    FakeDownloader(monkeypatch, media_dir / "basic.mp4")
    body = client.post(f"/api/v1/sources/{sid}/acquire").json()
    assert body["recording"]["id"] == earlier.id and body["already_acquired"] is True
    with db.session() as s:
        rec = s.get(Recording, earlier.id)
        assert rec.source_id == sid and rec.acquisition_method == "url_fetch"
        assert s.scalar(select(func.count()).select_from(Recording)) == 1


# ------------------------------------------------------------------- method integrity

def test_uploads_cannot_claim_url_fetch(w, db, settings, client):
    _permitted(w, db, settings)
    sid = w.source()
    r = client.post("/api/v1/recordings/import", content=b"x",
                    params={"filename": "x.mp4", "source_id": sid,
                            "acquisition_method": "url_fetch"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "url_fetch_reserved"
    r = client.post(f"/api/v1/sources/{sid}/recordings",
                    json={"recording_id": "00000000-0000-4000-8000-000000000000",
                          "acquisition_method": "url_fetch"})
    assert r.status_code in (404, 422)
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
