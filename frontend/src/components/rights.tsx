import { useState } from "react";
import type { FormEvent } from "react";
import type { BrandChannel, CreatorChannel, Decision, RightsRecord, Scope, Use } from "../types/rights";

export const USES: Use[] = ["acquire", "analyze", "edit", "publish"];
export const METHODS: Record<string, string> = {
  local_recording: "Own recordings",
  creator_file: "Creator's original files",
  platform_export: "Owner's platform download (YouTube Studio / Twitch)",
  direct_link: "Creator-provided media link",
  platform_api: "Platform-authorized acquisition",
  watch_folder: "Watch folder",
  url_fetch: "URL fetching (needs written platform permission)",
};
export const PLATFORMS: Record<string, string> = {
  youtube_shorts: "YouTube Shorts",
  instagram_reels: "Instagram Reels",
  facebook_reels: "Facebook Reels",
  x: "X",
};

const RIGHTS_TONE: Record<string, string> = {
  ok: "ok", expires_soon: "warn", expired: "bad", revoked: "bad", superseded: "muted",
  not_attested: "warn", not_yet_valid: "warn", none: "muted", blocked: "bad",
};
const RIGHTS_LABEL: Record<string, string> = {
  ok: "Rights OK", expires_soon: "Expires soon", expired: "Expired", revoked: "Revoked",
  superseded: "Superseded", not_attested: "Not attested", not_yet_valid: "Not yet valid",
  none: "No rights", blocked: "Blocked",
};

export function RightsBadge({ status }: { status: string }) {
  const tone = RIGHTS_TONE[status] ?? "muted";
  return (
    <span className={`badge badge-${tone}`} data-rights={status}>
      <span className="badge-dot" aria-hidden="true" />
      {RIGHTS_LABEL[status] ?? status}
    </span>
  );
}

export function DecisionLine({ d, label }: { d: Decision; label: string }) {
  return (
    <div className={`decision ${d.allowed ? "decision-ok" : "decision-no"}`} data-reason={d.reason}>
      <strong>{label}:</strong> {d.allowed ? "allowed" : "refused"}
      {!d.allowed && <span className="muted"> — {d.message}</span>}
      {d.allowed && d.version !== null && <span className="muted"> (licence v{d.version})</span>}
    </div>
  );
}

export function ScopeSummary({ scope, brands }: { scope: Scope | null; brands: BrandChannel[] }) {
  if (!scope) return <span className="err-text">Scope not understood: this record authorizes nothing.</span>;
  const brandNames = scope.brand_channels === "*" ? "all brand channels"
    : scope.brand_channels.map((id) => brands.find((b) => b.id === id)?.name ?? id).join(", ") || "none";
  return (
    <ul className="scope">
      <li><strong>Uses:</strong> {scope.uses.join(", ")}</li>
      <li><strong>Methods:</strong> {scope.acquisition_methods.map((m) => METHODS[m] ?? m).join(", ") || "none"}</li>
      <li><strong>Platforms:</strong> {scope.publish_platforms.map((p) => PLATFORMS[p] ?? p).join(", ") || "none"}</li>
      <li><strong>Brand channels:</strong> {brandNames}</li>
      <li><strong>Monetization:</strong> {scope.monetization ? "allowed" : "not allowed"}</li>
      {scope.max_clip_seconds !== null && <li><strong>Max clip:</strong> {scope.max_clip_seconds} s</li>}
      {scope.attribution_required && <li><strong>Attribution:</strong> {scope.attribution_required}</li>}
      {scope.territories !== null && <li><strong>Territories:</strong> {scope.territories.join(", ")} (cannot be enforced: publishing refused)</li>}
    </ul>
  );
}

function toggle<T>(list: T[], v: T): T[] {
  return list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
}

export interface LicenceFormValue {
  coverage: "creator" | "channels";
  channel_ids: string[];
  scope: Scope;
  valid_until: string | null;
  attestation_text: string;
}

const DEFAULT_SCOPE: Scope = {
  v: 1, uses: ["acquire", "analyze", "edit", "publish"],
  acquisition_methods: ["local_recording", "creator_file", "platform_export"],
  publish_platforms: ["youtube_shorts"], brand_channels: "*", monetization: false,
  attribution_required: null, max_clip_seconds: 60, territories: null, notes: "",
};

