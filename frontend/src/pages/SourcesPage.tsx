import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { LinkResolver } from "../components/LinkResolver";
import { RightsBadge, UseChips } from "../components/rights";
import { EmptyState, ErrorBanner, Loading, Pagination, Panel } from "../components/ui";
import { formatDate } from "../services/format";
import { rightsApi } from "../services/rightsApi";
import { usePolling } from "../services/usePolling";

function AddVideo({ onCreated }: { onCreated: () => void }) {
  const nav = useNavigate();
  return (
    <Panel title="Add a video">
      <LinkResolver onSource={(id, created) => { if (created) onCreated(); nav(`/sources/${id}`); }} />
    </Panel>
  );
}

const LIMIT = 50;

export function SourcesPage() {
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);
  const list = usePolling(() => rightsApi.listSources({ status, limit: LIMIT, offset }), 10_000, [status, offset]);

  return (
    <>
      <div className="page-head"><h1>Sources</h1></div>
      <AddVideo onCreated={list.refresh} />
      <Panel title="All sources" actions={
        <select aria-label="Status filter" value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }}>
          <option value="">Active and blocked</option>
          <option value="active">Active</option>
          <option value="blocked">Blocked</option>
          <option value="archived">Archived</option>
        </select>}>
        {list.error && <ErrorBanner error={list.error} title="Could not load sources" />}
        {list.loading && <Loading />}
        {list.data && list.data.items.length === 0 && (
          <EmptyState title="No sources yet">
            <ol className="steps">
              <li>Add your games and output channels in <Link to="/catalog">Games &amp; Channels</Link>.</li>
              <li>Add the creator and attest their licence in <Link to="/creators">Creators &amp; Rights</Link>.</li>
              <li>Paste one of their video links above, or <Link to="/import">import a file</Link> with a rights link.</li>
            </ol>
          </EmptyState>
        )}
        {list.data && list.data.items.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Video</th><th>Creator</th><th>Game</th><th>Licence</th><th>Authorized</th><th>Media</th><th>Added</th></tr></thead>
              <tbody>
                {list.data.items.map((s) => (
                  <tr key={s.id}>
                    <td><Link className="row-link" to={`/sources/${s.id}`}>
                      {s.title_user || s.url_canonical || "Local source"}</Link>
                      <div className="muted small">{s.kind}{s.status !== "active" && ` · ${s.status}`}</div></td>
                    <td>{s.creator_name ?? "—"}</td>
                    <td>{s.game_name ?? "—"}</td>
                    <td><RightsBadge status={s.rights_status} /></td>
                    <td><UseChips uses={s.authorized_uses} /></td>
                    <td className="num">{s.recording_count}</td>
                    <td>{formatDate(s.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <Pagination total={list.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
          </div>
        )}
      </Panel>
    </>
  );
}
