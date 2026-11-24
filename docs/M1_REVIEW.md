# M1 Storage Architecture: final engineering review

_PR #9 (draft), branch `phase2/m1-storage`. Internal review performed 2026-11-24 after the
owner's Mac verification (§1–§5). The independent review of code commit `4703e22` and its
corrections are in §6, which supersedes §3–§4 where they differ. The physical-SSD acceptance
gate remains open (§5)._

## 1. Scope reviewed

Each area was read end to end and probed with targeted tests:
- volume identity;
- mount-point races;
- persistent reservations;
- concurrent transfers;
- crash recovery;
- migration integrity;
- floor coverage of every write;
- no-fallback guarantees.

Design reference: [`ARCHITECTURE.md`](ARCHITECTURE.md) §19. The write inventory is §19.8 and
the residual risks are §19.9.

## 2. Findings and fixes

**Found by the owner's Mac runs** (before this review):

| # | Severity | Finding | Fix | Test |
|---|---|---|---|---|
| A | **High** | `diskutil info` rejects plain folders; the error crashed API/worker startup on macOS | Mount point from `df -P`, fs from `mount`, UUID from `diskutil <mount point>`; probe failures never crash startup | `test_darwin_probe_*`, `test_probe_failure_never_crashes_startup`; Mac re-run 22/22 |
| B | Low (test-only) | Simulated probe invented an internal-disk UUID; acceptance compared `/var` vs `/private/var` | Simulated probe uses the real probe for real disks; canonical-path comparison | Mac re-run green |

**Found by this review:**

| # | Severity | Finding | Fix | Test |
|---|---|---|---|---|
| 1 | **High** | With `temp` on another drive, **every thumbnail failed** (`EXDEV`): rendered in temp, then renamed into the internal thumbnails folder | Rendered next to the final thumbnail (`thumbnails/.job-<id>-<token>.tmp.jpg`); THUMBNAIL no longer needs `temp`; dead temp files reconciled by token | `test_thumbnails_work_when_temp_is_on_another_drive` (real 2nd filesystem), `test_reconcile_cleans_only_dead_thumbnail_temp_files` |
| 2 | **High** | Migration used `mkdir(parents=True)` for the destination and subfolders. A drive unplugged mid-run could have the path **recreated on the internal disk** | Folders created one level at a time below a verified root; destination creation re-checks the planned anchor's device; identity re-checked before each file, promotion and final re-hash | `test_migration_never_recreates_vanished_destination`, `test_create_destination_refuses_if_anchor_changed` |
| 3 | Medium | Crash between writing the destination marker and the journal row left a destination the tool then refused as "not empty" | Journal row first; marker written (or re-written) on resume | `test_migration_resumes_after_hard_crash_mid_copy` |
| 4 | Medium | Resume after a **hard** crash mid-copy failed on its own leftover `.partial` (same token, `O_EXCL`) | Own stale partial removed before each copy (safe: maintenance lock) | same test |
| 5 | Medium | Resume trusted journal rows marked VERIFIED even if the file had vanished from the destination | VERIFIED rows re-checked (exists and size) on resume, else re-copied; final re-hash reports missing files cleanly | `test_resume_recopies_verified_files_missing_at_destination` |
| 6 | Medium | Reservation leases (15 min) were not renewed during one long copy (cross-volume finalize, migration), so a lease could expire mid-copy and let others breach the floor | `reservations.Keeper` renews with remaining bytes during the copy; a floor breach aborts the copy cleanly | `test_keeper_renews_reservation_during_long_writes` |
| 7 | Medium | Thumbnail writes were not floor-gated | 4 MiB reservation on the internal volume per thumbnail | full suite (all thumbnail paths) |
| 8 | Medium | An API started during a migration ran recovery and the reconciler, which could delete source files being migrated (stale `temp/*.part`) | Startup skips recovery and cleanup while the maintenance lock is held, with an event | `test_api_startup_during_migration_never_cleans_up` |
| 9 | Low | Window between verification and promotion | `transfer_file(guard=…)` re-verifies the destination identity immediately before `link` (finalize and migration) | `test_transfer_guard_runs_before_promotion`, `test_finalize_rechecks_recordings_drive_before_promotion` |
| 10 | Low | `Settings.location()` returned the internal default path for an unregistered kind. No write path used it unguarded, but it was a latent fallback | Raises `StorageOffline` once a layout is loaded; reconciler never touches unregistered kinds | `test_probe_failure_never_crashes_startup` |
| 11 | Low | A vanished destination surfaced as a raw `FileNotFoundError` | Clean "drive no longer available" refusal | `test_migration_never_recreates_vanished_destination` |

