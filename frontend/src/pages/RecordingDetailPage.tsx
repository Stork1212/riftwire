import { Link, useParams } from "react-router-dom";
import { JobStages } from "../components/JobStages";
import { ErrorBanner, Loading, Panel, StatusBadge } from "../components/ui";
import { api } from "../services/api";
import { formatBytes, formatDate, formatDuration, formatFps, formatResolution } from "../services/format";
import { usePolling } from "../services/usePolling";

export function RecordingDetailPage() {
  const { id = "" } = useParams();
  const { data: r, error, loading } = usePolling(() => api.getRecording(id), 3000, [id]);

  return (
    <>
      <div className="page-head">
        <div>
          <Link to="/recordings" className="muted">← Recordings</Link>
          <h1 style={{ marginTop: 6 }}>{r?.original_filename ?? "Recording"}</h1>
        </div>
        {r && <StatusBadge status={r.status} />}
      </div>
      {error && <ErrorBanner error={error} title="Cannot load recording" />}
      {loading && !r && <Loading />}
      {r && (
        <>
          {r.error_message && <ErrorBanner error={r.error_message} title="Processing failed" />}
          <div className="split">
            <Panel title="Preview">
              <img className="thumb-lg" src={r.thumbnail_url} alt={`Thumbnail of ${r.original_filename}`} />
            </Panel>
            <Panel title="Technical details">
              <dl className="kv">
                <dt>Duration</dt><dd>{formatDuration(r.duration_ms)}</dd>
                <dt>Resolution</dt><dd>{formatResolution(r.width, r.height)}</dd>
                <dt>Frame rate</dt><dd>{formatFps(r.fps, r.fps_rational, r.is_variable_frame_rate)}</dd>
                <dt>Video codec</dt><dd>{r.video_codec ?? "—"}</dd>
                <dt>Audio</dt><dd>{r.has_audio === false ? "No audio stream" : r.audio_codec ?? "—"}</dd>
                <dt>Container</dt><dd>{r.container_format ?? "—"}</dd>
                <dt>File size</dt><dd>{formatBytes(r.file_size_bytes)}</dd>
                <dt>Imported</dt><dd>{formatDate(r.imported_at)}</dd>
                <dt>Stored as</dt><dd className="mono">{r.stored_filename}</dd>
                <dt>SHA-256</dt><dd className="mono">{r.sha256}</dd>
              </dl>
            </Panel>
          </div>
          <Panel title="Processing" actions={<Link className="btn btn-ghost btn-sm" to={`/queue?recording=${r.id}`}>Queue view</Link>}>
            <JobStages jobs={r.jobs} />
          </Panel>
        </>
      )}
    </>
  );
}
