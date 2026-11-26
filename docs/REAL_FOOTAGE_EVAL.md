# Real-footage evaluation protocol (M6 closeout)

This is the first quality measurement of the highlight workflow on **authorized, real
recordings**, compared against spans the owner marked as moments they would actually clip.
The matching rule and the metrics below are fixed **before** any footage is run. A baseline
uses the current settings unchanged. Scoring changes are proposed afterwards, from the
evidence, and tested separately.

## 1. Inputs (owner)

A manifest outside Git (template: `scripts/real_eval_manifest.example.json`). Each recording
gives:

- **`path`:** an absolute path to the recording on the Mac.
- **`authorized: true`** with an **`authorization`** statement (at least 10 characters). The
  statement is recorded verbatim as the owner's attestation on a rights record.
  - **`basis`:** one of `ownership`, `creator_permission` or `licence`. Public availability is not a basis.
  - **Scope:** `acquire` (importing the local file), `analyze` and `edit` only, with no publishing, and coverage limited to this one source.
  - The script refuses any recording without both fields.
- **`game`**, **`channel`** and **`request`:** how many highlights to ask for (default 5).
- **`marked`:** the spans the owner would clip. Each has `start` and `end` (`HH:MM:SS(.mmm)`
  or seconds) and an optional `label`. Marks are not shown to the system; they are used
  only for scoring.

Recordings, imported copies and rendered Shorts stay outside Git: the evaluation data folder
and the output folder both live under `~/Claude/riftwire-env/`.

## 2. Matching rule (pre-registered)

- **Overlap coefficient:** for a selected highlight H and a marked span M,
  `overlap(H, M) = |H ∩ M| / min(|H|, |M|)`.
- **Match:** H matches M when `overlap(H, M) >= 0.5`. Half of the shorter span must
  coincide. This tolerates a highlight that is a tight cut of a longer marked span, and the
  reverse.
- **One to one:** candidate pairs are taken greedily by overlap, then IoU, then the earlier
  start. Each mark and each highlight is used at most once.
- **Outcomes:**
  - A matched mark counts as **found**.
  - An unmatched mark counts as **missed**.
  - An unmatched highlight counts as **extra** (selected, not marked).
  - A pair with `0 < overlap < 0.5` is reported as a **near miss** and still counts as missed
    and extra.
- **Timestamp alignment** (matched pairs): start delta `H.start − M.start`, end delta
  `H.end − M.end` (seconds, signed) and IoU. Medians of the absolute deltas are reported.
- **Missed-mark diagnosis**, the first that applies:
  1. **window** — no candidate window covered at least half of the mark;
  2. **model** — a window covered it, but the model selected no candidate there, or rejected
     the window;
  3. **ranking** — a validated candidate matched it but was not selected. The stored reason is
     reported (below the cut, after penalties, duplicate, or beyond the requested count).

## 3. Reported per recording

- Requested and returned highlights. When fewer were returned, the stored summary and the
  reasons the other candidates were rejected.
- Marked moments found and missed (with the diagnosis), extra selections, and near misses.
- Timestamp alignment.
- Editorial score with its deductions: model score, then each penalty and its reason.
  Confidence is reported separately. Scores are editorial estimates, not probabilities of
  going viral.
- AI batches: status, attempts and repairs, output tokens, and wall time.
- Runtime per stage (import, transcript, audio signals, visual signals, AI highlights) and
  end to end.
- Cost (must be $0).
- Recall `found / marked` and precision `matched / returned`, per recording and pooled. Each
  is shown with its counts, never as a bare percentage.

## 4. Rendered Shorts

The top highlight of each recording is rendered as a 35/65 Short (title, captions, full
RumbleFrame outro), outside Git. Automatic checks:

- 1080×1920 at 30 fps.
- Duration equals the clip plus the outro.
- Audio peak below 0 dBFS, and loudness reported.
- The last 2.5 s match the outro asset (PSNR against its frames).

A contact sheet places frames at the times of caption words next to the word expected there.
At least one Short is inspected by eye for crop, caption timing and readability, audio, and
the full outro.

## 5. Limits stated with every result

- **Sample size:** how many recordings, marks and highlights. With a handful of recordings,
  the results describe these recordings. They do not estimate general quality.
- **Marks:** one person's marks are one editorial judgement, not ground truth for all
  audiences.
- **Baseline only:** prompt `gaming.gta.v2`, scoring `editorial.default` v1 (cut 60), the
  `qwen3.5:4b` digest `7f3251fa…`, and Whisper `base`.
- **Changes:** any recommended change is tested on recordings other than the ones that
  motivated it, or is labelled untested.
