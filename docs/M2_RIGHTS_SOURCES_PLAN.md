# M2 — Rights & Source Management: implementation plan

_Status: **proposal for owner approval**. No M2 code exists yet. Written 2026-11-24 on the
isolated branch `docs/m2-rights-sources-plan` (based on `phase2/m1-storage`)._

## 0. Ground rules for M2

- **The M1 physical-SSD gate stays open.** M2 code adds no new storage locations and no new
  large writes. If M2 merges before the SSD arrives, the §11 checks in
  `M1_STORAGE_CHECKLIST.md` still run on the SSD later, unchanged. M1 is not declared complete
  and `v0.3.0-m1` is not tagged until they pass.
- **No acquisition in M2.** M2 records sources and rights and enforces them. It does not
  download anything, call yt-dlp, or contact YouTube or Twitch. That is M3, which needs its
  own approval.
- **$0.** M2 makes no network calls at all by default. There are no paid services, no Ollama
  and no model downloads.
- **No new dependencies.** M2 uses the existing stack only: FastAPI, SQLAlchemy, Alembic and
  React.
- **No real data is migrated, and no drive is formatted.**
- M2 is developed on its own branch, `phase2/m2-rights-sources`, created only after approval.

## 1. Target workflow and what the platform terms allow

The owner's goal is a one-click flow:

> Paste an authorized video URL → analyse → highlights → branded Shorts → publish after
> approval.

M2 builds the first step of that flow: the URL becomes a **source** with a **rights record**,
and everything downstream is checked against it.

### 1.1 Platform terms (checked 2026-11-24)

- **YouTube.**
  - The Terms of Service (dated 15 Dec 2023) bar users from accessing, reproducing or
    downloading any Content except (a) as expressly authorized by the Service, or (b) with
    **prior written permission from YouTube and, if applicable, the respective rights
    holders**.
  - The terms separately prohibit automated access (robots, scrapers) without YouTube's prior
    written permission.
  - Consequence: **a creator's permission alone does not satisfy (b).** Under (b), YouTube's
    own written permission is also needed.
  - What *is* "expressly authorized by the Service" is the channel owner's own download
    of their videos in YouTube Studio.
- **YouTube Data API v3.** The default quota is 10,000 units per day, and `videos.list`
  costs 1 unit. It is free and returns metadata only, never media. Use requires a Google
  Cloud project with credentials and acceptance of the YouTube API Services terms.
- **Twitch.**
  - The Helix `Get Videos` endpoint returns VOD metadata and accepts an app access token
    (client-credentials flow).
  - I could **not** verify the current Twitch Terms of Service wording on downloading in
    this session. The page fetch needed an approval nobody was present to give.
  - Twitch's developer forum shows that even downloading one's *own* VODs programmatically
    is an open question. It must be treated as unverified until the owner or counsel
    confirms.

**Effect on the approved design.** PRODUCT_SPEC §2.3 method 3 and PHASE2_PROPOSAL §7.2 plan
yt-dlp for public videos when the owner confirms that the method is permitted. For YouTube,
that confirmation can only honestly be given with written permission from YouTube itself.
Recommendation, as decision **R1** in §10:

- **Own YouTube channels:** owner downloads in YouTube Studio, then imports locally (method 2).
- **Partner creators:** the creator sends the original file (method 1). This also gives the
  best quality.
- **Method 3 (yt-dlp):** allowed for a source only when the rights record carries a
  `platform_permission` document, i.e. written permission from YouTube or Twitch. Otherwise
  the source is blocked. Method 3 stays disabled globally by default.

M2 builds the data model for this. M3 decides acquisition.

### 1.2 One-click flow, as it can work under these terms

```
Paste URL ─► validate + canonicalise (offline) ─► existing source? ── yes ─► open it
                                                        │ no
                                                        ▼
                     match a known creator channel? ─► rights record covering this use?
                                                        │ no ─► "Rights needed" form (owner attests)
                                                        ▼ yes
                 media present? ── no ─► "Attach media": upload the creator file or the
                        │                     owner's Studio/Twitch download (methods 1–2),
                        │                     or (M3, only with platform_permission) fetch
                        ▼ yes
       analyse (M4) ─► highlights (M5–M6) ─► render (Phase 4) ─► owner approval ─► publish (Phase 5)
```

