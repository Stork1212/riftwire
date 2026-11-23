import type { SystemStatus } from "../types/api";
import { formatBytes } from "../services/format";

export function StorageBar({ storage }: { storage: SystemStatus["storage"] }) {
  const { total_bytes: total, used_bytes: used, free_bytes: free, min_free_bytes: floor } = storage;
  const usedPct = total ? (used / total) * 100 : 0;
  const floorPct = total ? Math.min(100 - usedPct, (floor / total) * 100) : 0;
  return (
    <div>
      {storage.below_floor && (
        <div className="alert alert-warn" role="alert">
          Free space is below the {formatBytes(floor)} safety minimum — new imports are blocked.
        </div>
      )}
      <div className="storage-bar" aria-hidden="true">
        <div className="storage-used" style={{ width: `${usedPct}%` }} />
        <div className="storage-floor" style={{ width: `${floorPct}%` }} />
      </div>
      <dl className="kv">
        <dt>Free</dt>
        <dd>{formatBytes(free)} of {formatBytes(total)}</dd>
        <dt>Safety minimum</dt>
        <dd>{formatBytes(floor)}</dd>
        <dt>Available for imports</dt>
        <dd><strong>{formatBytes(storage.importable_bytes)}</strong></dd>
      </dl>
    </div>
  );
}
