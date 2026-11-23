import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { JobStages } from "../components/JobStages";
import { ErrorBanner, Panel, ProgressBar, StatusBadge } from "../components/ui";
import { ApiError, api, uploadRecording, type UploadHandle } from "../services/api";
import { formatBytes } from "../services/format";
import { usePolling } from "../services/usePolling";
import type { Recording } from "../types/api";

const ACCEPT = ".mp4,.mov,.mkv,.webm,video/mp4,video/quicktime,video/x-matroska,video/webm";

type Phase =
  | { kind: "idle" }
  | { kind: "uploading"; loaded: number; total: number }
  | { kind: "error"; error: ApiError }
  | { kind: "done"; recording: Recording };

export function ImportPage() {
  const [file, setFile] = useState<File | null>(null);
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [drag, setDrag] = useState(false);
  const inFlight = useRef<UploadHandle | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => () => inFlight.current?.abort(), []);

  const choose = (f: File | null | undefined) => {
    if (!f || inFlight.current) return;
    setFile(f);
    setPhase({ kind: "idle" });
  };

  const start = useCallback(async () => {
    if (!file || inFlight.current) return; // guard against repeated submissions
    setPhase({ kind: "uploading", loaded: 0, total: file.size });
    const handle = uploadRecording(file, (loaded, total) =>
      setPhase({ kind: "uploading", loaded, total }));
    inFlight.current = handle;
    try {
      const res = await handle.promise;
      setPhase({ kind: "done", recording: res.recording });
      setFile(null);
    } catch (e) {
      setPhase({ kind: "error", error: e instanceof ApiError ? e : new ApiError(0, "unknown", String(e)) });
    } finally {
      inFlight.current = null;
      if (inputRef.current) inputRef.current.value = "";
    }
  }, [file]);

  const uploading = phase.kind === "uploading";

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Import</h1>
          <p>MP4, MOV, MKV or WEBM. Files are checked by content, fingerprinted and stored safely.</p>
        </div>
      </div>

      <Panel title="1 · Select recording">
        <label
          className={`dropzone${drag ? " drag" : ""}`}
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); choose(e.dataTransfer.files[0]); }}
        >
          <input ref={inputRef} className="visually-hidden" type="file" accept={ACCEPT}
            aria-label="Choose a recording file" disabled={uploading}
            onChange={(e) => choose(e.target.files?.[0])} />
          <strong>{file ? file.name : "Drop a recording here or click to browse"}</strong>
          <small>{file ? formatBytes(file.size) : "Large files are streamed to disk — they are never loaded into memory."}</small>
        </label>
        <div className="toolbar" style={{ marginTop: 14 }}>
          <button className="btn btn-primary" onClick={start} disabled={!file || uploading}>
            {uploading ? "Uploading…" : "Upload"}
          </button>
          {uploading && (
            <button className="btn btn-ghost" onClick={() => inFlight.current?.abort()}>Cancel</button>
          )}
        </div>
      </Panel>

      <Panel title="2 · Upload">
        {phase.kind === "idle" && <p className="muted">No upload in progress.</p>}
        {phase.kind === "uploading" && (
          <div>
            <ProgressBar value={phase.total ? (phase.loaded / phase.total) * 100 : 0} label="Upload progress" />
            <p className="muted num">{formatBytes(phase.loaded)} of {formatBytes(phase.total)} uploaded
              {phase.loaded >= phase.total && phase.total > 0 ? " — validating media…" : ""}</p>
          </div>
        )}
        {phase.kind === "error" && <UploadError error={phase.error} />}
        {phase.kind === "done" && (
          <div className="alert alert-ok" role="status">
            Uploaded <strong>{phase.recording.original_filename}</strong>. Background processing has started.
          </div>
        )}
      </Panel>

      {phase.kind === "done" && <Processing recording={phase.recording} />}
    </>
  );
}

function UploadError({ error }: { error: ApiError }) {
  const existing = error.details["existing_recording_id"];
  if (error.code === "duplicate_recording" && typeof existing === "string") {
    return (
      <div className="alert alert-warn" role="alert">
        <strong>Duplicate.</strong> {error.message}{" "}
        <Link to={`/recordings/${existing}`}>Open existing recording →</Link>
      </div>
    );
  }
  if (error.code === "aborted") return <p className="muted">Upload cancelled. Nothing was imported.</p>;
  return <ErrorBanner error={error.message} title="Import rejected" />;
}

function Processing({ recording }: { recording: Recording }) {
  const { data } = usePolling(() => api.getRecording(recording.id), 1500, [recording.id]);
  return (
    <Panel title="3 · Background processing"
      actions={data && <StatusBadge status={data.status} />}>
      <JobStages jobs={data?.jobs ?? []} />
      <p style={{ marginTop: 12 }}>
        <Link to={`/recordings/${recording.id}`}>View recording →</Link>
      </p>
    </Panel>
  );
}
