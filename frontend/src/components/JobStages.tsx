import type { Job, JobType } from "../types/api";
import { ProgressBar, StatusBadge } from "./ui";

const ORDER: JobType[] = ["INGEST", "METADATA", "THUMBNAIL"];
const LABEL: Record<JobType, string> = {
  HIGHLIGHT_SCAN: "Find highlights", TRANSCRIBE: "Transcript",
  AUDIO_SIGNALS: "Audio signals", VISUAL_SIGNALS: "Visual signals",
  ACQUIRE: "Acquire from link", INGEST: "Store & verify", METADATA: "Metadata", THUMBNAIL: "Thumbnail",
};

/** Per-stage processing progress for one recording. */
export function JobStages({ jobs }: { jobs: Job[] }) {
  return (
    <div aria-label="Processing stages">
      {(jobs.some((x) => x.job_type === "ACQUIRE") ? (["ACQUIRE", ...ORDER] as JobType[]) : ORDER).map((t) => {
        const j = jobs.find((x) => x.job_type === t);
        const tone = j?.status === "FAILED" ? "bad" : j?.status === "COMPLETED" ? "ok" : "info";
        return (
          <div className="stage" key={t}>
            <span className="stage-label">{LABEL[t]}</span>
            <ProgressBar value={j ? j.progress_percent : 0} label={`${LABEL[t]} progress`} tone={tone} />
            <span className="stage-status">{j ? <StatusBadge status={j.status} /> : <span className="muted">Waiting</span>}</span>
            {j?.last_error && j.status !== "COMPLETED" && (
              <span className="err-text stage-error">{j.last_error}</span>
            )}
          </div>
        );
      })}
    </div>
  );
}
