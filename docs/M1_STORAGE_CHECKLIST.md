# M1: Storage Architecture, implementation checklist

_Status: **approved by the owner (2026-11-24) and implemented** on branch
`phase2/m1-storage` (draft PR). Everything that can be done without the physical SSD is
implemented and verified in isolated environments. The physical-SSD acceptance checks are
listed in §11 and deferred until the drive arrives. Design: [`ARCHITECTURE.md`](ARCHITECTURE.md) §19.
Scope: Phase 2, milestone M1 ([`PHASE2_PROPOSAL.md`](PHASE2_PROPOSAL.md) §3 M1, §8,
acceptance A11)._

**Owner decisions applied (2026-11-24):**
- Start without the SSD.
- APFS required for the production external SSD, with **no exFAT override**.
- The database, application, logs and thumbnails stay internal.
- The 60 GiB floor is mandatory on every volume.
- Reservations are persistent, transactional and shared by the API and the worker.
- Promotion can never overwrite (including concurrent attempts), and the staged destination
  file is verified before promotion.
- Migration is dry-run by default, resumable, checksum-verified, and never deletes.
- No drive formatting, no real-data migration, no Ollama, no M2.

**Legend:** [x] implemented and tested. ↺ marks an implementation that differs from the
original wording, with the reason.

**Hard limits for M1**
- No migration of the owner's actual data and no drive formatting without separate approval.
- No M2 work (rights and sources).
- 60 GiB floor per volume is mandatory.
- No new dependencies are expected (stdlib + existing stack). Anything else needs approval
  first.

**Target hardware:** internal SSD plus the ordered **SanDisk 1 TB Portable SSD**. Logic is
capacity-independent: never assume a size; decide on measured free space.

## 0. Baseline (Phase 1, verified)
- One `RIFTWIRE_DATA_DIR` with `recordings/`, `temp/`, `thumbnails/`, `exports/`, `logs/`.
- Crash-safe ingest: same-volume link/fsync/unlink finalize, staged-copy fallback with verify
  and no-overwrite promote, and a reconciler.
- Free-space floor via `RIFTWIRE_MIN_FREE_DISK_BYTES`.
- **Gap:** disk reservations live in the API process's memory
  (`services/storage.py`). The worker cannot see them, they are lost on restart, and they are
  not per volume. M1 must fix this before any external volume is used.

## 1. Configurable storage locations
- [x] Location kinds: `recordings`, `temp`, `models`, `exports`, `assets`. The database and
      logs stay on the internal disk.
- [x] ↺ Defaults stay under `RIFTWIRE_DATA_DIR`, so the current layout is unchanged.
      **No `RIFTWIRE_<KIND>_DIR` environment overrides:** an env var could point a process
      at an unmounted drive at startup. A location is changed only through the verified
      migration tool, and the database row is the single authority.
- [x] Alembic migration `storage_locations`: kind, path, volume_uuid, floor_bytes, fs_type,
      created_at and updated_at.
- [x] Path validation:
  - [x] absolute and canonical (no `..`, no symlinked roots);
  - [x] locations neither nested in one another nor inside the repo;
  - [x] writable;
  - [x] never a system location.
- [x] ↺ Changing a location goes only through the migration tool (§6), with RIFTWIRE
      stopped. There is no API to change locations, because a change needs both processes
      restarted.
- [x] API `GET /api/v1/storage` (no absolute paths in media endpoints; paths only on the
      storage settings view) and `PUT /api/v1/storage/locations/{kind}` for empty locations.

## 2. Volume identity
- [x] ↺ Marker `.riftwire-location.json` at each **location root**, because the internal
      volume root is not writable without admin rights. It holds the kind, location UUID,
      volume UUID, the OS volume UUID and the creation time. Written atomically and
      exclusively (`link`), never overwritten.
- [x] Each location records its volume UUID. Resolve the mount point (macOS `statfs`
      `f_mntonname` / `st_dev` change), then read the marker.
- [x] **Verify before every write, finalize, migration step and (later) deletion:** volume
      mounted, marker present, UUID matches.
