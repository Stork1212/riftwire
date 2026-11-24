import { useState } from "react";
import type { FormEvent } from "react";
import { basisLabel, LicenceForm, RightsBadge, ScopeSummary } from "../components/rights";
import type { LicenceFormValue } from "../components/rights";
import { EmptyState, ErrorBanner, Loading, PageHeader, Panel } from "../components/ui";
import { formatBytes, formatDate } from "../services/format";
import { rightsApi } from "../services/rightsApi";
import { usePolling } from "../services/usePolling";
import type { BrandChannel, Creator, RightsRecord } from "../types/rights";

function errText(x: unknown) {
  return x instanceof Error ? x.message : String(x);
}

function NewCreator({ onCreated }: { onCreated: (c: Creator) => void }) {
  const [name, setName] = useState("");
  const [rel, setRel] = useState("partner");
  const [err, setErr] = useState<string>();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setErr(undefined);
    try {
      const c = await rightsApi.createCreator({ display_name: name, relationship: rel });
      setName("");
      onCreated(c);
    } catch (x) {
      setErr(errText(x));
    }
  };
  return (
    <form className="toolbar" onSubmit={submit} aria-label="New creator">
      <input type="text" aria-label="Creator name" placeholder="Creator name" value={name}
        onChange={(e) => setName(e.target.value)} required />
      <select aria-label="Relationship" value={rel} onChange={(e) => setRel(e.target.value)}>
        <option value="self">Me (own footage)</option>
        <option value="partner">Partner creator</option>
        <option value="licensor">Licensor</option>
            <option value="unaffiliated">No relationship (public footage only)</option>
      </select>
      <button className="btn btn-primary" type="submit" disabled={!name.trim()}>Add creator</button>
      {err && <ErrorBanner error={err} />}
    </form>
  );
}

function AddChannel({ creator, onAdded }: { creator: Creator; onAdded: () => void }) {
  const [platform, setPlatform] = useState("twitch");
  const [handle, setHandle] = useState("");
  const [err, setErr] = useState<string>();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setErr(undefined);
    try {
      await rightsApi.addChannel(creator.id, platform === "local" ? { platform }
        : platform === "youtube" && handle.startsWith("UC") ? { platform, platform_channel_id: handle }
          : { platform, handle });
      setHandle("");
      onAdded();
    } catch (x) {
      setErr(errText(x));
    }
  };
  return (
    <form className="toolbar" onSubmit={submit} aria-label="Add channel">
      <select aria-label="Channel platform" value={platform} onChange={(e) => setPlatform(e.target.value)}>
        <option value="twitch">Twitch</option>
        <option value="youtube">YouTube</option>
        <option value="local">Local files</option>
      </select>
      {platform !== "local" && (
        <input type="text" aria-label="Channel handle" value={handle} onChange={(e) => setHandle(e.target.value)}
          placeholder={platform === "youtube" ? "@handle or UC… channel id" : "login"} required />
      )}
      <button className="btn btn-sm" type="submit">Add channel</button>
      {err && <ErrorBanner error={err} />}
    </form>
  );
}

