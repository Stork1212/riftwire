import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AppRoutes } from "../App";
import { ApiError, api, uploadRecording } from "../services/api";
import { detail, job, recording, storageStatus, systemStatus } from "./fixtures";

vi.mock("../services/api", async (orig) => {
  const actual = await orig<typeof import("../services/api")>();
  return {
    ...actual,
    api: {
      systemStatus: vi.fn(),
      listRecordings: vi.fn(),
      getRecording: vi.fn(),
      listJobs: vi.fn(),
      retryJob: vi.fn(),
      listEvents: vi.fn(),
      storage: vi.fn(),
    },
    uploadRecording: vi.fn(),
  };
});

vi.mock("../services/rightsApi", () => ({
  rightsApi: {
    listCreators: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 }),
    listSources: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 }),
  },
}));

const m = vi.mocked(api);
const page = <T,>(items: T[]) => ({ items, total: items.length, limit: 50, offset: 0 });

function renderAt(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AppRoutes /></MemoryRouter>);
}

beforeEach(() => {
  m.systemStatus.mockResolvedValue(systemStatus());
  m.listRecordings.mockResolvedValue(page([]));
  m.listJobs.mockResolvedValue(page([]));
  m.listEvents.mockResolvedValue(page([]));
  m.storage.mockResolvedValue(storageStatus());
});

describe("Overview", () => {
  it("renders real counts, storage and worker status", async () => {
    renderAt("/");
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
    const stat = (label: string) =>
      screen.getByText(label, { selector: ".stat-label" }).closest(".stat") as HTMLElement;
    await waitFor(() => expect(within(stat("Recordings")).getByText("3")).toBeInTheDocument());
    expect(within(stat("Imported duration")).getByText("1h 2m")).toBeInTheDocument();
    expect(within(stat("Pending jobs")).getByText("3")).toBeInTheDocument(); // 2 pending + 1 retry
    expect(within(stat("Failed jobs")).getByText("1")).toBeInTheDocument();
    expect(screen.getByText("Running", { selector: ".badge" })).toBeInTheDocument();
    expect(screen.getByText("10.0 GB")).toBeInTheDocument(); // importable above the floor
  });

  it("shows an empty state and a backend error clearly", async () => {
    m.systemStatus.mockResolvedValueOnce(systemStatus({
      counts: { recordings: 0, recordings_by_status: {}, total_duration_ms: 0,
        jobs: { PENDING: 0, RUNNING: 0, COMPLETED: 0, FAILED: 0, RETRY_WAIT: 0, CANCELLED: 0 } },
    }));
    renderAt("/");
    expect(await screen.findByText("No recordings yet")).toBeInTheDocument();
  });

  it("reports an unreachable backend", async () => {
    m.systemStatus.mockRejectedValue(new ApiError(0, "network_error", "Cannot reach the RIFTWIRE backend."));
    renderAt("/");
    expect(await screen.findByRole("alert")).toHaveTextContent("Cannot reach the RIFTWIRE backend");
  });
});

describe("Recordings", () => {
  it("shows the empty state", async () => {
    renderAt("/recordings");
    expect(await screen.findByText("No recordings yet")).toBeInTheDocument();
  });

  it("lists recordings with formatted details and thumbnails", async () => {
    m.listRecordings.mockResolvedValue(page([recording(), recording({ id: "rec-2", original_filename: "chase.mkv",
      status: "PROCESSING", duration_ms: null, width: null, height: null })]));
    renderAt("/recordings");
    const row = (await screen.findByText("heist.mp4")).closest("tr") as HTMLElement;
    expect(within(row).getByText("2:05")).toBeInTheDocument();
    expect(within(row).getByText("1920×1080")).toBeInTheDocument();
    expect(within(row).getByText("5.0 MB")).toBeInTheDocument();
    expect(row.querySelector("img.thumb")).toHaveAttribute("src", "/api/v1/recordings/rec-1/thumbnail");
    const row2 = screen.getByText("chase.mkv").closest("tr") as HTMLElement;
    expect(within(row2).getByText("Processing")).toBeInTheDocument();
  });

  it("searches by filename", async () => {
    const user = userEvent.setup();
    renderAt("/recordings");
    await user.type(await screen.findByLabelText("Search recordings"), "heist");
    await waitFor(() => expect(m.listRecordings).toHaveBeenLastCalledWith(
      expect.objectContaining({ q: "heist", offset: 0 })));
  });

  it("shows the detail view with frame rate and processing stages", async () => {
    m.getRecording.mockResolvedValue(detail([job(), job({ id: "j2", job_type: "METADATA" }),
      job({ id: "j3", job_type: "THUMBNAIL", status: "RUNNING", progress_percent: 40, completed_at: null })]));
    renderAt("/recordings/rec-1");
    expect(await screen.findByText("29.97 fps (30000/1001)")).toBeInTheDocument();
    expect(screen.getByLabelText("Thumbnail progress")).toHaveAttribute("aria-valuenow", "40");
  });
});

