"""M1 storage architecture: locations, volume identity, offline safety, persistent
reservations, crash-safe cross-volume transfer and verified migration.

A real second filesystem is used where available (``/dev/shm``, a separate tmpfs mount)
so cross-volume paths run for real. macOS-specific behaviour (APFS VolumeUUID, exFAT) is
exercised through an injected volume probe.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import threading
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.config import GIB, Settings
from app.core.errors import InsufficientStorage, StorageOffline, StorageOfflineJobError
from app.db.base import utcnow
from app.db.session import Database
from app.models import (
    DiskReservation,
    Job,
    JobStatus,
    Recording,
    StorageLocation,
    StorageMigration,
    StorageMigrationItem,
    SystemEvent,
)
from app.services import locations, migration, queue, reservations, transfer, volumes
from app.services.ingest import UploadSession, reconcile
from app.services.locations import MARKER, get_layout, load_layout
from app.workers.handlers import JobContext, handle_ingest
from app.workers.worker import WorkerProcess
from tests.conftest import requires_ffmpeg
from tests.helpers import import_file, user_files
from tests.test_ingest import drain, get_rec, sha_file

SHM = Path("/dev/shm")
# Simulated drives carry real-format OS volume UUIDs (required on macOS).
DATA_UUID = "8EC78228-715D-4039-B15E-0F6A8CA34C02"
SSD_ONE = "11111111-1111-4111-8111-111111111111"
SSD_TWO = "22222222-2222-4222-8222-222222222222"
EXFAT_1 = "33333333-3333-4333-8333-333333333333"


def second_volume(tmp_path) -> Path | None:
    """A directory on a different filesystem than tmp_path, or None."""
    if not SHM.is_dir() or not os.access(SHM, os.W_OK):
        return None
    d = SHM / f"rw-m1-{uuid.uuid4().hex[:8]}"
    d.mkdir()
    if os.stat(d).st_dev == os.stat(tmp_path).st_dev:
        shutil.rmtree(d)
        return None
    return d


@pytest.fixture
def vol2(tmp_path):
    d = second_volume(tmp_path)
    if d is None:
        pytest.skip("no second filesystem available")
    yield d
    shutil.rmtree(d, ignore_errors=True)


class FakeProbe(volumes.VolumeProbe):
    """Simulates extra drives: maps path prefixes to (fs_type, hw_uuid). Every other path
    is answered by the real platform probe, so the actual internal disk keeps its real
    identity (on macOS its real VolumeUUID was recorded when the layout was loaded)."""

    def __init__(self):
        self.real = volumes.default_probe()
        # Same identity policy as the real platform (on macOS: OS volume UUID required).
        self.requires_hw_uuid = self.real.requires_hw_uuid
        self.rules: list[tuple[str, tuple[str, str | None]]] = []

    def set(self, prefix: Path, fs: str, hw: str | None) -> None:
        self.rules.insert(0, (str(prefix), (fs, hw)))

    def info(self, path: Path) -> volumes.VolumeInfo:
        p = str(path)
        for prefix, (fs, hw) in self.rules:
            if p == prefix or p.startswith(prefix.rstrip("/") + "/"):
                return volumes.VolumeInfo(prefix, fs, hw, "TestDrive")
        return self.real.info(path)


@pytest.fixture
def probe():
    fp = FakeProbe()
    volumes.set_probe(fp)
    yield fp
    volumes.set_probe(None)


def count(db, model, *where):
    with db.session() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def events(db, event_type):
    with db.session() as s:
        return list(s.scalars(select(SystemEvent.message).where(
            SystemEvent.event_type == event_type)))


# =========================================================================== locations

def test_defaults_registered_with_markers_once(db, settings):
    lay = get_layout(settings)
    assert set(lay.locations) == {"recordings", "temp", "models", "exports", "assets"}
    vols = {loc.volume_uuid for loc in lay.locations.values()}
    assert len(vols) == 1  # all on the internal disk: one volume
    for kind, loc in lay.locations.items():
        assert loc.path == settings.data_dir / kind
        m = json.loads((loc.path / MARKER).read_text())
        assert m["location_uuid"] == loc.location_uuid and m["kind"] == kind
        assert lay.state(kind).online
    n_events = len(events(db, "storage_location_registered"))
    load_layout(db, settings, "again")
    load_layout(db, settings, "again")
    assert count(db, StorageLocation) == 5
    assert len(events(db, "storage_location_registered")) == n_events == 5


def _marker(kind="temp", **kw):
    m = {"format": 1, "kind": kind, "location_uuid": str(uuid.uuid4()),
         "volume_uuid": str(uuid.uuid4()), "hw_volume_uuid": None,
         "created_at": utcnow().isoformat()}
    m.update(kw)
    return m


def test_marker_is_never_overwritten(tmp_path):
    a, b = _marker(), _marker()
    locations.write_marker(tmp_path, a)
    with pytest.raises(FileExistsError):
        locations.write_marker(tmp_path, b)
    assert json.loads((tmp_path / MARKER).read_text())["location_uuid"] == a["location_uuid"]
    assert [p.name for p in tmp_path.iterdir()] == [MARKER]  # no temp files left


def _external(db, settings, kind: str, root: Path, hw=None, fs="apfs"):
    """Register ``kind`` at ``root`` as the migration tool would (root must exist)."""
    with db.session() as s:
        rows = list(s.scalars(select(StorageLocation)))
        row = s.get(StorageLocation, kind)
        new = locations.register_root([r for r in rows if r.kind != kind], kind, root,
                                      settings.min_free_disk_bytes)
        for f in ("path", "location_uuid", "volume_uuid", "hw_volume_uuid", "fs_type"):
            setattr(row, f, getattr(new, f))
    return load_layout(db, settings, "test")


def test_marker_missing_or_foreign_makes_location_offline(db, settings):
    lay = get_layout(settings)
    marker = settings.temp_dir / MARKER
    saved = marker.read_text()
    marker.unlink()
    assert lay.state("temp").reason == locations.MARKER_MISSING
    marker.write_text(json.dumps({**json.loads(saved), "location_uuid": str(uuid.uuid4())}))
    assert lay.state("temp").reason == locations.MARKER_MISMATCH
    marker.write_text("{not json")
    assert lay.state("temp").reason == locations.MARKER_UNREADABLE
    marker.write_text(saved)
    assert lay.state("temp").online


def test_swapped_drive_detected_by_os_volume_uuid(db, settings, tmp_path, probe):
    ext = tmp_path / "ssd" / "riftwire-exports"
    ext.mkdir(parents=True)
    probe.set(tmp_path / "ssd", "apfs", SSD_ONE)
    lay = _external(db, settings, "exports", ext)
    assert lay.state("exports").online
    # Same path, marker copied over, but a DIFFERENT physical drive is mounted there.
    probe.set(tmp_path / "ssd", "apfs", SSD_TWO)
    st = lay.state("exports")
    assert not st.online and st.reason == locations.WRONG_VOLUME


def test_apfs_required_on_macos_no_override(db, settings, tmp_path, probe, monkeypatch):
    monkeypatch.setattr(volumes.platform, "system", lambda: "Darwin")
    assert volumes.filesystem_allowed("apfs")
    for fs in ("exfat", "msdos", "hfs", "ntfs"):
        assert not volumes.filesystem_allowed(fs)
    ext = tmp_path / "exfat-drive" / "rec"
    ext.mkdir(parents=True)
    probe.set(tmp_path / "exfat-drive", "exfat", EXFAT_1)
    with pytest.raises(StorageOffline, match="APFS"):
        _external(db, settings, "recordings", ext)
    assert not (ext / MARKER).exists()
    assert not hasattr(Settings(env="test", min_free_disk_bytes=0), "allow_exfat")


def test_unmounted_volume_path_is_offline_and_never_created(db, settings):
    ghost = Path("/Volumes") / f"RIFTWIRE-NOT-MOUNTED-{uuid.uuid4().hex[:6]}" / "recordings"
    with db.session() as s:
        row = s.get(StorageLocation, "recordings")
        row.path = str(ghost)
    lay = load_layout(db, settings, "test")
    st = lay.state("recordings")
    assert not st.online and st.reason == locations.VOLUME_NOT_MOUNTED
    assert settings.recordings_dir == ghost          # never silently internal
    settings.ensure_dirs()
    reconcile(db, settings, "test")
    assert not ghost.parent.exists()                 # nothing created anywhere
    with pytest.raises(StorageOffline):
        locations.ensure_subdir(lay, "recordings", ".staging")
    assert not ghost.parent.exists()


def test_missing_external_root_is_not_recreated(db, settings, tmp_path):
    ext = tmp_path / "ext-temp"
    ext.mkdir()
    lay = _external(db, settings, "temp", ext)
    shutil.rmtree(ext)  # drive contents gone / wrong folder
    lay = load_layout(db, settings, "test")
    assert lay.state("temp").reason == locations.MISSING_ROOT
    assert not ext.exists()


def test_deleted_internal_default_is_restored_with_same_identity(db, settings):
    lay = get_layout(settings)
    uid = lay.location("exports").location_uuid
    shutil.rmtree(settings.exports_dir)
    lay = load_layout(db, settings, "test")
    assert lay.state("exports").online and lay.location("exports").location_uuid == uid
    assert events(db, "storage_location_restored")


def test_floor_is_mandatory_outside_tests():
    with pytest.raises(ValueError, match="60 GiB"):
        Settings(_env_file=None, env="production", min_free_disk_bytes=59 * GIB)
    with pytest.raises(ValueError):
        Settings(_env_file=None, env="development", min_free_disk_bytes=0)
    assert Settings(_env_file=None, env="development").min_free_disk_bytes == 60 * GIB
    assert Settings(_env_file=None, env="test", min_free_disk_bytes=0).min_free_disk_bytes == 0


def test_floor_is_strictest_on_the_volume(db, settings):
    lay = get_layout(settings)
    vol = lay.location("temp").volume_uuid
    with db.session() as s:
        s.get(StorageLocation, "models").floor_bytes = 70 * GIB
    lay = load_layout(db, settings, "test")
    assert lay.floor_for_volume(vol) == 70 * GIB


def test_managed_path_resolves_through_layout_and_rejects_traversal(db, settings, tmp_path):
    ext = tmp_path / "elsewhere"
    ext.mkdir()
    _external(db, settings, "recordings", ext)
    assert settings.managed_path("recordings/a.mp4") == (ext / "a.mp4").resolve()
    for bad in ("recordings/../../etc/passwd", "thumbnails/x.jpg", "recordings", "/abs"):
        with pytest.raises(ValueError):
            settings.managed_path(bad)


# =========================================================================== uploads

def test_upload_refused_when_temp_or_recordings_offline(db, settings):
    for kind in ("temp", "recordings"):
        m = settings.location(kind) / MARKER
        saved = m.read_text()
        m.unlink()
        with pytest.raises(StorageOffline) as e:
            UploadSession(settings, "x.mp4", 1000, db)
        assert e.value.status_code == 503 and kind in e.value.message
        m.write_text(saved)
    assert user_files(settings.temp_dir) == []
    assert count(db, DiskReservation) == 0


def test_upload_refused_during_storage_maintenance(db, settings):
    lock = locations.MaintenanceLock(settings)
    assert lock.acquire()
    try:
        with pytest.raises(StorageOffline, match="maintenance"):
            UploadSession(settings, "x.mp4", 1000, db)
    finally:
        lock.release()
    up = UploadSession(settings, "x.mp4", None, db)
    up.abort()


def test_drive_lost_mid_upload_aborts_safely(db, settings, monkeypatch):
    from app.services import ingest
    monkeypatch.setattr(ingest, "SPACE_CHECK_EVERY", 1024)
    up = UploadSession(settings, "x.mp4", None, db)
    up.write(b"\0" * 512)
    (settings.temp_dir / MARKER).rename(settings.temp_dir / "moved-marker")
    with pytest.raises(StorageOffline):
        up.write(b"\0" * 2048)
    up.abort()
    (settings.temp_dir / "moved-marker").rename(settings.temp_dir / MARKER)
    assert user_files(settings.temp_dir) == []
    assert count(db, DiskReservation) == 0


# =========================================================================== reservations

def test_reservations_are_shared_across_processes_and_enforce_floor(db, settings):
    lay = get_layout(settings)
    other = Database(settings.database_url)  # a second "process"
    try:
        free = lambda _p: 100 * GIB  # noqa: E731
        settings.min_free_disk_bytes = 60 * GIB
        a = reservations.reserve(db, lay, "temp", "upload:a", 30 * GIB, lease_seconds=60,
                                 free_fn=free)
        with pytest.raises(InsufficientStorage) as e:
            reservations.reserve(other, lay, "temp", "job:b", 20 * GIB, lease_seconds=60,
                                 free_fn=free)  # 100 - 30 - 20 < 60
        assert e.value.details["reserved_bytes"] == 30 * GIB
        # Same volume, different kind: shares the budget.
        with pytest.raises(InsufficientStorage):
            reservations.reserve(other, lay, "recordings", "job:c", 20 * GIB,
                                 lease_seconds=60, free_fn=free)
        reservations.release(db, a)
        reservations.reserve(other, lay, "temp", "job:b", 20 * GIB, lease_seconds=60,
                             free_fn=free)
    finally:
        other.dispose()


def test_concurrent_reservations_never_jointly_breach_floor(db, settings):
    lay = get_layout(settings)
    settings.min_free_disk_bytes = 60 * GIB
    free = lambda _p: 100 * GIB  # noqa: E731
    admitted: list[int] = []
    lock = threading.Lock()

    def worker(i):
        d = Database(settings.database_url)
        try:
            reservations.reserve(d, lay, "temp", f"h{i}", 7 * GIB, lease_seconds=60,
                                 free_fn=free)
            with lock:
                admitted.append(i)
        except InsufficientStorage:
            pass
        finally:
            d.dispose()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(admitted) == 5  # 5 × 7 = 35 ≤ 40 available above the floor; 6 would breach
    assert reservations.reserved_bytes(db, lay.location("temp").volume_uuid) == 35 * GIB


def test_expired_reservations_are_reclaimed_and_renew_readmits(db, settings):
    lay = get_layout(settings)
    free = lambda _p: 100 * GIB  # noqa: E731
    settings.min_free_disk_bytes = 60 * GIB
    r = reservations.reserve(db, lay, "temp", "crashed", 30 * GIB, lease_seconds=60,
                             free_fn=free)
    with db.session() as s:
        s.get(DiskReservation, r.id).expires_at = utcnow() - timedelta(seconds=1)
    assert reservations.reclaim_expired(db, "test") == 1
    assert events(db, "reservations_reclaimed")
    reservations.renew(db, lay, r, 10 * GIB, lease_seconds=60, free_fn=free)  # re-admitted
    assert count(db, DiskReservation) == 1
    with pytest.raises(InsufficientStorage):
        reservations.renew(db, lay, r, 45 * GIB, lease_seconds=60, free_fn=free)


# =========================================================================== transfer

def _src(tmp_path, data=b"x" * 300_000):
    p = tmp_path / "src.bin"
    p.write_bytes(data)
    import hashlib
    return p, hashlib.sha256(data).hexdigest()


def test_transfer_verifies_the_actual_staged_file(tmp_path, monkeypatch):
    src, sha = _src(tmp_path)
    dst, staging = tmp_path / "out" / "f.bin", tmp_path / "out" / ".staging"
    dst.parent.mkdir()

    def corrupt(name):
        if name == "transfer_before_verify":
            part = next(staging.glob("*.partial"))
            with open(part, "r+b") as f:  # bit rot / bad write AFTER the copy loop
                f.seek(1000)
                f.write(b"\xff")
    monkeypatch.setattr(transfer, "crashpoint", corrupt)
    with pytest.raises(transfer.TransferError, match="verification") as e:
        transfer.transfer_file(src, dst, sha, staging, "tok")
    assert not e.value.permanent
    assert not dst.exists() and list(staging.iterdir()) == []


def test_transfer_refuses_if_staged_file_swapped_after_verify(tmp_path, monkeypatch):
    src, sha = _src(tmp_path)
    dst, staging = tmp_path / "out" / "f.bin", tmp_path / "out" / ".staging"
    dst.parent.mkdir()

    def swap(name):
        if name == "transfer_verified":
            part = next(staging.glob("*.partial"))
            part.unlink()
            part.write_bytes(b"evil" * 75_000)  # same size, new inode
    monkeypatch.setattr(transfer, "crashpoint", swap)
    with pytest.raises(transfer.TransferError, match="changed after verification"):
        transfer.transfer_file(src, dst, sha, staging, "tok")
    assert not dst.exists()


def test_transfer_never_overwrites_different_existing_file(tmp_path):
    src, sha = _src(tmp_path)
    dst, staging = tmp_path / "f.bin", tmp_path / ".staging"
    dst.write_bytes(b"precious")
    with pytest.raises(transfer.TransferError, match="refusing to overwrite") as e:
        transfer.transfer_file(src, dst, sha, staging, "tok")
    assert e.value.permanent and dst.read_bytes() == b"precious"


def test_transfer_concurrent_attempts_one_promotes(tmp_path):
    src, sha = _src(tmp_path)
    dst, staging = tmp_path / "f.bin", tmp_path / ".staging"
    staging.mkdir()
    n = 8
    barrier = threading.Barrier(n)
    out: list[str] = [""] * n

    def go(i):
        barrier.wait()
        out[i] = transfer.transfer_file(src, dst, sha, staging, f"t{i}")
    ts = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert sorted(out).count("promoted") == 1
    assert sorted(out).count("already_present") == n - 1
    assert sha_file(dst) == sha and list(staging.iterdir()) == []


def _proc_promote(staged: str, dst: str, q) -> None:
    q.put(transfer.promote_no_overwrite(Path(staged), Path(dst)))


def test_promotion_race_across_processes(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    dst = tmp_path / "final.bin"
    procs = []
    for i in range(6):
        p = tmp_path / f"s{i}.partial"
        p.write_bytes(f"proc {i}".encode())
        procs.append(ctx.Process(target=_proc_promote, args=(str(p), str(dst), q)))
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    results = [q.get(timeout=5) for _ in procs]
    assert results.count(True) == 1 and dst.read_bytes().startswith(b"proc ")


@requires_ffmpeg
def test_real_cross_volume_finalize(db, settings, media_dir, vol2):
    """temp on a second filesystem: link(2) fails with EXDEV for real; the verified copy
    path is used, the reservation is released, the staged upload is removed."""
    _external(db, settings, "temp", vol2)
    res = import_file(db, settings, media_dir / "basic.mp4")
    assert (vol2 / f"{res.recording.id}.staged").exists()
    drain(db, settings)
    r = get_rec(db, res.recording.id)
    assert r.status == "READY", r.error_message
    final = settings.managed_path(r.file_path)
    assert sha_file(final) == r.sha256 and final.parent == settings.data_dir / "recordings"
    assert user_files(vol2) == [] and count(db, DiskReservation) == 0


# =========================================================================== worker

@requires_ffmpeg
def test_offline_recordings_jobs_wait_without_consuming_attempts(db, settings, media_dir):
    settings.storage_check_seconds = 0  # no cached verification in this test
    res = import_file(db, settings, media_dir / "basic.mp4")
    rid = res.recording.id
    marker = settings.recordings_dir / MARKER
    saved = marker.read_text()
    marker.unlink()
    w = WorkerProcess(settings, db)
    blocked = w.blocked_job_types()
    assert {t.value for t in blocked} == {"INGEST", "METADATA", "THUMBNAIL"}
    assert queue.claim_next(db, w.id, 30, exclude_types=blocked) is None
    with db.session() as s:
        job = s.scalar(select(Job).where(Job.recording_id == rid))
        assert job.status == JobStatus.PENDING.value and job.attempt_count == 0
    assert events(db, "storage_offline")
    marker.write_text(saved)
    assert w.blocked_job_types() == set()
    assert events(db, "storage_online")
    drain(db, settings)
    assert get_rec(db, rid).status == "READY"


@requires_ffmpeg
def test_drive_lost_mid_job_defers_without_consuming_attempt(db, settings, media_dir):
    res = import_file(db, settings, media_dir / "basic.mp4")
    w = WorkerProcess(settings, db)
    c = queue.claim_next(db, w.id, 30)
    assert c.attempt == 1
    (settings.recordings_dir / MARKER).rename(settings.recordings_dir / "away")
    with pytest.raises(StorageOfflineJobError):
        handle_ingest(JobContext(db, settings, c))
    w._execute(queue.ClaimedJob(c.id, c.job_type, c.recording_id, c.token, c.attempt,
                                c.max_attempts))
    with db.session() as s:
        job = s.get(Job, c.id)
        assert job.status == JobStatus.RETRY_WAIT.value
        assert job.attempt_count == 0 and "waiting for storage" in job.last_error
    assert events(db, "job_waiting_for_storage")
    assert user_files(settings.recordings_dir, files_only=True) == ["away"]  # nothing written
    (settings.recordings_dir / "away").rename(settings.recordings_dir / MARKER)
    with db.session() as s:
        s.get(Job, c.id).next_attempt_at = utcnow()
    drain(db, settings)
    assert get_rec(db, res.recording.id).status == "READY"


def test_worker_refuses_to_start_during_maintenance(db, settings):
    from app.workers.worker import WorkerLockedError
    lock = locations.MaintenanceLock(settings)
    assert lock.acquire()
    try:
        with pytest.raises(WorkerLockedError, match="migration"):
            WorkerProcess(settings, db).startup()
    finally:
        lock.release()


def test_reconcile_never_touches_offline_location(db, settings):
    stale = settings.temp_dir / "old.part"
    stale.write_bytes(b"x")
    os.utime(stale, (0, 0))
    marker = settings.temp_dir / MARKER
    saved = marker.read_text()
    marker.unlink()
    rep = reconcile(db, settings, "test")
    assert rep.skipped_offline == ["temp"] and stale.exists()
    marker.write_text(saved)
    reconcile(db, settings, "test")
    assert not stale.exists()


# =========================================================================== API

def test_storage_api_and_readiness_report_offline(db, settings):
    from fastapi.testclient import TestClient

    from app.main import create_app
    with TestClient(create_app(settings)) as c:
        st = c.get("/api/v1/storage").json()
        kinds = {loc["kind"]: loc for loc in st["locations"]}
        assert set(kinds) == {"recordings", "temp", "models", "exports", "assets"}
        assert all(loc["online"] for loc in kinds.values())
        assert len(st["volumes"]) == 1 and st["volumes"][0]["floor_bytes"] >= 0
        (settings.exports_dir / MARKER).rename(settings.exports_dir / "x")
        r = c.get("/health/ready")
        assert r.status_code == 503
        assert "storage_offline:exports" in r.json()["checks"]["storage"]["offline"]
        loc = {x["kind"]: x for x in c.get("/api/v1/storage").json()["locations"]}["exports"]
        assert not loc["online"] and loc["reason"] == "marker_missing"
        (settings.exports_dir / "x").rename(settings.exports_dir / MARKER)


# =========================================================================== migration

@pytest.fixture
def mig_settings(settings):
    settings.api_port = 1  # nothing listens there: the "API running" check passes
    return settings


def _files_snapshot(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): sha_file(p) for p in root.rglob("*")
            if p.is_file() and p.name != MARKER}


@requires_ffmpeg
def test_migration_dry_run_writes_nothing(db, mig_settings, media_dir, tmp_path):
    s = mig_settings
    import_file(db, s, media_dir / "basic.mp4")
    drain(db, s)
    dest = tmp_path / "new-drive" / "recordings"
    before = (count(db, StorageMigration), count(db, SystemEvent))
    p = migration.plan(db, s, get_layout(s), "recordings", dest)
    assert p.ok, p.problems
    assert len(p.files) == 1 and p.files[0].sha256  # known from import
    assert not dest.exists() and not dest.parent.exists()
    assert (count(db, StorageMigration), count(db, SystemEvent)) == before


@requires_ffmpeg
def test_migration_execute_verifies_switches_and_keeps_originals(db, mig_settings,
                                                                 media_dir, vol2):
    s = mig_settings
    ids = [import_file(db, s, media_dir / n).recording.id for n in ("basic.mp4", "basic.mkv")]
    drain(db, s)
    src_root = s.recordings_dir
    before = _files_snapshot(src_root)
    dest = vol2 / "riftwire" / "recordings"
    r = migration.execute(db, s, "recordings", dest)
    assert r.copied == 2 and r.already_verified == 0
    assert _files_snapshot(dest) == before            # byte-identical copies
    assert _files_snapshot(src_root) == before        # originals untouched, not deleted
    lay = load_layout(db, s, "test")
    assert lay.path("recordings") == dest and lay.state("recordings").online
    assert lay.location("recordings").volume_uuid != lay.location("temp").volume_uuid
    for rid in ids:                                   # DB paths resolve to the new drive
        rec = get_rec(db, rid)
        assert s.managed_path(rec.file_path).parent == dest
    with db.session() as sess:
        m = sess.scalar(select(StorageMigration))
        assert m.status == "SWITCHED"
        assert {i.state for i in sess.scalars(select(StorageMigrationItem))} == {"VERIFIED"}
    assert count(db, DiskReservation) == 0
    assert not (dest / ".staging").exists()
    # New imports land on the new drive (temp on the internal disk: real cross-volume).
    res = import_file(db, s, media_dir / "noaudio.mp4")
    drain(db, s)
    assert (dest / f"{res.recording.id}.mp4").exists()


@requires_ffmpeg
def test_migration_resumes_without_recopying(db, mig_settings, media_dir, tmp_path,
                                            monkeypatch):
    s = mig_settings
    for n in ("basic.mp4", "basic.mkv", "noaudio.mp4"):
        import_file(db, s, media_dir / n)
    drain(db, s)
    dest = tmp_path / "dest"
    real = transfer.transfer_file
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt  # owner hits Ctrl-C / power loss mid-run
        return real(*a, **k)
    monkeypatch.setattr(migration.transfer, "transfer_file", flaky)
    with pytest.raises(KeyboardInterrupt):
        migration.execute(db, s, "recordings", dest)
    monkeypatch.setattr(migration.transfer, "transfer_file", real)
    assert get_layout(s).path("recordings") == s.data_dir / "recordings"  # not switched
    p = migration.plan(db, s, get_layout(s), "recordings", dest)
    assert p.ok and p.resume_of and p.already_verified == 1
    r = migration.execute(db, s, "recordings", dest)
    assert r.copied == 2 and r.already_verified == 1
    assert load_layout(db, s, "test").path("recordings") == dest


@requires_ffmpeg
def test_migration_refusals_write_nothing(db, mig_settings, media_dir, tmp_path, probe,
                                         monkeypatch):
    from app.workers.worker import WorkerLock
    s = mig_settings
    import_file(db, s, media_dir / "basic.mp4")
    drain(db, s)
    lay = get_layout(s)

    # Not empty.
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "someone-elses-file").write_text("keep")
    assert any("not empty" in x for x in migration.plan(db, s, lay, "recordings", busy).problems)
    # Unmounted drive.
    ghost = Path("/Volumes") / f"NOPE-{uuid.uuid4().hex[:6]}" / "rec"
    assert any("not mounted" in x
               for x in migration.plan(db, s, lay, "recordings", ghost).problems)
    # Overlaps another location.
    assert any("overlaps" in x for x in migration.plan(
        db, s, lay, "recordings", s.temp_dir / "sub").problems)
    # exFAT on macOS.
    monkeypatch.setattr(volumes.platform, "system", lambda: "Darwin")
    exfat = tmp_path / "exfat"
    exfat.mkdir()
    probe.set(exfat, "exfat", EXFAT_1)
    probs = migration.plan(db, s, lay, "recordings", exfat / "rec").problems
    assert any("APFS" in x for x in probs), probs
    monkeypatch.setattr(volumes.platform, "system", lambda: "Linux")
    # Too little space above the floor.
    monkeypatch.setattr(migration, "free_bytes", lambda _p: 10)
    assert any("not enough free space" in x for x in migration.plan(
        db, s, lay, "recordings", tmp_path / "small").problems)
    monkeypatch.undo()
    volumes.set_probe(None)
    # Worker running.
    wl = WorkerLock(s.data_dir / "worker.lock")
    wl.acquire()
    try:
        with pytest.raises(migration.MigrationRefused, match="worker is running"):
            migration.execute(db, s, "recordings", tmp_path / "x")
    finally:
        wl.release()
    for p in (tmp_path / "small", tmp_path / "x", ghost.parent, exfat / "rec"):
        assert not p.exists()
    assert count(db, StorageMigration) == 0
    assert (busy / "someone-elses-file").read_text() == "keep"


@requires_ffmpeg
def test_migration_conflicting_destination_file_aborts_without_switch(db, mig_settings,
                                                                      media_dir, tmp_path,
                                                                      monkeypatch):
    s = mig_settings
    rec = import_file(db, s, media_dir / "basic.mp4").recording
    drain(db, s)
    dest = tmp_path / "dest"
    real = transfer.transfer_file

    def plant_conflict(src, dst, *a, **k):
        dst.write_bytes(b"something else entirely")  # appears just before promotion
        return real(src, dst, *a, **k)
    monkeypatch.setattr(migration.transfer, "transfer_file", plant_conflict)
    with pytest.raises(migration.MigrationRefused, match="refusing to overwrite"):
        migration.execute(db, s, "recordings", dest)
    assert (dest / f"{rec.id}.mp4").read_bytes() == b"something else entirely"
    assert get_layout(s).path("recordings") == s.data_dir / "recordings"
    assert migration.abort(db, "recordings").status == "ABORTED"


def test_migration_cli_dry_run_is_default(db, mig_settings, tmp_path, monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "migrate_storage", Path(__file__).resolve().parents[2] / "scripts" / "migrate_storage.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "get_settings", lambda: mig_settings)
    dest = tmp_path / "cli-dest"
    assert mod.main(["exports", str(dest)]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "all checks pass" in out
    assert not dest.exists()
    assert count(db, StorageMigration) == 0
    assert mod.main(["exports", str(dest), "--execute"]) == 0
    assert load_layout(db, mig_settings, "test").path("exports") == dest
    assert "NOT deleted" in capsys.readouterr().out
    from app.services.system import storage_status
    mig = storage_status(db, mig_settings)["migrations"][0]
    assert mig["status"] == "SWITCHED" and mig["dest_path"] == str(dest)


def test_migration_destination_validation_and_canonical_paths(db, mig_settings, tmp_path):
    from app.core.config import REPO_ROOT
    lay = get_layout(mig_settings)
    for bad, why in ((Path("/usr/local/riftwire"), "system location"),
                     (Path("/System/Volumes/x"), "system location"),
                     (REPO_ROOT / "backend" / "media-here", "source folder"),
                     (Path("relative/dir"), "absolute")):
        probs = migration.plan(db, mig_settings, lay, "exports", bad).problems if \
            bad.is_absolute() else migration.check_destination(lay, "exports", bad)
        assert any(why in x for x in probs), (bad, probs)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    p = migration.plan(db, mig_settings, lay, "exports", link / "exports" / ".." / "exp")
    assert p.ok, p.problems
    assert p.dest == real / "exp"          # symlink and '..' resolved before anything is stored


# =========================================================================== macOS probe

DF_DATA = """Filesystem   512-blocks      Used Available Capacity  Mounted on
/dev/disk3s5  965595304 812345678 131234567    87%    /System/Volumes/Data
"""
DF_SSD = """Filesystem   512-blocks      Used Available Capacity  Mounted on
/dev/disk5s1  1953503232 1024 1953502208     1%    /Volumes/SanDisk SSD
"""
MOUNT = """/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)
devfs on /dev (devfs, local, nobrowse)
/dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled, nobrowse, protect, root data)
/dev/disk5s1 on /Volumes/SanDisk SSD (apfs, local, nodev, nosuid, journaled, noowners)
/dev/disk6s1 on /Volumes/Camera Card (exfat, local, nodev, nosuid, noowners, noatime, fskit)
"""


def _plist(uuid_: str, name: str, fs: str = "apfs") -> str:
    import plistlib
    return plistlib.dumps({"VolumeUUID": uuid_, "VolumeName": name, "FilesystemType": fs,
                           "MountPoint": "/x"}).decode()


class FakeRunner:
    def __init__(self, df: str, diskutil: str | None):
        self.df, self.diskutil, self.calls = df, diskutil, []

    def __call__(self, args):
        self.calls.append(args)
        if args[0].endswith("df"):
            return self.df
        if args[0].endswith("mount"):
            return MOUNT
        if self.diskutil is None:
            raise RuntimeError("diskutil failed")
        return self.diskutil


def test_darwin_probe_asks_diskutil_about_the_mount_point_not_the_folder():
    run = FakeRunner(DF_DATA, _plist(DATA_UUID.lower(), "Data"))
    vi = volumes.DarwinProbe(run).info(Path("/Users/greg/Claude/riftwire/data/recordings"))
    assert vi == volumes.VolumeInfo("/System/Volumes/Data", "apfs", DATA_UUID, "Data")
    du = [c for c in run.calls if c[0].endswith("diskutil")][0]
    assert du[-1] == "/System/Volumes/Data"  # regression: diskutil rejects plain folders


def test_darwin_probe_handles_names_with_spaces_and_exfat():
    vi = volumes.DarwinProbe(FakeRunner(DF_SSD, _plist(SSD_ONE, "SanDisk SSD"))).info(
        Path("/Volumes/SanDisk SSD/riftwire/recordings"))
    assert (vi.mount_point, vi.fs_type, vi.hw_volume_uuid) == (
        "/Volumes/SanDisk SSD", "apfs", SSD_ONE)
    df_card = DF_SSD.replace("/Volumes/SanDisk SSD", "/Volumes/Camera Card")
    vi = volumes.DarwinProbe(FakeRunner(df_card, _plist(EXFAT_1, "Card", "exfat"))).info(
        Path("/Volumes/Camera Card/x"))
    assert vi.fs_type == "exfat" and not volumes.filesystem_allowed(vi.fs_type, "Darwin")


def test_darwin_probe_fails_closed_without_a_valid_volume_uuid():
    """Review finding 2: no identity → no storage on macOS (was: hw_volume_uuid=None)."""
    for diskutil in (None, _plist("not-a-uuid", "Data"), _plist("", "Data"),
                     "<?xml version='1.0'?><plist version='1.0'><array/></plist>"):
        with pytest.raises(volumes.IdentityUnavailable):
            volumes.DarwinProbe(FakeRunner(DF_DATA, diskutil)).info(Path("/Users/x"))


class BrokenProbe(volumes.VolumeProbe):
    def info(self, path):
        raise RuntimeError("probe exploded")


def test_probe_failure_never_crashes_startup(settings, tmp_path):
    from tests.conftest import run_migrations
    run_migrations(settings.database_url)
    d = Database(settings.database_url)
    volumes.set_probe(BrokenProbe())
    try:
        lay = load_layout(d, settings, "test")           # must not raise
        assert lay.locations == {}
        assert all(st.reason == locations.NOT_REGISTERED for st in lay.states())
        assert len(events(d, "storage_registration_failed")) == 5
        with pytest.raises(StorageOffline):
            UploadSession(settings, "x.mp4", 10, d)
        with pytest.raises(StorageOffline):           # never the internal default path
            _ = settings.recordings_dir
        rep = reconcile(d, settings, "test")           # startup cleanup must not crash
        assert set(rep.skipped_offline) == {"temp", "recordings"}
    finally:
        volumes.set_probe(None)
    lay = load_layout(d, settings, "test")               # probe works again → registers
    assert all(st.online for st in lay.states())
    d.dispose()


# =========================================================================== audit fixes

@requires_ffmpeg
def test_thumbnails_work_when_temp_is_on_another_drive(db, settings, media_dir, vol2):
    """Audit: thumbnails used to be rendered in temp and renamed into the internal
    thumbnails folder — EXDEV when temp is on another drive."""
    _external(db, settings, "temp", vol2)
    res = import_file(db, settings, media_dir / "basic.mp4")
    drain(db, settings)
    r = get_rec(db, res.recording.id)
    assert (r.status, r.thumbnail_status) == ("READY", "READY")
    assert (settings.thumbnails_dir / f"{r.id}.jpg").is_file()
    assert not list(settings.thumbnails_dir.glob(".job-*"))
    assert count(db, DiskReservation) == 0


@requires_ffmpeg
def test_thumbnail_does_not_need_temp_location(db, settings, media_dir):
    import_file(db, settings, media_dir / "basic.mp4")
    from app.models import JobType
    from app.workers.handlers import REQUIRED_KINDS
    assert REQUIRED_KINDS[JobType.THUMBNAIL] == ("recordings",)


def test_reconcile_cleans_only_dead_thumbnail_temp_files(db, settings):
    rid = str(uuid.uuid4())
    with db.session() as s:
        s.add(Recording(id=rid, original_filename="x.mp4", stored_filename=f"{rid}.mp4",
                        file_path=f"recordings/{rid}.mp4", sha256="0" * 64, file_size_bytes=1,
                        status="READY"))
        s.flush()
        s.add(Job(recording_id=rid, job_type="THUMBNAIL", status="RUNNING", max_attempts=3,
                  lease_token="livetoken", lease_expires_at=utcnow() + timedelta(minutes=5)))
    jid = str(uuid.uuid4())
    live = settings.thumbnails_dir / f".job-{jid}-livetoken.tmp.jpg"
    dead = settings.thumbnails_dir / f".job-{jid}-deadtoken.tmp.jpg"
    live.write_bytes(b"x")
    dead.write_bytes(b"x")
    reconcile(db, settings, "test")
    assert live.exists() and not dead.exists()


def test_transfer_guard_runs_before_promotion(tmp_path):
    src, sha = _src(tmp_path)
    dst, staging = tmp_path / "f.bin", tmp_path / ".staging"

    def guard():
        raise StorageOffline("drive swapped")
    with pytest.raises(StorageOffline):
        transfer.transfer_file(src, dst, sha, staging, "tok", guard=guard)
    assert not dst.exists() and list(staging.iterdir()) == []


@requires_ffmpeg
def test_finalize_rechecks_recordings_drive_before_promotion(db, settings, media_dir,
                                                            monkeypatch):
    """The recordings drive is swapped after the copy is verified: nothing is promoted and
    the job is deferred (attempt not counted)."""
    settings.test_force_copy_fallback = True   # take the cross-volume path
    res = import_file(db, settings, media_dir / "basic.mp4")
    marker = settings.recordings_dir / MARKER

    def swap(name):
        if name == "transfer_verified":
            marker.rename(settings.recordings_dir / "elsewhere")
    monkeypatch.setattr(transfer, "crashpoint", swap)
    w = WorkerProcess(settings, db)
    c = queue.claim_next(db, w.id, 30)
    w._execute(c)
    with db.session() as s:
        job = s.get(Job, c.id)
        assert job.status == "RETRY_WAIT" and job.attempt_count == 0
    assert not (settings.recordings_dir / f"{res.recording.id}.mp4").exists()
    assert not [p for p in (settings.recordings_dir / ".staging").iterdir()]


def test_keeper_renews_reservation_during_long_writes(db, settings):
    lay = get_layout(settings)
    free = lambda _p: 100 * GIB  # noqa: E731
    r = reservations.reserve(db, lay, "temp", "long-copy", 10 * GIB, lease_seconds=60,
                             free_fn=free)
    with db.session() as s:
        before = s.get(DiskReservation, r.id).expires_at
    k = reservations.Keeper(db, lay, r, 10 * GIB, lease_seconds=600, free_fn=free, every=0)
    k.tick(4 * GIB)
    with db.session() as s:
        row = s.get(DiskReservation, r.id)
        assert row.bytes == 6 * GIB and row.expires_at > before


def test_api_startup_during_migration_never_cleans_up(db, settings):
    from fastapi.testclient import TestClient

    from app.main import create_app
    stale = settings.temp_dir / "being-migrated.part"
    stale.write_bytes(b"x")
    os.utime(stale, (0, 0))
    lock = locations.MaintenanceLock(settings)
    assert lock.acquire()
    try:
        with TestClient(create_app(settings)) as c:
            assert c.get("/health").status_code == 200
        assert stale.exists()
        assert events(db, "api_started_during_maintenance")
    finally:
        lock.release()


@requires_ffmpeg
def test_migration_never_recreates_vanished_destination(db, mig_settings, media_dir,
                                                        tmp_path, monkeypatch):
    """The destination drive disappears after the first file: the run stops; nothing is
    recreated where the drive was (it would otherwise land on the internal disk)."""
    s = mig_settings
    for n in ("basic.mp4", "basic.mkv"):
        import_file(db, s, media_dir / n)
    drain(db, s)
    dest = tmp_path / "drive" / "recordings"
    real = transfer.transfer_file
    calls = {"n": 0}

    def unplug_after_first(*a, **k):
        calls["n"] += 1
        out = real(*a, **k)
        if calls["n"] == 1:
            shutil.rmtree(tmp_path / "drive")     # the drive vanished
        return out
    monkeypatch.setattr(migration.transfer, "transfer_file", unplug_after_first)
    with pytest.raises(migration.MigrationRefused, match="no longer available"):
        migration.execute(db, s, "recordings", dest)
    assert not (tmp_path / "drive").exists()
    assert get_layout(s).path("recordings") == s.data_dir / "recordings"


@requires_ffmpeg
def test_migration_resumes_after_hard_crash_mid_copy(db, mig_settings, media_dir, tmp_path):
    """A hard crash leaves our own .partial behind (no finally runs); resume must not trip
    over it."""
    s = mig_settings
    rec = import_file(db, s, media_dir / "basic.mp4").recording
    drain(db, s)
    dest = tmp_path / "dest"
    real_create = migration.create_destination
    # First run: journal + destination created, then the process "dies".
    def die(p):
        real_create(p)
        raise KeyboardInterrupt
    migration.create_destination = die
    try:
        with pytest.raises(KeyboardInterrupt):
            migration.execute(db, s, "recordings", dest)
    finally:
        migration.create_destination = real_create
    m = migration.open_migration(db, "recordings")
    assert m is not None and dest.is_dir() and not (dest / MARKER).exists()
    (dest / ".staging").mkdir()
    (dest / ".staging" / transfer.staged_name(f"{rec.id}.mp4",
                                              f"migration-{m.id[:8]}")).write_bytes(b"junk")
    r = migration.execute(db, s, "recordings", dest)   # resumes: marker written, copy done
    assert r.copied == 1 and load_layout(db, s, "test").path("recordings") == dest
    assert sha_file(dest / f"{rec.id}.mp4") == rec.sha256


def test_create_destination_refuses_if_anchor_changed(tmp_path):
    anchor = tmp_path / "a"
    anchor.mkdir()
    p = migration.Plan("exports", tmp_path, anchor / "x" / "y", [], 0, None, 0,
                       anchor=anchor, anchor_dev=os.stat(anchor).st_dev + 1)
    with pytest.raises(migration.MigrationRefused, match="changed since planning"):
        migration.create_destination(p)
    assert not (anchor / "x").exists()
    with pytest.raises(migration.MigrationRefused, match="no longer available"):
        migration.make_dirs_below(tmp_path / "gone", Path("a/b"))
    assert not (tmp_path / "gone").exists()


@requires_ffmpeg
def test_resume_recopies_verified_files_missing_at_destination(db, mig_settings, media_dir,
                                                               tmp_path, monkeypatch):
    s = mig_settings
    for n in ("basic.mp4", "basic.mkv"):
        import_file(db, s, media_dir / n)
    drain(db, s)
    dest = tmp_path / "dest"
    real = transfer.transfer_file
    calls = {"n": 0}

    def stop_after_first(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real(*a, **k)
    monkeypatch.setattr(migration.transfer, "transfer_file", stop_after_first)
    with pytest.raises(KeyboardInterrupt):
        migration.execute(db, s, "recordings", dest)
    monkeypatch.setattr(migration.transfer, "transfer_file", real)
    copied = [p for p in dest.iterdir() if p.is_file() and p.name != MARKER]
    assert len(copied) == 1
    copied[0].unlink()                                   # the verified copy disappears
    r = migration.execute(db, s, "recordings", dest)
    assert r.copied == 2 and r.already_verified == 0     # both copied (one re-copied)
    assert load_layout(db, s, "test").path("recordings") == dest
