import { useState } from "react";
import { Link } from "react-router-dom";
import { rightsApi } from "../services/rightsApi";
import type { Acquired } from "../types/rights";
import { ActionableError } from "./ActionableError";

/** Fetch a saved video link and import it as a recording linked to its source. The server
 * checks the licence (URL fetching allowed, written platform permission on record) before
 * any download, re-checks after it, and records how the footage was obtained. */
export function SourceAcquire({ sourceId, onAcquired }: {
  sourceId?: string; onAcquired?: (a: Acquired) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [done, setDone] = useState<Acquired>();

  const acquire = async () => {
    if (!sourceId || busy) return;
    setBusy(true);
    setError(undefined);
    try {
      const a = await rightsApi.acquireSource(sourceId);
      setDone(a);
      onAcquired?.(a);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack">
      <div className="toolbar">
        <button type="button" className="btn" disabled={!sourceId || busy || !!done} onClick={acquire}>
          {busy ? "Acquiring footage…" : "Acquire footage"}
        </button>
        {!sourceId && <span className="muted">Save the source first; acquisition needs a licence that allows URL fetching and written platform permission.</span>}
        {done && (
          <span role="status">
            {done.already_acquired ? "Already acquired: " : "Acquired and linked: "}
            <Link to={`/recordings/${done.recording.id}`}>{done.recording.original_filename}</Link>
          </span>
        )}
      </div>
      {error !== undefined && <ActionableError title="Not acquired" error={error} />}
    </div>
  );
}
