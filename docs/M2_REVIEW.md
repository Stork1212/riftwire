# M2 Rights & Source Management: review package

_PR #12 (draft), branch `phase2/m2-rights-sources`. Implemented 2026-11-24 under owner
decisions R1–R6. The independent review of `9cbe705` and its corrections are in §7, which
supersedes earlier sections where they differ ([`M2_RIGHTS_SOURCES_PLAN.md`](M2_RIGHTS_SOURCES_PLAN.md) §12). Design:
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
| Owner's Mac (macOS 26.5.2, M3 Pro, APFS internal disk, **60 GiB floor**) | `9cbe705` | All stages exit 0, clean tree before and after: backend **365 passed, 3 skipped** (second filesystem), including the macOS volume-identity test; bench 47 + 1; frontend **28/28**; build; acceptance **28/28** (M2-1 to M2-6); smoke **30/30**. Also covers the M1 cache and marker changes merged in `8e5b5e3`. Evidence: `docs/acceptance/2026-11-24-macos-m2-9cbe705/` |

**Not yet verified:**
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

## 7. Independent review of `9cbe705`: findings and corrections

Each suspected backend defect was first written as a regression test and run against
`9cbe705`. Only reproduced defects were fixed. Tests: `backend/tests/test_rights_review.py`.

### 7.1 Backend

| # | Finding | Reproduced on `9cbe705`? | Correction |
|---|---|---|---|
| A | The actual acquisition method was not preserved | **Yes.** The method used to attach media was only recorded in provenance. Analysis checked the source's *declared* method. Media attached as `creator_file` stayed analysable after the licence stopped permitting `creator_file` (`Decision(allowed=True)`) | New column `recordings.acquisition_method` (migration **0005**), backfilled from the source for media linked before it existed. `attach_media` records the method actually used. Analysis, editing and publishing use it, and it travels with the decision, so the output fence re-checks it too |
| B | Creator authority for source-specific rights | **Yes.** A licence with coverage `sources` could name another creator's source | Coverage `sources` is accepted only for sources on the record creator's own channels (`does not belong to this creator`) |
| C | The badge mixed up "licence active" with "authorized" | **Yes.** A source whose licence allowed only `acquire`, or whose scope could not be read, showed "Rights OK" | `rights_status` now reports only the licence state: `active`, `expires_soon`, `unusable` (in force but unreadable, or the creator is archived), `expired`, `revoked`, `not_attested`, `not_yet_valid`, `none` or `blocked`. A new `authorized_uses` lists what is allowed now (`acquire`, `analyze`). Edit and publish are always checked per clip, platform and brand channel and are never shown as blanket permissions |
| D | Refusals were not actionable | **Yes.** Refusals gave a reason but no next step | Every reason has an action. It appears in `DecisionOut.action`, in `RightsRefused` messages and `details.action`, and in the dashboard, linked to the right page |
| E | URL validation | **Partly.** A link naming two different YouTube ids (`v=A&v=B`) was accepted using the first id. Encoded-parameter smuggling was already refused (invalid id) | Two or more different `v` values are refused (`ambiguous_video_id`); repeated identical ids are still fine |

**Checked and found sound (no change):**
- **Concurrent supersession:** four parallel edits of one licence produce exactly one
  new version, and three get `ConflictError`. Now covered by a test.
- **Revocation races:** worker start, output fence and import linking (each in its own
  `BEGIN IMMEDIATE`) were re-read and are unchanged.
- **Output authorization:** the fence now also carries the media's acquisition method.
- **Evidence storage:** limits, reservation and read-only promotion were re-read. The
  orphan-file residual on commit failure is documented in §6.
- **Database immutability:** the 12 triggers survive migration 0005. It adds a column to
  `recordings` only, which has no triggers, and the migration test confirms the count.

### 7.2 Frontend

