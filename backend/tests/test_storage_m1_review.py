"""Regression tests for the independent M1 review (PR #9, code 4703e22).

One section per confirmed finding, plus the additional audit items. Each test fails on the
reviewed code and passes after the fix (see docs/M1_REVIEW.md §6).
"""

from __future__ import annotations

import json
import os
import threading

import pytest
from sqlalchemy import select

from app.core.errors import InsufficientStorage, StorageOffline
from app.db.base import utcnow
from app.models import DiskReservation, StorageLocation, StorageMigration
from app.services import ingest, locations, migration, reservations, transfer, volumes
from app.services.ingest import UploadSession, reconcile
from app.services.locations import MARKER, get_layout, load_layout
from tests.helpers import user_files
from tests.test_storage_m1 import (
    EXFAT_1,
    SSD_ONE,
    SSD_TWO,
    FakeProbe,
    _external,
    count,
    events,
)

MIB = 1024 * 1024


def dest_files(root) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.is_file() and p.name != MARKER)


@pytest.fixture
def probe():
    fp = FakeProbe()
    volumes.set_probe(fp)
    yield fp
    volumes.set_probe(None)


@pytest.fixture
def mig_settings(settings):
    settings.api_port = 1  # nothing listens there
    return settings


def _fill_exports(settings, sizes: dict[str, int]) -> None:
    for name, size in sizes.items():
        (settings.exports_dir / name).write_bytes(os.urandom(size))


def _stop_on_call(monkeypatch, n: int, action=None):
    """Make the n-th transfer raise KeyboardInterrupt (power loss / Ctrl-C) or run
    ``action`` before it."""
    real = transfer.transfer_file
    calls = {"n": 0}

    def wrapped(*a, **k):
        calls["n"] += 1
        if calls["n"] == n:
            if action is None:
                raise KeyboardInterrupt
            action()
        return real(*a, **k)
    monkeypatch.setattr(migration.transfer, "transfer_file", wrapped)
    return real


# =========================================================================== finding 1

def test_f1_copied_marker_on_replacement_drive_never_resumes(db, mig_settings, tmp_path,
                                                             probe, monkeypatch):
    """Interrupted migration; a different drive with the same mount path and a copy of the
    destination folder (marker included) appears. Resume is refused; nothing switches."""
    s = mig_settings
    _fill_exports(s, {"a.bin": 4096, "b.bin": 4096})
    drive = tmp_path / "ssd"
    drive.mkdir()
    probe.set(drive, "apfs", SSD_ONE)
    dest = drive / "exports"
    real = _stop_on_call(monkeypatch, 2)
    with pytest.raises(KeyboardInterrupt):
        migration.execute(db, s, "exports", dest)
    monkeypatch.setattr(migration.transfer, "transfer_file", real)
    with db.session() as sess:
        assert sess.scalar(select(StorageMigration)).dest_hw_volume_uuid == SSD_ONE

    probe.set(drive, "apfs", SSD_TWO)                       # replacement drive, copied data
    p = migration.plan(db, s, get_layout(s), "exports", dest)
    assert not p.ok and any("different drive" in x for x in p.problems)
    with pytest.raises(migration.MigrationRefused, match="different drive"):
        migration.execute(db, s, "exports", dest)
    assert get_layout(s).path("exports") == s.data_dir / "exports"   # not switched
    assert dest_files(dest) == ["a.bin"]             # nothing more written


def test_f1_drive_swap_mid_copy_stops_before_promotion(db, mig_settings, tmp_path, probe,
                                                       monkeypatch):
    s = mig_settings
    _fill_exports(s, {"a.bin": 4096, "b.bin": 4096})
    drive = tmp_path / "ssd"
    drive.mkdir()
    probe.set(drive, "apfs", SSD_ONE)
    dest = drive / "exports"
    _stop_on_call(monkeypatch, 2, action=lambda: probe.set(drive, "apfs", SSD_TWO))
    with pytest.raises(migration.MigrationRefused, match="OS volume UUID"):
        migration.execute(db, s, "exports", dest)
    assert dest_files(dest) == ["a.bin"]             # b.bin never promoted
    assert get_layout(s).path("exports") == s.data_dir / "exports"