Caught during the fix itself: listing the thumbnail temp files *after* reading the active
tokens would race with a just-claimed job. Corrected to the Phase 1 rule (list files first,
then read tokens) before commit.

**Documentation inconsistencies fixed:**
- `TROUBLESHOOTING.md` still advised moving data with `rsync` and `RIFTWIRE_DATA_DIR`. That
  is unsafe after M1, because locations are recorded with full paths. Replaced with the
  migration tool, plus a new "Storage offline" section.
- `PHASE2_PROPOSAL.md` still offered an exFAT override and named the old marker file.
- `ROADMAP.md` and `CLAUDE.md` status lines were out of date.
- `CLAUDE.md` rule 4 described the Phase 1 same-volume rename only.
- `ARCHITECTURE.md` §2 recommended moving `RIFTWIRE_DATA_DIR`.
- `PHASE1_REPORT.md`'s copy-fallback note is now marked superseded.

## 3. Guarantees confirmed

- **60 GiB floor on every storage write:**
  - uploads, cross-volume finalize, thumbnails and migrations go through persistent
    per-volume reservations;
  - same-volume links and renames add no data;
  - a floor below 60 GiB is refused unless `RIFTWIRE_ENV=test`;
  - explicit, documented exception: the SQLite database and capped rotating logs on the
    internal disk (§19.8).
- **No silent fallback to internal storage:**
  - every path resolves through the loaded layout;
  - unregistered kinds raise;
  - offline locations refuse writes with 503 / deferral;
  - no folder is created under an absent `/Volumes/<name>`;
  - no `parents=True` below a location root;
  - external roots are never recreated.
- **Concurrent transfers:** `link(2)` promotion. Exactly one winner across 16 threads and
  6 processes; the staged file is verified via a held fd plus an identity re-check.
- **Crash recovery:** a real-SIGKILL matrix at every transfer and finalize crash point; the
  reconciler is token-based and skips offline locations; migration is resumable at every
  point (journal first).
- **Migration integrity:**
  - per-file SHA-256 (recordings against the import hash);
  - full re-hash before the switch;
  - the switch is one transaction;
  - originals are never deleted.

## 4. Verified

| Where | Commit | Result |
|---|---|---|
| Owner's Mac (macOS 26.5.2, M3 Pro, FFmpeg 9.1), internal disk, **60 GiB floor** | `9fc026f` | `scripts/test.sh`: backend 208 + 2 skipped (2nd-filesystem tests), bench 47 + 1 skip, frontend 17, build. **Acceptance 22/22.** Smoke 30/30 (`29f612e`). Evidence: `docs/acceptance/2026-11-24-macos-m1-9fc026f-*` |
| **Owner's Mac, reviewed code** (macOS 26.5.2, M3 Pro, Python 3.13.2, FFmpeg 9.1, APFS internal disk), **60 GiB floor** | **`4703e22`** | Pinned run (HEAD verified, clean tree before and after, `pipefail`, per-stage exit codes all **0**): ruff clean; backend **218 passed, 3 skipped** (the three 2nd-filesystem tests; no second filesystem on the Mac), 1 warning; bench **47 passed, 1 skipped**; frontend lint/typecheck clean, **17/17**; build OK; **acceptance 22/22** (incl. M1–M3, VideoToolbox detected); **smoke 30 PASS, 0 FAIL, 0 SKIP**. Free space 69.80 GiB before and after. Evidence: `docs/acceptance/2026-11-24-macos-m1-4703e22/` |
| Cloud container (Linux), after this review | `4703e22` | Backend **221** passed (incl. 50 storage tests; a real second filesystem `/dev/shm` for cross-volume), frontend 17, build, acceptance **22/22**, smoke 28 + 1 macOS-only skip |

The review fixes (§2, rows 1–11) are verified on the Mac and in the container. The three
cross-drive tests that skip on the Mac (thumbnails with external temp, cross-volume transfer
and migration paths) ran and passed in the container on a real second filesystem; on real
hardware they are covered by the SSD gate (§5).

**Warnings (Mac run):** one, third-party: Starlette's `StarletteDeprecationWarning` about
`httpx` in `starlette.testclient` (test client only, no runtime effect). Not acted on: changing
the test HTTP client would be a new dependency, which needs approval. No warnings in acceptance
or smoke.

## 5. Open gate: physical-SSD acceptance (unchanged)

Requires the SanDisk 1 TB SSD, formatted APFS by the owner. No real recordings are moved.
1. Real APFS identity (`diskutil` UUID) on the external drive, stable across re-plugging.
2. Physical unplug while idle: offline, **no folder created**, jobs wait, recovery on
   re-plug.