| Finding | Correction |
|---|---|
| Import offered only a local file | Two modes. **Local file** is unchanged, with an optional rights link. **Video URL** resolves the link to a source (existing, matched or new), then asks for the footage file and how it was obtained (the creator's file, or your own Studio or Twitch download). There is **no download control**, and the page says RIFTWIRE does not download from YouTube or Twitch |
| No path when no creator matches | Inline **Set up a new creator** form (name, relationship, channel handle pre-filled from a Twitch link). It states that the footage stays unusable until a licence is attested |
| Stale URL resolutions | Each check has a generation number. A reply for an older link is ignored, and editing the link invalidates replies in flight. A test with a deliberately late reply fails without the guard (checked by temporarily removing it) |
| The frontend and backend validated URLs differently | The browser's `type="url"` check refused scheme-less links the backend accepts (`youtu.be/…`). It is now a text field, so the backend's validator is the only rule set |
| Empty states and rights errors | The Sources empty state lists the three setup steps with links. `ActionableError` shows the backend's reason and next step, with a link to the page that fixes it. The source page shows authorized uses and the next step for each refused use |

### 7.3 Interface direction (not M3 work)
- **Navigation:** grouped into **Workspace** (Overview, Import, Sources, Recordings),
  **Management** (Creators & Rights, Games & Channels, Storage) and **System**
  (Processing Queue, System).
- **Project workflow:** the unified workflow, configurable defaults (RumbleFrame is
  configuration, never code) and the proposed data model are in
  [`M3_INTERFACE_DIRECTION.md`](M3_INTERFACE_DIRECTION.md), for owner review.
- **Nothing unavailable is shown as an active control.**

### 7.4 Verification of the corrections (cloud container)
| Check | Result |
|---|---|
| `scripts/test.sh` (exit 0) | ruff clean; backend **376 passed, 1 skipped** (macOS-only), 1 third-party warning; bench **48**; frontend lint/typecheck clean, **35/35**; build OK |
| Alembic | up → down to 0004 → up; `alembic check` clean; 12 triggers present after 0005 |
| Acceptance (`--min-free-gib 1`, test-only) | **28/28** |
| Smoke (test-only 1 GiB floor) | 28 PASS + 1 macOS-only SKIP |
| Regression proof | 6 of the 9 new backend tests fail on `9cbe705` (the other 3 cover behaviour confirmed sound); the stale-response frontend test fails with the guard removed |

**Mac run of `55ae624`** (evidence: `docs/acceptance/2026-11-24-macos-m2-55ae624/`):
- **All stages exit 0;** clean tree before and after.
- **Tests:** backend **374 passed, 3 skipped** (second filesystem), including the macOS
  identity test. Bench 47 + 1, frontend **35/35**, build OK.
- **Acceptance:** **28/28** at the 60 GiB floor.
- **Smoke:** **30/30**.

## 8. Dashboard layout refinement (owner request, within M2)

Frontend-only. No backend, API, schema or dependency changes; all functionality kept.

### 8.1 What was wrong (found in a real browser)
Every page was checked in headless Chrome at **1440×900, 1280×800 and 1920×1080**
(13 views × 3 sizes = 39 page views), with an automated check for page-level horizontal
scrolling, elements beyond the viewport, clipped text, overlapping controls and overlapping
text, plus tables that scroll inside their panel.

- **All 39 views failed before.** The grouped navigation added in `55ae624` broke the
  sidebar: the groups rendered inline and overlapped page content on every page. This was
  a regression I introduced; the jsdom tests could not see it.
- Page-level horizontal scrolling on Recordings (1280) and Processing Queue (1280, 1440).
- Cramped forms and tables; dates wrapping onto three lines; long filenames and URLs
  pushing columns off-screen; the System worker badge overlapping its heartbeat text.

### 8.2 What changed
- **Global tokens** in `styles.css`:
  - spacing 4 / 8 / 12 / 16 / 24 / 32 / 48 px;
  - a type scale (11.5 to 24 px);
  - fixed control heights (36 px, 30 px small);
  - sidebar and content-width tokens.
- **Shell:**
  - a grid sidebar with labelled groups and an accent bar on the active item;
  - `min-width: 0` on the main column, which removes the page-level scrolling;
  - a single-column layout below 860 px.
- **Reusable components and classes:**
  - layout and header: `PageHeader` (title, description, optional back link, actions), `Field`, `.grid-2` (stacks below about 1100 px of content), `.stack`, `.toolbar`, `.subsection`;
  - forms and lists: `.form` / `.form-row`, `.item-list`, `.hint`;
  - text: `.wrap-anywhere` / `.truncate` / `.nowrap`;
  - tables: `cell-primary` / `cell-status` / `num`.
  - All 11 pages use them; page-specific inline styles were removed.
- **Tables:** taller rows and more padding, with tighter padding below 1500 px. Long
  names wrap inside a bounded primary column. Dates do not wrap.
  - Processing Queue now has the progress bar under the status badge.
  - It also combines Started and Completed into one Timing cell, so all columns fit at 1280.
- **Small fixes:**
  - Ghost buttons have a visible outline.
  - System key/value rows wrap instead of overlapping.
  - Games use a divided item list.

### 8.3 Verification (cloud container, headless Chrome)
| Check | Result |
|---|---|
| Layout check, 39 page views | **before: 39 with issues; after: 0** (viewport top and scrolled bottom) |
| `scripts/test.sh` (exit 0) | ruff clean; backend **376 passed, 1 skipped**; bench **48**; frontend lint/typecheck clean, **35/35**; build OK |
| `alembic check` | no new upgrade operations |
| Acceptance (`--min-free-gib 1`, test-only) | **28/28** |
| Smoke (test-only floor) | **28 PASS, 0 FAIL** |

The screenshots and the checker script are review aids kept out of the repository. Fonts
in the container differ slightly from macOS, so the Mac run of the pinned script is still
the reference for everything except the visual check.

