# M2 Rights & Source Management: review package

_PR #12 (draft), branch `phase2/m2-rights-sources`. Implemented 2026-11-24 under owner
decisions R1–R6 ([`M2_RIGHTS_SOURCES_PLAN.md`](M2_RIGHTS_SOURCES_PLAN.md) §12). Design:
[`ARCHITECTURE.md`](ARCHITECTURE.md) §20. **Not merged; M3 not started.** The M1
physical-SSD gate remains open separately._

## 1. What M2 delivers, by owner priority

| Priority | Delivered | Main tests |
|---|---|---|
| Fail-closed rights evaluation | Only the current version authorizes. Strict v1 scope: unknown fields or versions make a record unusable. Every constraint is enforced or refused: uses, acquisition method, brand-channel scope and assignment, monetization, clip length (required when limited), platform, territories (always refuse). Attribution is an obligation the consumer must confirm | `test_rights.py` evaluation section (14 tests) |
| Revocation and supersession | Revocation is immediate and permanent. Editing creates a new attested version, and the old one stops authorizing in the same transaction. Revoking the head never revives an older version | `test_revocation_is_immediate_and_final`, `test_superseded_versions_cannot_authorize`, `test_fence_refuses_after_supersession` |
| Immutable provenance | 12 SQLite triggers. Attested rights, evidence and provenance can't be edited or deleted, even with direct SQL. Every change writes a provenance event, and a per-source chain API shows creator → rights → source → recordings | `test_rights_schema.py` (15), acceptance M2-6 (5/5 tampering attempts refused) |
| Worker enforcement and final-output authorization | Gated job types are declared (`JOB_USE`, test-enforced). Enqueue refuses unlinked or unauthorized media. The worker authorizes before running, and a refusal fails permanently with no retry, plus an event and provenance. The rights decision travels with the job, and `queue.complete` fences it inside the output transaction | `test_gated_job_never_queued_for_unlinked_media`, `test_worker_refuses_revoked_rights_permanently`, `test_output_fence_refuses_completion_after_revocation`, `test_output_fence_and_revocation_serialize` |
| Automated creator and source matching | `SourceMatcher` chain: existing canonical URL, Twitch clip login, file hash. Standing licences cover every new source from a creator's channels with no per-file attestation. A link resolves and the source is created with one click | `test_twitch_clip_matches_its_creator_channel`, `test_source_creation_auto_matches_and_dedupes`, `test_file_hash_matcher`, `test_api_import_with_standing_licence_links_every_file`, acceptance M2-2/M2-3/M2-5 |
| Evidence storage limits | 25 MB per file (refused before reading); 1 GiB aggregate (checked before receiving and under the write lock); persistent reservation within the 60 GiB floor; read-only, no-overwrite promotion; never proof | `test_evidence_*` (6, including 5 concurrent uploads against the aggregate) |
| Clean, functional dashboard | Sources (paste a link, automatic match, rights badge, one-click create), Source detail (decisions, licences, brand channels, media, provenance timeline), Creators & Rights (channels, attest with explicit confirmation, edit as a new version, revoke, evidence), Games & Channels, rights link on Import. Loading, error and empty states everywhere | `rights.test.tsx` (11) |

## 2. Acquisition (R1)
- **One contract for every method:** `AcquisitionProvider` per method, and every
  acquisition is checked twice, for content rights and for method permission.
- **Implemented in M2:** only providers fed by the existing upload path (own recordings,
  creator files, the owner's platform download).
- **Declared but unavailable** (501 `acquisition_unavailable`): `direct_link`,
  `platform_api`, `watch_folder` and `url_fetch`.
- **`url_fetch`** additionally needs written platform permission evidence, and the global
  switch is off by default.
- **No network:** no download code, no network calls, no yt-dlp.

## 3. Deviations from the plan (all minor)
- **Acceptance M2-6:** planned as "revocation during a gated job's output". M2 adds no real
  gated job type (analysis arrives in M4–M6), so an end-to-end run can't exercise it. That
  scenario is covered by unit tests with a test-only gated declaration: the fence and the
  thread race. M2-6 instead checks supersession and immutable history against the
  isolated database.
- **No seeded games:** the plan mentioned seeding `gta6`, but R5 says configure
  everything in the dashboard and seed nothing, so no game is seeded either.
- **Plain HTTP links refused:** the URL parser accepts `https` and scheme-less links only,
  as planned.

## 4. Pre-existing defect found during verification
- **What happened:** the container smoke test once reported the worker as `STOPPING`
  after a graceful stop. The Phase 1 shutdown did not wait for the heartbeat thread, so
  its final beat (which writes STOPPING) could land after the STOPPED write.
- **Not caused by M2:** previous runs passed only by timing.
- **Fix:** `_shutdown` joins the heartbeat thread before writing STOPPED. A deterministic
  regression test (`test_final_heartbeat_never_overwrites_stopped`) fails without the fix
  and passes with it. The smoke test then passed twice in a row.

## 5. Verification

| Where | Commit | Result |
|---|---|---|
| Cloud container (Linux) | head of PR #12 | `scripts/test.sh` exit 0: ruff clean; backend **367 passed, 1 skipped** (macOS-only), 1 third-party warning; bench **48**; frontend lint/typecheck clean, **28/28**; build OK. Acceptance **28/28** (`--min-free-gib 1`, test-only: the container has 28 GiB free). Alembic up/down/up + `alembic check` clean. Smoke (test-only 1 GiB floor) **28 PASS + 1 macOS-only SKIP**, twice |
| Mutation checks | — | Removing the output fence, the supersession check or the aggregate re-check each made the corresponding test fail |
| Owner's Mac | pending | Pinned script; this run also covers the final M1 cache and marker changes merged in `8e5b5e3` |

**Not yet verified:**
- the Mac run;
- a visual check of the new dashboard pages in a real browser (covered by component
  tests, not yet looked at);
- the M1 physical-SSD gate (unchanged).

## 6. Residual risks
- **Attestation is the owner's statement.** RIFTWIRE records it immutably and shows it; it
  can't verify a creator's permission. Evidence supports the statement but never
  replaces it.
- **Publishing (Phase 5) is not transactional with the platform.** The fence runs
  immediately before and after the upload. A revocation during the upload is flagged for
  takedown (plan §12.5).
- **Evidence commit failure:** if the database commit fails after `link`, an orphan
  evidence file can remain. It overwrites nothing.
- **Future migrations:** a `batch_alter_table` on the M2 tables would drop the
  immutability triggers. The migration docstring says such a migration must recreate them.
- **Matching is offline.** YouTube video URLs don't name their channel, so the owner picks
  it until the M3 official-API matcher.
