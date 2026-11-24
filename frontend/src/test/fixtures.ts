// Test-only fixtures (never used by the running application).
import type { StorageStatus, Job, Recording, RecordingDetail, SystemStatus } from "../types/api";

export const recording = (over: Partial<Recording> = {}): Recording => ({
  id: "rec-1",
  original_filename: "heist.mp4",
  stored_filename: "rec-1.mp4",
  sha256: "a".repeat(64),
  file_size_bytes: 5 * 1024 * 1024,
  container_format: "mov,mp4,m4a,3gp,3g2,mj2",
  duration_ms: 125_000,
  width: 1920,
  height: 1080,
  fps: 30000 / 1001,
  fps_rational: "30000/1001",
  is_variable_frame_rate: false,
  video_codec: "h264",
  audio_codec: "aac",
  has_audio: true,
  imported_at: "2026-11-23T20:00:00Z",
  status: "READY",
  thumbnail_status: "READY",
  error_message: null,
  thumbnail_url: "/api/v1/recordings/rec-1/thumbnail",
  ...over,
});

export const job = (over: Partial<Job> = {}): Job => ({
  id: "job-1",
  recording_id: "rec-1",
  recording_filename: "heist.mp4",
  job_type: "INGEST",
  status: "COMPLETED",
  progress_percent: 100,
  attempt_count: 1,
  max_attempts: 3,
  created_at: "2026-11-23T20:00:00Z",
  updated_at: "2026-11-23T20:00:05Z",
  started_at: "2026-11-23T20:00:01Z",
  completed_at: "2026-11-23T20:00:05Z",
  next_attempt_at: null,
  lease_expires_at: null,
  last_error: null,
  retryable: false,
  ...over,
});

export const detail = (jobs: Job[], over: Partial<Recording> = {}): RecordingDetail => ({
  ...recording(over),
  jobs,
});

export const systemStatus = (over: Partial<SystemStatus> = {}): SystemStatus => ({
  version: "0.1.0",
  environment: "test",
  backend: { status: "ok" },
  readiness: { ready: true, checks: { database: { ok: true } } },
  worker: { state: "RUNNING", id: "w", last_heartbeat_at: "2026-11-23T20:00:00Z",
    heartbeat_age_seconds: 2, current_job_id: null, concurrency: 1 },
  ffmpeg: { available: true, path: "/opt/homebrew/bin/ffmpeg", version: "ffmpeg version 9.1",
    hwaccels: ["videotoolbox"], videotoolbox: true },
  ffprobe: { available: true, path: "/opt/homebrew/bin/ffprobe", version: "ffprobe version 9.1" },
  data_dir: "/Users/test/riftwire/data",
  storage: { total_bytes: 500 * 2 ** 30, used_bytes: 430 * 2 ** 30, free_bytes: 70 * 2 ** 30,
    min_free_bytes: 60 * 2 ** 30, importable_bytes: 10 * 2 ** 30, recordings_bytes: 2 ** 30,
    below_floor: false, locations: [], volumes: [] },
  counts: { recordings: 3, recordings_by_status: { READY: 3 }, total_duration_ms: 3_720_000,
    jobs: { PENDING: 2, RUNNING: 1, COMPLETED: 7, FAILED: 1, RETRY_WAIT: 1, CANCELLED: 0 } },
  recent_errors: [],
  ...over,
});

export const storageStatus = (over: Partial<StorageStatus> = {}): StorageStatus => ({
  total_bytes: 500 * 2 ** 30, used_bytes: 430 * 2 ** 30, free_bytes: 70 * 2 ** 30,
  min_free_bytes: 60 * 2 ** 30, importable_bytes: 10 * 2 ** 30, recordings_bytes: 2 ** 30,
  below_floor: false,
  locations: (["recordings", "temp", "models", "exports", "assets"] as const).map((kind) => ({
    kind, path: `/data/${kind}`, online: true, reason: "online", message: "online",
    fs_type: "apfs", volume_uuid: "vol-internal", volume_label: "Macintosh HD",
    mount_point: "/System/Volumes/Data", usage_bytes: kind === "recordings" ? 2 ** 30 : 0,
  })),
  volumes: [{ volume_uuid: "vol-internal", kinds: ["recordings", "temp", "models", "exports",
    "assets"], online: true, label: "Macintosh HD", mount_point: "/System/Volumes/Data",
  floor_bytes: 60 * 2 ** 30, total_bytes: 500 * 2 ** 30, free_bytes: 70 * 2 ** 30,
  reserved_bytes: 2 ** 30, headroom_bytes: 9 * 2 ** 30, below_floor: false }],
  ...over,
});
