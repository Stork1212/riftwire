import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { EmptyState, ErrorBanner, Loading, PageHeader, Pagination, Panel, ProgressBar, StatusBadge } from "../components/ui";
import { api } from "../services/api";
import { formatDate } from "../services/format";
import { usePolling } from "../services/usePolling";

const LIMIT = 50;
const STATUSES = ["", "PENDING", "RUNNING", "RETRY_WAIT", "FAILED", "COMPLETED", "CANCELLED"];

export function QueuePage() {
  const [params] = useSearchParams();
  const recordingId = params.get("recording") ?? undefined;
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);
  const [retrying, setRetrying] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const { data, error, loading, refresh } = usePolling(
    () => api.listJobs({ limit: LIMIT, offset, status, recording_id: recordingId }),
    2000, [offset, status, recordingId]);

  const retry = async (id: string) => {
    if (retrying) return;
    setRetrying(id);
    setActionError(null);
    try {
      await api.retryJob(id);
      refresh();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setRetrying(null);
    }
  };

  return (
    <>
      <PageHeader title="Processing Queue"
        description={recordingId ? <>Jobs for one recording · <Link to="/queue">show all</Link></> : "All background jobs, newest first."}
        actions={<>
          <label className="visually-hidden" htmlFor="status-filter">Filter by status</label>
          <select id="status-filter" value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }}>
            {STATUSES.map((s) => <option key={s} value={s}>{s ? s.replace("_", " ") : "All statuses"}</option>)}
          </select>
        </>} />
      {error && <ErrorBanner error={error} title="Cannot load jobs" />}
      {actionError && <ErrorBanner error={actionError} title="Retry failed" />}
      <Panel>
        {loading && !data && <Loading />}
        {data && data.items.length === 0 && (
          <EmptyState title={status ? "No jobs with this status" : "The queue is empty"}>
            Jobs are created automatically when a recording is imported.
          </EmptyState>
        )}
        {data && data.items.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Job</th><th>Recording</th><th>Status</th>
                  <th>Attempts</th><th>Timing</th><th>Error</th><th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {data.items.map((j) => (
                  <tr key={j.id} data-testid={`job-${j.id}`}>
                    <td className="nowrap"><strong>{j.job_type}</strong></td>
                    <td className="cell-primary"><Link className="row-link" to={`/recordings/${j.recording_id}`}>{j.recording_filename ?? j.recording_id}</Link></td>
                    <td className="cell-status">
                      <StatusBadge status={j.status} />
                      <ProgressBar value={j.progress_percent} label={`${j.job_type} progress`}
                        tone={j.status === "FAILED" ? "bad" : j.status === "COMPLETED" ? "ok" : "info"} />
                    </td>
                    <td className="num">{j.attempt_count} / {j.max_attempts}</td>
                    <td className="num">
                      <div>Started {formatDate(j.started_at)}</div>
                      <div className="sub">Completed {formatDate(j.completed_at)}</div>
                    </td>
                    <td>{j.last_error && j.status !== "COMPLETED" ? <span className="err-text">{j.last_error}</span> : <span className="muted">—</span>}</td>
                    <td>
                      {j.retryable && (
                        <button className="btn btn-sm" disabled={retrying !== null} onClick={() => retry(j.id)}>
                          {retrying === j.id ? "Retrying…" : "Retry"}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {data && <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />}
      </Panel>
    </>
  );
}
