import { Link } from "react-router-dom";
import { Panel, StatCard, StatusBadge, ErrorBanner, Loading, EmptyState } from "../components/ui";
import { StorageBar } from "../components/StorageBar";
import { api } from "../services/api";
import { formatBytes, formatTotalDuration } from "../services/format";
import { usePolling } from "../services/usePolling";

export function OverviewPage() {
  const { data, error, loading } = usePolling(api.systemStatus, 3000);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Overview</h1>
          <p>Library, processing queue and storage at a glance.</p>
        </div>
        <Link to="/import" className="btn btn-primary">Import recording</Link>
      </div>
      {error && <ErrorBanner error={error} title="Cannot load status" />}
      {loading && !data && <Loading />}
      {data && (
        <>
          <div className="grid stats">
            <StatCard label="Recordings" value={data.counts.recordings} />
            <StatCard label="Imported duration" value={formatTotalDuration(data.counts.total_duration_ms)} />
            <StatCard label="Pending jobs" value={data.counts.jobs.PENDING + data.counts.jobs.RETRY_WAIT}
              hint={data.counts.jobs.RETRY_WAIT ? `${data.counts.jobs.RETRY_WAIT} waiting to retry` : undefined} />
            <StatCard label="Running jobs" value={data.counts.jobs.RUNNING} tone={data.counts.jobs.RUNNING ? "info" : undefined} />
            <StatCard label="Completed jobs" value={data.counts.jobs.COMPLETED} tone="ok" />
            <StatCard label="Failed jobs" value={data.counts.jobs.FAILED} tone={data.counts.jobs.FAILED ? "bad" : undefined} />
          </div>
          <div className="split">
            <Panel title="Storage">
              <StorageBar storage={data.storage} />
            </Panel>
            <Panel title="Worker">
              <dl className="kv">
                <dt>Status</dt>
                <dd><StatusBadge status={data.worker.state} /></dd>
                <dt>Concurrency</dt>
                <dd>{data.worker.concurrency ?? "—"}</dd>
                <dt>Last heartbeat</dt>
                <dd>{data.worker.heartbeat_age_seconds !== undefined ? `${data.worker.heartbeat_age_seconds}s ago` : "—"}</dd>
              </dl>
              {data.worker.state !== "RUNNING" && data.worker.state !== "STOPPING" && (
                <p className="muted">Imports are accepted, but processing waits until the worker runs
                  (<code>scripts/start.sh</code>).</p>
              )}
            </Panel>
          </div>
          {data.counts.recordings === 0 && (
            <Panel>
              <EmptyState title="No recordings yet">
                Import an MP4, MOV, MKV or WEBM recording to get started.{" "}
                <Link to="/import">Go to Import →</Link>
              </EmptyState>
            </Panel>
          )}
          {data.recent_errors.length > 0 && (
            <Panel title="Recent errors" actions={<Link to="/system" className="btn btn-ghost btn-sm">View all</Link>}>
              <ul className="events">
                {data.recent_errors.slice(0, 5).map((e) => (
                  <li key={e.id}><span className="muted">{new Date(e.timestamp).toLocaleString()}</span><span>{e.message}</span></li>
                ))}
              </ul>
            </Panel>
          )}
          <p className="muted">Recordings occupy {formatBytes(data.storage.recordings_bytes)}.</p>
        </>
      )}
    </>
  );
}
