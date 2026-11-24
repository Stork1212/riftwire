import { EmptyState, ErrorBanner, Loading, Panel, StatusBadge } from "../components/ui";
import { StorageBar } from "../components/StorageBar";
import { api } from "../services/api";
import { formatDate } from "../services/format";
import { usePolling } from "../services/usePolling";
import type { ToolInfo } from "../types/api";

function Tool({ t }: { t: ToolInfo }) {
  return t.available
    ? <span><StatusBadge status="READY" /> <span className="muted">{t.version}</span></span>
    : <span><StatusBadge status="FAILED" /> <span className="muted">not found — install with <code>brew install ffmpeg</code></span></span>;
}

export function SystemPage() {
  const status = usePolling(api.systemStatus, 3000);
  const events = usePolling(() => api.listEvents({ limit: 25, severity: "ERROR" }), 5000);
  const d = status.data;

  return (
    <>
      <div className="page-head"><div><h1>System</h1><p>Health of the local RIFTWIRE services.</p></div></div>
      {status.error && <ErrorBanner error={status.error} title="Backend unreachable" />}
      {status.loading && !d && <Loading />}
      {d && (
        <div className="split">
          <Panel title="Services">
            <dl className="kv">
              <dt>Application version</dt><dd>{d.version} <span className="muted">({d.environment})</span></dd>
              <dt>Backend</dt><dd><StatusBadge status={d.readiness.ready ? "READY" : "FAILED"} />
                {!d.readiness.ready && <span className="muted"> not ready: {Object.entries(d.readiness.checks).filter(([, c]) => !c.ok).map(([k]) => k).join(", ")}</span>}</dd>
              <dt>Worker</dt><dd><StatusBadge status={d.worker.state} />
                {d.worker.last_heartbeat_at && <span className="muted"> last heartbeat {formatDate(d.worker.last_heartbeat_at)}</span>}</dd>
              <dt>FFmpeg</dt><dd><Tool t={d.ffmpeg} /></dd>
              <dt>ffprobe</dt><dd><Tool t={d.ffprobe} /></dd>
              <dt>VideoToolbox</dt><dd>{d.ffmpeg.videotoolbox ? "Available" : <span className="muted">Not available</span>}</dd>
              <dt>Data directory</dt><dd className="mono">{d.data_dir}</dd>
            </dl>
          </Panel>
          <Panel title="Storage"><StorageBar storage={d.storage} /></Panel>
        </div>
      )}
      <Panel title="Recent system errors">
        {events.error && <ErrorBanner error={events.error} title="Cannot load events" />}
        {events.data && events.data.items.length === 0 && <EmptyState title="No errors recorded" />}
        {events.data && events.data.items.length > 0 && (
          <ul className="events">
            {events.data.items.map((e) => (
              <li key={e.id}>
                <span className="muted num">{formatDate(e.timestamp)}</span>
                <span><strong>{e.component}</strong> · {e.event_type} — {e.message}</span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </>
  );
}
