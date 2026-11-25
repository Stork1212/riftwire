import { useState } from "react";
import { api, getHighlightScan, queueHighlightScan, type HighlightCandidate } from "../services/api";
import { usePolling } from "../services/usePolling";
import { ActionableError } from "./ActionableError";
import { timecode } from "./ClipListReview";
import { ProgressBar, StatusBadge } from "./ui";

export function HighlightReview({ recordingId, disabled, onSelect }: {
  recordingId: string;
  disabled: boolean;
  onSelect: (candidate: HighlightCandidate) => void;
}) {
  const scan = usePolling(() => getHighlightScan(recordingId), 2500, [recordingId]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>();
  const [selected, setSelected] = useState<number>();
  const job = scan.data?.job;
  const result = scan.error ? undefined : scan.data?.result;
  const running = job && ["PENDING", "RUNNING", "RETRY_WAIT"].includes(job.status);

  async function runScan() {
    setPending(true);
    setError(undefined);
    try {
      if (job?.status === "FAILED") await api.retryJob(job.id);
      else await queueHighlightScan(recordingId);
      scan.refresh();
    } catch (e) {
      setError(e);
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="subsection stack" aria-label="Full recording highlights">
      <strong>Full recording highlights</strong>
      <p className="muted">Find candidates using visual activity and local speech cues. Review each range before exporting.</p>
      <div className="toolbar">
        {!result && (
          <button type="button" className="btn btn-primary"
            disabled={disabled || pending || Boolean(running || scan.error) || scan.loading}
            onClick={() => void runScan()}>
            {job?.status === "FAILED" ? "Retry highlight scan" : "Find highlights"}
          </button>
        )}
        {job && <StatusBadge status={job.status} />}
      </div>
      {running && <ProgressBar value={job.progress_percent} label="Highlight scan progress" />}
      {job?.last_error && job.status !== "COMPLETED" && <p role="status">{job.last_error}</p>}
      {(error !== undefined || scan.error) && <ActionableError title="Highlight scan unavailable" error={error ?? scan.error} />}
      {result && (
        <>
          <p role="status">
            Scanned {timecode(result.coverage_ms)} of {timecode(result.duration_ms)}.{" "}
            {result.speech_available ? "Visual and speech cues." : "Visual cues only."}{" "}
            {result.items.length} candidates to review.
          </p>
          <ol className="stack">
            {result.items.map((candidate, index) => (
              <li key={candidate.start_ms}>
                <div className="toolbar">
                  <strong>{timecode(candidate.start_ms)}–{timecode(candidate.end_ms)}</strong>
                  <span className="muted">Activity peak {timecode(candidate.peak_ms)}</span>
                  <button type="button" className="btn btn-sm" disabled={disabled}
                    onClick={() => {
                      onSelect(candidate);
                      setSelected(index);
                    }}>
                    {selected === index ? "Range loaded" : "Use range"}
                  </button>
                </div>
                {candidate.speech_cues.length > 0 && <p>Speech cues: {candidate.speech_cues.join(", ")}</p>}
                {candidate.excerpt && <details><summary>Transcript excerpt</summary><p>{candidate.excerpt}</p></details>}
                <details>
                  <summary>View sampled frame</summary>
                  <img className="thumb-lg" loading="lazy"
                    src={`/api/v1/recordings/${encodeURIComponent(recordingId)}/highlight-scan/frame/${index}`}
                    alt={`Video frame at ${timecode(candidate.peak_ms)}`} />
                </details>
              </li>
            ))}
          </ol>
        </>
      )}
    </section>
  );
}
