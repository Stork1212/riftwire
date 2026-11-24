import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { RightsBadge } from "../components/rights";
import { EmptyState, ErrorBanner, Loading, Pagination, Panel } from "../components/ui";
import { ApiError } from "../services/api";
import { formatDate } from "../services/format";
import { rightsApi } from "../services/rightsApi";
import { usePolling } from "../services/usePolling";
import type { CreatorChannel, Resolved } from "../types/rights";

function PasteUrl({ onCreated }: { onCreated: () => void }) {
  const nav = useNavigate();
  const [url, setUrl] = useState("");
  const [res, setRes] = useState<Resolved>();
  const [err, setErr] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [channelId, setChannelId] = useState("");
  const [gameId, setGameId] = useState("");
  const games = usePolling(() => rightsApi.listGames(), 60_000);
  const creators = usePolling(() => rightsApi.listCreators(), 60_000);

  const platformChannels: (CreatorChannel & { creator: string })[] = (creators.data?.items ?? [])
    .flatMap((c) => c.channels.map((ch) => ({ ...ch, creator: c.display_name })))
    .filter((ch) => ch.platform === res?.platform);

  const resolve = async (e: FormEvent) => {
    e.preventDefault();
    setErr(undefined);
    setRes(undefined);
    setBusy(true);
    try {
      const r = await rightsApi.resolve(url);
      setRes(r);
      setChannelId(r.match.creator_channel_id ?? "");
    } catch (x) {
      setErr(x instanceof Error ? x.message : String(x));
    } finally {
      setBusy(false);
    }
  };

  const create = async () => {
    setErr(undefined);
    setBusy(true);
    try {
      const s = await rightsApi.createSource({ url, creator_channel_id: channelId || undefined,
        game_id: gameId || undefined });
      onCreated();
      nav(`/sources/${s.id}`);
    } catch (x) {
      if (x instanceof ApiError && x.status === 409 && typeof x.details.source_id === "string") {
        nav(`/sources/${x.details.source_id}`);
        return;
      }
      setErr(x instanceof Error ? x.message : String(x));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel title="Add a video">
      <form className="toolbar" onSubmit={resolve} aria-label="Paste a video link">
        <input type="url" className="grow" aria-label="Video link" required value={url}
          placeholder="Paste an authorized YouTube or Twitch video link"
          onChange={(e) => { setUrl(e.target.value); setRes(undefined); }} />
        <button className="btn btn-primary" type="submit" disabled={busy || !url}>Check link</button>
      </form>
      <p className="muted small">Links are checked offline. Nothing is downloaded: footage arrives
        through a permitted method (your import, the creator's file, your platform download).</p>
      {err && <ErrorBanner error={err} title="Not accepted" />}
      {res && (
        <div className="resolve" data-testid="resolve-result">
          <dl className="kv">
            <dt>Video</dt><dd><a href={res.canonical_url} target="_blank" rel="noreferrer noopener">{res.canonical_url}</a></dd>
            <dt>Creator</dt>
            <dd>{res.match.status === "matched" && res.creator_name
              ? <>{res.creator_name} <span className="muted">(matched automatically)</span></>
              : <span className="muted">{res.match.reason}</span>}</dd>
            <dt>Rights</dt>
            <dd>{res.existing_source ? <RightsBadge status={res.existing_source.rights_status} />
              : res.covered ? <RightsBadge status="ok" /> : res.match.status === "matched"
                ? <><RightsBadge status="none" /> <Link to="/creators">Add a licence</Link></>
                : <span className="muted">choose the creator channel</span>}</dd>
          </dl>
          {res.existing_source ? (
            <Link className="btn" to={`/sources/${res.existing_source.id}`}>Already a source: open it</Link>
          ) : (
            <div className="toolbar">
              {res.match.status !== "matched" && (
                <select aria-label="Creator channel" value={channelId} onChange={(e) => setChannelId(e.target.value)}>
                  <option value="">Creator channel…</option>
                  {platformChannels.map((ch) => (
                    <option key={ch.id} value={ch.id}>{ch.creator} — {ch.handle ?? ch.platform_channel_id}</option>
                  ))}
                </select>
              )}
              <select aria-label="Game" value={gameId} onChange={(e) => setGameId(e.target.value)}>
                <option value="">Game (optional)</option>
                {(games.data ?? []).filter((g) => g.active).map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
              </select>
              <button className="btn btn-primary" onClick={create} disabled={busy || !channelId}>Create source</button>
            </div>
          )}
        </div>
      )}
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
      <PasteUrl onCreated={list.refresh} />
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
          <EmptyState title="No sources yet">Paste a video link above, or import a file with a
            creator channel selected.</EmptyState>
        )}
        {list.data && list.data.items.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Video</th><th>Creator</th><th>Game</th><th>Rights</th><th>Media</th><th>Added</th></tr></thead>
              <tbody>
                {list.data.items.map((s) => (
                  <tr key={s.id}>
                    <td><Link className="row-link" to={`/sources/${s.id}`}>
                      {s.title_user || s.url_canonical || "Local source"}</Link>
                      <div className="muted small">{s.kind}{s.status !== "active" && ` · ${s.status}`}</div></td>
                    <td>{s.creator_name ?? "—"}</td>
                    <td>{s.game_name ?? "—"}</td>
                    <td><RightsBadge status={s.rights_status} /></td>
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
