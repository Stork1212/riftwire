import { useRef, useState } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "../services/api";
import { rightsApi } from "../services/rightsApi";
import { usePolling } from "../services/usePolling";
import type { CreatorChannel, Resolved } from "../types/rights";
import { RightsBadge } from "./rights";
import { SourceAcquire } from "./SourceAcquire";
import { ActionableError } from "./ActionableError";

function VideoPreview({ video }: { video: Resolved }) {
  const [loaded, setLoaded] = useState(false);
  const platform = video.platform === "youtube" ? "YouTube" : "Twitch";
  const params = new URLSearchParams();
  let player: string;
  if (video.platform === "youtube") {
    player = `https://www.youtube.com/embed/${encodeURIComponent(video.video_id)}`;
    const start = Math.floor((video.start_ms ?? 0) / 1000);
    if (start > 0) params.set("start", String(start));
  } else {
    params.set("parent", window.location.hostname);
    params.set("autoplay", "false");
    if (video.media === "clip") {
      player = "https://clips.twitch.tv/embed";
      params.set("clip", video.video_id);
    } else {
      player = "https://player.twitch.tv/";
      params.set("video", `v${video.video_id}`);
      const start = Math.floor((video.start_ms ?? 0) / 1000);
      if (start > 0) params.set("time", `${start}s`);
    }
  }
  const query = params.toString();

  return (
    <div className="video-preview">
      {loaded && <iframe title={`${platform} video preview`}
        src={query ? `${player}?${query}` : player}
        referrerPolicy="strict-origin-when-cross-origin" allow="encrypted-media; fullscreen; picture-in-picture"
        allowFullScreen />}
      <div className="toolbar">
        {!loaded && <button type="button" className="btn btn-primary" onClick={() => setLoaded(true)}>
          Load {platform} preview
        </button>}
        <a href={video.canonical_url} target="_blank" rel="noopener noreferrer">
          Open on {platform}
        </a>
      </div>
    </div>
  );
}

/** Paste a link → offline check → existing source / automatic creator match / inline
 *  creator setup → create the source. Only the backend validates links (one rule set).
 *  Responses for an older link are ignored, so a slow reply can never overwrite a newer one. */
