import type { Job, JobType } from "../types/api";
import { ProgressBar, StatusBadge } from "./ui";

const ORDER: JobType[] = ["INGEST", "METADATA", "THUMBNAIL"];
const LABEL: Record<JobType, string> = { INGEST: "Store & verify", METADATA: "Metadata", THUMBNAIL: "Thumbnail" };

/** Per-stage processing progress for one recording. */
export function JobStages({ jobs }: { jobs: Job[] }) {
  return (
    <div aria-label="Processing stages">
      {ORDER.map((t) => {
        const j = jobs.find((x) => x.job_type === t);
        const tone = j?.status === "FAILED" ? "bad" : j?.status === "COMPLETED" ? "ok" : "info";
        return (
          <div className="stage" key={t}>
            <span className="stage-label">{LABEL[t]}</span>
            <ProgressBar value={j ? j.progress_percent : 0} label={`${LABEL[t]} progress`} tone={tone} />
            <span style={{ justifySelf: "end" }}>{j ? <StatusBadge status={j.status} /> : <span className="muted">Waiting</span>}</span>
            {j?.last_error && j.status !== "COMPLETED" && (
              <span className="err-text" style={{ gridColumn: "2 / 4" }}>{j.last_error}</span>
            )}
          </div>
        );
      })}
    </div>
  );
}
