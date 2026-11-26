import { useState } from "react";
import { Link } from "react-router-dom";
import { downloadClip, suggestClips, type ClipLayout, type ClipSuggestion } from "../services/api";
import { formatDuration } from "../services/format";
import type { Recording } from "../types/api";
import { HighlightReview } from "./HighlightReview";
import { TranscriptPanel } from "./TranscriptPanel";
import { ClipListReview, timecode } from "./ClipListReview";
import { ActionableError } from "./ActionableError";
import { Panel } from "./ui";

function timestamp(value: string): number | undefined {
  const parts = /^(\d{1,3}):([0-5]\d):([0-5]\d)(?:\.(\d{3}))?$/.exec(value.trim());
  if (!parts) return undefined;
  return (Number(parts[1]) * 3600 + Number(parts[2]) * 60 + Number(parts[3])) * 1000 + Number(parts[4] ?? 0);
}

export function RecordingClips({ recording }: { recording: Recording }) {
  const [items, setItems] = useState<ClipSuggestion[]>();
  const [busy, setBusy] = useState<"analyze" | "cut">();
  const [error, setError] = useState<unknown>();
  const [facecam, setFacecam] = useState(false);
  const [title, setTitle] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const splitLayout: ClipLayout = facecam ? "split_facecam" : "split";

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

  const cut = async (startMs: number, endMs: number, subtitles = false,
    layout: ClipLayout = "original") => {
    setBusy("cut");
    setError(undefined);
    try {
      await downloadClip(recording.id, startMs, endMs, subtitles, layout, layout === "original" ? undefined : title);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(undefined);
    }
  };

  return (
    <Panel title="Highlights and Shorts">
      {!recording.source_id ? (
        <p>Link this recording to a <Link to="/sources">licensed source</Link> to analyze it.</p>
      ) : (
        <div className="stack">
          <p>Suggestions read the first five minutes of the shared transcript and propose up to three clips.</p>
          <div className="toolbar">
            <button type="button" className="btn btn-primary" disabled={Boolean(busy)} onClick={analyze}>
              {busy === "analyze" ? "Analyzing…" : "Find clips"}
            </button>
            <Link to={`/sources/${recording.source_id}`}>View source and rights</Link>
          </div>
          <TranscriptPanel recordingId={recording.id} disabled={Boolean(busy)} />
          <HighlightReview recordingId={recording.id} disabled={Boolean(busy)} onSelect={(candidate) => {
            setStart(timecode(candidate.start_ms));
            setEnd(timecode(candidate.end_ms));
            setError(undefined);
          }} />
          <ClipListReview recordingId={recording.id} disabled={Boolean(busy)} onSelect={(clip) => {
            setStart(timecode(clip.from_ms));
            setEnd(timecode(clip.to_ms));
            setTitle(clip.label.replaceAll("_", " ").slice(0, 80));
            setError(undefined);
          }} />
          <div className="subsection stack">
            <strong>35/65 Short</strong>
            <div className="toolbar">
              <label><input type="checkbox" checked={facecam} disabled={Boolean(busy)}
                onChange={(event) => setFacecam(event.target.checked)} /> Use lower-left facecam</label>
              <input aria-label="Short hook" placeholder="On-screen hook (optional)"
                maxLength={80} value={title} disabled={Boolean(busy)}
                onChange={(event) => setTitle(event.target.value)} />
            </div>
            <div className="toolbar">
              <input aria-label="Clip start timestamp" placeholder="Start HH:MM:SS" value={start}
                onChange={(event) => setStart(event.target.value)} />
              <input aria-label="Clip end timestamp" placeholder="End HH:MM:SS" value={end}
                onChange={(event) => setEnd(event.target.value)} />
              <button type="button" className="btn" disabled={Boolean(busy)} onClick={() => {
                const from = timestamp(start);
                const to = timestamp(end);
                if (from === undefined || to === undefined || to <= from) {
                  setError(new Error("Enter a start and a later end in HH:MM:SS format."));
                  return;
                }
                void cut(from, to, false, splitLayout);
              }}>Export timestamp range</button>
            </div>
          </div>
          {error !== undefined && <ActionableError error={error} />}
          {items?.length === 0 && <p role="status">No speech-based clip suggestions found.</p>}
          {items?.map((item) => (
            <div className="subsection" key={item.start_ms}>
              <strong>{formatDuration(item.start_ms)} – {formatDuration(item.end_ms)}</strong>
              <p>{item.transcript}</p>
              <div className="toolbar">
                <button type="button" className="btn" disabled={Boolean(busy)} onClick={() => cut(item.start_ms, item.end_ms)}>
                  {busy === "cut" ? "Cutting…" : "Download MP4"}
                </button>
                {recording.has_audio !== false && (
                  <button type="button" className="btn" disabled={Boolean(busy)} onClick={() => cut(item.start_ms, item.end_ms, true)}>
                    Download MP4 with subtitles
                  </button>
                )}
                <button type="button" className="btn btn-primary" disabled={Boolean(busy)}
                  onClick={() => cut(item.start_ms, item.end_ms, false, splitLayout)}>
                  {busy === "cut" ? "Rendering…" : "Export 35/65 Short"}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}
