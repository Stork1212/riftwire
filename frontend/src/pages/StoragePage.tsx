import { EmptyState, ErrorBanner, Loading, Panel, StatusBadge } from "../components/ui";
import { api } from "../services/api";
import { formatBytes, formatDate } from "../services/format";
import { usePolling } from "../services/usePolling";
import type { StorageLocation, StorageVolume } from "../types/api";

const KIND_LABEL: Record<string, string> = {
  recordings: "Recordings",
  temp: "Temporary processing",
  models: "AI models",
  exports: "Exports",
  assets: "Brand assets",
};

function driveName(v: StorageVolume): string {
  return v.label || v.mount_point || "Unknown drive";
}

function VolumeCard({ v }: { v: StorageVolume }) {
  const total = v.total_bytes ?? 0;
  const free = v.free_bytes ?? 0;
  const usedPct = total ? ((total - free) / total) * 100 : 0;
  const floorPct = total ? Math.min(100 - usedPct, (v.floor_bytes / total) * 100) : 0;
  return (
    <div className="volume" data-testid={`volume-${v.volume_uuid}`}>
      <h3>{driveName(v)} {!v.online && <StatusBadge status="OFFLINE" />}</h3>
      <p className="muted">{v.kinds.map((k) => KIND_LABEL[k] ?? k).join(" · ")}</p>
      {v.below_floor && (
        <div className="alert alert-warn" role="alert">
          Free space is below the {formatBytes(v.floor_bytes)} safety minimum on this drive —
          writes to it are blocked.
        </div>
      )}
      {v.online ? (
        <>
          <div className="storage-bar" aria-hidden="true">
            <div className="storage-used" style={{ width: `${usedPct}%` }} />
            <div className="storage-floor" style={{ width: `${floorPct}%` }} />
          </div>
          <dl className="kv">
            <dt>Free</dt><dd>{formatBytes(v.free_bytes)} of {formatBytes(v.total_bytes)}</dd>
            <dt>Safety minimum</dt><dd>{formatBytes(v.floor_bytes)}</dd>
            <dt>Reserved by in-progress writes</dt><dd>{formatBytes(v.reserved_bytes)}</dd>
            <dt>Available headroom</dt><dd><strong>{formatBytes(v.headroom_bytes)}</strong></dd>
          </dl>
        </>
      ) : (
        <p className="muted">Drive not available — nothing is written to it, and nothing is
          redirected to another drive.</p>
      )}
    </div>
  );
}

function LocationRow({ loc }: { loc: StorageLocation }) {
  return (
    <tr data-kind={loc.kind}>
      <td>{KIND_LABEL[loc.kind] ?? loc.kind}</td>
      <td className="mono">{loc.path ?? "—"}</td>
      <td>{loc.volume_label || loc.mount_point || "—"}</td>
      <td>{loc.fs_type ? loc.fs_type.toUpperCase() : "—"}</td>
      <td>
        {loc.online
          ? <StatusBadge status="ONLINE" />
          : <span><StatusBadge status="OFFLINE" /> <span className="muted">{loc.message}</span></span>}
      </td>
      <td className="num">{loc.online ? formatBytes(loc.usage_bytes) : "—"}</td>
    </tr>
  );
}

export function StoragePage() {
  const st = usePolling(api.storage, 5000);
  const d = st.data;
  const offline = d?.locations.filter((l) => !l.online) ?? [];
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Storage</h1>
          <p>Where RIFTWIRE keeps its files, and the drives behind them.</p>
        </div>
      </div>
      {st.error && <ErrorBanner error={st.error} title="Cannot load storage status" />}
      {st.loading && !d && <Loading />}
      {d && offline.length > 0 && (
        <div className="alert alert-warn" role="alert">
          <strong>Storage offline:</strong>{" "}
          {offline.map((l) => `${KIND_LABEL[l.kind] ?? l.kind} (${l.message})`).join("; ")}.
          Jobs that need it wait without using up retries; imports are refused until it is back.
        </div>
      )}
      {d && (
        <>
          <Panel title="Drives">
            {d.volumes.length === 0
              ? <EmptyState title="No storage locations registered yet" />
              : <div className="volumes">{d.volumes.map((v) => <VolumeCard key={v.volume_uuid} v={v} />)}</div>}
          </Panel>
          <Panel title="Locations">
            {d.locations.length === 0 ? <EmptyState title="No storage locations registered yet" /> : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr><th>Location</th><th>Folder</th><th>Drive</th><th>Filesystem</th>
                      <th>Status</th><th className="num">Used</th></tr>
                  </thead>
                  <tbody>{d.locations.map((l) => <LocationRow key={l.kind} loc={l} />)}</tbody>
                </table>
              </div>
            )}
            {(d.migrations ?? []).length > 0 && (
              <div className="table-wrap">
                <h3>Recent migrations</h3>
                <table>
                  <thead><tr><th>Location</th><th>To</th><th>Status</th>
                    <th className="num">Verified</th><th>Updated</th></tr></thead>
                  <tbody>
                    {(d.migrations ?? []).map((m) => (
                      <tr key={m.id} data-migration={m.status}>
                        <td>{KIND_LABEL[m.kind] ?? m.kind}</td>
                        <td className="mono">{m.dest_path}</td>
                        <td>{m.status === "SWITCHED" ? "Completed" : m.status === "COPYING"
                          ? "Unfinished (resume or abort with the tool)" : "Aborted"}</td>
                        <td className="num">{m.verified_files} / {m.total_files} files</td>
                        <td>{formatDate(m.updated_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <p className="muted">
              The database, logs and thumbnails always stay on the internal disk. To move a
              location to another drive, stop RIFTWIRE and use{" "}
              <code>scripts/migrate_storage.py</code> (dry run by default; originals are never
              deleted).
            </p>
          </Panel>
        </>
      )}
    </>
  );
}