- [x] **Never create directories at an absent mount point.** `ensure_dirs` refuses when the
      mount root is missing or not a mount (`os.path.ismount`), or when the marker is
      missing or different.
- [x] ↺ Filesystem check: **APFS required for every location on macOS, with no override**
      (owner decision). Filesystems without hard links fail permanently ("APFS required");
      there is no non-atomic fallback. The filesystem type is recorded. Case-sensitivity is
      not recorded: no code depends on it, since names are generated UUIDs.

## 3. Offline-volume handling
- [x] `/health/ready` reports `storage_offline:<kind>` per location. The app still starts and
      the library stays browsable (DB and thumbnails are internal).
- [x] Jobs that need an offline location move to `RETRY_WAIT` as a **transient** condition
      that does not consume `max_attempts`, with a `system_events` entry.
- [x] Remount detection (periodic check in the worker) resumes waiting jobs. Recovery runs
      the reconciler for that volume first.
- [x] Unplugging mid-write gives an I/O error, which is transient. Attempt-scoped
      temp/staging files are cleaned by token on remount. No partial final files.
- [x] Dashboard: offline banner per location, and the affected jobs are shown as waiting.

## 4. Disk reservations (persistent, per volume)
- [x] Alembic migration `disk_reservations`: id, volume_uuid, owner (import/job id + attempt
      token), bytes, created_at, lease_expires_at.
- [x] Admission in one `BEGIN IMMEDIATE` transaction:
      `free_now − Σ active reservations(volume) − need ≥ floor(volume)`. Shared by the API and
      the worker.
- [x] Released on completion or failure. Expired leases are reclaimed by the reconciler at
      startup and periodically. Reservations are fenced by attempt token, like job leases.
- [x] Long writes (uploads, copies, later downloads) re-check every 256 MB and abort as
      transient if the floor would be crossed.
- [x] Per-volume floor: default and minimum **60 GiB**. Settings below 60 GiB are refused
      outside the isolated test environment (the acceptance and smoke scripts' explicit
      test-only override is kept).
- [x] Replace the in-memory `RESERVATIONS`. Keep the Phase 1 API behaviour (HTTP 507
      `insufficient_storage`).

## 5. Crash-safe cross-volume transfers
- [x] One transfer primitive used by finalize and migration:
  - [x] stream copy to `<dest>/.staging/<id>.<token>.partial` on the **destination** volume;
  - [x] fsync the file;
  - [x] SHA-256 verify against the recorded hash;
  - [x] ↺ no-overwrite promote via atomic `link(2)` (kernel-level EEXIST; holds across
        processes without a lock). Verification re-reads the **staged file** through an fd
        kept open until promotion, and its identity is re-checked before linking.
  - [x] fsync the directory.
- [x] The source is never removed for recordings; only attempt-scoped temp files are
      removed, by token.
- [x] Same-volume fast path (link) is kept, and the layout default keeps temp on the
      recordings volume.
- [x] Crash matrix: real SIGKILL at every step (during copy, after copy before verify, after
      verify before promote, after promote before DB commit). The reconciler finishes or
      rolls back with no duplicate and no loss. Tested for same-volume and two-volume
      layouts.

## 6. Verified migration tool (`scripts/migrate_storage.py`)
- [x] **Dry run by default.** Shows the plan: files, bytes, destination free space minus
      floor, and estimated duration. `--execute` is required to copy.
- [x] Requires the worker stopped (single-worker lock) and the API in maintenance (no new
      imports).
- [x] ↺ Resumable journal **in the database** (`storage_migration_items`, PENDING →
      VERIFIED, committed per file) instead of JSON lines, so each step is transactional.
      Re-running skips verified files.
- [x] The location switch is **one DB transaction** after every file is verified. Then
      start and reconcile.
- [x] **Never deletes the old copies.** It prints a removal list for the owner. Rollback =
      switch the config back (the old copies are intact).
- [x] Tested only on synthetic data. **Migrating the owner's real data needs separate
      approval.**

