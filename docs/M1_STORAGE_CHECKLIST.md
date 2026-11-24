# M1: Storage Architecture, implementation checklist

_Status: **prepared for owner review (2026-11-24). Implementation is not started.** It
begins only after the owner approves this checklist. Scope: Phase 2, milestone M1
([`PHASE2_PROPOSAL.md`](PHASE2_PROPOSAL.md) §3 M1, §8, acceptance A11)._

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
- [ ] Location kinds: `recordings`, `temp`, `models`, `exports`, `assets`. The database and
      logs stay on the internal disk.
- [ ] Settings `RIFTWIRE_<KIND>_DIR` (default: under `RIFTWIRE_DATA_DIR`, so the current
      layout is unchanged). Read only via `get_settings()`.
- [ ] Alembic migration `storage_locations`: kind, path, volume_uuid, floor_bytes, fs_type,
      created_at and updated_at.
- [ ] Path validation:
  - [ ] absolute and canonical (no `..`, no symlinked roots);
  - [ ] locations neither nested in one another nor inside the repo;
  - [ ] writable;
  - [ ] never a system location.
- [ ] Changing a location that already holds files is refused through the API. It goes only
      through the migration tool (§6).
- [ ] API `GET /api/v1/storage` (no absolute paths in media endpoints; paths only on the
      storage settings view) and `PUT /api/v1/storage/locations/{kind}` for empty locations.

## 2. Volume identity
- [ ] Marker `.riftwire-volume` at the location's volume root: JSON with `uuid`, `label`,
      `created_at` and `riftwire_version`. Written atomically (temp file → fsync → rename) and
      never overwritten.
- [ ] Each location records its volume UUID. Resolve the mount point (macOS `statfs`
      `f_mntonname` / `st_dev` change), then read the marker.
- [ ] **Verify before every write, finalize, migration step and (later) deletion:** volume
      mounted, marker present, UUID matches.
- [ ] **Never create directories at an absent mount point.** `ensure_dirs` refuses when the
      mount root is missing or not a mount (`os.path.ismount`), or when the marker is
      missing or different.
- [ ] Filesystem check: APFS required for recordings and temp (hard links, rename
      atomicity). exFAT is refused unless an explicit override is set, which forces the
      staged-copy path. Case-sensitivity is detected and recorded.

## 3. Offline-volume handling
- [ ] `/health/ready` reports `storage_offline:<kind>` per location. The app still starts and
      the library stays browsable (DB and thumbnails are internal).
- [ ] Jobs that need an offline location move to `RETRY_WAIT` as a **transient** condition
      that does not consume `max_attempts`, with a `system_events` entry.
- [ ] Remount detection (periodic check in the worker) resumes waiting jobs. Recovery runs
      the reconciler for that volume first.
- [ ] Unplugging mid-write gives an I/O error, which is transient. Attempt-scoped
      temp/staging files are cleaned by token on remount. No partial final files.
- [ ] Dashboard: offline banner per location, and the affected jobs are shown as waiting.

## 4. Disk reservations (persistent, per volume)
- [ ] Alembic migration `disk_reservations`: id, volume_uuid, owner (import/job id + attempt
      token), bytes, created_at, lease_expires_at.
- [ ] Admission in one `BEGIN IMMEDIATE` transaction:
      `free_now − Σ active reservations(volume) − need ≥ floor(volume)`. Shared by the API and
      the worker.
- [ ] Released on completion or failure. Expired leases are reclaimed by the reconciler at
      startup and periodically. Reservations are fenced by attempt token, like job leases.
- [ ] Long writes (uploads, copies, later downloads) re-check every 256 MB and abort as
      transient if the floor would be crossed.
- [ ] Per-volume floor: default and minimum **60 GiB**. Settings below 60 GiB are refused
      outside the isolated test environment (the acceptance and smoke scripts' explicit
      test-only override is kept).
- [ ] Replace the in-memory `RESERVATIONS`. Keep the Phase 1 API behaviour (HTTP 507
      `insufficient_storage`).

## 5. Crash-safe cross-volume transfers
- [ ] One transfer primitive used by finalize and migration:
  - [ ] stream copy to `<dest>/.staging/<id>.<token>.partial` on the **destination** volume;
  - [ ] fsync the file;
  - [ ] SHA-256 verify against the recorded hash;
  - [ ] no-overwrite promote (flock + exists check + rename);
  - [ ] fsync the directory.
- [ ] The source is never removed for recordings; only attempt-scoped temp files are
      removed, by token.
- [ ] Same-volume fast path (link) is kept, and the layout default keeps temp on the
      recordings volume.
- [ ] Crash matrix: real SIGKILL at every step (during copy, after copy before verify, after
      verify before promote, after promote before DB commit). The reconciler finishes or
      rolls back with no duplicate and no loss. Tested for same-volume and two-volume
      layouts.

## 6. Verified migration tool (`scripts/migrate_storage.py`)
- [ ] **Dry run by default.** Shows the plan: files, bytes, destination free space minus
      floor, and estimated duration. `--execute` is required to copy.
- [ ] Requires the worker stopped (single-worker lock) and the API in maintenance (no new
      imports).
- [ ] Resumable journal (JSON lines) per file: copied → verified (SHA-256 vs DB) → done.
      Re-running skips verified files.
- [ ] The location switch is **one DB transaction** after every file is verified. Then
      start and reconcile.
- [ ] **Never deletes the old copies.** It prints a removal list for the owner. Rollback =
      switch the config back (the old copies are intact).
- [ ] Tested only on synthetic data. **Migrating the owner's real data needs separate
      approval.**

## 7. Dashboard: Storage page
- [ ] Per location: kind, path, volume label and UUID, online/offline, filesystem, measured
      capacity and free space, floor, reserved, headroom, usage per category.
- [ ] Migration status (read-only).

## 8. Tests and acceptance
- [ ] Unit: path validation, marker read/write, mount resolution, admission arithmetic,
      lease expiry, floor-setting validation.
- [ ] Two-volume tests in CI with two separate filesystems (e.g. tmpfs plus disk on Linux),
      plus a simulated unmount (marker removed or changed, mount root missing).
- [ ] **A11:**
  - [ ] unmounted volume creates no directories;
  - [ ] jobs wait, then recover on remount;
  - [ ] per-volume floor enforced;
  - [ ] migration verifies every byte and deletes nothing.
- [ ] Phase 1 suite, acceptance (19/19) and smoke (30/30) stay green. Acceptance is extended
      with storage steps.
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
