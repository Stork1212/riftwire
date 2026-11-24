# Phase 1 — Independent Review Guide

For reviewers (human or AI) of PR #1 (`phase1/foundation` → `main`). Read with
[`ARCHITECTURE.md`](ARCHITECTURE.md) (approved design) and [`PHASE1_REPORT.md`](PHASE1_REPORT.md)
(results).

## 1. What is being reviewed

- Phase 1 only: local ingestion, metadata, thumbnails, persistent queue, dashboard, scripts, docs.
  No Phase 2 features, AI models, cloud services or real footage.
- **Verified on the target Mac** (M3 Pro, macOS 26.5.2, Python 3.13.2, FFmpeg 9.1) at commit
  `10ea57b` (includes the review-round-1 fixes): 169/169 pytest, 13/13 Vitest, build OK,
  19/19 acceptance steps at the 60 GiB floor. Evidence:
  `docs/acceptance/2026-11-24-macos-m3pro-10ea57b*`. Earlier run at `9d1e8c5`: 148/148 and 19/19.
- Changes **after** the Mac-verified commit `10ea57b`: docs and evidence files only. Check
  with `git diff --stat 10ea57b HEAD -- backend frontend scripts` (expected to be empty).

## 1a. Review round 1 (2026-11-24)

Both findings from the first review are fixed. See PHASE1_REPORT, "Review round 1", for
details and test names. To focus a second review:
- `git diff 6d16b85 HEAD -- backend/app` shows the code changes: `queue.py`, `ingest.py`,
  `media.py`, `handlers.py`, `worker.py`, `main.py`, `config.py`.
- New tests: `tests/test_queue.py` (section "review finding 1"), `tests/test_ingest.py`
  (section "review finding 2") and `tests/test_worker_process.py::test_hard_crash_during_copy_fallback_recovers`.
- Two key tests were run against the **old** `recover_stuck_jobs` (with only the test hook
  added) and **fail** there: the owner is killed and taken over, and the owner can still
  renew at kill time. They pass on the fix.

- A macOS run of `599f910` exposed a zombie-process edge case in the new `kill_failed`
  check (macOS `killpg` treats zombie-only groups as gone). It is fixed in `media.py`
  (`pid_running`, reaping in `kill_process_group`) and covered by a BSD-semantics
  emulation test. See PHASE1_REPORT, "macOS re-run after the review fixes".

## 2. Reproduce

```bash
git clone -b phase1/foundation https://github.com/Stork1212/riftwire.git && cd riftwire
scripts/bootstrap.sh                                   # project-local venv + npm ci
scripts/test.sh                                        # ruff, pytest, eslint, tsc, vitest, build
backend/.venv/bin/python scripts/acceptance.py         # 19-step real-process E2E (isolated data dir)
#   add --min-free-gib 1 on machines with < ~61 GiB free
```

## 3. The safety-critical code (review these closely)

| Requirement | Where | What to check |
|---|---|---|
| Crash-safe ingestion: intent row before file move | `backend/app/services/ingest.py` → `complete_upload` | `.part` → validate → rename to `.staged` → **one transaction** inserting recording (STAGED) + INGEST job; dedupe by SHA-256 unique constraint; temp cleaned on every failure path |
| Idempotent finalize, never overwrite | `ingest.py` → `finalize_recording`, `_copy_via_staging`, `promote_no_replace` | 4 cases (src only / both / dst only / neither); `os.link` refuses to overwrite. **Fallback without hard links:** attempt-unique `.staging/*.partial`, fsync, SHA-256 verify, `flock`-serialized exists-check + atomic rename (never overwrites), staging file always removed unless promoted |
| Startup reconciler deletes only RIFTWIRE temp/staging files | `ingest.py` → `reconcile` | `.part` / row-less `.staged` by age; attempt-scoped `job-*.tmp.*` and `.staging/*.partial` by **inactive lease token** (files listed *before* tokens are read); recordings are only **reported**. Startup runs recovery first, then the reconciler |
| No double processing | `backend/app/services/queue.py` → `claim_next`, `fenced` | Compare-and-set claim; new `lease_token` per attempt; every write goes through `fenced` (status RUNNING **and** matching token) |
| Lease recovery: fence → terminate → transition | `queue.py` → `recover_stuck_jobs`, `_recoverable` | Step 1 re-checks eligibility **and** swaps the token in one transaction before any kill (PID read in that transaction); `kill_failed` keeps the job fenced; step 3 is a CAS on the recovery token |
| Orphaned FFmpeg / PID reuse | `backend/app/services/media.py` → `run_command`, `kill_orphan_if_matches` | `start_new_session=True`; PID and `ps lstart` recorded via fenced write; kill only if start time **and** program name match |
| Single worker | `backend/app/workers/worker.py` → `WorkerLock`, `startup` | `flock` non-blocking; holding the lock justifies immediate recovery of foreign RUNNING jobs |
| Zombie attempts can't publish output | `backend/app/workers/handlers.py` → `handle_thumbnail` | attempt-unique temp file; `os.replace` inside the fenced completion |
| Content validation | `media.py` → `parse_probe`, `verify_decodable` | container family allow-list; real video stream (not cover art); decode at start **and** near end |
| No path leaks / localhost only | `media.py` → `_scrub`; `app/main.py`; `scripts/start.sh` | Error messages scrubbed; CORS allow-list; uvicorn `--host 127.0.0.1` |

## 4. Invariants a reviewer can test directly

1. Nothing under `recordings/` is ever deleted or overwritten by code. Look for `unlink`,
   `os.replace`, `os.rename` and `open(..., "w")` on recordings paths. Finalize only links,
   or renames a verified staging file into a **non-existent** name under a lock. The only
   deletions are attempt-scoped files inside `recordings/.staging/` and `.lock` files of
   recordings that are no longer STAGED.
2. Each job attempt's DB writes are fenced. Look for `fenced(` around progress, process,
   complete, fail and release.
3. No `shell=True` and no string commands: `grep -rn "shell=True" backend/app` → nothing.
4. The app never creates tables: no `create_all` in `backend/app`.
5. The 60 GiB default: `app/core/config.py` → `min_free_disk_bytes = 60 * GIB`.

## 5. Test map (169 backend + 13 frontend tests)

`docs/TESTING.md` maps every requirement to its tests. The real-process crash tests are in
`backend/tests/test_worker_process.py`, and the full scenario is in `scripts/acceptance.py`.

## 6. Known limitations (accepted for Phase 1)

See `PHASE1_REPORT.md` §6–8:
- `start.sh`/`stop.sh` and the browser UI were exercised in the dev container, not on the Mac.
- Multi-GB files have not been tested (owner instruction: small synthetic media only).
- Browser upload keeps a second copy of the file.
- About 9 GiB of import headroom above the 60 GiB floor until the external SSD exists.

## 7. Merge

**Squash-merge.** Commit `f6e2644` accidentally added a 3.8 MB `.whl` file (removed in
`c828a53`), and squashing keeps it out of `main`.