describe("Import workflow", () => {
  const file = () => new File([new Uint8Array(2048)], "gta.mp4", { type: "video/mp4" });

  it("uploads once, shows upload progress, then background processing", async () => {
    const user = userEvent.setup();
    let resolve!: (v: { recording: ReturnType<typeof recording>; job_id: string }) => void;
    let progress!: (l: number, t: number) => void;
    vi.mocked(uploadRecording).mockImplementation((_f, onProgress) => {
      progress = onProgress;
      return { promise: new Promise((r) => { resolve = r; }), abort: vi.fn() };
    });
    m.getRecording.mockResolvedValue(detail([job({ status: "RUNNING", progress_percent: 55, completed_at: null })],
      { status: "STAGED" }));

    renderAt("/import");
    await user.upload(screen.getByLabelText("Choose a recording file"), file());
    const button = screen.getByRole("button", { name: "Upload" });
    await user.click(button);
    await user.click(screen.getByRole("button", { name: "Uploading…" })); // repeated click ignored
    expect(uploadRecording).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Uploading…" })).toBeDisabled();

    act(() => progress(1024, 2048));
    expect(await screen.findByText(/1.0 KB of 2.0 KB uploaded/)).toBeInTheDocument();
    expect(screen.getByLabelText("Upload progress")).toHaveAttribute("aria-valuenow", "50");

    await act(async () => resolve({ recording: recording({ status: "STAGED" }), job_id: "job-1" }));
    expect(await screen.findByText("3 · Background processing")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByLabelText("Store & verify progress")).toHaveAttribute("aria-valuenow", "55"));
    expect(screen.getByRole("button", { name: "Upload" })).toBeDisabled(); // no file selected now
  });

  it("explains duplicates with a link to the existing recording", async () => {
    const user = userEvent.setup();
    vi.mocked(uploadRecording).mockImplementation(() => ({
      promise: Promise.reject(new ApiError(409, "duplicate_recording",
        "This recording was already imported as 'heist.mp4'.", { existing_recording_id: "rec-1" })),
      abort: vi.fn(),
    }));
    renderAt("/import");
    await user.upload(screen.getByLabelText("Choose a recording file"), file());
    await user.click(screen.getByRole("button", { name: "Upload" }));
    expect(await screen.findByText(/already imported/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open existing recording/ })).toHaveAttribute("href", "/recordings/rec-1");
  });

  it("shows validation errors from the backend", async () => {
    const user = userEvent.setup();
    vi.mocked(uploadRecording).mockImplementation(() => ({
      promise: Promise.reject(new ApiError(422, "invalid_media", "Invalid or unsupported media: no video stream found")),
      abort: vi.fn(),
    }));
    renderAt("/import");
    await user.upload(screen.getByLabelText("Choose a recording file"), file());
    await user.click(screen.getByRole("button", { name: "Upload" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Import rejected. Invalid or unsupported media");
  });
});

describe("Processing queue", () => {
  it("displays job status, progress, attempts and errors; retries failed jobs", async () => {
    const user = userEvent.setup();
    m.listJobs.mockResolvedValue(page([
      job({ id: "a", status: "RUNNING", progress_percent: 30, completed_at: null }),
      job({ id: "b", job_type: "THUMBNAIL", status: "FAILED", attempt_count: 3, last_error: "thumbnail extraction failed", retryable: true }),
      job({ id: "c", job_type: "METADATA", status: "RETRY_WAIT", attempt_count: 1 }),
    ]));
    m.retryJob.mockResolvedValue(job({ id: "b", status: "PENDING" }));
    renderAt("/queue");

    const running = await screen.findByTestId("job-a");
    expect(within(running).getByText("Running")).toBeInTheDocument();
    expect(within(running).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "30");
    expect(within(running).queryByRole("button")).not.toBeInTheDocument();

    const failed = screen.getByTestId("job-b");
    expect(within(failed).getByText("Failed")).toBeInTheDocument();
    expect(within(failed).getByText("3 / 3")).toBeInTheDocument();
    expect(within(failed).getByText("thumbnail extraction failed")).toBeInTheDocument();
    expect(within(screen.getByTestId("job-c")).getByText("Retry wait")).toBeInTheDocument();

    await user.click(within(failed).getByRole("button", { name: "Retry" }));
    expect(m.retryJob).toHaveBeenCalledWith("b");
  });

  it("shows the empty state", async () => {
    renderAt("/queue");
    expect(await screen.findByText("The queue is empty")).toBeInTheDocument();
  });
});

describe("System", () => {
  it("shows versions, data dir, and a missing FFmpeg clearly", async () => {
    m.systemStatus.mockResolvedValue(systemStatus({
      ffmpeg: { available: false, path: null, version: null, hwaccels: [], videotoolbox: false },
      worker: { state: "UNRESPONSIVE", id: "w", last_heartbeat_at: null, current_job_id: null, concurrency: 1 },
    }));
    renderAt("/system");
    expect(await screen.findByText("/Users/test/riftwire/data")).toBeInTheDocument();
    expect(screen.getByText(/brew install ffmpeg/)).toBeInTheDocument();
    expect(screen.getByText("Unresponsive")).toBeInTheDocument();
    expect(await screen.findByText("No errors recorded")).toBeInTheDocument();
  });
});

describe("Storage", () => {
  it("lists every location and its drive", async () => {
    renderAt("/storage");
    expect(await screen.findByRole("heading", { name: "Storage" })).toBeInTheDocument();
    for (const label of ["Recordings", "Temporary processing", "AI models", "Exports", "Brand assets"]) {
      expect(await screen.findByRole("cell", { name: label })).toBeInTheDocument();
    }
    expect(screen.getAllByText("APFS")).toHaveLength(5);
    expect(screen.getByText("Macintosh HD", { selector: "h3" })).toBeInTheDocument();
    expect(screen.getByText("9.0 GB")).toBeInTheDocument(); // headroom above the floor
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows an offline drive clearly and never as healthy", async () => {
    const base = storageStatus();
    m.storage.mockResolvedValue(storageStatus({
      locations: base.locations.map((l) => l.kind === "recordings"
        ? { ...l, path: "/Volumes/SSD/riftwire/recordings", online: false,
          reason: "volume_not_mounted", message: "the drive is not mounted", usage_bytes: null }
        : l),
      volumes: [...base.volumes, { volume_uuid: "vol-ssd", kinds: ["recordings"], online: false,
        label: null, mount_point: null, floor_bytes: 60 * 2 ** 30, total_bytes: null,
        free_bytes: null, reserved_bytes: 0, headroom_bytes: null, below_floor: null }],
    }));
    renderAt("/storage");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Storage offline");
    expect(alert).toHaveTextContent("Recordings (the drive is not mounted)");
    const row = document.querySelector('tr[data-kind="recordings"]') as HTMLElement;
    expect(within(row).getByText("Offline", { selector: ".badge" })).toBeInTheDocument();
    expect(screen.getAllByText("Online", { selector: ".badge" })).toHaveLength(4);
    expect(screen.getByText(/nothing is redirected to another drive/)).toBeInTheDocument();
  });

  it("shows migration history read-only", async () => {
    m.storage.mockResolvedValue(storageStatus({ migrations: [{ id: "m1", kind: "recordings",
      source_path: "/data/recordings", dest_path: "/Volumes/SSD/riftwire/recordings",
      status: "COPYING", total_files: 10, verified_files: 4, total_bytes: 2 ** 30,
      updated_at: "2026-11-24T05:00:00Z" }] }));
    renderAt("/storage");
    expect(await screen.findByText("Recent migrations")).toBeInTheDocument();
    expect(screen.getByText("4 / 10 files")).toBeInTheDocument();
    expect(screen.getByText(/Unfinished/)).toBeInTheDocument();
  });

  it("shows an error when the backend is unreachable", async () => {
    m.storage.mockRejectedValue(new ApiError(0, "network_error", "Cannot reach the RIFTWIRE backend."));
    renderAt("/storage");
    expect(await screen.findByRole("alert")).toHaveTextContent(/Cannot load storage status/);
  });
});
