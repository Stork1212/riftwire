import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ActionableError } from "../components/ActionableError";
import { DecisionLine, METHODS, RightsBadge, ScopeSummary, UseChips } from "../components/rights";
import { EmptyState, ErrorBanner, Loading, PageHeader, Panel, StatusBadge } from "../components/ui";
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

// Methods whose footage arrives through Import (the only ones available in M2).
const RECORDABLE_METHODS = ["local_recording", "creator_file", "platform_export"];

/** Linked media whose acquisition method is not on record (data linked before migration
 * 0005 without a usable provenance event): analysis, editing and publishing stay refused
 * until the owner states, once, how the footage was obtained. */
function RecordMethod({ sourceId, recordingId, onDone }: {
  sourceId: string; recordingId: string; onDone: () => void;
}) {
  const [method, setMethod] = useState("");
  const [err, setErr] = useState<unknown>();
  const save = async () => {
    setErr(undefined);
    try {
      await rightsApi.attachRecording(sourceId, recordingId, method);
      onDone();
    } catch (x) {
      setErr(x);
    }
  };
  return (
    <div className="stack">
      <p className="hint">How this footage was obtained is not on record, so analysis, editing
        and publishing stay blocked. It can be recorded once.</p>
      <div className="toolbar">
        <select aria-label="How the footage was obtained" value={method}
          onChange={(e) => setMethod(e.target.value)}>
          <option value="">How was it obtained?…</option>
          {RECORDABLE_METHODS.map((m) => <option key={m} value={m}>{METHODS[m] ?? m}</option>)}
        </select>
        <button className="btn btn-sm" disabled={!method} onClick={save}>Record method</button>
      </div>
      {err !== undefined && <ActionableError error={err} title="Not recorded" />}
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
  attached_to_source: "linked to source", acquisition_method_recorded: "acquisition method recorded",
  evidence_added: "evidence added", updated: "updated",
  rights_refused: "refused (rights)",
};

function Timeline({ events }: { events: ProvenanceEvent[] }) {
  if (events.length === 0) return <EmptyState title="No history yet" />;
  return (
    <ol className="timeline">
      {events.map((e) => (
        <li key={e.id}>
          <span className="muted nowrap">{formatDate(e.at)}</span>
          <span><strong>{e.entity}</strong> {ACTION_TEXT[e.action] ?? e.action}
          {e.actor === "system" && <span className="muted"> (system)</span>}
          {typeof e.details.reason === "string" && <span className="muted"> — {e.details.reason}</span>}</span>
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
      <PageHeader eyebrow={<Link to="/sources" className="muted">← Sources</Link>}
        title={s.title_user || s.url_canonical || "Local source"} actions={<RightsBadge status={s.rights_status} />} />
      {err && <ErrorBanner error={err} />}
      <div className="grid-2">
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
            <dt>Declared footage method</dt><dd>{METHODS[s.acquisition_method] ?? s.acquisition_method}</dd>
            <dt>Status</dt><dd>{s.status}</dd>
          </dl>
        </Panel>
        <Panel title="Rights">
          <div className="authorized"><strong>Authorized now</strong> <UseChips uses={s.authorized_uses} /></div>
          <DecisionLine label="Obtain footage" d={s.acquire} />
          <DecisionLine label="Analyse" d={s.analyze} />
          <p className="hint">Editing and publishing are checked for each clip, platform and
            brand channel when those steps exist (M3 onwards).</p>
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
      <div className="grid-2">
        <Panel title="Brand channels">
          <Assignments key={s.brand_channel_ids.join()} src={s} brands={brands.data ?? []} onSaved={refresh} />
        </Panel>
        <Panel title={`Media (${s.recordings.length})`}>
          {s.recordings.length === 0 ? <EmptyState title="No footage linked yet">
            Import with this source selected, or attach an imported recording.</EmptyState> : (
            <ul className="plain">
              {s.recordings.map((r) => (
                <li key={r.id}>
                  <div><Link to={`/recordings/${r.id}`}>{r.original_filename}</Link>{" "}
                    <StatusBadge status={r.status} />{" "}
                    <span className="muted small">
                      {r.acquisition_method ? METHODS[r.acquisition_method] ?? r.acquisition_method
                        : "method not on record"}
                    </span></div>
                  {r.acquisition_method === null &&
                    <RecordMethod sourceId={s.id} recordingId={r.id} onDone={refresh} />}
                </li>
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
