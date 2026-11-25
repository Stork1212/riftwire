# URL acquisition: a saved link becomes a linked recording

Status: **prototype in the integration draft PR #14, not merged.** The owner approved it
for inclusion, subject to independent review (2026-11-25). **Public availability is not
permission** (§7, owner decision 2026-11-25, which supersedes the download-by-default
policy of 2026-11-24). The §2 requirements apply to every saved link. **Only a stand-in
downloader has been run; real platform downloads and `yt-dlp` are not authorized.**

## 1. What it does
Paste a YouTube or Twitch link → identify the source → match the creator → check standing
permissions → acquire by an authorized method → process the footage. With standing
permission (§2) and fetching switched on, saving the link queues one `ACQUIRE` job with no
further prompt; otherwise the link is saved, nothing is fetched, and the source's page
says why and what to do. **Acquire footage** queues it on demand under the same checks. The worker fetches the video and imports it as a
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
- The M3 acquisition infrastructure (job, checks, provenance) is approved for inclusion in
  PR #14, subject to review (2026-11-25). **Installing `yt-dlp` and running real platform
  downloads are not authorized yet.** `yt-dlp` is an external tool, not a Python
  dependency.
- `yt-dlp` uses a JavaScript runtime (Node) to run YouTube's player code. Rule 14 forbids
  bypassing access restrictions, and platform terms limit downloading without permission.
  The routes that are clearly authorized are:
  - the owner's own uploads, via a YouTube Studio download;
  - creator-supplied files;
  - the owner's own Twitch downloads.

  All three can already be imported without `url_fetch`.

## 7. Acquisition policy (owner decision 2026-11-25)
**Public availability alone authorizes nothing: no downloading, analysis, editing or
publishing.** This supersedes the 2026-11-24 policy (fetching on by default, plus an
automatic `public_web` record allowing fetch and analysis), which `b13b91f` implemented.

- **Fetching is off by default** (`RIFTWIRE_URL_ACQUISITION_ENABLED=false`).
- **Switching it on bypasses nothing.** A link is fetched only when a **user-confirmed**
  record covers it: ownership, creator permission or a licence, attested by the owner. That
  record must:
  - allow `acquire` (the content authorization);
  - list `url_fetch` (the authorization for the method);
  - have written platform permission attached (evidence of kind `platform_permission`).

  This applies to every basis; the `public_web` exception is removed.
- **Standing permission is used without asking again.** A creator-wide or channel-wide
  permission covers each newly saved link of that creator, and saving it queues the fetch.
- **Missing permission:** the link is still saved. Nothing is queued (an
  `acquire_not_queued` event is recorded when fetching is on), and the source's page shows
  the reason and the next step. For example, `no_rights_record`: "Record the creator's
  permission or a licence … it must allow URL fetching and have written platform
  permission attached."
- **`public_web` is a classification, not a permission.**
  - A saved platform link with no permission on record is shown as "Public footage · no
    permission on record" (`status_summary` = `public_web`).
  - No `public_web` rights records are written any more.
  - Rows written by `b13b91f` stay as immutable history. They authorize nothing, whatever
    their stored scope says (`PUBLIC_WEB_USES` is empty), and refuse with
    `creator_permission_needed`.
  - They are never upgraded: a creator's permission is a separate, owner-attested record.
    The API still refuses to create or supersede a `public_web` record by hand.
- **Analysis** needs footage obtained by a permitted method (the recording's actual
  method), under a record that allows `analyze`.
- **Existing data:** the rules are applied when access is evaluated, not when records are
  written, so they hold for existing records too. An `ACQUIRE` job queued under the old
  policy fails permanently, with `rights_refused` provenance, before any download.
- **Migration `0009`** (formerly `0008` on `phase2/url-download`) still adds the
  `public_web` basis and the `unaffiliated` relationship to the schema, so that history
  stays valid.
  - It rebuilds `rights_records` and `creators`, and recreates from frozen copies the 10
    triggers that mention `rights_records`, so all 14 immutability triggers survive.
  - Its downgrade refuses while `public_web` rows or unaffiliated creators exist.
- **Out of scope:** publishing of any footage (Phase 5).

## 8. Status at integration (2026-11-25)
- Integrated in `phase2/integration` (draft PR #14) with the highlight queue; see
  [`INTEGRATION_REVIEW.md`](INTEGRATION_REVIEW.md).
- **Verified with a stand-in downloader only:** backend tests, acceptance M2-7, and the
  Mac end-to-end run. **No real platform download has been run**, and none is authorized
  yet.