3. Physical unplug mid-upload and mid-copy (real I/O errors): no partial final files;
   deferral; recovery.
4. Swapped-drive refusal (different VolumeUUID at the same mount path).
5. Multi-GB cross-volume finalize throughput, including verification time.
6. Migration to the SSD: dry run, execute, interrupted run then resume, re-hash, switch,
   imports landing on the SSD.
7. Floor and reservations on the SSD's real capacity.
8. Sleep/wake unmount behaviour.

**Merge policy (proposed):** PR #9 can merge once it passes review and the Mac re-run, so M2
can build on it. The tag `v0.3.0-m1` and the declaration that M1 is complete wait for this
gate. Until then `PROJECT_STATUS.md` shows M1 as "merged, SSD acceptance pending". The
owner decides.

## 6. Independent review of `4703e22`: findings and corrections

Every finding was validated against the source **before** any change. Each was then
reproduced with a regression test that fails on `4703e22` and passes after the fix. The
regression tests are in `backend/tests/test_storage_m1_review.py`.

To check this, the test file was run against an untouched checkout of `4703e22`:
- **15 of its 18 tests fail.** Each fails with the reported defect, not a missing name.
  For example, three concurrent unknown-length uploads went **7 MiB below the floor**, and
  resume onto a replacement drive with a copied marker was **not refused**.
- **The other 3 pass there by design.** They guard related behaviour that was already
  correct (an upload is refused during maintenance, exFAT is still refused, an expired
  reservation must pass the floor check again).

### 6.1 Findings

| # | Sev. | Validated in source (`4703e22`) | Correction | Regression tests |
|---|---|---|---|---|
| 1 | High | **Confirmed.** `migration.py` `dest_guard()` compared only the marker's location UUID, and the switch recorded whatever drive was mounted at that moment. A copy of the destination folder on another drive at the same mount path would resume and switch | Journal column `dest_hw_volume_uuid` (Alembic **0003**). It is recorded when the destination is first identified and compared against a fresh probe on resume (`plan` and `execute`), before each file, promotion, cleanup and final re-hash, and immediately before the switch. The switch records the approved UUID | `test_f1_copied_marker_on_replacement_drive_never_resumes`, `test_f1_drive_swap_mid_copy_stops_before_promotion`, `test_f1_swap_after_final_rehash_never_switches`, `test_f1_switch_records_the_approved_drive` |
| 2 | High | **Confirmed.** `DarwinProbe` returned `hw_volume_uuid=None` when diskutil failed. `register_root` accepted it, and `verify` skipped the UUID check for such rows permanently. **Cache confirmed:** a 10 s cache keyed by `st_dev` could return a previous drive's UUID after a remount that reuses the device number | On macOS a valid VolumeUUID is required: `IdentityUnavailable` makes registration fail, the location report `probe_failed`, and migration refuse. Rows without a UUID report `identity_unrecorded`. Internal defaults get their UUID recorded at startup; external locations are never adopted. UUIDs are validated and normalised. The device-number cache is replaced by one keyed by the **volume root's identity** (device, root path, root inode, root creation time; 10 s; macOS only), so a replacement drive at the same path always misses it. Removing the cache entirely was tried first, but made the Mac test run 7× slower (61 s → 472 s) | `test_f2_no_registration_without_os_volume_uuid`, `test_f2_registered_without_identity_fails_closed`, `test_f2_identity_is_never_served_from_a_cache`, `test_f2_cache_is_keyed_by_volume_root_identity`, `test_f2_volume_root_key_walks_to_the_mount_root`, `test_darwin_probe_fails_closed_without_a_valid_volume_uuid`, `test_f2_real_macos_disk_has_a_valid_volume_uuid` (macOS only) |
| 3 | High | **Confirmed.** Unknown-length uploads reserved 0 bytes. The floor was re-checked only *after* writing, every 256 MB or 60 s, so uploads shorter than that were never checked. Concurrent uploads could each overshoot | Admission **before** every write: the full length up front if known, otherwise 256 MiB windows admitted before the bytes are written. Leases are renewed before the next write, and an expired lease must be re-admitted first. Final floor check at `finish()` | `test_f3_concurrent_unknown_length_uploads_never_cross_the_floor` (3 threads), `test_f3_short_unknown_upload_needs_admission_before_any_byte`, `test_f3_floor_enforced_at_finish`, `test_f3_no_write_after_the_lease_expired_without_readmission` |
| 4 | Medium | **Confirmed.** `plan()` used `need = total` even when resuming | `remaining_need()`: only files without a full-size copy at the destination count (those copies are still verified by hash), minus the run's own stale partials. Peak usage never exceeds that figure, because each partial is removed before its file is copied. The reservation uses the same per-file figures | `test_f4_resume_needs_only_the_remaining_bytes`: 1.6 MiB headroom finishes the resume; 1.4 MiB is refused; the full 3 MiB is no longer required |
| 5 | Medium | **Confirmed.** `read_marker()` returned any JSON value, and `verify()` called `.get` on it. A marker containing `[]` raised `AttributeError` at startup, in readiness and in the worker | Strict validation: exact keys, `format == 1` as an integer, a known kind, UUID fields, `hw_volume_uuid` null or a valid volume UUID, a ≤ 64 KiB cap, UTF-8 JSON. Invalid markers report `marker_unreadable` and never raise. Registration and migration refuse cleanly, and markers are validated before writing | `test_f5_invalid_markers_report_offline_without_crashing` (18 malformed variants × startup, readiness, storage API, upload, reconcile), `test_f5_invalid_marker_refused_by_registration_and_migration` |