- For a known creator with a standing licence, the flow is one paste plus one file drop.
- Once rights and media exist, the rest of the chain needs no further clicks until the
  approval step.
- Every later stage re-checks the rights record (§5). A revoked or expired licence stops
  the chain wherever it is.

## 2. Scope

**In M2:**
- creators, creator channels, sources and rights records;
- licence scope, expiry and revocation;
- evidence and an append-only provenance log;
- per-source game and brand-channel assignments;
- offline URL validation and canonicalisation;
- a rights-evaluation service and its enforcement in the queue and in every job handler;
- linking local uploads to a source;
- API and dashboard: Creators, Sources, source detail, and the paste-URL box;
- tests and new acceptance steps.

**Not in M2:**
- downloading or probing URLs;
- any YouTube/Twitch API call, credential or Keychain storage (optional, M3, approval
  needed);
- transcription, highlights, rendering, publishing;
- legal review of licence text. The system records the owner's attestation; it does not
  judge it.

## 3. Data model (Alembic `0003_rights_sources`)

| Table | Key columns | Notes |
|---|---|---|
| `games` | id, slug (unique), name, active | Seeded by migration with `gta6` (data, not code) |
| `brand_channels` | id, slug, name, platform, handle, active | RIFTWIRE's own **output** channels (publishing targets later) |
| `creators` | id, display_name, contact (optional), notes, relationship (`self`/`partner`/`licensor`), created/updated | A person or organisation whose footage is used |
| `creator_channels` | id, creator_id, platform (`youtube`/`twitch`/`local`), platform_channel_id (nullable), handle, url_canonical, verified_by_owner_at | Unique (platform, platform_channel_id) and (platform, handle) |
| `rights_records` | id, creator_id, basis (`ownership`/`creator_permission`/`licence`), scope_json, valid_from, valid_until (nullable), revoked_at, revoked_reason, attested_by, attested_at, attestation_text, version | Applies to all of a creator's sources or to one source (see `rights_source_links`) |
| `rights_evidence` | id, rights_record_id, kind (`email`/`document`/`platform_permission`/`note`), reference, sha256, stored_name (nullable), size_bytes, created_at | An optional evidence file is stored internally under `data/evidence/<uuid>.<ext>` with a reservation, atomic write and a read-only result |
| `rights_source_links` | rights_record_id, source_id | Narrows a record to particular sources |
| `sources` | id, kind (`local`/`youtube`/`twitch`), platform_video_id, url_canonical (unique when not null), creator_channel_id, title_user, game_id, status, acquisition_method (`local_file`/`platform_export`/`url_fetch`), created/updated | One per external video or local recording origin. A `url_fetch` source is unusable in M2 by design |
| `source_assignments` | source_id, brand_channel_id | Which RIFTWIRE channels may publish material from this source |
| `recordings.source_id` | new nullable FK, plus `source_offset_ms` (default 0) | Links media to provenance; offsets prepare for M3 section fetches |
| `provenance_events` | id, entity (`source`/`rights`/`recording`/…), entity_id, action, actor (`owner`/`system`), details_json, at | **Append-only.** Rows are never updated or deleted, and API writes are not allowed. A test checks that no code path issues UPDATE or DELETE |

**`scope_json`** is validated by Pydantic and versioned (`"v": 1`):

```json
{"v": 1, "uses": ["analyze", "edit", "publish"],
 "publish_platforms": ["youtube_shorts", "instagram_reels", "facebook_reels", "x"],
 "monetization": true, "attribution_required": "Credit: @creator",
 "max_clip_seconds": 60, "territories": null, "notes": ""}
```

**Rights records are immutable once attested.** Editing creates a new version and
supersedes the old one, which gets an event and a pointer. Revocation is a timestamp, never a
delete.

## 4. URL validation (offline, `services/source_urls.py`)

- Pure parsing with `urllib.parse`. No DNS and no HTTP. Hosts are checked against an
  allow-list, and every other host is refused.
