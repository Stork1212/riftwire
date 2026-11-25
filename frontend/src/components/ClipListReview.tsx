import { useState } from "react";
import { validateClipList, type ClipRange } from "../services/api";
import { ActionableError } from "./ActionableError";

export function timecode(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  const clock = [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60]
    .map((part) => String(part).padStart(2, "0")).join(":");
  return ms % 1000 ? `${clock}.${String(ms % 1000).padStart(3, "0")}` : clock;
}

export function ClipListReview({ recordingId, disabled, onSelect }: {
  recordingId: string;
  disabled: boolean;
  onSelect: (clip: ClipRange) => void;
}) {
  const [text, setText] = useState("");
  const [items, setItems] = useState<ClipRange[]>([]);
  const [checking, setChecking] = useState(false);
  const [selected, setSelected] = useState<number>();
  const [error, setError] = useState<unknown>();
  const busy = disabled || checking;

  async function check() {
    setChecking(true);
    setError(undefined);
    setItems([]);
    setSelected(undefined);
    try {
      setItems((await validateClipList(recordingId, text)).items);
    } catch (e) {
      setError(e);
    } finally {
      setChecking(false);
    }
  }

  return (
    <section className="subsection stack" aria-label="Review clip timestamps">
      <strong>Review clip timestamps</strong>
      <textarea aria-label="clips.txt contents" rows={4} maxLength={32768} value={text}
        placeholder={"URL=https://www.youtube.com/watch?v=…\n00:11:57-00:12:31 pickup_in_the_shallows"}
        disabled={busy} onChange={(event) => {
          setText(event.target.value);
          setItems([]);
          setSelected(undefined);
          setError(undefined);
        }} />
      <div className="toolbar">
        <button type="button" className="btn" disabled={busy || !text.trim()} onClick={() => void check()}>
          {checking ? "Checking timestamps…" : "Review timestamps"}
        </button>
      </div>
      {error !== undefined && <ActionableError title="Timestamp list needs attention" error={error} />}
      {items.length > 0 && (
        <ol className="stack">
          {items.map((clip) => (
            <li key={clip.line}>
              <div className="toolbar">
                <strong>{clip.label.replaceAll("_", " ")}</strong>
                <span className="muted">{timecode(clip.start_ms)}–{timecode(clip.end_ms)}</span>
                <button type="button" className="btn btn-sm" disabled={busy}
                  onClick={() => {
                    onSelect(clip);
                    setSelected(clip.line);
                  }}>
                  {selected === clip.line ? "Range loaded" : "Use range"}
                </button>
                {clip.to_ms - clip.from_ms > 45000 && <span className="muted">Trim to 45 seconds for Shorts export.</span>}
              </div>
              {clip.from_ms !== clip.start_ms && (
                <small>In this recording {timecode(clip.from_ms)}–{timecode(clip.to_ms)}</small>
              )}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