def test_f1_swap_after_final_rehash_never_switches(db, mig_settings, tmp_path, probe):
    s = mig_settings
    _fill_exports(s, {"a.bin": 4096})
    drive = tmp_path / "ssd"
    drive.mkdir()
    probe.set(drive, "apfs", SSD_ONE)

    def progress(msg: str) -> None:
        if msg.startswith("final verification"):
            probe.set(drive, "apfs", SSD_TWO)
    with pytest.raises(migration.MigrationRefused):
        migration.execute(db, s, "exports", drive / "exports", progress=progress)
    with db.session() as sess:
        row = sess.get(StorageLocation, "exports")
        assert row.path == str(s.data_dir / "exports")


def test_f1_switch_records_the_approved_drive(db, mig_settings, tmp_path, probe):
    s = mig_settings
    _fill_exports(s, {"a.bin": 4096})
    drive = tmp_path / "ssd"
    drive.mkdir()
    probe.set(drive, "apfs", SSD_ONE)
    migration.execute(db, s, "exports", drive / "exports")
    with db.session() as sess:
        assert sess.get(StorageLocation, "exports").hw_volume_uuid == SSD_ONE
        assert sess.scalar(select(StorageMigration)).dest_hw_volume_uuid == SSD_ONE
    lay = load_layout(db, s, "test")
    assert lay.state("exports").online
    probe.set(drive, "apfs", SSD_TWO)                        # copied marker, other drive
    assert lay.state("exports").reason == locations.WRONG_VOLUME


# =========================================================================== finding 2

class StrictProbe(FakeProbe):
    """Behaves like macOS: identity required. Unmatched paths get ``default_hw``."""

    def __init__(self, default_hw: str | None):
        super().__init__()
        self.requires_hw_uuid = True
        self.default_hw = default_hw
        self.calls = 0

    def info(self, path):
        self.calls += 1
        p = str(path)
        for prefix, (fs, hw) in self.rules:
            if p == prefix or p.startswith(prefix.rstrip("/") + "/"):
                return volumes.VolumeInfo(prefix, fs, hw, "TestDrive")
        return volumes.VolumeInfo("/", "apfs", self.default_hw, "Data")


def test_f2_no_registration_without_os_volume_uuid(settings, tmp_path):
    from app.db.session import Database
    from tests.conftest import run_migrations
    run_migrations(settings.database_url)
    d = Database(settings.database_url)
    volumes.set_probe(StrictProbe(default_hw=None))
    try:
        lay = load_layout(d, settings, "test")               # must not raise
        assert lay.locations == {}                            # nothing registered
        assert len(events(d, "storage_registration_failed")) == 5
        with pytest.raises(StorageOffline):
            UploadSession(settings, "x.mp4", 10, d)
    finally:
        volumes.set_probe(None)
        d.dispose()


def test_f2_registered_without_identity_fails_closed(db, settings, tmp_path, probe):
    """A row recorded without an OS volume UUID is never trusted where identity is
    required: external rows stay offline; internal defaults get their identity recorded."""
    ext = tmp_path / "ssd" / "exports"
    ext.mkdir(parents=True)
    probe.set(tmp_path / "ssd", "apfs", None)                 # Linux-style: no identity
    probe.requires_hw_uuid = False
    _external(db, settings, "exports", ext)
    strict = StrictProbe(default_hw=SSD_TWO)
    strict.set(tmp_path / "ssd", "apfs", SSD_ONE)
    with db.session() as sess:                                # simulate pre-fix rows
        for r in sess.scalars(select(StorageLocation)):
            r.hw_volume_uuid = None
    volumes.set_probe(strict)
    lay = load_layout(db, settings, "test")
    assert lay.state("exports").reason == locations.IDENTITY_UNRECORDED   # external
    for k in ("recordings", "temp", "models", "assets"):                   # internal
        assert lay.location(k).hw_volume_uuid == SSD_TWO and lay.state(k).online
    assert len(events(db, "storage_identity_recorded")) == 4
    strict.default_hw = None                                  # diskutil now failing
    assert lay.state("temp").reason == locations.PROBE_FAILED


