import type {
  ApiErrorBody,
  ImportResponse,
  Job,
  Page,
  Recording,
  RecordingDetail,
  StorageStatus,
  SystemEvent,
  SystemStatus,
} from "../types/api";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function parseError(status: number, text: string): ApiError {
  try {
    const body = JSON.parse(text) as Partial<ApiErrorBody>;
    if (body.error) {
      return new ApiError(status, body.error.code, body.error.message, body.error.details ?? {});
    }
  } catch {
    /* not JSON */
  }
  if (status === 0) {
    return new ApiError(0, "network_error", "Cannot reach the RIFTWIRE backend. Is it running?");
  }
  return new ApiError(status, "http_error", `Request failed (HTTP ${status})`);
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { headers: { Accept: "application/json" }, ...init });
  } catch {
    throw parseError(0, "");
  }
  const text = await res.text();
  if (!res.ok) throw parseError(res.status, text);
  return JSON.parse(text) as T;
}

export function qs(params: Record<string, string | number | undefined | null>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") u.set(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : "";
}

export interface ClipSuggestion {
  start_ms: number;
  end_ms: number;
  transcript: string;
}

export function suggestClips(id: string): Promise<{ items: ClipSuggestion[] }> {
  return request(`/api/v1/recordings/${encodeURIComponent(id)}/clip-suggestions`, { method: "POST" });
}

export interface ClipRange {
  line: number;
  label: string;
  start_ms: number;
  end_ms: number;
  from_ms: number;
  to_ms: number;
}

export function validateClipList(recordingId: string, text: string):
  Promise<{ source_url: string | null; items: ClipRange[] }> {
  return request(`/api/v1/recordings/${encodeURIComponent(recordingId)}/clip-list`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
}

export type ClipLayout = "original" | "split" | "split_facecam";

export async function downloadClip(id: string, start: number, end: number, subtitles = false,
  layout: ClipLayout = "original", title?: string): Promise<void> {
  const query = qs({ start_ms: start, end_ms: end, subtitles: subtitles ? "true" : undefined,
    layout: layout === "original" ? undefined : layout, title });
  const path = `/api/v1/recordings/${encodeURIComponent(id)}/clip${query}`;
  const suffix = layout !== "original" ? "-short" : subtitles ? "-captioned" : "";
  return downloadMp4(path, `clip-${start}-${end}${suffix}.mp4`);
}

async function downloadMp4(path: string, filename: string): Promise<void> {
  let response: Response;
  try {
    response = await fetch(path, { method: "POST" });
  } catch {
    throw parseError(0, "");
  }
  if (!response.ok) throw parseError(response.status, await response.text());
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export interface ListParams {
  limit?: number;
  offset?: number;
}

export const api = {
  systemStatus: () => request<SystemStatus>("/api/v1/system/status"),

  storage: () => request<StorageStatus>("/api/v1/storage"),

  listRecordings: (p: ListParams & { q?: string; status?: string } = {}) =>
    request<Page<Recording>>(`/api/v1/recordings${qs({ ...p })}`),

  getRecording: (id: string) =>
    request<RecordingDetail>(`/api/v1/recordings/${encodeURIComponent(id)}`),

  listJobs: (p: ListParams & { status?: string; job_type?: string; recording_id?: string } = {}) =>
    request<Page<Job>>(`/api/v1/jobs${qs({ ...p })}`),

  retryJob: (id: string) =>
    request<Job>(`/api/v1/jobs/${encodeURIComponent(id)}/retry`, { method: "POST" }),

  listEvents: (p: ListParams & { severity?: string } = {}) =>
    request<Page<SystemEvent>>(`/api/v1/events${qs({ ...p })}`),
};

export interface UploadHandle {
  promise: Promise<ImportResponse>;
  abort: () => void;
}

/**
 * Streams the file as the raw request body (no multipart), so the backend writes it to
 * disk once. XHR is used because fetch() has no upload-progress events.
 */
export function uploadRecording(
  file: File,
  onProgress: (loaded: number, total: number) => void,
  link: { source_id?: string; creator_channel_id?: string; acquisition_method?: string } = {},
): UploadHandle {
  const xhr = new XMLHttpRequest();
  const promise = new Promise<ImportResponse>((resolve, reject) => {
    xhr.open("POST", `/api/v1/recordings/import${qs({ filename: file.name, ...link })}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded, e.total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(JSON.parse(xhr.responseText) as ImportResponse);
      } else {
        reject(parseError(xhr.status, xhr.responseText));
      }
    };
    xhr.onerror = () => reject(parseError(0, ""));
    xhr.onabort = () => reject(new ApiError(0, "aborted", "Upload cancelled"));
    xhr.send(file);
  });
  return { promise, abort: () => xhr.abort() };
}
