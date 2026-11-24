import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { rightsApi } from "../services/rightsApi";
import type { Job } from "../types/api";
import { ActionableError } from "./ActionableError";
import { ProgressBar, StatusBadge } from "./ui";

const ACTIVE = new Set(["PENDING", "RUNNING", "RETRY_WAIT"]);
const POLL_MS = 2000;

/** Queue fetching a saved video link into a recording linked to its source (one ACQUIRE
 * job per source). The server checks the licence (URL fetching allowed, written platform
 * permission on record) before queueing, before and after the download, and records how
 * the footage was obtained. Shows the job's live state. */
export function SourceAcquire({ sourceId, job: initial, onChange }: {
  sourceId?: string; job?: Job | null; onChange?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [job, setJob] = useState<Job | null>(initial ?? null);
  const status = job?.status;
  const changed = useRef(onChange);
  useEffect(() => { changed.current = onChange; }, [onChange]);

  useEffect(() => {
    if (!job || !ACTIVE.has(job.status)) return;
    const t = setTimeout(() => {
      rightsApi.acquireJob(job.id).then((j) => {
        setJob(j);
        if (!ACTIVE.has(j.status)) changed.current?.();
      }).catch(setError);
    }, POLL_MS);
    return () => clearTimeout(t);
  }, [job]);

  const acquire = async () => {
    if (!sourceId || busy) return;
    setBusy(true);
    setError(undefined);
    try {
      const a = await rightsApi.acquireSource(sourceId);
      setJob(a.job);
      changed.current?.();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack">
      <div className="toolbar">
        <button type="button" className="btn" onClick={acquire}
          disabled={!sourceId || busy || (status !== undefined && status !== "FAILED")}>
          {busy ? "Queueing…" : "Acquire footage"}
        </button>
        {!sourceId && <span className="muted">Save the source first; acquisition needs a licence that allows URL fetching and written platform permission.</span>}
        {job && <StatusBadge status={job.status} />}
        {job && ACTIVE.has(job.status) && (
          <span className="muted" role="status">
            {job.status === "RETRY_WAIT" ? `Retrying (attempt ${job.attempt_count} of ${job.max_attempts})` : "Acquiring in the background"}
            {" · "}<Link to="/queue">Processing Queue</Link>
          </span>
        )}
        {job?.status === "COMPLETED" && job.recording_id && (
          <span role="status">Acquired and linked:{" "}
            <Link to={`/recordings/${job.recording_id}`}>{job.recording_filename ?? "recording"}</Link>
          </span>
        )}
      </div>
      {job && ACTIVE.has(job.status) &&
        <ProgressBar value={job.progress_percent} label="Acquisition progress" tone="info" />}
      {job?.status === "FAILED" && (
        <div className="alert alert-bad" role="alert">
          <strong>Not acquired.</strong> {job.last_error}{" "}
          {job.retryable && <Link to="/queue">Retry it from the Processing Queue</Link>}
        </div>
      )}
      {error !== undefined && <ActionableError title="Not acquired" error={error} />}
    </div>
  );
}
