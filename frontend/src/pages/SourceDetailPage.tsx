import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { DecisionLine, METHODS, RightsBadge, ScopeSummary } from "../components/rights";
import { EmptyState, ErrorBanner, Loading, Panel, StatusBadge } from "../components/ui";
import { api } from "../services/api";
import { formatDate } from "../services/format";
import { rightsApi } from "../services/rightsApi";
import { usePolling } from "../services/usePolling";
import type { BrandChannel, ProvenanceEvent, SourceDetail } from "../types/rights";

function Assignments({ src, brands, onSaved }: { src: SourceDetail; brands: BrandChannel[]; onSaved: () => void }) {
  const [sel, setSel] = useState<string[]>(src.brand_channel_ids);
  const [err, setErr] = useState<string>();
  const dirty = sel.slice().sort().join() !== src.brand_channel_ids.slice().sort().join();
  const save = async () => {
    setErr(undefined);
    try {
      await rightsApi.setAssignments(src.id, sel);
      onSaved();
    } catch (x) {
      setErr(x instanceof Error ? x.message : String(x));
    }
  };
  if (brands.length === 0) return <EmptyState title="No brand channels"><Link to="/catalog">Add one</Link> first.</EmptyState>;
  return (
    <div>
      {brands.map((b) => (
        <label key={b.id} className="check">
          <input type="checkbox" checked={sel.includes(b.id)}
            onChange={() => setSel(sel.includes(b.id) ? sel.filter((x) => x !== b.id) : [...sel, b.id])} />
          {b.name} <span className="muted">({b.platform}{b.monetized ? ", monetized" : ""})</span>
        </label>
      ))}
      {err && <ErrorBanner error={err} />}
      <button className="btn btn-sm" disabled={!dirty} onClick={save}>Save assignments</button>
    </div>
  );
}

function AttachRecording({ sourceId, onDone }: { sourceId: string; onDone: () => void }) {
  const recs = usePolling(() => api.listRecordings({ limit: 200 }), 30_000);
  const [rid, setRid] = useState("");
  const [err, setErr] = useState<string>();
  const unlinked = (recs.data?.items ?? []).filter((r) => !r.source_id && r.status !== "FAILED");
  const attach = async () => {
    setErr(undefined);
    try {
      await rightsApi.attachRecording(sourceId, rid);
      setRid("");
      onDone();
    } catch (x) {
      setErr(x instanceof Error ? x.message : String(x));
    }
  };
  return (
    <div className="toolbar">
      <select aria-label="Recording to attach" value={rid} onChange={(e) => setRid(e.target.value)}>
        <option value="">Attach an imported recording…</option>
        {unlinked.map((r) => <option key={r.id} value={r.id}>{r.original_filename}</option>)}
      </select>
      <button className="btn btn-sm" disabled={!rid} onClick={attach}>Attach</button>
      {err && <ErrorBanner error={err} title="Not attached" />}
    </div>
  );
}

const ACTION_TEXT: Record<string, string> = {
  created: "created", attested: "attested", revoked: "revoked", superseded: "superseded",
  channel_added: "channel added", assignments_set: "brand channels set", media_attached: "media attached",
  attached_to_source: "linked to source", evidence_added: "evidence added", updated: "updated",
  rights_refused: "refused (rights)",
};

function Timeline({ events }: { events: ProvenanceEvent[] }) {
  if (events.length === 0) return <EmptyState title="No history yet" />;
  return (
    <ol className="timeline">
      {events.map((e) => (
        <li key={e.id}>
          <span className="muted">{formatDate(e.at)}</span>{" "}
          <strong>{e.entity}</strong> {ACTION_TEXT[e.action] ?? e.action}
          {e.actor === "system" && <span className="muted"> (system)</span>}
          {typeof e.details.reason === "string" && <span className="muted"> — {e.details.reason}</span>}
        </li>
      ))}
    </ol>
  );
}

export function SourceDetailPage() {
  const { id = "" } = useParams();
  const src = usePolling(() => rightsApi.getSource(id), 10_000, [id]);
  const brands = usePolling(() => rightsApi.listBrandChannels(), 60_000);
  const prov = usePolling(() => rightsApi.provenance(id), 10_000, [id]);
  const [err, setErr] = useState<string>();

  const setStatus = async (status: string) => {
    setErr(undefined);
    try {
      await rightsApi.updateSource(id, { status });
      src.refresh();
      prov.refresh();
    } catch (x) {
      setErr(x instanceof Error ? x.message : String(x));
    }
  };

  if (src.error) return <ErrorBanner error={src.error} title="Could not load the source" />;
  if (!src.data) return <Loading />;
  const s = src.data;
  const refresh = () => { src.refresh(); prov.refresh(); };

  return (
    <>
      <div className="page-head">
        <h1>{s.title_user || s.url_canonical || "Local source"}</h1>
        <RightsBadge status={s.rights_status} />
      </div>
      {err && <ErrorBanner error={err} />}
      <div className="grid">
        <Panel title="Source" actions={
          <div className="toolbar">
            {s.status === "active"
              ? <button className="btn btn-sm btn-ghost" onClick={() => setStatus("blocked")}>Block</button>
              : <button className="btn btn-sm" onClick={() => setStatus("active")}>Reactivate</button>}
            {s.status !== "archived" && <button className="btn btn-sm btn-ghost" onClick={() => setStatus("archived")}>Archive</button>}
          </div>}>
          <dl className="kv">
            <dt>Link</dt><dd>{s.url_canonical
              ? <a href={s.url_canonical} target="_blank" rel="noreferrer noopener">{s.url_canonical}</a> : "local files"}</dd>
            <dt>Creator</dt><dd>{s.creator_name ?? "—"} <span className="muted">{s.channel_label}</span></dd>
            <dt>Game</dt><dd>{s.game_name ?? "—"}</dd>
            <dt>Footage via</dt><dd>{METHODS[s.acquisition_method] ?? s.acquisition_method}</dd>
            <dt>Status</dt><dd>{s.status}</dd>
          </dl>
        </Panel>
        <Panel title="Rights">
          <DecisionLine label="Obtain footage" d={s.acquire} />
          <DecisionLine label="Analyse" d={s.analyze} />
          {s.records.length === 0 && <EmptyState title="No licence covers this source">
            Add one on the <Link to="/creators">Creators</Link> page.</EmptyState>}
          {s.records.map((r) => (
            <div key={r.id} className="record">
              <div><strong>v{r.version}</strong> · {r.basis} · covers {r.coverage}{" "}
                <RightsBadge status={r.state} /></div>
              <ScopeSummary scope={r.scope} brands={brands.data ?? []} />
            </div>
          ))}
        </Panel>
      </div>
      <div className="grid">
        <Panel title="Brand channels">
          <Assignments key={s.brand_channel_ids.join()} src={s} brands={brands.data ?? []} onSaved={refresh} />
        </Panel>
        <Panel title={`Media (${s.recordings.length})`}>
          {s.recordings.length === 0 ? <EmptyState title="No footage linked yet">
            Import with this source selected, or attach an imported recording.</EmptyState> : (
            <ul className="plain">
              {s.recordings.map((r) => (
                <li key={r.id}><Link to={`/recordings/${r.id}`}>{r.original_filename}</Link>{" "}
                  <StatusBadge status={r.status} /></li>
              ))}
            </ul>
          )}
          <AttachRecording sourceId={s.id} onDone={refresh} />
        </Panel>
      </div>
      <Panel title="History (provenance)">
        {prov.error && <ErrorBanner error={prov.error} />}
        {prov.data ? <Timeline events={prov.data} /> : <Loading />}
      </Panel>
    </>
  );
}
