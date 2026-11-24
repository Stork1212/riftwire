import { useState } from "react";
import { downloadSource } from "../services/api";
import { ActionableError } from "./ActionableError";

export function SourceDownload({ sourceId }: { sourceId?: string }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [saved, setSaved] = useState(false);

  const download = async () => {
    if (!sourceId || busy) return;
    setBusy(true);
    setError(undefined);
    setSaved(false);
    try {
      await downloadSource(sourceId);
      setSaved(true);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack">
      <div className="toolbar">
        <button type="button" className="btn" disabled={!sourceId || busy} onClick={download}>
          {busy ? "Preparing MP4…" : "Download MP4"}
        </button>
        {!sourceId && <span className="muted">Save the source to check download permission.</span>}
        {saved && <span role="status">MP4 sent to your browser downloads.</span>}
      </div>
      {error !== undefined && <ActionableError title="Download unavailable" error={error} />}
    </div>
  );
}