function Evidence({ record, onAdded }: { record: RightsRecord; onAdded: () => void }) {
  const [kind, setKind] = useState("document");
  const [ref, setRef] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [err, setErr] = useState<string>();
  const add = async (e: FormEvent) => {
    e.preventDefault();
    setErr(undefined);
    try {
      if (file) await rightsApi.addEvidenceFile(record.id, kind, file, ref || undefined);
      else await rightsApi.addEvidenceNote(record.id, kind, ref);
      setRef("");
      setFile(null);
      onAdded();
    } catch (x) {
      setErr(errText(x));
    }
  };
  return (
    <div className="evidence">
      {record.evidence.length > 0 && (
        <ul className="plain">
          {record.evidence.map((ev) => (
            <li key={ev.id}>{ev.kind}: {ev.reference ?? ev.original_filename}
              {ev.file_url && <> · <a href={ev.file_url}>{ev.original_filename}</a> ({formatBytes(ev.size_bytes)})</>}
            </li>
          ))}
        </ul>
      )}
      <form className="toolbar" onSubmit={add} aria-label="Add evidence">
        <select aria-label="Evidence kind" value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="document">Document</option>
          <option value="email">Email</option>
          <option value="platform_permission">Written platform permission</option>
          <option value="note">Note</option>
        </select>
        <input type="text" aria-label="Evidence reference" placeholder="Reference or note" value={ref}
          onChange={(e) => setRef(e.target.value)} />
        <input type="file" aria-label="Evidence file" accept=".pdf,.png,.jpg,.jpeg,.webp,.txt,.md,.eml"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        <button className="btn btn-sm" type="submit" disabled={!file && !ref.trim()}>Add evidence</button>
      </form>
      <p className="hint">Supporting documentation only (max 25 MB per file). It never activates rights by itself.</p>
      {err && <ErrorBanner error={err} />}
    </div>
  );
}