def test_f2_identity_is_never_served_from_a_cache(tmp_path):
    strict = StrictProbe(default_hw=SSD_ONE)
    volumes.set_probe(strict)
    try:
        assert volumes.volume_info(tmp_path).hw_volume_uuid == SSD_ONE
        strict.default_hw = SSD_TWO      # same st_dev, different drive (device reuse)
        assert volumes.volume_info(tmp_path).hw_volume_uuid == SSD_TWO
        assert strict.calls == 2
        strict.default_hw = "not-a-uuid"
        with pytest.raises(volumes.IdentityUnavailable):
            volumes.volume_info(tmp_path)
        strict.default_hw = SSD_ONE.lower()                   # normalised
        assert volumes.volume_info(tmp_path).hw_volume_uuid == SSD_ONE
    finally:
        volumes.set_probe(None)


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS only")
def test_f2_real_macos_disk_has_a_valid_volume_uuid(tmp_path):
    vi = volumes.volume_info(tmp_path)
    assert volumes.identity_required()
    assert volumes.valid_volume_uuid(vi.hw_volume_uuid) == vi.hw_volume_uuid
    assert vi.fs_type == "apfs"


# =========================================================================== finding 3

def _floor(settings) -> int:
    lay = get_layout(settings)
    return lay.floor_for_volume(lay.location("temp").volume_uuid)


def _temp_bytes(settings) -> int:
    return sum(p.stat().st_size for p in settings.temp_dir.iterdir()
               if p.is_file() and p.name != MARKER)


