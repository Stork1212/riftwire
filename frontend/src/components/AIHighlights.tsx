import { useState } from "react";
import {
  api, getAIHighlights, requestAIHighlights, reviewAIHighlight,
  type AIHighlight,
} from "../services/api";
import { usePolling } from "../services/usePolling";
import { ActionableError } from "./ActionableError";
import { timecode } from "./ClipListReview";
import { ProgressBar, StatusBadge } from "./ui";

const COUNTS = [3, 5, 10, 20];

/** M6: ask the local model for ranked, evidence-grounded highlights, review them, and
 * load one into the Short renderer. The score is an editorial estimate, never a
 * probability of going viral. */
export function AIHighlights({ recordingId, disabled, onSelect }: {
  recordingId: string;
  disabled: boolean;
  onSelect: (h: AIHighlight) => void;
}) {
  const state = usePolling(() => getAIHighlights(recordingId), 2500, [recordingId]);
  const [game, setGame] = useState("gta");
  const [channel, setChannel] = useState("rumbleframe");
  const [count, setCount] = useState(5);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>();
  const [selected, setSelected] = useState<string>();
  const data = state.error ? undefined : state.data;
  const req = data?.request;
  const job = data?.job;
  const running = req && ["PENDING", "RUNNING"].includes(req.status);

  async function start() {
    setPending(true);
    setError(undefined);
    try {
      if (job?.status === "FAILED" && req?.status !== "FAILED") await api.retryJob(job.id);
      else await requestAIHighlights(recordingId, { game: game || null, channel: channel || null, target_count: count });
      state.refresh();
    } catch (e) {
      setError(e);
    } finally {
      setPending(false);
    }
  }

  async function review(h: AIHighlight, status: AIHighlight["review_status"]) {
    try {
      await reviewAIHighlight(recordingId, h.id, status);
      state.refresh();
    } catch (e) {
      setError(e);
    }
  }

  return (
    <section className="subsection stack" aria-label="AI highlights">
      <strong>AI highlights (local model)</strong>
      <p className="muted">
        Uses the stored transcript and signals. Every clip cites its evidence; fewer clips are returned when fewer moments qualify.
      </p>
      <div className="toolbar">
        <label>Game <input value={game} onChange={(e) => setGame(e.target.value)} size={8} /></label>
        <label>Channel <input value={channel} onChange={(e) => setChannel(e.target.value)} size={12} /></label>
        <label>Clips
          <select value={count} onChange={(e) => setCount(Number(e.target.value))}>
            {COUNTS.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
        <button type="button" className="btn btn-primary"
          disabled={disabled || pending || state.loading || Boolean(running) || Boolean(state.error)}
          onClick={() => void start()}>
          {running ? "Finding highlights…" : "Find highlights"}
        </button>
        {req && <StatusBadge status={req.status === "COMPLETE" ? "COMPLETED" : req.status} />}
      </div>
      {state.loading && <p role="status" className="muted">Loading AI highlights…</p>}
      {data && !req && <p className="muted">No highlights requested for this recording yet.</p>}
      {running && job && <ProgressBar value={job.progress_percent} label="AI highlight progress" />}
      {running && job?.last_error && <p role="status">{job.last_error}</p>}
      {req?.status === "FAILED" && <p role="status" className="err-text">{req.error}</p>}
      {(error !== undefined || state.error) && <ActionableError title="AI highlights unavailable" error={error ?? state.error} />}
      {req?.status === "COMPLETE" && req.summary && (
        <p role="status">
          {req.summary.returned} of {req.summary.requested} requested
          {req.summary.fewer_than_requested ? ` — ${req.summary.reason}` : ""}.
        </p>
      )}
      {data && data.highlights.length > 0 && (
        <ol className="stack" aria-label="Ranked highlights">
          {data.highlights.map((h) => (
            <li key={h.id}>
              <div className="toolbar">
                <strong>#{h.rank} {h.title}</strong>
                <span>{timecode(h.start_ms)}–{timecode(h.end_ms)}</span>
                <span className="muted" title="Estimated editorial quality (0–100), not a probability of going viral">
                  Editorial score {h.editorial_score}
                </span>
                <span className="muted">{h.category.replaceAll("_", " ")}</span>
                <StatusBadge status={h.review_status.toUpperCase()} />
              </div>
              {h.hook && <p><em>{h.hook}</em></p>}
              <p>{h.what_happens}</p>
              <div className="toolbar">
                <button type="button" className="btn btn-sm" disabled={disabled}
                  onClick={() => { onSelect(h); setSelected(h.id); }}>
                  {selected === h.id ? "Loaded for render" : "Use for Short"}
                </button>
                <button type="button" className="btn btn-sm" onClick={() => void review(h, "approved")}>Approve</button>
                <button type="button" className="btn btn-sm" onClick={() => void review(h, "rejected")}>Reject</button>
              </div>
              <details>
                <summary>Evidence and checks</summary>
                <ul>
                  {h.evidence.map((e, i) => (
                    <li key={i}>{e.claim} <span className="muted">({e.basis}: {e.ids.length} ids)</span>
                      {e.quote ? <> — “{e.quote}”</> : null}</li>
                  ))}
                </ul>
                {h.penalties.length > 0 && <p>Penalties: {h.penalties.map((p) => `${p.name} −${p.points} (${p.reason})`).join("; ")}</p>}
                {h.boundary_notes.length > 0 && <p>Boundaries: {h.boundary_notes.join("; ")}</p>}
                {h.text_checks.length > 0 && <p>Text checks: {h.text_checks.join("; ")}</p>}
                {h.uncertainty && <p>Model uncertainty: {h.uncertainty}</p>}
                <p className="muted">Model confidence {h.confidence.toFixed(2)} (separate from the editorial score)</p>
              </details>
            </li>
          ))}
        </ol>
      )}
      {data && data.rejected.length > 0 && (
        <details>
          <summary>{data.rejected.length} candidates not selected</summary>
          <ul>{data.rejected.map((h) => (
            <li key={h.id}>{timecode(h.start_ms)}–{timecode(h.end_ms)} {h.title}: {h.reason}</li>
          ))}</ul>
        </details>
      )}
    </section>
  );
}
