# Integration review package — PR #14 (`phase2/integration`)

_Updated 2026-11-25 for the final corrections. This is a draft PR for independent review:
**do not merge** until the review and the native Mac verification of the final commit
are complete._

| | |
|---|---|
| **Code commit** | **`51c8fa4`** "fix: public availability is not permission; render the whole approved outro" |
| Previous review point | `3ae0a55` (docs and evidence for `580d476`) |
| Base | `main` @ `d9c878e` (PR #12, merged 2026-11-24). It is an ancestor of the branch, so no rebase is needed |
| Branch head | The docs commit after `51c8fa4` (see the PR). The Mac script is pinned to the head |

## 1. Scope (approved for inclusion by the owner, 2026-11-25, subject to review)
1. **M3 acquisition infrastructure:** URL `ACQUIRE` job, rights checks, provenance.
2. **Durable acquisition and highlight-scan jobs:** migrations `0007` and `0008`.
3. **Rule-based highlight detection as a baseline:** not contextual AI (§6).
4. **Timestamp and candidate review:** `clips.txt` and the scan results.
5. **Vertical Shorts rendering prototype** (35/65), with animated word captions.
6. **Pillow 13.0.0** as a pinned dependency (§2).
7. **Corrected outro integration** (§7).

**Not authorized:**
- real platform downloads;
- installing `yt-dlp`;
- publishing;
- M4 implementation;
- contextual AI.

The shared transcript architecture is approved in principle for M4 and is **not**
implemented here (§8).

**Commits over `main`:**
- the prototype `1180a46`–`b05d881`;
- `5991b5d`, `81fb773` (acquisition job);
- `b13b91f` (the superseded `public_web` policy);
- `bf82c9a`, `52831e2`, `5841feb`, `1b73afe` (highlight queue and Shorts);
- `580d476` (merge);
- `3ae0a55` (evidence of `580d476`);
- **`51c8fa4` (this correction)**;
- the docs commit.


## Independent-review follow-up: rights scope of the outro's audio tail (2026-11-25)
**Finding** (ChatGPT, PR #14 @ `5ceb2e2`): `clips.cut` checked `edit` rights for
`end_ms - start_ms` only. In `continue` mode, and in `outro` mode with a fade, the render
also plays source audio from **after** `end_ms` during the 2.5 s bumper. Under a licence
with `max_clip_seconds`, that tail could take the footage actually used past the
rights-checked length.

**Fix:**
- **New check:** `clips.outro_tail` evaluates `edit` again for **clip length + tail**.
  - If that is allowed, the tail is used, and its decision is fenced again before the
    export is returned.
  - If it is refused (for example `clip_too_long`), the tail is **dropped**: the gameplay
    audio ends at the cut.
  - A recording that ends at the cut never has a tail.
- **Unchanged:** the outro always plays whole (75 frames, 2.5 s), and the clip's own audio
  is untouched. Each export logs the tail length and the reason (`clip_outro_tail`).

**Regression tests** (`backend/tests/test_outro.py`):
- the tail is checked against the scope across 7 cases: no limit, a limit with room, a limit
  reached at the cut, a limit 0.5 s short, fade mode fitting and not fitting, and no fade;
- a restricted clip renders 17.5 s / 525 frames with **no source audio after the cut** and
  the clip's own audio intact;
- an allowed tail still continues the gameplay under the outro;
- the fade-mode tail is used only when it fits;
- a recording that ends at the clip boundary has no tail.

**Native Mac verification of `7911092`: all passed.** Test suites, acceptance 29/29,
Alembic checks, the demo-DB copy migration and end-to-end 14/14 passed at 15:05Z. The smoke
test was re-run at 15:13Z after stopping a leftover smoke instance that held its port:
30/0/0. Evidence: [`acceptance/2026-11-25-macos-pr14-7911092/`](acceptance/2026-11-25-macos-pr14-7911092/README.md).

## 2. Pillow (approved by the owner, 2026-11-25)
| | |
|---|---|
| **Pinned** | **13.0.0** in `backend/constraints.txt`; `Pillow>=10.1` in `pyproject.toml` |
| **Licence** | **MIT-CMU** (permissive) |
| **Purpose** | Draws the Short's title header and per-word caption overlays (`services/split_shorts.py`). FFmpeg composites them |

## 3. Migrations (unchanged by this correction)
- **The conflict:** two `0008` revisions, one per branch.
- **The resolution:**
  - `0008_highlight_scan` keeps `0008`, because the demo database may already be at it;
  - the public-web migration becomes `0009_public_web_basis`;
  - there is a single head, `0009`.
- **Populated-database results:**
  - a test migrates a populated `0008` database up to `0009`, down to `0008` and up
    again, with scan results, jobs and events preserved, 14 triggers and no FK
    violations;
  - on the Mac (run of `580d476`), a copy of the demo database went `0008` → `0009` with
    no lost rows, 14 triggers and FK clean.
- **No schema change** was needed for the policy correction: it is enforced when access is
  evaluated.

## 4. Acquisition policy — corrected (supersedes the 2026-11-24 decision)
**Decision (owner, 2026-11-25):** public availability alone does not authorize
downloading, analysing, editing or publishing footage. It **explicitly supersedes** the
download-by-default policy of 2026-11-24 that `b13b91f` implemented.

| Requirement | Implementation (`51c8fa4`) |
|---|---|
| Fetching off by default | `Settings.url_acquisition_enabled = False` |
| `public_web` is a classification, not proof of permission | `PUBLIC_WEB_USES = frozenset()`: a `public_web` row refuses every use with `creator_permission_needed`, whatever its stored scope. `status_summary` reports `public_web` for a saved link with no permission (badge: "Public footage · no permission on record"). No new `public_web` rows are written (`record_public_web` was removed) |
| Acquisition authorization removed from `public_web`; its exception removed | The `public_web` branches in `_check_record` (declared method `url_fetch`, the platform-permission exemption) are deleted |
| A valid, user-confirmed record before URL acquisition, covering content **and** method | `acquire` in scope and `url_fetch` in `acquisition_methods`, on an owner-attested, current record (ownership, creator permission or licence) |
| Written platform permission kept for URL fetching | `platform_permission` evidence required for every basis |
| Analysis only for lawfully obtained footage, as the rights allow | `analyze` needs a record that allows it and lists the recording's **actual** method; `public_web` never does |
| Existing records and queued jobs | Rules are applied at evaluation time. Legacy `public_web` rows authorize nothing; a legacy `ACQUIRE` job fails permanently before any download |
| Immutable history; never silently upgraded | Legacy rows stay byte-for-byte (triggers); a creator permission is a separate owner-attested record; the API refuses to create or supersede `public_web` |
| Standing permissions without repeated confirmation | A creator-wide or channel-wide permission covers each new link; saving it queues the fetch |
| Missing permission: save the source, explain | The source is saved, nothing is queued, and its page shows the reason and an action. The **Acquire footage** button is disabled with the reason |

**Paths reviewed for bypasses:**
- **API:** `POST /sources`, `POST /sources/{id}/acquire`, `rights-check`, and `attach` and
  import with `url_fetch` (`url_fetch_reserved`).
- **Queue:** `enqueue_acquire` checks, and `queue_for_new_source`.
- **Worker:** the `ACQUIRE` handler re-checks before the download. Refusals are permanent,
  and transient retries re-check.
- **Attachment:** `attach_media` re-checks inside the linking transaction.
- **Output fences:** the fence after the download; the export fences on `edit` and
  `analyze`; the `HIGHLIGHT_SCAN` fence.
- **The duplicate-bytes path:** it links only after the same check.

None bypasses the policy; each is covered by a test (§9).

**Frontend and documents updated:**
- badge and basis labels;
- the source page explanation, and the disabled Acquire button with its reason;
- the creator relationship label;
- `.env.example`, `URL_ACQUISITION.md` §7, `ARCHITECTURE.md` §21, `INSTALLATION.md`.

The corrected policy matches `PRODUCT_SPEC.md` §2.3 (generic URL fetching needs a
recorded platform permission and is off by default) and CLAUDE.md rule 14.

## 5. Stand-in downloader vs real acquisition
- **Verified only with a stand-in `yt-dlp`:** a script that copies a synthetic clip. This
  covers the backend tests, acceptance M2-7 and the Mac end-to-end run.
- **No platform was contacted.** Real platform acquisition is **not verified** and **not
  authorized**, and `yt-dlp` is not installed by the project.

## 6. Highlight detection is a rule-based baseline
`services/highlight_scan.py` ranks the best non-overlapping windows. It slides a 25 s
window in 5 s steps, scoring:
- visual activity (frame differences of a 2 Hz grayscale proxy);
- three keyword regexes over Whisper words.

The score is `min(motion / median, 5) + 0.85 × cue types`. It does **not**:
- identify events, speakers, game mechanics or humour;
- use any prompt or model;
- record a prompt version.

It is a baseline for ordering human review, **not contextual AI**.

## 7. Outro — corrected
**Approved assets, reviewed:**
- **PR #6:** `branding/outro/RumbleFrame_Outro_9x16_1080x1920_30fps_2p5s.mp4`.
  - Checksum: SHA-256 `df4ba728…`.
  - Format: 1080×1920, 30 fps, 75 frames, 2.5 s.
  - Audio: an AAC stereo sting. Its handoff asks to keep the gameplay audio under the
    bumper and mix the sting beneath.
- **PR #11** (v1 launch kit): `branding/rumbleframe/v1/outro.mp4`.
  - Checksum: SHA-256 `b76742bc…`; the same 75 frames and 2.5 s.
  - Audio: **no audio track**. The spec says silent, with silence appended.

The two specifications differ on audio; both are supported. No branding asset is
duplicated into this branch: tests use synthetic stand-ins, and the Mac script extracts
the real files from those PR commits.

| Requirement | Implementation (`services/outro.py`, `clips._render_split`) |
|---|---|
| No 2 s trim; all 75 frames kept | The duration is counted from the asset (`ffprobe -count_packets` ÷ fps). The video passes through `fps=30` and `trim=end_frame=<asset frames>`, and is never shortened |
| Configurable assets and durations | `RIFTWIRE_CLIP_OUTRO_PATH`; the optional `RIFTWIRE_CLIP_OUTRO_DURATION_SECONDS` is validated against the asset. A mismatch refuses (`outro_duration_mismatch`); it never trims |
| Sting, silent track, or no audio track | Sting → `volume` → padded to the duration; no track → `anullsrc` |
| Original gameplay audio preserved for the whole segment | The segment's audio is only resampled and padded or trimmed to its exact length. The mix and limiter apply to the outro portion only |
| Configurable transition; gameplay continuation | `RIFTWIRE_CLIP_OUTRO_AUDIO`: `continue` (gameplay keeps running under the outro and fades over `…_FADE_MS`) or `outro` (gameplay ends at the cut, or fades over `…_FADE_MS`) |
| Optional sting without clipping | `…_STING_GAIN_DB` (default −6 dB); `amix` (no normalisation) → `alimiter` at −1 dBFS |
| Consistent 1080×1920, frame rate, sync | Output `-r 30`, 48 kHz AAC; the gameplay audio is padded or trimmed to the exact segment length; concat of equal-length pairs |
| Re-encoding | A single FFmpeg pass encodes the Short once; the asset is probed once and cached by path, size and mtime |

**Result:** 15 s clip + approved outro = **17.5 s, 525 frames**. This was verified with
both real assets in the container: PR #6 in `continue` mode peaked at −1.0 dBFS; v1 in
`outro` mode was silent.

## 8. Transcription
- **Today:** four call sites run Whisper independently, and nothing is stored. The scan
  read "this get is insane" where the captions read "Getaway is insane".
- **Approved in principle for M4:**
  [`TRANSCRIPT_ARCHITECTURE_PROPOSAL.md`](TRANSCRIPT_ARCHITECTURE_PROPOSAL.md) (one
  stored, versioned transcript per recording and configuration; MLX Whisper `base`
  provisional).
- **Not implemented in PR #14.** The detailed M4 plan follows after this PR is merged.

## 9. Regression tests and results
**New and rewritten tests in `51c8fa4`** (33 tests):

| Area | Tests |
|---|---|
| **Acquisition and rights** (`test_public_web.py`, 20) | disabled by default; saving a public link grants and queues nothing, and explains (fetching off and on); enabling fetching does not bypass rights; local sources are not classified as public; legacy `public_web` rows authorize no use (all four), even with a full scope; a legacy queued `ACQUIRE` job is refused before any download, with provenance; `public_web` is never upgraded and cannot be superseded or recorded by hand; creator permission without platform permission is refused; both content and method must be authorized; standing permission queues new links without new records; revoked, expired and superseded permission is refused (3); superseded during the download leaves no output; a permitted fetch records `url_fetch` and still gates editing; analysis needs a permitted method; migration triggers; downgrade refusal with legacy rows |
| **Integration** (`test_integration_highlights.py`, 3) | scan and export follow the creator's permission (edit refused until permitted); public footage without permission cannot be scanned; populated `0008` → `0009` → `0008` → `0009` migration |
| **Rendering** (`test_outro.py`, 10) | 15 s + outro = 17.5 s and 525 frames, with a sting, a silent track or no track (3); the duration comes from the asset, and a mismatch refuses; gameplay audio preserved (r > 0.99, same level); `continue` mode: limited below 0 dBFS, gameplay continues under the outro and fades; `outro` mode: silence at the cut, plus a fade transition; a recording ending at the cut still gets the whole outro; captions in place and in time, never over the outro; a failed render (corrupt asset, or FFmpeg failing) leaves no output, no workspace and no reservation |
| **Updated** | `test_url_acquisition.py` (refusals without permission or with a legacy row); `test_rights.py` (paste-URL flow); acceptance M2-4 and M2-7 |

**Existing coverage, still passing:**
- populated migration and the 14 triggers;
- FK integrity;
- job recovery and idempotency;
- storage reservations and volume identity;
- the frontend tests (38: pages, layout components, rights views);
- smoke.

**Container results on `51c8fa4`:**
- `scripts/test.sh`: backend **450 passed, 1 macOS-only skip**; bench 48; frontend 38;
  `tsc`, ESLint and build OK.
- Acceptance **29/29** (isolated, floor lowered for the 28 GiB container).
- Smoke **28 PASS / 0 FAIL / 1 macOS-only skip**.
- End-to-end driver **14/14**, with stand-ins for `say` and Whisper and both real outro
  assets.

**Native Mac run of the head `ef969f1` (owner, 2026-11-25 04:43Z): all passed.**
- Real 60 GiB floor: 127.6 → 127.5 GiB free.
- Real Whisper `base` and macOS speech.
- Results:
  - backend 448 + 3 skips; bench 47 + 1; frontend 38; build;
  - acceptance 29/29; smoke 30/0/0;
  - Alembic single head, with upgrade and downgrade checks;
  - the demo-DB copy migrates `0008` → `0009` with no lost rows;
  - **end-to-end 14/14**: 17.50 s / 525 frames for both outros, every PR #6 outro frame
    intact.
- Evidence: [`acceptance/2026-11-25-macos-pr14-ef969f1/`](acceptance/2026-11-25-macos-pr14-ef969f1/README.md).


## 10. Remaining limitations and known risks
- **Real acquisition is unverified.** `yt-dlp` and platform downloads are not authorized;
  only a stand-in has run.
- **Source-level "Analyse" for a saved link.** It is judged by the source's *declared*
  method (`platform_export` by default), while fetched recordings are judged by their
  actual method (`url_fetch`). With a `url_fetch`-only permission, the source page can
  show "Analyse: refused" while the fetched recording is analysable. This is correct per
  the M2 rules but confusing. Proposed follow-up: offer `url_fetch` as the declared method
  for links meant to be fetched.
- **The highlight scan is a baseline** (§6). It can rank motion without meaning, and its
  keyword list is English and GTA-specific.
- **Transcripts are not stored.** Whisper is repeated, and evidence and captions can
  differ (§8, M4).
- **Brand-kit gaps** (Phase 4):
  - caption highlight colour: the v1 kit specifies orange `#FF5635`; the prototype uses
    red `#EC2028`, as PRODUCT_SPEC D7 says;
  - one global outro, not per-channel brand kits;
  - the outro hash and preset version are not stored with the export;
  - no −14 LUFS normalisation; only peak limiting of the outro mix.
- **Exports are returned as downloads**, not stored artifacts; there is no retention
  record yet (Phase 5).
- **Risk: platform terms.** Even with recorded permission, downloading can breach platform
  terms if the permission is not genuine. Records are user attestations; the owner remains
  responsible.
- **Risk: audio continuation.** In `continue` mode the gameplay heard under the outro
  comes from after the cut (up to 2.5 s). If that audio is inappropriate, `outro` mode or
  a shorter fade avoids it.
- **Documentation conflicts with PR #13:** see §12.

## 11. Mac verification instructions
Run the pinned script delivered as `~/Claude/integration-e2e-<head>.sh`, with the bundle
in `~/Claude/riftwire-env/`. It:
1. fetches the bundle and checks out `phase2/integration` at the pinned head;
2. checks Pillow, local Whisper `base` and macOS `say`;
3. records the environment and disk usage;
4. runs, with the **real 60 GiB floor** and isolated data folders and ports:
   - `scripts/test.sh`;
   - acceptance (port 18931);
   - smoke (port 18930);
   - Alembic checks: a single head, and upgrade → downgrade → upgrade on an empty
     database;
5. migrates a **disposable copy** of the demo database (the live demo is untouched);
6. runs the end-to-end test (port 18941) with real Whisper, synthetic speech and footage,
   a stand-in downloader, and both **approved RumbleFrame outros**, extracted from the
   PR #6 and PR #11 commits and checked against their SHA-256. The 14 checks are:
   - a public link is refused and explained;
   - a synthetic creator permission plus platform permission is recorded;
   - acquisition, history, the scan, the transcript evidence and the sampled frame;
   - the `clips.txt` review;
   - the edit-permission gate;
   - 17.5 s / 525 frames / A/V sync;
   - the approved outro is intact (frame comparison) and its peak is below 0 dBFS;
   - red words;
   - the original audio;
   - a second Short with the silent v1 outro.

   It saves both Shorts (not committed) and a media-spec JSON.

## 12. Conflicts with other PRs (checked 2026-11-25)
- **PR #13** (`docs/m1-ssd-acceptance` @ `66b15cc`, draft): a trial merge into this head
  conflicts in one documentation file only, `docs/ROADMAP.md` (the M1 and M2 milestone
  rows). There are no code conflicts. Whichever PR merges second resolves it.
- **PR #6** (`29f6a7b`) and **PR #11** (`724d1be`): they merge cleanly into this branch
  (assets and docs only).

## 13. Merge-readiness assessment
**Ready for independent review; not yet approved to merge.** The code is complete for
the approved scope. The container verification and the **native Mac verification of
`ef969f1`** are green, and the evidence is committed (§9). Merging still needs:
1. ChatGPT's independent source review of `51c8fa4` and the docs commits;
2. the owner's merge decision. Before or after the merge, a documentation-only resolution
   with PR #13 is needed.

No known blocker remains in the code. The open items in §10 are recorded limitations,
not merge blockers, unless the reviewer decides otherwise.
