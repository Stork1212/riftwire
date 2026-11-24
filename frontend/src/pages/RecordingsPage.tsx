import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { EmptyState, ErrorBanner, Loading, Pagination, Panel, StatusBadge } from "../components/ui";
import { api } from "../services/api";
import { formatBytes, formatDate, formatDuration, formatResolution } from "../services/format";
import { usePolling } from "../services/usePolling";

const LIMIT = 25;

export function RecordingsPage() {
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [offset, setOffset] = useState(0);

  useEffect(() => {
    const t = window.setTimeout(() => { setDebounced(query.trim()); setOffset(0); }, 300);
    return () => window.clearTimeout(t);
  }, [query]);

  const { data, error, loading } = usePolling(
    () => api.listRecordings({ limit: LIMIT, offset, q: debounced }), 4000, [offset, debounced]);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Recordings</h1>
          <p>{data ? `${data.total} recording${data.total === 1 ? "" : "s"}` : " "}</p>
        </div>
        <div className="toolbar">
          <label className="visually-hidden" htmlFor="rec-search">Search recordings</label>
          <input id="rec-search" type="search" placeholder="Search by filename…" value={query}
            onChange={(e) => setQuery(e.target.value)} />
          <Link to="/import" className="btn btn-primary">Import</Link>
        </div>
      </div>
      {error && <ErrorBanner error={error} title="Cannot load recordings" />}
      <Panel>
        {loading && !data && <Loading />}
        {data && data.items.length === 0 && (
          debounced
            ? <EmptyState title="No matches">No recording filename contains “{debounced}”.</EmptyState>
            : <EmptyState title="No recordings yet">
                Imported recordings appear here with their thumbnail and technical details.{" "}
                <Link to="/import">Import one →</Link>
              </EmptyState>
        )}
        {data && data.items.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th aria-label="Thumbnail" />
                  <th>Filename</th><th>Duration</th><th>Resolution</th><th>Size</th>
                  <th>Imported</th><th>Status</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((r) => (
                  <tr key={r.id}>
                    <td><img className="thumb" src={r.thumbnail_url} alt="" loading="lazy" /></td>
                    <td><Link className="row-link" to={`/recordings/${r.id}`}>{r.original_filename}</Link></td>
                    <td className="num">{formatDuration(r.duration_ms)}</td>
                    <td className="num">{formatResolution(r.width, r.height)}</td>
                    <td className="num">{formatBytes(r.file_size_bytes)}</td>
                    <td className="num">{formatDate(r.imported_at)}</td>
                    <td><StatusBadge status={r.status} /></td>
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