- **YouTube:** `youtube.com/watch?v=`, `youtu.be/`, `youtube.com/shorts/`, `/live/` (a
  recorded video ID), `m.` and `music.` hosts.
  - The video ID must match `^[A-Za-z0-9_-]{11}$`.
  - Canonical form: `https://www.youtube.com/watch?v=<id>`. Tracking parameters (`t`, `si`,
    `feature`, `list`…) are dropped.
  - `t=` is kept separately as an optional start hint.
  - Playlist-only and channel URLs are refused with a clear message ("paste a video
    link"). Channel URLs are used only to identify creator channels.
- **Twitch:** `twitch.tv/videos/<digits>` (VOD), `twitch.tv/<login>/clip/<slug>` and
  `clips.twitch.tv/<slug>`.
  - Logins match `^[a-z0-9_]{4,25}$`.
  - Canonical: `https://www.twitch.tv/videos/<id>` and
    `https://clips.twitch.tv/<slug>`.
  - Live channel URLs are refused: streams in progress are out of scope (proposal §7.2).
- Credentials, userinfo, non-https schemes, IP-literal hosts, IDN look-alikes (punycode
  hosts) and over-long URLs (> 2048 characters) are refused.
- **Deduplication.** One source per canonical URL. Pasting a variant URL opens the
  existing source.
- **Creator matching.** A video URL does not reveal its channel offline. M2 asks the owner
  to pick or create the creator channel when adding a source. Automatic matching via the
  official APIs is an optional M3 extension (decision R4).

## 5. Rights evaluation and enforcement

`services/rights.py` contains one pure function:

```
evaluate(source, use, *, platform=None, brand_channel=None, at=now) -> Decision
Decision(allowed: bool, reason_code, rights_record_id, version, details)
```

Reason codes: `no_rights_record`, `not_attested`, `revoked`, `expired`, `not_yet_valid`,
`use_not_in_scope`, `platform_not_in_scope`, `brand_channel_not_assigned`,
`acquisition_method_not_permitted`, `source_blocked`, `ok`.

**Enforcement points.** The check is never only in the UI.

1. **API.** Creating jobs, attaching media and changing assignments call `evaluate`. Refusals
   return 403 `rights_refused` with the reason code.
2. **Queue enqueue.** A job of a rights-gated type is never created for a source that fails
   the check.
3. **Worker, at handler start and again just before writing outputs.** This catches a
   revocation that happens mid-job. Failure is a **permanent** job error
   (`RightsRefusedJobError`), recorded as `rights_refused` events plus a provenance event.
   These jobs are never retried; changing the rights re-enables manual retry.
4. **Future stages.** M3–M7, rendering and publishing declare `REQUIRED_USE` per job type,
   in the same way as `REQUIRED_KINDS` in M1. A test fails if any registered job type lacks
   a declaration.

**Which jobs are gated** (decision R2). Phase 1 types (INGEST, METADATA, THUMBNAIL) are
technical housekeeping on media the owner already holds. Proposal:
- they stay ungated;
- every analysis, editing or publishing type is gated (`analyze`/`edit`/`publish`);
- existing recordings get no source, so they show **"Rights: not attested"** and can't be
  analysed until the owner attaches them to a source.

This keeps today's ingest working and meets the M2 exit criterion: 100 % of rights-gated
jobs on unconfirmed, expired or out-of-scope sources fail permanently.

**Local uploads.** The upload form gains an optional source selector. A new local import
can start a `local` source attested as `ownership` in one step: a checkbox plus attestation
text, recorded with time and actor.

## 6. API (`/api/v1`)

- `GET/POST /creators`, `GET/PATCH /creators/{id}` (archive, never delete),
  `POST /creators/{id}/channels`
- `POST /sources/resolve` `{url}` returns the canonical form plus an existing source if any.
  It never creates anything.
- `GET/POST /sources`, `GET/PATCH /sources/{id}`, `PUT /sources/{id}/assignments`,
  `POST /sources/{id}/recordings` (attach an existing recording)
- `POST /rights`, `POST /rights/{id}/supersede`, `POST /rights/{id}/revoke`,
  `POST /rights/{id}/evidence` (multipart, size-capped, through reservations)
- `GET /sources/{id}/rights-check?use=publish&platform=youtube_shorts&brand_channel=…`
- `GET /sources/{id}/provenance` (chronological chain from creator to rights to recordings)
- `GET/POST /games`, `GET/POST /brand_channels`

All lists use `{items,total,limit,offset}`. Errors use the standard envelope. No absolute
paths are exposed.

## 7. Dashboard

- **Sources page:**
  - a paste-URL box at the top: resolve, then an existing source or the "new source"
    form;
  - a table with platform, creator, game, rights status badge (`OK`, `EXPIRES SOON`,
    `EXPIRED`, `REVOKED`, `NOT ATTESTED`), assignments and media count.
- **Source detail:**
  - rights panel with the active version, scope summary and evidence list;
  - provenance timeline;
  - attached recordings;
  - an "Attach media" button (upload or pick a recording);
  - brand-channel assignment.
- **Creators page:** creators, their channels and their rights records. Rights forms show
  the attestation text the owner is confirming.
- **Library:** each recording shows its source and rights badge, and unattested
  recordings are flagged.
- Every page has loading, error and empty states (CLAUDE.md §5).

## 8. Tests and acceptance

- **URL parser table tests:**
  - about 60 cases: valid, variant, tracking parameters, Shorts, `/live/`, clips;
  - hostile input: lookalike, punycode, userinfo, `javascript:`, IP literal, over-long,
    playlist-only, live channel.
- **Rights evaluation matrix:** basis × scope × time windows × revocation × platform ×
  brand channel. Boundary cases at `valid_until` use injected clocks.
- **Enforcement:**
  - every job type has a declaration;
  - a rights-gated job fails permanently when rights are revoked between enqueue and
    start, and again between start and output (fault injection);
  - no retry happens.
- **Immutability:** rights versions are superseded, never edited, and `provenance_events`
  is append-only.
- **Migration:** `0003` up/down/up, `alembic check`, existing recordings untouched (source
  is NULL).
- **Evidence upload:** reservation, floor, atomic write, path traversal, size cap and
  read-only result.
- **Frontend:** Sources, Creators and detail views, the paste flow, and their states.
- **New acceptance steps (M2-1 to M2-4):**
  1. create a creator and a licence;
  2. paste a URL variant and confirm deduplication;
  3. attach a synthetic upload and check the provenance chain;
  4. revoke the licence and confirm the gated job is refused while Phase 1 housekeeping
     still works.
  A temporary gated no-op job type is registered in tests only.

## 9. Sequence (estimate: 4–6 working sessions)

1. Migration and models, URL parser with tests.
2. Rights evaluation service and its matrix tests.
3. Enforcement hooks in the queue and worker, the job-type declaration test, events.
4. API and evidence upload.
5. Dashboard pages.
6. Acceptance steps, docs (ARCHITECTURE §20, CHECKLIST, STATUS), Mac run, PR.

## 10. Decisions requested from the owner

| # | Decision | Recommendation |
|---|---|---|
| R1 | Acquisition under platform terms (§1.1) | Methods 1–2 by default; method 3 only with a recorded `platform_permission` document; keep the global switch off. Update PRODUCT_SPEC §2.3 and PHASE2_PROPOSAL §7.2 accordingly (spec change, so approval) |
| R2 | Which job types are rights-gated | Phase 1 housekeeping ungated; every analyse/edit/publish job gated |
| R3 | Evidence files stored locally (internal disk, `data/evidence/`, small, floor-gated) | Yes, capped at 25 MB per file |
| R4 | Official-API metadata (YouTube Data API, Twitch Helix) for titles and channel matching | Defer to M3. Needs owner-created credentials stored in the Keychain. Free, but it requires accepting both platforms' developer terms |
| R5 | First authorized creators and brand channels (open question 4 in PHASE2_PROPOSAL) | Owner to list them; M2 works without them |
| R6 | Approve starting M2 on `phase2/m2-rights-sources` | After PR #9 is reviewed (merging it first keeps M2's diff clean) |

## 11. Risks

- **Attestation is only as good as the owner's statement.** RIFTWIRE records it and shows it
  clearly; it cannot verify a creator's permission. Evidence attachments and immutability
  make the record auditable.
- **Platform terms change.** The terms are dated in this plan, and R1 keeps method 3 off by
  default.
- **Gating gaps in future job types.** The mandatory-declaration test turns a missing gate
  into a test failure.
- **Two branches in flight (M1 review and M2).** M2 touches different files, apart from the
  `recordings` FK and the worker hook. Rebasing on the merged PR #9 before the M2 PR
  resolves this.

Sources: YouTube Terms of Service (15 Dec 2023); YouTube Data API quota documentation;
Twitch API documentation (Get Videos, authentication); Twitch developer forum thread on
downloading own VODs.