def test_f3_concurrent_unknown_length_uploads_never_cross_the_floor(db, settings,
                                                                    monkeypatch):
    monkeypatch.setattr(ingest, "UNKNOWN_LENGTH_WINDOW", 256 * 1024)
    floor = _floor(settings)
    headroom = 1 * MIB
    lowest = {"v": 10**18}

    def fake_free(_path):
        v = floor + headroom - _temp_bytes(settings)
        lowest["v"] = min(lowest["v"], v)
        return v
    monkeypatch.setattr(ingest, "free_bytes", fake_free)
    refused = []
    lock = threading.Lock()

    def uploader():
        try:
            up = UploadSession(settings, "x.mp4", None, db)
        except InsufficientStorage:
            with lock:
                refused.append("start")
            return
        try:
            for _ in range(64):                               # up to 4 MiB each
                up.write(b"\0" * (64 * 1024))
        except InsufficientStorage:
            with lock:
                refused.append("write")
        finally:
            peak = _temp_bytes(settings)
            with lock:
                lowest["v"] = min(lowest["v"], floor + headroom - peak)
            up.abort()
    threads = [threading.Thread(target=uploader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert refused                                            # the floor stopped them
    assert lowest["v"] >= floor                               # never below the floor
    assert count(db, DiskReservation) == 0 and user_files(settings.temp_dir) == []


def test_f3_short_unknown_upload_needs_admission_before_any_byte(db, settings, monkeypatch):
    monkeypatch.setattr(ingest, "UNKNOWN_LENGTH_WINDOW", 256 * 1024)
    floor = _floor(settings)
    monkeypatch.setattr(ingest, "free_bytes", lambda _p: floor + 100 * 1024)
    with pytest.raises(InsufficientStorage):
        UploadSession(settings, "x.mp4", None, db)            # was admitted with 0 bytes
    assert user_files(settings.temp_dir) == [] and count(db, DiskReservation) == 0


def test_f3_floor_enforced_at_finish(db, settings, monkeypatch):
    floor = _floor(settings)
    free = {"v": floor + 10 * MIB}
    monkeypatch.setattr(ingest, "free_bytes", lambda _p: free["v"])
    up = UploadSession(settings, "x.mp4", 4096, db)
    up.write(b"\0" * 4096)
    free["v"] = floor - 1                                    # another program filled the disk
    with pytest.raises(InsufficientStorage):
        up.finish()
    up.abort()
    assert user_files(settings.temp_dir) == [] and count(db, DiskReservation) == 0


def test_f3_no_write_after_the_lease_expired_without_readmission(db, settings, monkeypatch):
    monkeypatch.setattr(ingest, "RENEW_EVERY_SECONDS", 0.0)   # every write renews first
    monkeypatch.setattr(ingest, "UNKNOWN_LENGTH_WINDOW", 64 * 1024)
    floor = _floor(settings)
    free = {"v": floor + 10 * MIB}
    monkeypatch.setattr(ingest, "free_bytes", lambda _p: free["v"])
    up = UploadSession(settings, "x.mp4", None, db)
    up.write(b"\0" * 1024)
    with db.session() as sess:                                # lease expired and reclaimed
        sess.query(DiskReservation).delete()
    free["v"] = floor                                         # re-admission must fail
    with pytest.raises(InsufficientStorage):
        up.write(b"\0" * 1024)
    up._fh.flush()
    assert up.size == 1024 and up.part_path.stat().st_size == 1024   # nothing unadmitted
    up.abort()


# =========================================================================== finding 4

def test_f4_resume_needs_only_the_remaining_bytes(db, mig_settings, tmp_path, monkeypatch):
    s = mig_settings
    _fill_exports(s, {"a.bin": MIB, "b.bin": MIB, "c.bin": MIB})
    dest = tmp_path / "dest"
    real = _stop_on_call(monkeypatch, 2)
    with pytest.raises(KeyboardInterrupt):
        migration.execute(db, s, "exports", dest)
    monkeypatch.setattr(migration.transfer, "transfer_file", real)
    m = migration.open_migration(db, "exports")
    copied = dest_files(dest)
    assert len(copied) == 1
    left = sorted({"a.bin", "b.bin", "c.bin"} - set(copied))
    stale = dest / ".staging" / transfer.staged_name(left[0], f"migration-{m.id[:8]}")
    stale.parent.mkdir(exist_ok=True)
    stale.write_bytes(b"\0" * (MIB // 2))                     # our own partial from a crash
    floor = migration._effective_floor(s)
    # Remaining: 2 MiB − 0.5 MiB stale partial = 1.5 MiB. Not enough for the full 3 MiB.
    monkeypatch.setattr(migration, "free_bytes", lambda _p: floor + int(1.4 * MIB))
    p = migration.plan(db, s, get_layout(s), "exports", dest)
    assert not p.ok and any("not enough free space" in x for x in p.problems)
    monkeypatch.setattr(migration, "free_bytes", lambda _p: floor + int(1.6 * MIB))
    p = migration.plan(db, s, get_layout(s), "exports", dest)
    assert p.ok, p.problems                                   # was refused (needed 3 MiB)
    r = migration.execute(db, s, "exports", dest)
    assert r.copied == 2 and r.already_verified == 1
    assert dest_files(dest) == ["a.bin", "b.bin", "c.bin"]
    assert not stale.exists()


# =========================================================================== finding 5

BAD_MARKERS: list[bytes] = [
    b"[]", b'"x"', b"1", b"null", b'{"format": 1}', b"\xff\xfe\x00",
    b" " * (locations.MARKER_MAX_BYTES + 1),
]


def _bad_marker_variants(valid: dict) -> list[bytes]:
    out = list(BAD_MARKERS)
    for k, v in [("format", 2), ("format", "1"), ("format", True), ("kind", "nope"),
                 ("location_uuid", 5), ("location_uuid", "not-a-uuid"),
                 ("volume_uuid", None), ("hw_volume_uuid", "bad"), ("created_at", 0)]:
        out.append(json.dumps({**valid, k: v}).encode())
    out.append(json.dumps({**valid, "extra": 1}).encode())
    out.append(json.dumps({k: v for k, v in valid.items() if k != "kind"}).encode())
    return out


def test_f5_invalid_markers_report_offline_without_crashing(db, settings):
    from fastapi.testclient import TestClient

    from app.main import create_app
    marker = settings.temp_dir / MARKER
    saved = marker.read_bytes()
    variants = _bad_marker_variants(json.loads(saved))
    with TestClient(create_app(settings)) as c:
        for raw in variants:
            marker.write_bytes(raw)
            lay = load_layout(db, settings, "test")               # startup: no crash
            assert lay.state("temp").reason == locations.MARKER_UNREADABLE, raw[:40]
            r = c.get("/health/ready")                             # readiness: no crash
            assert r.status_code == 503 and "storage_offline:temp" in \
                r.json()["checks"]["storage"]["offline"]
            assert c.get("/api/v1/storage").status_code == 200
            with pytest.raises(StorageOffline):                    # no writes
                UploadSession(settings, "x.mp4", 10, db)
            assert "temp" in reconcile(db, settings, "test").skipped_offline   # worker
    marker.write_bytes(saved)
    assert load_layout(db, settings, "test").state("temp").online


def test_f5_invalid_marker_refused_by_registration_and_migration(db, mig_settings, tmp_path):
    s = mig_settings
    root = tmp_path / "x"
    root.mkdir()
    (root / MARKER).write_text("[]")
    with pytest.raises(StorageOffline, match="invalid"):
        locations.register_root([], "exports", root, 0)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / MARKER).write_text('{"format": 1}')
    p = migration.plan(db, s, get_layout(s), "exports", dest)
    assert not p.ok and any("marker is invalid" in x for x in p.problems)
    with pytest.raises(locations.MarkerInvalid):
        locations.write_marker(tmp_path, {"kind": "temp"})    # never written invalid
    assert not (tmp_path / MARKER).exists()


# =========================================================================== audit

def test_audit_maintenance_lock_is_taken_before_checking_for_writers(db, settings,
                                                                     monkeypatch):
    seen = {}

    def api_running(_s, timeout=0.5):
        seen["maintenance"] = locations.maintenance_active(settings)
        return True
    monkeypatch.setattr(migration, "api_running", api_running)
    with pytest.raises(migration.MigrationRefused, match="API is running"):
        migration.execute(db, settings, "exports", settings.data_dir / "x")
    assert seen["maintenance"] is True                        # no window for a new writer
    assert not locations.maintenance_active(settings)         # released afterwards


def test_audit_upload_started_after_lock_is_refused(db, settings):
    lock = locations.MaintenanceLock(settings)
    assert lock.acquire()
    try:
        with pytest.raises(StorageOffline, match="maintenance"):
            UploadSession(settings, "x.mp4", 10, db)
    finally:
        lock.release()


def test_audit_exfat_still_refused_with_strict_identity(db, settings, tmp_path, monkeypatch):
    monkeypatch.setattr(volumes.platform, "system", lambda: "Darwin")
    strict = StrictProbe(default_hw=SSD_ONE)
    strict.set(tmp_path / "card", "exfat", EXFAT_1)
    (tmp_path / "card" / "r").mkdir(parents=True)
    volumes.set_probe(strict)
    try:
        with pytest.raises(StorageOffline, match="APFS"):
            locations.register_root([], "exports", tmp_path / "card" / "r", 0)
    finally:
        volumes.set_probe(None)


def test_audit_reservation_renew_after_expiry_readmits_with_floor(db, settings):
    lay = get_layout(settings)
    floor = _floor(settings)
    free = {"v": floor + 10 * MIB}
    res = reservations.reserve(db, lay, "temp", "t", MIB, lease_seconds=60,
                               free_fn=lambda _p: free["v"])
    with db.session() as sess:
        sess.get(DiskReservation, res.id).expires_at = utcnow()
    reservations.reclaim_expired(db, "test")
    assert count(db, DiskReservation) == 0
    free["v"] = floor + MIB // 2
    with pytest.raises(InsufficientStorage):                  # re-admission re-checks floor
        reservations.renew(db, lay, res, MIB, lease_seconds=60,
                           free_fn=lambda _p: free["v"])
    assert count(db, DiskReservation) == 0
    reservations.renew(db, lay, res, MIB // 4, lease_seconds=60,
                       free_fn=lambda _p: free["v"])
    assert count(db, DiskReservation) == 1
