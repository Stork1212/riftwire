import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TranscriptPanel } from "../components/TranscriptPanel";
import { getTranscript, requestTranscript, type TranscriptState } from "../services/api";

vi.mock("../services/api", async (orig) => {
  const actual = await orig<typeof import("../services/api")>();
  return { ...actual, getTranscript: vi.fn(), requestTranscript: vi.fn(), api: { retryJob: vi.fn() } };
});

const engine = { available: true as const, model_id: "whisper-base-mlx", model_sha256: "ab".repeat(32),
  language: "en", chunk_ms: 120000, overlap_ms: 4000, merge_version: 1 };

describe("TranscriptPanel", () => {
  beforeEach(() => vi.resetAllMocks());

  it("offers one transcription and queues it", async () => {
    const empty: TranscriptState = { engine, job: null, transcript: null };
    vi.mocked(getTranscript).mockResolvedValue(empty);
    vi.mocked(requestTranscript).mockResolvedValue({ queued: true, job: null });
    render(<TranscriptPanel recordingId="rec-1" disabled={false} />);
    const button = await screen.findByRole("button", { name: "Transcribe" });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);
    expect(requestTranscript).toHaveBeenCalledWith("rec-1");
  });

  it("shows the finished transcript with raw timestamps and a word preview", async () => {
    vi.mocked(getTranscript).mockResolvedValue({
      engine,
      job: { id: "j", status: "COMPLETED", progress_percent: 100, attempt_count: 1, max_attempts: 3, last_error: null },
      transcript: { id: "t", status: "COMPLETE", word_count: 2, duration_ms: 60000, coverage_ms: 60000,
        engine_version: "mlx-whisper 0.4.2", completed_at: null, timestamps: "raw" },
      words: [{ idx: 0, w: "No", start_ms: 0, end_ms: 200, prob: 0.9 },
        { idx: 1, w: "way!", start_ms: 200, end_ms: 500, prob: 0.9 }],
    });
    render(<TranscriptPanel recordingId="rec-1" disabled={false} />);
    expect(await screen.findByText(/2 words over/)).toHaveTextContent("raw timestamps");
    expect(screen.getByLabelText("Transcript preview")).toHaveTextContent("No way!");
    expect(screen.queryByRole("button", { name: "Transcribe" })).toBeNull();
  });

  it("explains when local Whisper is not configured instead of offering a download", async () => {
    vi.mocked(getTranscript).mockResolvedValue({
      engine: { available: false, reason: "Local Whisper is not configured." }, job: null, transcript: null,
    });
    render(<TranscriptPanel recordingId="rec-1" disabled={false} />);
    expect(await screen.findByText("Local Whisper is not configured.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Transcribe" })).toBeDisabled();
  });
});
