import { useState } from "react";
import { api, getTranscript, requestTranscript } from "../services/api";
import { usePolling } from "../services/usePolling";
import { ActionableError } from "./ActionableError";
import { timecode } from "./ClipListReview";
import { ProgressBar, StatusBadge } from "./ui";

/** The recording's shared transcript (M4): one local Whisper run that highlight scans,
 * clip suggestions, captions and subtitles all reuse. Timestamps shown are the raw ones. */
export function TranscriptPanel({ recordingId, disabled }: { recordingId: string; disabled: boolean }) {
  const state = usePolling(() => getTranscript(recordingId, true), 3000, [recordingId]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>();
  const data = state.error ? undefined : state.data;
  const job = data?.job;
  const transcript = data?.transcript;
  const running = !transcript && job && ["PENDING", "RUNNING", "RETRY_WAIT"].includes(job.status);

  async function start() {
    setPending(true);
    setError(undefined);
    try {
      if (job?.status === "FAILED") await api.retryJob(job.id);
      else await requestTranscript(recordingId);
      state.refresh();
    } catch (e) {
      setError(e);
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="subsection stack" aria-label="Transcript">
      <strong>Transcript</strong>
      {data && !data.engine.available && <p className="muted">{data.engine.reason}</p>}
      {data?.engine.available && !transcript && (
        <p className="muted">
          Transcribe once with local Whisper ({data.engine.model_id}); highlights, suggestions and captions reuse it.
        </p>
      )}
      <div className="toolbar">
        {!transcript && (
          <button type="button" className="btn btn-primary"
            disabled={disabled || pending || Boolean(running) || !data?.engine.available}
            onClick={() => void start()}>
            {job?.status === "FAILED" ? "Retry transcription" : "Transcribe"}
          </button>
        )}
        {job && <StatusBadge status={transcript ? "COMPLETED" : job.status} />}
      </div>
      {running && <ProgressBar value={job.progress_percent} label="Transcription progress" />}
      {job?.last_error && !transcript && <p role="status">{job.last_error}</p>}
      {(error !== undefined || state.error) && (
        <ActionableError title="Transcript unavailable" error={error ?? state.error} />
      )}
      {transcript && (
        <>
          <p role="status">
            {transcript.word_count} words over {timecode(transcript.coverage_ms)} of{" "}
            {timecode(transcript.duration_ms)} · raw timestamps · {transcript.engine_version ?? "unknown engine"}
          </p>
          {data?.words && data.words.length > 0 && (
            <p className="muted" aria-label="Transcript preview">
              {data.words.slice(0, 60).map((w) => w.w).join(" ")}
              {data.words.length > 60 ? " …" : ""}
            </p>
          )}
        </>
      )}
    </section>
  );
}
