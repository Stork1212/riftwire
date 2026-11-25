import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { rightsApi } from "../services/rightsApi";
import type { Job } from "../types/api";
import type { Decision } from "../types/rights";
import { ActionableError } from "./ActionableError";
import { ProgressBar, StatusBadge } from "./ui";

const ACTIVE = new Set(["PENDING", "RUNNING", "RETRY_WAIT"]);
const POLL_MS = 2000;

/** Fetching a saved video link into a recording linked to its source (one ACQUIRE job per
 * source). Public availability is not permission: a link is fetched only under a
 * user-confirmed permission that allows URL fetching, with written platform permission
 * attached, and only while URL fetching is switched on. With standing permission, saving
 * the link queues it. The server checks the rights before queueing, before and after the
 * download, and records how the footage was obtained. ``decision``, when known, disables
 * the button and shows why. */
export function SourceAcquire({ sourceId, job: initial, onChange, decision }: {
  sourceId?: string; job?: Job | null; onChange?: () => void; decision?: Decision;
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
          disabled={!sourceId || busy || (status !== undefined && status !== "FAILED")
            || (decision !== undefined && !decision.allowed)}>
          {busy ? "Queueing…" : "Acquire footage"}
        </button>
        {!sourceId && <span className="muted">Save the link first. It is fetched only when a creator's permission allows URL fetching.</span>}
        {sourceId && !job && decision && !decision.allowed && (
          <span className="muted" data-testid="acquire-blocked">Not fetched: {decision.message}.</span>)}
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
