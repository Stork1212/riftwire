import { useState } from "react";
import { Link } from "react-router-dom";
import { downloadClip, suggestClips, type ClipSuggestion } from "../services/api";
import { formatDuration } from "../services/format";
import type { Recording } from "../types/api";
import { ActionableError } from "./ActionableError";
import { Panel } from "./ui";

export function RecordingClips({ recording }: { recording: Recording }) {
  const [items, setItems] = useState<ClipSuggestion[]>();
  const [busy, setBusy] = useState<"analyze" | "cut">();
  const [error, setError] = useState<unknown>();

  const analyze = async () => {
    setBusy("analyze");
    setError(undefined);
    try {
      setItems((await suggestClips(recording.id)).items);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(undefined);
    }
  };

  const cut = async ({ start_ms, end_ms }: ClipSuggestion) => {
    setBusy("cut");
    setError(undefined);
    try {
      await downloadClip(recording.id, start_ms, end_ms);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(undefined);
    }
  };

  return (
    <Panel title="AI clip suggestions">
      {!recording.source_id ? (
        <p>Link this recording to a <Link to="/sources">licensed source</Link> to analyze it.</p>
      ) : (
        <div className="stack">
          <p>Local Whisper checks the first five minutes of speech and suggests up to three clips.</p>
          <div className="toolbar">
            <button type="button" className="btn btn-primary" disabled={Boolean(busy)} onClick={analyze}>
              {busy === "analyze" ? "Analyzing…" : "Find clips"}
            </button>
            <Link to={`/sources/${recording.source_id}`}>View source and rights</Link>
          </div>
          {error !== undefined && <ActionableError error={error} />}
          {items?.length === 0 && <p role="status">No speech-based clip suggestions found.</p>}
          {items?.map((item) => (
            <div className="subsection" key={item.start_ms}>
              <strong>{formatDuration(item.start_ms)} – {formatDuration(item.end_ms)}</strong>
              <p>{item.transcript}</p>
              <button type="button" className="btn" disabled={Boolean(busy)} onClick={() => cut(item)}>
                {busy === "cut" ? "Cutting…" : "Cut and download MP4"}
              </button>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}