function RecordCard({ r, brands, creator, onChanged }: { r: RightsRecord; brands: BrandChannel[];
  creator: Creator; onChanged: () => void }) {
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const current = r.state === "ok" || r.state === "not_yet_valid" || r.state === "expired";
  const revoke = async () => {
    const reason = window.prompt("Why are these rights revoked? (recorded permanently)");
    if (!reason) return;
    try {
      await rightsApi.revokeRights(r.id, reason);
      onChanged();
    } catch (x) {
      setErr(errText(x));
    }
  };
  const supersede = async (v: LicenceFormValue) => {
    setBusy(true);
    setErr(undefined);
    try {
      await rightsApi.supersedeRights(r.id, { scope: v.scope, attestation_text: v.attestation_text,
        valid_until: v.valid_until });
      setEditing(false);
      onChanged();
    } catch (x) {
      setErr(errText(x));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="record" data-testid={`record-${r.id}`}>
      <div className="record-head">
        <strong>{r.basis === "public_web" ? "Record" : "Licence"} v{r.version}</strong> · {basisLabel(r)} · covers {r.coverage === "creator" ? "every video" : r.coverage === "sources" && r.basis === "public_web" ? "one saved link" : r.coverage}
        <RightsBadge status={r.state} />
        {current && r.revoked_at === null && r.superseded_by_id === null && (
          <span className="toolbar">
            {r.basis !== "public_web" &&
              <button className="btn btn-sm btn-ghost" onClick={() => setEditing(!editing)}>Edit</button>}
            <button className="btn btn-sm btn-ghost" onClick={revoke}>Revoke</button>
          </span>
        )}
      </div>
      <ScopeSummary scope={r.scope} brands={brands} />
      <p className="hint">Attested {r.attested_at ? formatDate(r.attested_at) : "—"}
        {r.valid_until && <> · valid until {formatDate(r.valid_until)}</>}
        {r.revoked_at && <> · revoked {formatDate(r.revoked_at)}: {r.revoked_reason}</>}</p>
      {r.attestation_text && <blockquote>{r.attestation_text}</blockquote>}
      {err && <ErrorBanner error={err} />}
      {editing && <LicenceForm channels={creator.channels} brands={brands} initial={r} editing busy={busy}
        onSubmit={supersede} onCancel={() => setEditing(false)} />}
      <Evidence record={r} onAdded={onChanged} />
    </div>
  );
}

function CreatorPanel({ creator, brands, onChanged }: { creator: Creator; brands: BrandChannel[]; onChanged: () => void }) {
  const recs = usePolling(() => rightsApi.creatorRights(creator.id), 15_000, [creator.id]);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const refresh = () => { recs.refresh(); onChanged(); };
  const create = async (v: LicenceFormValue) => {
    setBusy(true);
    setErr(undefined);
    try {
      await rightsApi.createRights({ creator_id: creator.id, basis: creator.relationship === "self" ? "ownership"
        : creator.relationship === "unaffiliated" ? "creator_permission" : "licence",
        coverage: v.coverage, channel_ids: v.channel_ids, scope: v.scope, valid_until: v.valid_until,
        attestation_text: v.attestation_text });
      setAdding(false);
      refresh();
    } catch (x) {
      setErr(errText(x));
    } finally {
      setBusy(false);
    }
  };
  const archive = async () => {
    try {
      await rightsApi.updateCreator(creator.id, { archived: !creator.archived });
      onChanged();
    } catch (x) {
      setErr(errText(x));
    }
  };
  return (
    <Panel title={creator.display_name} actions={
      <button className="btn btn-sm btn-ghost" onClick={archive}>{creator.archived ? "Unarchive" : "Archive"}</button>}>
      {err && <ErrorBanner error={err} />}
      <div className="subsection">
      <h3>Channels</h3>
      {creator.channels.length === 0 ? <p className="muted">No channels yet.</p> : (
        <ul className="plain">
          {creator.channels.map((c) => <li key={c.id}>{c.platform}: {c.handle ?? c.platform_channel_id ?? "local files"}</li>)}
        </ul>
      )}
      <AddChannel creator={creator} onAdded={onChanged} />
      </div>
      <div className="subsection">
      <h3>Licences</h3>
      {recs.error && <ErrorBanner error={recs.error} />}
      {recs.loading && <Loading />}
      {recs.data && recs.data.length === 0 && !adding && (
        <EmptyState title="No licence yet">Nothing from this creator can be acquired, analysed,
          edited or published until you attest a licence.</EmptyState>
      )}
      {recs.data?.map((r) => <RecordCard key={r.id} r={r} brands={brands} creator={creator} onChanged={refresh} />)}
      {adding ? <LicenceForm channels={creator.channels} brands={brands} busy={busy} onSubmit={create}
        onCancel={() => setAdding(false)} />
        : !creator.archived && <button className="btn btn-primary" onClick={() => setAdding(true)}>New licence</button>}
      </div>
    </Panel>
  );
}

export function CreatorsPage() {
  const [archived, setArchived] = useState(false);
  const creators = usePolling(() => rightsApi.listCreators(archived), 15_000, [archived]);
  const brands = usePolling(() => rightsApi.listBrandChannels(), 60_000);
  const [selected, setSelected] = useState<string>();
  const items = creators.data?.items ?? [];
  const current = items.find((c) => c.id === selected) ?? items[0];

  return (
    <>
      <PageHeader title="Creators & rights"
        description="Who the footage belongs to, and the licences you have attested. Nothing is usable without one." />
      <Panel title="Creators" actions={
        <label className="check"><input type="checkbox" checked={archived} onChange={() => setArchived(!archived)} /> Show archived</label>}>
        <NewCreator onCreated={(c) => { creators.refresh(); setSelected(c.id); }} />
        {creators.error && <ErrorBanner error={creators.error} title="Could not load creators" />}
        {creators.loading && <Loading />}
        {creators.data && items.length === 0 && <EmptyState title="No creators yet">Add yourself (own
          footage) or a partner creator, then attest a licence.</EmptyState>}
        {items.length > 0 && (
          <div className="chips" role="tablist">
            {items.map((c) => (
              <button key={c.id} role="tab" aria-selected={c.id === current?.id}
                className={`chip${c.id === current?.id ? " active" : ""}`} onClick={() => setSelected(c.id)}>
                {c.display_name}{c.archived && " (archived)"} <span className="muted">· {c.current_rights} current</span>
              </button>
            ))}
          </div>
        )}
      </Panel>
      {current && <CreatorPanel key={current.id} creator={current} brands={brands.data ?? []} onChanged={creators.refresh} />}
    </>
  );
}
