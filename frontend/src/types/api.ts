// Mirrors backend/app/schemas/api.py and /api/v1/system/status.

export type RecordingStatus = "STAGED" | "PROCESSING" | "READY" | "FAILED";
export type ThumbnailStatus = "PENDING" | "READY" | "FAILED";
export type JobType = "ACQUIRE" | "HIGHLIGHT_SCAN" | "INGEST" | "METADATA" | "THUMBNAIL" | "TRANSCRIBE";
export type JobStatus =
  | "PENDING"
  | "RUNNING"
  | "COMPLETED"
  | "FAILED"
  | "RETRY_WAIT"
  | "CANCELLED";

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface Job {
  id: string;
  /** null for an ACQUIRE job until its recording exists */
  recording_id: string | null;
  /** set for ACQUIRE jobs (fetching a saved source link) */
  source_id?: string | null;
  recording_filename: string | null;
  job_type: JobType;
  status: JobStatus;
  progress_percent: number;
  attempt_count: number;
  max_attempts: number;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
  next_attempt_at: string | null;
  lease_expires_at: string | null;
  last_error: string | null;
  retryable: boolean;
}

export interface Recording {
  id: string;
  original_filename: string;
  stored_filename: string;
  sha256: string;
  file_size_bytes: number;
  container_format: string | null;
  duration_ms: number | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  fps_rational: string | null;
  is_variable_frame_rate: boolean | null;
  video_codec: string | null;
  audio_codec: string | null;
  has_audio: boolean | null;
  imported_at: string;
  status: RecordingStatus;
  thumbnail_status: ThumbnailStatus;
  error_message: string | null;
  thumbnail_url: string;
  source_id?: string | null;
  /** How linked media was obtained; null = not on record (rights-gated uses refused). */
  acquisition_method?: string | null;
}

export interface RecordingDetail extends Recording {
  jobs: Job[];
}

export interface SystemEvent {
  id: number;
  timestamp: string;
  severity: "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";
  component: string;
  event_type: string;
  message: string;
  related_job_id: string | null;
  related_recording_id: string | null;
}

export interface ImportResponse {
  recording: Recording;
  job_id: string;
  source_id?: string | null;
  source_error?: string | null;
}

export type WorkerState =
  | "RUNNING"
  | "STOPPING"
  | "UNRESPONSIVE"
  | "STOPPED"
  | "NEVER_STARTED";

export interface ToolInfo {
  available: boolean;
  path: string | null;
  version: string | null;
  hwaccels?: string[];
  videotoolbox?: boolean;
}

export interface SystemStatus {
  version: string;
  environment: string;
  backend: { status: string };
  readiness: { ready: boolean; checks: Record<string, { ok: boolean; [k: string]: unknown }> };
  worker: {
    state: WorkerState;
    id: string | null;
    last_heartbeat_at: string | null;
    heartbeat_age_seconds?: number;
    current_job_id: string | null;
    concurrency: number | null;
  };
  ffmpeg: ToolInfo;
  ffprobe: ToolInfo;
  data_dir: string;
  storage: StorageStatus;
  counts: {
    recordings: number;
    recordings_by_status: Partial<Record<RecordingStatus, number>>;
    total_duration_ms: number;
    jobs: Record<JobStatus, number>;
  };
  recent_errors: SystemEvent[];
}

export type LocationKind = "recordings" | "temp" | "models" | "exports" | "assets";

export interface StorageLocation {
  kind: LocationKind;
  path?: string;
  online: boolean;
  reason: string;
  message: string;
  fs_type?: string | null;
  volume_uuid?: string;
  volume_label?: string | null;
  mount_point?: string | null;
  usage_bytes?: number | null;
}

export interface StorageVolume {
  volume_uuid: string;
  kinds: LocationKind[];
  online: boolean;
  label: string | null;
  mount_point: string | null;
  floor_bytes: number;
  total_bytes: number | null;
  free_bytes: number | null;
  reserved_bytes: number;
  headroom_bytes: number | null;
  below_floor: boolean | null;
}

export interface StorageMigration {
  id: string;
  kind: LocationKind;
  source_path: string;
  dest_path: string;
  status: "COPYING" | "SWITCHED" | "ABORTED";
  total_files: number;
  verified_files: number;
  total_bytes: number;
  updated_at: string;
}

export interface StorageStatus {
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  min_free_bytes: number;
  importable_bytes: number;
  recordings_bytes: number;
  below_floor: boolean;
  locations: StorageLocation[];
  volumes: StorageVolume[];
  migrations?: StorageMigration[];
}

export interface ApiErrorBody {
  error: { code: string; message: string; details: Record<string, unknown> };
}