No finding was disputed.

### 6.2 Additional audit items

| Item | Result |
|---|---|
| Reservation renewal after lease expiry | **Uploads:** renewal now happens *before* the next write, and re-admission after reclamation goes through the full floor check, so nothing is written under an expired reservation (`test_f3_no_write_after_the_lease_expired_without_readmission`, `test_audit_reservation_renew_after_expiry_readmits_with_floor`). **Long copies** (`Keeper`): renewed between chunks at least every 60 s against a 15 min lease. Residual: one chunk whose write itself stalls longer than the lease. Documented in ARCHITECTURE §19.9 |
| Check-then-write / check-then-delete during drive replacement | Not fully closable, and documented exactly in ARCHITECTURE §19.9. Writes go through descriptors opened after verification, so after a swap they fail rather than land on the new drive. Promotion is re-verified immediately before `link`, which never overwrites. Deletions only touch RIFTWIRE-generated temporary names in a just-verified location. The switch is guarded immediately before its transaction |
| Maintenance lock vs. active writers | **Ordering gap found and fixed.** `execute()` checked for a running API *before* taking the maintenance lock, leaving a window for an API (and an upload) to start in between. Now the maintenance lock is taken first, then the worker lock, then the API check. Uploads and worker start already refuse while the lock is held (`test_audit_maintenance_lock_is_taken_before_checking_for_writers`, `test_audit_upload_started_after_lock_is_refused`) |
| Unchanged requirements | 60 GiB floor (the container smoke run at the real floor correctly refused imports with **507**, having 28 GiB free), APFS-only (`test_audit_exfat_still_refused_with_strict_identity`), $0, and the eight SSD checks (§5) |

### 6.3 Verification of the corrections (cloud container, Linux)

| Check | Result |
|---|---|
| `scripts/test.sh` (exit 0) | ruff clean; backend **241 passed, 1 skipped** (macOS-only test), 1 third-party warning; bench **48 passed**; frontend lint/typecheck clean, **17/17**; build OK |
| Alembic | upgrade → downgrade 0002 → upgrade; `alembic check`: no drift; head `0003` |
| Acceptance (`--min-free-gib 1`, test-only floor: the container has 28 GiB free) | **22/22** (exit 0) |
| Smoke at the **production 60 GiB floor** | imports refused with 507 as designed (only 28 GiB free) |
| Smoke with a test-only 1 GiB floor | **28 PASS, 0 FAIL, 1 SKIP** (caffeinate, macOS only), exit 0 |

**Mac run of `25a2734`** (evidence: `docs/acceptance/2026-11-24-macos-m1-25a2734/`):
- **Tests:** exit 0. Backend **237 passed, 3 skipped** (second filesystem), including the
  macOS-only identity test. Bench 47 passed, 1 skipped. Frontend 17/17. Build OK.
- **Acceptance:** **22/22** at the 60 GiB floor.
- **Problems it exposed:**
  - **Speed regression:** the test suite took **472 s** instead of 61 s. Removing the
    identity cache meant every check ran `df`, `mount` and `diskutil`. The root-keyed cache
    in the next commit fixes it.
  - **Smoke test:** exit 1 in 0 s. Port 8000 was still held by the smoke instance the
    previous run had left running for its dashboard check. This was environmental, not a
    test result.

**Not yet verified:**
- a **Mac re-run of the commit that contains the cache fix**;
- the macOS-only identity test;
- the physical-SSD gate (§5).
