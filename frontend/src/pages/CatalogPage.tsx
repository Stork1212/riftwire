import { useState } from "react";
import type { FormEvent } from "react";
import { PLATFORMS } from "../components/rights";
import { EmptyState, ErrorBanner, Loading, Panel } from "../components/ui";
import { rightsApi } from "../services/rightsApi";
import { usePolling } from "../services/usePolling";

const slugify = (s: string) => s.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 63);

function errText(x: unknown) {
  return x instanceof Error ? x.message : String(x);
}

function Games() {
  const games = usePolling(() => rightsApi.listGames(), 30_000);
  const [name, setName] = useState("");
  const [err, setErr] = useState<string>();
  const add = async (e: FormEvent) => {
    e.preventDefault();
    setErr(undefined);
    try {
      await rightsApi.createGame(slugify(name), name.trim());
      setName("");
      games.refresh();
    } catch (x) {
      setErr(errText(x));
    }
  };
  const toggle = async (id: string, active: boolean) => {
    try {
      await rightsApi.updateGame(id, { active });
      games.refresh();
    } catch (x) {
      setErr(errText(x));
    }
  };
  return (
    <Panel title="Games">
      <form className="toolbar" onSubmit={add} aria-label="New game">
        <input type="text" aria-label="Game name" placeholder="e.g. GTA VI" value={name}
          onChange={(e) => setName(e.target.value)} required />
        <button className="btn btn-primary" type="submit" disabled={!slugify(name)}>Add game</button>
      </form>
      {err && <ErrorBanner error={err} />}
      {games.error && <ErrorBanner error={games.error} title="Could not load games" />}
      {games.loading && <Loading />}
      {games.data && games.data.length === 0 && <EmptyState title="No games yet" />}
      {games.data && games.data.length > 0 && (
        <ul className="plain">
          {games.data.map((g) => (
            <li key={g.id}>{g.name} <span className="muted">({g.slug})</span>{" "}
              <button className="btn btn-sm btn-ghost" onClick={() => toggle(g.id, !g.active)}>
                {g.active ? "Deactivate" : "Activate"}</button>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function BrandChannels() {
  const list = usePolling(() => rightsApi.listBrandChannels(), 30_000);
  const [name, setName] = useState("");
  const [platform, setPlatform] = useState("youtube_shorts");
  const [handle, setHandle] = useState("");
  const [monetized, setMonetized] = useState(false);
  const [err, setErr] = useState<string>();
  const add = async (e: FormEvent) => {
    e.preventDefault();
    setErr(undefined);
    try {
      await rightsApi.createBrandChannel({ slug: slugify(`${name}-${platform}`), name: name.trim(), platform,
        handle: handle || undefined, monetized });
      setName("");
      setHandle("");
      setMonetized(false);
      list.refresh();
    } catch (x) {
      setErr(errText(x));
    }
  };
  const update = async (id: string, patch: { monetized?: boolean; active?: boolean }) => {
    try {
      await rightsApi.updateBrandChannel(id, patch);
      list.refresh();
    } catch (x) {
      setErr(errText(x));
    }
  };
  return (
    <Panel title="Brand channels (where RIFTWIRE publishes)">
      <form className="toolbar" onSubmit={add} aria-label="New brand channel">
        <input type="text" aria-label="Brand name" placeholder="e.g. RumbleFrame" value={name}
          onChange={(e) => setName(e.target.value)} required />
        <select aria-label="Platform" value={platform} onChange={(e) => setPlatform(e.target.value)}>
          {Object.entries(PLATFORMS).map(([p, l]) => <option key={p} value={p}>{l}</option>)}
        </select>
        <input type="text" aria-label="Handle" placeholder="@handle (optional)" value={handle}
          onChange={(e) => setHandle(e.target.value)} />
        <label className="check"><input type="checkbox" checked={monetized}
          onChange={() => setMonetized(!monetized)} /> Monetized</label>
        <button className="btn btn-primary" type="submit" disabled={!slugify(name)}>Add channel</button>
      </form>
      <p className="muted small">Monetized channels can only use footage whose licence allows monetization.</p>
      {err && <ErrorBanner error={err} />}
      {list.error && <ErrorBanner error={list.error} title="Could not load brand channels" />}
      {list.loading && <Loading />}
      {list.data && list.data.length === 0 && <EmptyState title="No brand channels yet" />}
      {list.data && list.data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Name</th><th>Platform</th><th>Handle</th><th>Monetized</th><th>Active</th></tr></thead>
            <tbody>
              {list.data.map((b) => (
                <tr key={b.id}>
                  <td>{b.name}</td>
                  <td>{PLATFORMS[b.platform] ?? b.platform}</td>
                  <td>{b.handle ?? "—"}</td>
                  <td><input type="checkbox" aria-label={`${b.name} monetized`} checked={b.monetized}
                    onChange={() => update(b.id, { monetized: !b.monetized })} /></td>
                  <td><input type="checkbox" aria-label={`${b.name} active`} checked={b.active}
                    onChange={() => update(b.id, { active: !b.active })} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

export function CatalogPage() {
  return (
    <>
      <div className="page-head"><h1>Games &amp; channels</h1></div>
      <div className="grid">
        <Games />
        <BrandChannels />
      </div>
    </>
  );
}