export function LinkResolver({ onSource, submitLabel = "Create source" }: {
  onSource: (sourceId: string, created: boolean) => void;
  submitLabel?: string;
}) {
  const [url, setUrl] = useState("");
  const [res, setRes] = useState<Resolved>();
  const [err, setErr] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const [channelId, setChannelId] = useState("");
  const [gameId, setGameId] = useState("");
  const [setupOpen, setSetupOpen] = useState(false);
  const generation = useRef(0);
  const games = usePolling(() => rightsApi.listGames(), 60_000);
  const creators = usePolling(() => rightsApi.listCreators(), 60_000);

  const platformChannels: (CreatorChannel & { creator: string })[] = (creators.data?.items ?? [])
    .flatMap((c) => c.channels.map((ch) => ({ ...ch, creator: c.display_name })))
    .filter((ch) => ch.platform === res?.platform);

  const resolveNow = async (link: string) => {
    const gen = ++generation.current;
    setErr(undefined);
    setRes(undefined);
    setBusy(true);
    try {
      const r = await rightsApi.resolve(link);
      if (gen !== generation.current) return;          // a newer link was entered meanwhile
      setRes(r);
      setChannelId(r.match.creator_channel_id ?? "");
      setSetupOpen(false);
    } catch (x) {
      if (gen === generation.current) setErr(x);
    } finally {
      if (gen === generation.current) setBusy(false);
    }
  };

  const resolve = (e: FormEvent) => {
    e.preventDefault();
    if (url.trim()) void resolveNow(url.trim());
  };

  const create = async () => {
    setErr(undefined);
    setBusy(true);
    try {
      const s = await rightsApi.createSource({ url: url.trim(), creator_channel_id: channelId || undefined,
        game_id: gameId || undefined });
      onSource(s.id, true);
    } catch (x) {
      if (x instanceof ApiError && x.status === 409 && typeof x.details.source_id === "string") {
        onSource(x.details.source_id, false);
        return;
      }
      setErr(x);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <form className="toolbar" onSubmit={resolve} aria-label="Paste a video link">
        <input type="text" inputMode="url" autoComplete="off" spellCheck={false} className="grow"
          aria-label="Video link" value={url} placeholder="Paste or drop a YouTube or Twitch link"
          onPaste={(event) => {
            const link = event.clipboardData.getData("text/plain").trim();
            if (!link) return;
            event.preventDefault();
            setUrl(link);
            void resolveNow(link);
          }}
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => {
            event.preventDefault();
            const link = event.dataTransfer.getData("text/uri-list")
              || event.dataTransfer.getData("text/plain");
            const first = link.split(/\r?\n/).map((line) => line.trim())
              .find((line) => line && !line.startsWith("#"));
            if (!first) return;
            setUrl(first);
            void resolveNow(first);
          }}
          onChange={(e) => {
            generation.current += 1;   // editing invalidates any reply still in flight
            setUrl(e.target.value); setRes(undefined); setErr(undefined); setBusy(false);
          }} />
        <button className="btn btn-primary" type="submit" disabled={busy || !url.trim()}>Check link</button>
      </form>
      <p className="hint">Links are checked offline. Loading a preview opens the platform's player.</p>
      {err !== undefined && <ActionableError error={err} title="Link not accepted" />}
      {res && (
        <div className="resolve" data-testid="resolve-result">
          <VideoPreview video={res} />
          <SourceAcquire sourceId={res.existing_source?.id} />
          <dl className="kv">
            <dt>Creator</dt>
            <dd>{res.match.status === "matched" && res.creator_name
              ? <>{res.creator_name} <span className="muted">(matched automatically)</span></>
              : <span className="muted">{res.match.reason}</span>}</dd>
            <dt>Licence</dt>
            <dd>{res.existing_source ? <RightsBadge status={res.existing_source.rights_status} />
              : res.covered ? <RightsBadge status="active" />
                : res.match.status === "matched"
                  ? <><RightsBadge status="none" /> <Link to="/creators">Add a licence for {res.creator_name}</Link></>
                  : <span className="muted">choose or set up the creator below</span>}</dd>
          </dl>
          {res.existing_source ? (
            <button className="btn" onClick={() => onSource(res.existing_source!.id, false)}>
              Already a source: use it</button>
          ) : (
            <>
              <div className="toolbar">
                {res.match.status !== "matched" && (
                  <select aria-label="Creator channel" value={channelId} onChange={(e) => setChannelId(e.target.value)}>
                    <option value="">{platformChannels.length ? "Creator channel…" : "No matching creator channel yet"}</option>
                    {platformChannels.map((ch) => (
                      <option key={ch.id} value={ch.id}>{ch.creator} — {ch.handle ?? ch.platform_channel_id}</option>
                    ))}
                  </select>
                )}
                {res.match.status !== "matched" && !setupOpen && (
                  <button type="button" className="btn btn-ghost" onClick={() => setSetupOpen(true)}>Set up a new creator</button>
                )}
                <select aria-label="Game" value={gameId} onChange={(e) => setGameId(e.target.value)}>
                  <option value="">{(games.data ?? []).length ? "Game (optional)" : "Game (none configured)"}</option>
                  {(games.data ?? []).filter((g) => g.active).map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
                </select>
                <button className="btn btn-primary" onClick={create} disabled={busy || !channelId}>{submitLabel}</button>
              </div>
              {setupOpen && (
                <CreatorSetup platform={res.platform} suggestedHandle={res.channel_login ?? ""}
                  onCancel={() => setSetupOpen(false)}
                  onDone={(chId) => { creators.refresh(); void resolveNow(url.trim()).then(() => setChannelId(chId)); }} />
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

/** Inline: a new creator plus their channel on the link's platform. Rights are still
 *  attested separately (Creators & Rights); nothing here grants permission. */
function CreatorSetup({ platform, suggestedHandle, onDone, onCancel }: {
  platform: string; suggestedHandle: string; onDone: (channelId: string) => void; onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [rel, setRel] = useState("partner");
  const [handle, setHandle] = useState(suggestedHandle);
  const [err, setErr] = useState<unknown>();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setErr(undefined);
    try {
      const c = await rightsApi.createCreator({ display_name: name.trim(), relationship: rel });
      const h = handle.trim();
      const ch = await rightsApi.addChannel(c.id, platform === "youtube" && h.startsWith("UC")
        ? { platform, platform_channel_id: h } : { platform, handle: h });
      onDone(ch.id);
    } catch (x) {
      setErr(x);
    }
  };
  return (
    <form className="form" onSubmit={submit} aria-label="Set up a new creator">
      <div className="form-row">
        <label>Creator name<input type="text" value={name} onChange={(e) => setName(e.target.value)} required /></label>
        <label>Relationship
          <select value={rel} onChange={(e) => setRel(e.target.value)}>
            <option value="self">Me (own footage)</option>
            <option value="partner">Partner creator</option>
            <option value="licensor">Licensor</option>
            <option value="unaffiliated">No relationship yet (no permission on record)</option>
          </select>
        </label>
        <label>{platform === "youtube" ? "YouTube @handle or UC… channel id" : "Twitch login"}
          <input type="text" value={handle} onChange={(e) => setHandle(e.target.value)} required />
        </label>
      </div>
      <p className="hint">This only records who the creator is. Their footage stays unusable
        until you attest a licence on Creators &amp; Rights.</p>
      {err !== undefined && <ActionableError error={err} />}
      <div className="toolbar">
        <button className="btn btn-primary" type="submit" disabled={!name.trim() || !handle.trim()}>Add creator</button>
        <button className="btn btn-ghost" type="button" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}
