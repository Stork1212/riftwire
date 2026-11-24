# URL acquisition: a saved link becomes a linked recording (branch `phase2/url-download`)

Status: **prototype on a local branch, not merged, not a milestone acceptance.** Built at
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
