# URL acquisition: a saved link becomes a linked recording (branch `phase2/url-download`)

Status: **prototype on a local branch, not merged, not a milestone acceptance.** §7
describes the owner acquisition policy of 2026-11-24 (default-allow fetching, automatic
`public_web` record), which supersedes the platform-permission requirement in §2 for
saved links. Built at
the owner's direction (2026-11-24) in two separate patches. Highlight detection (the GTA VI
prompt) is deliberately **not** part of this work.

## 1. What it does
Paste a YouTube or Twitch link, save it as a source, click **Acquire footage**. RIFTWIRE
queues one `ACQUIRE` job for the source. The worker fetches the video and imports it as a
recording that is linked to the source and recorded as obtained by `url_fetch`, with its
provenance. Processing (INGEST, METADATA, THUMBNAIL) then runs as for any import.

## 2. Checks (unchanged rights model, M2 R1)
`acquire` by `url_fetch` must be allowed by the source's **current** rights. That requires:
- `url_fetch` in the licence's acquisition methods;
- **written platform permission** on the licence (evidence of kind `platform_permission`);
- URL acquisition switched on (`RIFTWIRE_URL_ACQUISITION_ENABLED`, off by default).

It is checked at four points:
- when the job is queued: a job that could only be refused is never created;
- when the job starts, before any network access;
- after the download, before the import: if the rights changed, the download is discarded;
- in the transaction that links the recording.

Afterwards, analysis, editing and publishing of the recording are checked against its
**actual** method, `url_fetch`. Only RIFTWIRE's own fetcher may record `url_fetch`: uploads
and the attach endpoint refuse it (`url_fetch_reserved`).

## 3. One path for every import
The downloaded file goes through `UploadSession` / `complete_upload`, the same code as a
dashboard upload. That gives it:
- validation by content;
- SHA-256 duplicate detection;
- reservations against the 60 GiB floor;
- the INGEST job.

The download itself is limited:
- MP4 at up to 720p, 256 MiB, 5 minutes;
- no playlists, no live streams;
- written to a reserved workspace on the temp location.

The source's provenance records an `acquired_by_url` event with:
- the URL;
- the downloader and its version;
- the SHA-256 and size;
- the recording.

## 4. The job (patch 2)
- **One `ACQUIRE` job per source** (`uq_jobs_source_type`), keyed by `jobs.source_id`.
  `jobs.recording_id` is set once the recording exists (migration `0007`).
- **Retried with backoff:** download and network failures, a full disk.
- **Permanent, never retried:**
  - rights refusals, recorded with `rights_refused` provenance;
  - URL fetching switched off;
  - no `yt-dlp` or FFmpeg installed;
  - a file over the size limit;
  - bytes that already belong to another source.
- **Deferred without using an attempt:** the temp location is offline.
- **Crash-safe:**
  - the workspace is named after the job and cleared at the start of each attempt;
  - a finished acquisition is returned as is;
  - identical bytes imported by a crashed attempt are linked, not imported twice.
- The downloader runs as a tracked child process, for orphan cleanup and cancellation on
  shutdown or lease loss.

## 5. Verified, and not
- **Verified:**
  - automated tests with a stand-in downloader that writes a synthetic clip, covering
    refusals, revocation during the download, retries, permanent failures, crash recovery,
    idempotency and method integrity (`backend/tests/test_url_acquisition.py`);
  - acceptance step M2-7, the same flow through the real API and worker processes with a
    stand-in downloader script.
- **Not verified: a live YouTube or Twitch download.** It needs a source whose licence
  records written platform permission from the owner. Nothing here enters or invents that
  permission.

## 6. Open points for the owner
- This pulls forward acquisition work that R4 deferred to M3, and adds `yt-dlp` (an
  external tool, not a Python dependency). Both need the owner's approval as a milestone.
- `yt-dlp` uses a JavaScript runtime (Node) to run YouTube's player code. Rule 14 forbids
  bypassing access restrictions, and platform terms limit downloading without permission.
  The routes that are clearly authorized are:
  - the owner's own uploads, via a YouTube Studio download;
  - creator-supplied files;
  - the owner's own Twitch downloads.

  All three can already be imported without `url_fetch`.

## 7. Owner acquisition policy (2026-11-24): public footage is fetched and analysed
- **Fetching is on by default** (`RIFTWIRE_URL_ACQUISITION_ENABLED=true`). Saving a link
  queues its `ACQUIRE` job with no prompt. A refusal never blocks saving the link: it
  becomes a `acquire_not_queued` system event.
- **Every saved link gets an automatic `public_web` rights record** (migration `0008`, now `0009`).
  - It is written by `system`, in the same transaction as the source, and covers that one
    source.
  - It names the source's creator but claims nothing from them: "Publicly accessible
    footage … Not creator-confirmed."
  - Scope: `acquire` and `analyze`, method `url_fetch` only. No written platform
    permission is needed under this basis; other bases keep that requirement.
- **`public_web` never authorizes editing or publishing,** whatever its scope says. They
  are refused with `creator_permission_needed`, whose next step is "ask the creator;
  record their permission on Creators & Rights". When a creator's permission or licence
  exists and refuses for its own reason (revoked, not in scope), that reason is reported
  instead.
- **It covers fetched media only.** A copy imported by hand is judged by its own method.
- **Nobody can create or widen it by hand.** The API refuses `public_web` with
  `public_web_automatic`, and it cannot be superseded. The owner can still revoke it to
  stop fetching and analysing one link; a revoked one is never recreated.
- **Creator relationship `unaffiliated`:** a creator whose public footage was saved, with
  no relationship. Recording their permission creates a `creator_permission` record.
- **Migration `0008` (renumbered `0009` at integration)** rebuilds `rights_records` and
  `creators`. It drops and recreates,
  from frozen copies, the 10 triggers that mention `rights_records`, so all 14 immutability
  triggers survive (tested before and after, with tampering still refused). Its downgrade
  refuses while `public_web` records or unaffiliated creators exist, because they are
  immutable history.
- Tests and acceptance never contact a platform: fetching is switched off in their
  settings, and only acceptance step M2-7 switches it on, with a stand-in downloader.
- **Out of scope, by decision:** publishing public footage without the creator's
  permission is not built.


## 8. Status at integration (2026-11-25)
- Merged into `phase2/integration` (`580d476`) with the highlight queue. The `public_web`
  migration is now **`0009`** (it was `0008` on this branch); see
  [`INTEGRATION_REVIEW.md`](INTEGRATION_REVIEW.md) §3.
- **Verified natively on the Mac, with a stand-in downloader only.** Evidence:
  `docs/acceptance/2026-11-25-macos-integration-580d476/`. **No real platform download
  has been run.**
- **Open policy conflict.** On 2026-11-25 the owner stated that public availability alone
  does not authorize downloading, editing or publishing. §7 above still downloads by
  default under the system-written `public_web` record. Editing and publishing are
  refused as stated. The proposed change and the decision are in
  [`INTEGRATION_REVIEW.md`](INTEGRATION_REVIEW.md) §5.
