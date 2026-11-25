# M1 physical-SSD acceptance — `main` at `d9c878e` (checklist §11, checks 1–8)

Run by the owner on 2026-11-24 (EST evening; times below in UTC), on the target Mac (Apple M3
Pro, macOS 26.5), with the **SanDisk 1 TB Portable SSD**, formatted APFS by the owner
(`/Volumes/SANDISK SSD`, OS VolumeUUID `727641EE-8044-4FDD-BECC-AE2220870904`).

- **Code:** `main` at `d9c878ea86add7ce54eabf14579c79798cdd5108`, checked out in a separate
  git worktree. The scripts verify the commit before running.
- **Data:** synthetic media only, about 19 GiB of generated test video. An isolated library
  on the internal disk was deleted afterwards. A new test folder on the SSD was kept for
  inspection.
- **Floor:** the real 60 GiB floor applied on both drives.
- **Untouched:** the owner's real library, checkout and branch.

| # | Check | Result | Run |
|---|---|---|---|
| 1 | Real identity | **PASS:** APFS; the OS VolumeUUID equals the marker's `hw_volume_uuid` before unplugging, after each of three physical re-plugs, and after sleep | full, sleep |
| 2 | Physical unplug while idle | **PASS:** see below | full |
| 3 | Physical unplug mid-copy | **PASS:** see below | full |
| 4 | Swapped drive at the same path | **PASS:** see below | full |
| 5 | Throughput at realistic size | **PASS:** 4.78 GiB cross-volume finalize (internal temp → SSD recordings) including SHA-256 verification in 13 s, **376 MiB/s**; the upload to internal temp took 5 s | full |
| 6 | Migration to the SSD | **PASS:** see below | full, sleep |
| 7 | Per-volume floor and reservations | **PASS:** the SSD volume floor is 60 GiB (913 GiB free); the location is APFS; all imports were admitted against the SSD's reservations | full |
| 8 | Sleep/wake | **PASS:** see below | sleep |

**Check 2, unplug while idle (cable pulled without ejecting):**
- offline as `volume_not_mounted`;
- no folder at the mount path or on the internal disk;
- readiness 503 (the dashboard banner's source);
- an upload refused with `storage_offline`, nothing written to temp;
- a queued INGEST waited with 0 attempts used;
- after re-plug: `storage_offline` then `storage_online`, and the job completed on attempt 1.

**Check 3, unplug mid-copy:**
- the cable was pulled at 35% of a 7.4 GiB cross-volume finalize copy;
- the job went to `RETRY_WAIT` with 0 attempts used ("waiting for storage: recordings: the
  drive is not mounted"), and the recording was not READY;
- after re-plug, the copy was redone and SHA-256-verified, and the recording became READY;
- no partial or staging file was left on the SSD.

**Check 4, swapped drive:**
- a different APFS volume (a disk image, VolumeUUID `CD6108E4-…`) was mounted at the same
  path, with the RIFTWIRE folder and marker copied onto it;
- it was refused as `wrong_volume`, readiness was 503, an upload was refused, and nothing
  was written to it;
- the real SSD came back online after re-plug.

**Check 6, migration to the SSD:**
- a clean dry run (writes nothing);
- `--execute` was killed with SIGKILL after 1 file was promoted;
- the resumed run reused that file ("1 verified earlier"), the final re-hash matched for all
  3 files (5.48 GiB), and the location switched;
- the originals were kept on the internal disk;
- a new import landed on the SSD.

**Check 8, sleep/wake:**
- macOS's power log (`pmset -g log`) shows Sleep at 20:53:53 −0500 and Wake at 20:57:02;
  the full log extract is in `sleep-run/pmset-sleep-wake.txt`;
- after wake: the location was online, readiness 200, the identity matched, and an import
  landed on the SSD;
- the SSD stayed mounted through sleep.

## Limits of this evidence (stated, not hidden)
- **Check 3:** the mid-copy unplug covered the cross-volume finalize into recordings on the
  SSD. An unplug during an upload into a **temp location on the SSD** was not run, because
  temp stayed on the internal disk. That path uses the same transfer and offline handling,
  and is covered by the automated tests, but not physically.
- **Check 4:** the second drive was an APFS disk image, not a second physical drive. It is a
  genuinely different volume with its own OS VolumeUUID at the same mount path, which is
  exactly what the identity check distinguishes.
- **Check 8:** macOS did not unmount the SSD during this sleep (on battery, about 3 min), so
  the sleep test showed clean continuity. Offline-and-return handling is shown by the
  physical unplugs in checks 2 and 3.
- **Event check in check 8:** the script's check accepts any `Wake…` line in the power log.
  This log contains a real `Wake from Deep Idle` entry; it was checked by hand.

## Runs
- `full-run/`: started 01:47:50Z, all checks. Its check 8 is **superseded**: Enter was
  pressed without the Mac sleeping (2.5 s). The sleep-only script was then written to prove
  the sleep from the power log.
- `sleep-run/`: started 01:52:58Z. It repeats preflight and check 6 to put the library on the
  SSD, then runs check 8 with a 209 s sleep.
- Each folder has `summary.txt`, `results.json`, the driver log (`driver.txt`), the migration
  outputs and `storage-events.txt`. The last is the storage events extracted from the API and
  worker logs; the raw app logs are not committed (repository rule).
- `m1-ssd-acceptance.sh` and `m1-ssd-sleep.sh` are the exact scripts that ran (checksums
  match the copies on the Mac).

## Discarded runs (not evidence)
- **01:20:43Z and 01:22:23Z:** macOS privacy protection did not yet allow Terminal to write
  to the removable drive. RIFTWIRE's migration refused before writing anything (the first
  time at creating the folder, the second at its writability pre-check). The script was then
  changed to check write access first.
- **01:39:02Z:** all substantive checks passed, but the script asked the event API for 500
  items (the maximum is 200), so checks 2 and 8 crashed on their final event query. The
  storage events in that run's worker log were correct. The script was fixed and the run
  repeated as `full-run/`.
