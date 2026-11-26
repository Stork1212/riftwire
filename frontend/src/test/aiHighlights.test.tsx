import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AIHighlights } from "../components/AIHighlights";
import { getAIHighlights, requestAIHighlights, type AIHighlight, type AIHighlightState } from "../services/api";

vi.mock("../services/api", async (orig) => {
  const actual = await orig<typeof import("../services/api")>();
  return { ...actual, getAIHighlights: vi.fn(), requestAIHighlights: vi.fn(),
    reviewAIHighlight: vi.fn(), api: { retryJob: vi.fn() } };
});

const hl = (over: Partial<AIHighlight> = {}): AIHighlight => ({
  id: "h1", rank: 1, status: "selected", reason: null, start_ms: 12_000, end_ms: 31_500,
  category: "police_pursuit", title: "Chase into the harbor", hook: "He did not brake",
  what_happens: "A pursuit ends in the water.", why_entertaining: "Sudden payoff.",
  editorial_score: 81, confidence: 0.55, scores: {}, uncertainty: "",
  evidence: [{ ids: ["w:t:1", "w:t:2"], claim: "streamer shouts", basis: "transcript", quote: "no way" }],
  review_status: "pending", model_clip: [12_300, 31_000], boundary_notes: ["pre-roll 300 ms"],
  penalties: [], model_editorial_score: 81, text_checks: [], window: { id: "c:1", reason: "seeds: baseline_scan" },
  ...over,
});

const done = (highlights: AIHighlight[], returned = highlights.length): AIHighlightState => ({
  request: { id: "r", status: "COMPLETE", game: "gta", channel: "rumbleframe", prompt_id: "gaming.gta.v2",
    target_count: 5, error: null, created_at: "2026-11-25T00:00:00Z",
    summary: { requested: 5, returned, fewer_than_requested: returned < 5,
      reason: returned < 5 ? "only these candidates passed" : null,
      editorial_score_meaning: "estimated editorial quality 0-100; not a probability of going viral" } },
  job: { id: "j", status: "COMPLETED", progress_percent: 100, last_error: null },
  highlights, rejected: [], runs: [],
});

describe("AIHighlights", () => {
  beforeEach(() => vi.resetAllMocks());

  it("requests the chosen number of highlights for a game and channel", async () => {
    vi.mocked(getAIHighlights).mockResolvedValue({ request: null, job: null, highlights: [], rejected: [], runs: [] });
    vi.mocked(requestAIHighlights).mockResolvedValue({ request_id: "r", job_id: "j", status: "PENDING" });
    render(<AIHighlights recordingId="rec-1" disabled={false} onSelect={() => undefined} />);
    const button = await screen.findByRole("button", { name: "Find highlights" });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.selectOptions(screen.getByLabelText("Clips"), "10");
    await userEvent.click(button);
    expect(requestAIHighlights).toHaveBeenCalledWith("rec-1", { game: "gta", channel: "rumbleframe", target_count: 10 });
  });

  it("shows ranked highlights, says fewer were returned, and loads one for rendering", async () => {
    vi.mocked(getAIHighlights).mockResolvedValue(done([hl()], 1));
    const onSelect = vi.fn();
    render(<AIHighlights recordingId="rec-1" disabled={false} onSelect={onSelect} />);
    expect(await screen.findByText(/#1 Chase into the harbor/)).toBeInTheDocument();
    expect(screen.getByText(/1 of 5 requested/)).toHaveTextContent("only these candidates passed");
    expect(screen.getByText("Editorial score 81")).toHaveAttribute("title", expect.stringContaining("not a probability"));
    await userEvent.click(screen.getByRole("button", { name: "Use for Short" }));
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: "h1", start_ms: 12_000 }));
  });

  it("shows a loading state, then an empty state before any request", async () => {
    let resolve: (v: AIHighlightState) => void = () => undefined;
    vi.mocked(getAIHighlights).mockReturnValue(new Promise((r) => { resolve = r; }));
    render(<AIHighlights recordingId="rec-1" disabled={false} onSelect={() => undefined} />);
    expect(screen.getByText("Loading AI highlights…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Find highlights" })).toBeDisabled();
    resolve({ request: null, job: null, highlights: [], rejected: [], runs: [] });
    expect(await screen.findByText(/No highlights requested/)).toBeInTheDocument();
    expect(screen.queryByText("Loading AI highlights…")).not.toBeInTheDocument();
  });
});
