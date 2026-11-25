# Integration review package — `phase2/integration` @ `580d476`

_Prepared 2026-11-25 for independent review. Draft PR; **not to be merged** until the review
and the owner's decisions below are complete._

This branch combines two lines of work that were built outside the approved milestone
sequence:
- **URL acquisition** (`phase2/url-download` @ `b13b91f`): a saved link becomes a linked
  recording through a durable `ACQUIRE` job, plus the automatic `public_web` rights basis.
- **Highlight queue and Shorts export** (`phase2/highlight-queue` @ `1b73afe`): a queued
  full-recording highlight scan, `clips.txt` timestamp review, and a 35/65 Short with
  burned-in word highlights and an outro.

Both pull forward work the roadmap places in M3 (acquisition), M4 (transcription), M6–M7
(highlights, review) and Phase 4 (rendering). The owner approved preparing this PR; it does
**not** approve those milestones, which still need their own approval (§11).

## 1. Base and branch state (checked 2026-11-25)
- `origin/main` = **`d9c878e`**, "M2: Rights & Source Management (#12)".
- **PR #12 is merged** (squash, 2026-11-24 19:58 UTC, merge commit `d9c878e`). PR #10 is
  closed.
- `d9c878e` is an ancestor of `580d476`, so the PR base is **`main`** with no rebase.
- The PR carries 13 commits over `main`:
  - prototype `1180a46`–`b05d881`, by another agent: link preview, Whisper suggestions,
    cuts, permission-checked downloads;
  - `5991b5d`, `81fb773`: URL acquisition as an `ACQUIRE` job;
  - `b13b91f`: the `public_web` basis;
  - `bf82c9a`, `52831e2`, `5841feb`, `1b73afe`: Shorts export, `clips.txt` review,
    highlight scan, queued scan;
  - `580d476`: the merge, with the resolution below;
  - plus the documentation and evidence commit for this review.
- **PR #13** (`docs/m1-ssd-acceptance`, draft) is independent and untouched. It edits
  `PROJECT_STATUS.md`, `ROADMAP.md` and `CLAUDE.md`. Whichever PR merges second will need
  a documentation-only conflict resolution.