## 7. Dashboard: Storage page
- [x] Per location: kind, path, volume label and UUID, online/offline, filesystem, measured
      capacity and free space, floor, reserved, headroom, usage per category.
- [x] Migration status (read-only).

## 8. Tests and acceptance
- [x] Unit: path validation, marker read/write, mount resolution, admission arithmetic,
      lease expiry, floor-setting validation.
- [x] Two-volume tests in CI with two separate filesystems (e.g. tmpfs plus disk on Linux),
      plus a simulated unmount (marker removed or changed, mount root missing).
- [x] **A11:**
  - [x] unmounted volume creates no directories;
  - [x] jobs wait, then recover on remount;
  - [x] per-volume floor enforced;
  - [x] migration verifies every byte and deletes nothing.
- [x] Phase 1 suite, acceptance and smoke stay green. Acceptance is extended with steps
      M1–M3: **22/22**. Smoke in the Linux container: 28 passed, 1 macOS-only skip.
- [ ] Mac verification:
  - [ ] internal disk only, first;
  - [ ] then, **with approval**, the SanDisk SSD, owner-formatted APFS, with an empty
        `riftwire` folder. No real data is moved.

## 9. Owner approvals needed before or within M1
1. Approve this checklist (start of M1 implementation).
2. Mac-side multi-volume test method: (a) owner-formatted SSD once it arrives, or (b) a small
   APFS disk image created with `hdiutil` (a file, not a drive format). Either needs
   explicit approval.
3. exFAT policy: refuse by default, allow with override (proposal).
4. Later and separately: migrating real recordings to the SSD.

## 10. Exit criteria
- All items above are checked, with tests.
- The macOS run is recorded in `docs/acceptance/`.
- Independent review passed.
- PR merged; tag `v0.3.0-m1` after verified `main`.

## 11. Acceptance checks that depend on the external SSD (deferred)

Verified now, without the SSD, using isolated data folders, a second real filesystem
(`/dev/shm`) for cross-volume paths, and a simulated macOS volume probe:
- marker identity;
- wrong-drive detection;
- the APFS rule;
- unmounted `/Volumes` paths;
- deferral without attempts;
- persistent reservations across processes;
- the transfer guarantees;
- migration (dry run, execute, resume, refusals).

**Still to verify on the Mac, internal disk only (possible now):**
- the full suite (`scripts/test.sh`);
- acceptance (22 steps);
- the smoke test at the real 60 GiB floor;
- that `diskutil` reports the internal volume as `apfs` with a VolumeUUID.

**Requires the SanDisk 1 TB SSD, formatted APFS by the owner** (separate approval; no real
recordings are moved):
1. **Real identity:** `diskutil` reports `apfs` and a VolumeUUID for the external drive. The
   marker and the recorded OS UUID match after re-plugging.
2. **Physical unplug while idle:**
   - the location goes offline as `volume_not_mounted`;
   - **no folder is created under `/Volumes`** or on the internal disk;
   - readiness returns 503 and the dashboard banner shows it;
   - jobs wait with no attempts used;
   - on re-plug: `storage_online` event, reconcile, and the jobs complete.
3. **Physical unplug mid-operation** (upload into `temp` on the SSD; cross-volume finalize
   copy into `recordings` on the SSD): real I/O errors give an aborted upload or a deferred
   job. No partial file appears at a final path, and recovery works after re-plug.
4. **Swapped drive:** a different physical drive mounted at the same path, with the RIFTWIRE
   folder copied onto it, is refused as `wrong_volume`.
5. **Throughput at realistic size:** cross-volume finalize of multi-GB synthetic recordings
   (internal temp → SSD recordings), including the time for SHA-256 verification.
6. **Migration to the SSD** of synthetic data: dry run, execute, interrupted run then
   resumed, final re-hash time, switch, then imports landing on the SSD. Originals remain.
7. **Per-volume floor and reservations** on the SSD's actual capacity (capacity-independent
   logic; 60 GiB floor on the SSD as well).
8. **Sleep/wake:** macOS can unmount external drives on sleep. Locations must go offline and
   come back cleanly.

