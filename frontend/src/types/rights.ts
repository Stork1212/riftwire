// Mirrors backend/app/schemas/rights.py (M2 rights & sources).
import type { Job, Recording } from "./api";

export type Use = "acquire" | "analyze" | "edit" | "publish";

export interface Game {
  id: string;
  slug: string;
  name: string;
  active: boolean;
  created_at: string;
}

export interface BrandChannel {
  id: string;
  slug: string;
  name: string;
  platform: string;
  handle: string | null;
  monetized: boolean;
  active: boolean;
  created_at: string;
}

export interface CreatorChannel {
  id: string;
  creator_id: string;
  platform: "youtube" | "twitch" | "local";
  handle: string | null;
  platform_channel_id: string | null;
  url_canonical: string | null;
  verified_by_owner_at: string | null;
  created_at: string;
}

export interface Creator {
  id: string;
  display_name: string;
  relationship: "self" | "partner" | "licensor" | "unaffiliated";
  contact: string | null;
  notes: string | null;
  archived: boolean;
  created_at: string;
  updated_at: string;
  channels: CreatorChannel[];
  current_rights: number;
}

export interface Scope {
  v: 1;
  uses: Use[];
  acquisition_methods: string[];
  publish_platforms: string[];
  brand_channels: string[] | "*";
  monetization: boolean;
  attribution_required: string | null;
  max_clip_seconds: number | null;
  territories: string[] | null;
  notes: string;
}

export interface Evidence {
  id: string;
  rights_record_id: string;
  kind: string;
  reference: string | null;
  original_filename: string | null;
  has_file: boolean;
  sha256: string | null;
  size_bytes: number | null;
  created_at: string;
  file_url: string | null;
}

export interface RightsRecord {
  id: string;
  lineage_id: string;
  version: number;
  creator_id: string;
  basis: string;
  coverage: "creator" | "channels" | "sources";
  scope: Scope | null;
  scope_sha256: string;
  valid_from: string;
  valid_until: string | null;
  attestation_text: string | null;
  attested_by?: string | null;
  attested_at: string | null;
  superseded_by_id: string | null;
  revoked_at: string | null;
  revoked_reason: string | null;
  created_at: string;
  state: string;
  channel_ids: string[];
  source_ids: string[];
  evidence: Evidence[];
}

export interface Decision {
  allowed: boolean;
  reason: string;
  message: string;
  use: Use;
  source_id: string | null;
  record_id: string | null;
  version: number | null;
  obligations: Record<string, unknown>[];
  action: string | null;
}

/** POST /sources/{id}/acquire: fetching the saved link is queued as an ACQUIRE job
 * (one per source); ``recording`` is set once the job has linked it. */
export interface Acquired {
  source_id: string;
  job: Job;
  recording: Recording | null;
}

export interface Source {
  id: string;
  kind: "local" | "youtube" | "twitch";
  platform_video_id: string | null;
  url_canonical: string | null;
  start_hint_ms: number | null;
  creator_channel_id: string | null;
  creator_id: string | null;
  creator_name: string | null;
  channel_label: string | null;
  title_user: string | null;
  game_id: string | null;
  game_name: string | null;
  status: "active" | "blocked" | "archived";
  acquisition_method: string;
  created_at: string;
  updated_at: string;
  rights_status: string;
  authorized_uses: string[];
  brand_channel_ids: string[];
  recording_count: number;
}

export interface SourceDetail extends Source {
  analyze: Decision;
  acquire: Decision;
  records: RightsRecord[];
  recordings: Recording[];
  /** the source's ACQUIRE job, if one was queued */
  acquire_job?: Job | null;
}

export interface Resolved {
  platform: string;
  media: string;
  video_id: string;
  canonical_url: string;
  start_ms: number | null;
  channel_login: string | null;
  existing_source: Source | null;
  match: { status: "matched" | "ambiguous" | "unmatched"; matcher: string | null; reason: string;
    creator_channel_id: string | null; candidates: string[] };
  channel: CreatorChannel | null;
  creator_name: string | null;
  covered: boolean;
  covering_record_ids: string[];
}

export interface ProvenanceEvent {
  id: number;
  entity: string;
  entity_id: string;
  action: string;
  actor: "owner" | "system";
  details: Record<string, unknown>;
  at: string;
}

export interface AcquisitionMethodInfo {
  method: string;
  available: boolean;
  description: string;
}

export interface RightsInput {
  creator_id: string;
  basis: string;
  coverage: string;
  scope: Scope;
  valid_until?: string | null;
  channel_ids?: string[];
  source_ids?: string[];
  attestation_text: string;
}