## 2. New dependency: Pillow (approved by the owner, 2026-11-25)
| | |
|---|---|
| Package | **Pillow** (the maintained fork of PIL) |
| Pinned version | **13.0.0** in `backend/constraints.txt`; `Pillow>=10.1` in `backend/pyproject.toml` |
| Licence | **MIT-CMU** (from the package's `License-Expression`); permissive, no copyleft |
| Purpose | Draws the Short's overlay images: the title header and the per-word caption frames with the red active-word box (`backend/app/services/split_shorts.py`). FFmpeg composites them. |
| Scope | Backend only; imported by one module. Not used for reading or writing video. |
| Install | `backend/.venv/bin/pip install -c backend/constraints.txt Pillow` (also installed by `scripts/bootstrap.sh` from `pyproject.toml`) |
| Verified | Installed in the Mac project environment (`environment.txt` of the evidence); all suites pass with it |

It came in with `bf82c9a` without approval at the time. It is now recorded in
`docs/INSTALLATION.md`.

## 3. Migration conflict and its resolution
**Conflict.** Both branches added an Alembic revision numbered `0008`, each with
`down_revision = "0007"`:
- `0008_highlight_scan.py` (highlight queue): adds `HIGHLIGHT_SCAN` to
  `ck_jobs_job_type_valid` and the nullable `jobs.result_json` column;
- `0008_public_web_basis.py` (URL acquisition): rebuilds `rights_records` and `creators`
  for the `public_web` basis and the `unaffiliated` relationship, and recreates the 10
  triggers that mention `rights_records`.

Two heads would have made `alembic upgrade head` fail. There was no textual conflict in
any other file.

**Resolution.**
- The highlight migration **keeps revision `0008`**. The live demo database was built from
  the highlight branch and may already be at `0008`. Renumbering it would strand that
  database at a revision that no longer exists.
- The public-web migration **becomes `0009_public_web_basis.py`** (`revision = "0009"`,
  `down_revision = "0008"`). It had only ever run on disposable test databases.
- The two migrations touch disjoint tables (`jobs` vs `rights_records`/`creators`), so
  their order does not change either result.
- There is a single head, `0009`.
- Follow-on edits:
  - `test_public_web.py` (downgrade target and version strings);
  - `test_storage_m1.py`, whose job-type expectation lacked `HIGHLIGHT_SCAN`. It already
    failed on `1b73afe` alone.

## 4. Populated-database migration results
**Automated (every run of the suite)** —
`backend/tests/test_integration_highlights.py::test_highlight_scan_results_survive_the_public_web_migration`:
1. Builds a database at `0008` holding a source, a recording, a completed `HIGHLIGHT_SCAN`
   job with `result_json`, other jobs and events.
2. Upgrades to `0009`, downgrades to `0008`, and upgrades to `0009` again.
3. After each step it checks:
   - every job, `result_json` value and event is preserved;
   - all 14 immutability triggers are present;
   - tampering is still refused;
   - `PRAGMA foreign_key_check` is clean.

**On the Mac, against a copy of the live demo database** (`demo-migration.txt` in the
evidence):
- The script copied the demo database with `sqlite3 .backup` into the evidence folder and
  ran `alembic upgrade head` on the copy only. The live demo database and its folder were
  not opened for writing; the agent's request for access to that folder was declined, and
  it was not retried.
- **Before:** revision `0008`, 14 triggers, 0 FK violations.
- **After:** revision `0009`, 14 triggers, 0 FK violations, **no lost rows** across all
  20 tables. The copy held 7 jobs, 9 provenance events, 2 recordings, 1 source, 1 rights
  record, 25 system events and 5 storage locations.
- **Not done:** migrating the live demo database. That needs the owner, after review.

## 5. What public availability authorizes
**Editing and publishing: never, from public availability alone.** On this branch,
`public_web` refuses every use other than `acquire` and `analyze` with
`creator_permission_needed`, whatever its stored scope says. Exporting a cut or a Short is
checked as `edit`. The Mac run confirmed it: the
export was refused until a creator permission was recorded. Publishing of any footage is
not built.

**Downloading: on this branch, yes. This conflicts with the owner's 2026-11-25
statement, and needs a decision before merge.**
- The owner's statement: "public availability alone does not authorize downloading,
  editing or publishing footage."
- The current behaviour: `b13b91f` implements the owner's earlier acquisition policy of
  2026-11-24 ([`URL_ACQUISITION.md`](URL_ACQUISITION.md) §7):
  - fetching is **on by default** (`RIFTWIRE_URL_ACQUISITION_ENABLED=true`);
  - every saved link gets a **system-written** `public_web` record covering `acquire` and
    `analyze`;
  - saving a link therefore **queues a download with no user-confirmed rights record**.
- That also departs from CLAUDE.md rule 14: "Sources require a user-confirmed rights
  record". It is the main policy question for this review.

**Smallest change that would make the statement true, proposed and not made** (the owner
asked for no further feature work before the review):
1. Default `RIFTWIRE_URL_ACQUISITION_ENABLED` to `false`.
2. Make `public_web` cover `analyze` only, for media already obtained by a permitted
   method. `acquire` via `url_fetch` would then require a user-confirmed record:
   - ownership;
   - a creator permission that names `url_fetch`;
   - or a licence with written platform permission.
3. Keep `public_web` and `unaffiliated` as history. No downgrade is needed, because only
   the evaluation of `acquire` changes.

The tests and acceptance already run with fetching off, so the change is small and
testable. It is the owner's decision.

## 6. Highlight detection is a rule-based baseline
`backend/app/services/highlight_scan.py` slides a 25-second window in 5-second steps,
keeps the best non-overlapping windows, and ranks them by:
- **visual activity:** the mean frame difference of a 2 Hz grayscale proxy. It measures
  how much the picture changes, not what happens;
- **speech cues:** three fixed regular expressions over Whisper words (`reaction`,
  `pursuit`, `crash`, such as "cops", "getaway", "no way").

The score is `min(motion / median motion, 5) + 0.85 × (number of cue types, at most 3)`.
The output is called "candidates for review" and records its
evidence: excerpt, cues, motion and peak time.

It is **not contextual AI**. It does not:
- identify events, speakers, game mechanics or humour;
- use any model or prompt (no `gta6.v1` prompt, no provider interface, no prompt version
  or model recorded);
- validate a candidate against the footage beyond the measured signals.

It is useful as a **baseline**, for ordering what a human reviews and for later comparison
when M5–M6 add the provider interface and versioned per-game prompts (CLAUDE.md rule 16).
It must not be described as AI highlight detection.

## 7. Outro discrepancy: 2.0 s in code, 2.5 s approved
- **Approved asset:** the RumbleFrame outro is **2.5 seconds**:
  - PR #6: "exactly 2.5 seconds", 1080×1920, 30 fps, AAC stereo;
  - PR #11, launch kit v1: "75 frames over 2.500 seconds", described as silent.
- **Code:** `clips.py` trims any outro to `trim=duration=2` / `atrim=duration=2`, so the
  approved outro would lose its last 0.5 s. The roadmap's Phase 4 line also says "2 s
  outro"; it predates the asset.
- **The E2E check** verified the 2 s behaviour with a synthetic outro. It did **not** use
  the RumbleFrame asset.
- **Two related gaps** found while checking, not changed:
  - **Audio.** The filter requires an outro audio track (`[3:a]`) and concatenates it, so
    gameplay audio stops at the outro. PR #6's render handoff asks to keep gameplay audio
    audible and mix the sting beneath it. Whether the silent v1 file has a silent audio
    track or none was not checked; with none, the render would fail.
  - **Configuration.** The outro is one global path (`RIFTWIRE_CLIP_OUTRO_PATH`), not a
    per-channel brand kit (Phase 4).
- **Proposed:** take the outro's duration from the asset (ffprobe) or the brand kit rather
  than a constant, and make the audio handling match the handoff. This is Phase 4 work,
  for approval.

## 8. Transcription inconsistencies
In the Mac run, Whisper `base` heard the same speech two ways:
- the highlight scan's excerpt reads "this **get** is insane";
- the Short's captions read "**Getaway** is insane".

The cause is architectural. There are **four independent transcription call sites**, each
running Whisper on its own audio window, and none stores its result:

| Call site | Window |
|---|---|
| `highlight_scan.scan_video` | the full recording, in chunks |
| `clips.suggest` | the first `ANALYSIS_SECONDS` (300 s) |
| `clips._render_split` (35/65 Short) | the clip range only |
| `clips.cut(subtitles=True)` | the clip range only |

Different context windows give different words and timings for the same audio. Work is
repeated on every export. The raw word timestamps that CLAUDE.md requires to be
preserved are not kept at all.

**Proposal:** one stored, versioned, timestamped transcript per recording that every
consumer reads. See [`TRANSCRIPT_ARCHITECTURE_PROPOSAL.md`](TRANSCRIPT_ARCHITECTURE_PROPOSAL.md).
It is M4 scope and needs approval.

## 9. Stand-in downloader vs real authorized acquisition
| | Verified | How |
|---|---|---|
| Job, rights checks, provenance, crash recovery, idempotency, method integrity | **Yes** | Backend tests with a fake `yt-dlp`; acceptance step M2-7 and the E2E run with a stand-in script that writes a synthetic clip |
| A download from YouTube, Twitch or any platform | **No** | Never run. No platform was contacted by any test, acceptance step or E2E run |
| `yt-dlp` itself (version, JS runtime, formats, failures) | **No** | Not installed or pinned by the project; not approved |

A real acquisition needs:
- a source the owner is authorized to download;
- the policy decision in §5;
- approval of `yt-dlp` as a tool (it is an external program, not a Python dependency).

## 10. Verification
| Where | Commit | Result |
|---|---|---|
| Container | `580d476` | `scripts/test.sh`: backend 432 + 1 skip (same 433 tests as the Mac), bench 48, frontend 38, build; acceptance 29/29; smoke 28 + 1 macOS-only skip; E2E driver 11/11 with stand-ins for `say` and Whisper |
| **Mac, native** | `b13b91f` | backend 417 + 3 skips, bench 47 + 1, frontend 38, build, acceptance 29/29, smoke 30/30, real 60 GiB floor |
| **Mac, native** | `580d476` | backend 430 + 3 skips, bench 47 + 1, frontend 38, build, acceptance 29/29, smoke 30/30, demo-copy migration PASS, **E2E 11/11 with real Whisper `base` and macOS speech**, real 60 GiB floor |

Evidence: [`docs/acceptance/2026-11-25-macos-integration-580d476/`](acceptance/2026-11-25-macos-integration-580d476/README.md).

## 11. Decisions for the owner (after review)
1. **Acquisition policy** (§5): keep download-by-default under `public_web`, or adopt the
   proposed change so downloads need a user-confirmed record.
2. **Milestones:** approve URL acquisition (R4 deferred it to M3), the highlight baseline,
   and the Shorts export (Phase 4 scope), or split them into separate PRs.
3. **`yt-dlp`** as an external tool, if real acquisition is wanted.
4. **Outro** (§7): use the asset's 2.5 s and fix the audio handling (Phase 4).
5. **Transcript architecture** (§8): approve for M4.
6. **The live demo database** may be migrated to `0009` only after review and approval.

## 12. Not done, by instruction
- No merge.
- No change to the live demo, its database, launchd jobs or data folder.
- No change to PR #13 or the physical-SSD test folders.
- No content published.
- No further feature development.