/** Create a licence, or edit one (= a new attested version that supersedes it). */
export function LicenceForm({ channels, brands, initial, editing, busy, onSubmit, onCancel }: {
  channels: CreatorChannel[];
  brands: BrandChannel[];
  initial?: RightsRecord;
  editing?: boolean;
  busy: boolean;
  onSubmit: (v: LicenceFormValue) => void;
  onCancel: () => void;
}) {
  const [scope, setScope] = useState<Scope>(initial?.scope ?? DEFAULT_SCOPE);
  const [coverage, setCoverage] = useState<"creator" | "channels">(
    initial?.coverage === "channels" ? "channels" : "creator");
  const [channelIds, setChannelIds] = useState<string[]>(initial?.channel_ids ?? []);
  const [validUntil, setValidUntil] = useState(initial?.valid_until?.slice(0, 10) ?? "");
  const [attestation, setAttestation] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const allBrands = scope.brand_channels === "*";

  const submit = (e: FormEvent) => {
    e.preventDefault();
    onSubmit({ coverage, channel_ids: coverage === "channels" ? channelIds : [], scope,
      valid_until: validUntil ? `${validUntil}T23:59:59Z` : null, attestation_text: attestation });
  };
  const canSubmit = confirmed && attestation.trim().length >= 10 && scope.uses.length > 0
    && (coverage === "creator" || channelIds.length > 0) && !busy;

  return (
    <form className="form" onSubmit={submit} aria-label={editing ? "Edit licence" : "New licence"}>
      {editing && <p className="muted">Editing creates version {(initial?.version ?? 0) + 1}. Version {initial?.version} stops
        authorizing anything immediately, including work already running.</p>}
      {!editing && (
        <fieldset>
          <legend>Covers</legend>
          <label><input type="radio" name="coverage" checked={coverage === "creator"}
            onChange={() => setCoverage("creator")} /> Every video from this creator (standing licence)</label>
          <label><input type="radio" name="coverage" checked={coverage === "channels"}
            onChange={() => setCoverage("channels")} /> Only selected channels</label>
          {coverage === "channels" && channels.map((c) => (
            <label key={c.id} className="indent"><input type="checkbox" checked={channelIds.includes(c.id)}
              onChange={() => setChannelIds(toggle(channelIds, c.id))} /> {c.platform}: {c.handle ?? c.platform_channel_id ?? "local files"}</label>
          ))}
        </fieldset>
      )}
      <fieldset>
        <legend>Allowed uses</legend>
        {USES.map((u) => (
          <label key={u}><input type="checkbox" checked={scope.uses.includes(u)}
            onChange={() => setScope({ ...scope, uses: toggle(scope.uses, u) })} /> {u}</label>
        ))}
      </fieldset>
      <fieldset>
        <legend>How footage may be obtained</legend>
        {Object.entries(METHODS).map(([m, label]) => (
          <label key={m}><input type="checkbox" checked={scope.acquisition_methods.includes(m)}
            onChange={() => setScope({ ...scope, acquisition_methods: toggle(scope.acquisition_methods, m) })} /> {label}</label>
        ))}
      </fieldset>
      <fieldset>
        <legend>Publishing</legend>
        {Object.entries(PLATFORMS).map(([p, label]) => (
          <label key={p}><input type="checkbox" checked={scope.publish_platforms.includes(p)}
            onChange={() => setScope({ ...scope, publish_platforms: toggle(scope.publish_platforms, p) })} /> {label}</label>
        ))}
        <label><input type="checkbox" checked={allBrands}
          onChange={() => setScope({ ...scope, brand_channels: allBrands ? [] : "*" })} /> Any RIFTWIRE brand channel</label>
        {!allBrands && brands.map((b) => (
          <label key={b.id} className="indent"><input type="checkbox"
            checked={(scope.brand_channels as string[]).includes(b.id)}
            onChange={() => setScope({ ...scope, brand_channels: toggle(scope.brand_channels as string[], b.id) })} /> {b.name}</label>
        ))}
        <label><input type="checkbox" checked={scope.monetization}
          onChange={() => setScope({ ...scope, monetization: !scope.monetization })} /> Monetized channels allowed</label>
      </fieldset>
      <div className="form-row">
        <label>Max clip length (seconds)
          <input type="number" min={1} value={scope.max_clip_seconds ?? ""} placeholder="no limit"
            onChange={(e) => setScope({ ...scope, max_clip_seconds: e.target.value ? Number(e.target.value) : null })} />
        </label>
        <label>Attribution text
          <input type="text" value={scope.attribution_required ?? ""} placeholder="none"
            onChange={(e) => setScope({ ...scope, attribution_required: e.target.value || null })} />
        </label>
        <label>Valid until
          <input type="date" value={validUntil} onChange={(e) => setValidUntil(e.target.value)} />
        </label>
      </div>
      <label className="block">Attestation (what permission you have, from whom, and how)
        <textarea rows={3} value={attestation} onChange={(e) => setAttestation(e.target.value)}
          placeholder="e.g. The creator granted RIFTWIRE a written licence on 2026-11-20 to edit and publish clips of their GTA VI streams." />
      </label>
      <label><input type="checkbox" checked={confirmed} onChange={() => setConfirmed(!confirmed)} /> I confirm this
        statement is true. Uploaded evidence supports it but does not replace it.</label>
      <div className="toolbar">
        <button type="submit" className="btn btn-primary" disabled={!canSubmit}>
          {editing ? "Save as new version" : "Attest licence"}
        </button>
        <button type="button" className="btn btn-ghost" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}
