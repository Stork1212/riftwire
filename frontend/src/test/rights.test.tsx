import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AppRoutes } from "../App";
import { ApiError, api } from "../services/api";
import { rightsApi } from "../services/rightsApi";
import type { Creator, Decision, RightsRecord, Source, SourceDetail } from "../types/rights";

vi.mock("../services/api", async (orig) => {
  const actual = await orig<typeof import("../services/api")>();
  return { ...actual, api: { listRecordings: vi.fn() }, uploadRecording: vi.fn() };
});
vi.mock("../services/rightsApi", () => ({
  rightsApi: Object.fromEntries([
    "listGames", "createGame", "updateGame", "listBrandChannels", "createBrandChannel",
    "updateBrandChannel", "listCreators", "createCreator", "updateCreator", "addChannel",
    "creatorRights", "createRights", "supersedeRights", "revokeRights", "addEvidenceNote",
    "addEvidenceFile", "acquisitionMethods", "resolve", "listSources", "createSource", "getSource",
    "updateSource", "setAssignments", "attachRecording", "rightsCheck", "provenance",
  ].map((k) => [k, vi.fn()])),
}));

const r = vi.mocked(rightsApi);
const page = <T,>(items: T[]) => ({ items, total: items.length, limit: 50, offset: 0 });
const T = "2026-11-24T10:00:00Z";

const creator = (over: Partial<Creator> = {}): Creator => ({
  id: "cr-1", display_name: "Some Streamer", relationship: "partner", contact: null, notes: null,
  archived: false, created_at: T, updated_at: T, current_rights: 0,
  channels: [{ id: "ch-1", creator_id: "cr-1", platform: "twitch", handle: "somestreamer",
    platform_channel_id: null, url_canonical: null, verified_by_owner_at: T, created_at: T }],
  ...over,
});

const source = (over: Partial<Source> = {}): Source => ({
  id: "src-1", kind: "twitch", platform_video_id: "Clip", url_canonical: "https://clips.twitch.tv/Clip",
  start_hint_ms: null, creator_channel_id: "ch-1", creator_id: "cr-1", creator_name: "Some Streamer",
  channel_label: "twitch: somestreamer", title_user: null, game_id: null, game_name: null,
  status: "active", acquisition_method: "platform_export", created_at: T, updated_at: T,
  rights_status: "ok", brand_channel_ids: [], recording_count: 0, ...over,
});

const decision = (over: Partial<Decision> = {}): Decision => ({
  allowed: true, reason: "ok", message: "authorized", use: "analyze", source_id: "src-1",
  record_id: "rr-1", version: 1, obligations: [], ...over,
});

const record = (over: Partial<RightsRecord> = {}): RightsRecord => ({
  id: "rr-1", lineage_id: "l-1", version: 1, creator_id: "cr-1", basis: "licence", coverage: "creator",
  scope: { v: 1, uses: ["analyze"], acquisition_methods: ["platform_export"], publish_platforms: [],
    brand_channels: "*", monetization: false, attribution_required: null, max_clip_seconds: null,
    territories: null, notes: "" },
  scope_sha256: "0".repeat(64), valid_from: T, valid_until: null, attestation_text: "Written licence",
  attested_at: T, superseded_by_id: null, revoked_at: null, revoked_reason: null, created_at: T,
  state: "ok", channel_ids: [], source_ids: [], evidence: [], ...over,
});

function renderAt(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AppRoutes /></MemoryRouter>);
}

beforeEach(() => {
  vi.clearAllMocks();
  r.listGames.mockResolvedValue([]);
  r.listBrandChannels.mockResolvedValue([]);
  r.listCreators.mockResolvedValue(page([creator()]));
  r.listSources.mockResolvedValue(page([]));
  r.creatorRights.mockResolvedValue([]);
  vi.mocked(api.listRecordings).mockResolvedValue(page([]));
});

describe("Sources", () => {
  it("shows an empty state", async () => {
    renderAt("/sources");
    expect(await screen.findByText("No sources yet")).toBeInTheDocument();
  });

  it("checks a pasted link, shows the automatic match and creates the source", async () => {
    r.resolve.mockResolvedValue({
      platform: "twitch", media: "clip", video_id: "Clip", canonical_url: "https://clips.twitch.tv/Clip",
      start_ms: null, channel_login: "somestreamer", existing_source: null,
      match: { status: "matched", matcher: "twitch_login", reason: "", creator_channel_id: "ch-1", candidates: ["ch-1"] },
      channel: creator().channels[0] ?? null, creator_name: "Some Streamer", covered: true, covering_record_ids: ["rr-1"],
    });
    r.createSource.mockResolvedValue(source());
    r.getSource.mockResolvedValue({ ...source(), analyze: decision(), acquire: decision({ use: "acquire" }),
      records: [], recordings: [] } as SourceDetail);
    r.provenance.mockResolvedValue([]);
    renderAt("/sources");
    await userEvent.type(screen.getByLabelText("Video link"), "https://www.twitch.tv/somestreamer/clip/Clip");
    await userEvent.click(screen.getByRole("button", { name: "Check link" }));
    const box = await screen.findByTestId("resolve-result");
    expect(within(box).getByText("Some Streamer")).toBeInTheDocument();
    expect(within(box).getByText("(matched automatically)")).toBeInTheDocument();
    expect(box.querySelector('[data-rights="ok"]')).not.toBeNull();
    await userEvent.click(within(box).getByRole("button", { name: "Create source" }));
    await waitFor(() => expect(r.createSource).toHaveBeenCalledWith({
      url: "https://www.twitch.tv/somestreamer/clip/Clip", creator_channel_id: "ch-1", game_id: undefined }));
    expect(await screen.findByText("History (provenance)")).toBeInTheDocument();
  });

  it("shows a clear refusal for unsupported links", async () => {
    r.resolve.mockRejectedValue(new ApiError(422, "unsupported_host", "Only YouTube and Twitch video links are accepted."));
    renderAt("/sources");
    await userEvent.type(screen.getByLabelText("Video link"), "https://vimeo.com/1");
    await userEvent.click(screen.getByRole("button", { name: "Check link" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Only YouTube and Twitch");
  });

  it("requires a channel choice when the link does not name one", async () => {
    r.resolve.mockResolvedValue({
      platform: "youtube", media: "video", video_id: "dQw4w9WgXcQ", canonical_url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
      start_ms: null, channel_login: null, existing_source: null,
      match: { status: "unmatched", matcher: null, reason: "YouTube links do not name their channel", creator_channel_id: null, candidates: [] },
      channel: null, creator_name: null, covered: false, covering_record_ids: [],
    });
    renderAt("/sources");
    await userEvent.type(screen.getByLabelText("Video link"), "https://youtu.be/dQw4w9WgXcQ");
    await userEvent.click(screen.getByRole("button", { name: "Check link" }));
    const box = await screen.findByTestId("resolve-result");
    expect(within(box).getByRole("button", { name: "Create source" })).toBeDisabled();
    expect(within(box).getByLabelText("Creator channel")).toBeInTheDocument();
  });
});

describe("Source detail", () => {
  it("shows refused rights with the reason", async () => {
    r.getSource.mockResolvedValue({
      ...source({ rights_status: "revoked" }),
      analyze: decision({ allowed: false, reason: "revoked", message: "the rights were revoked", record_id: null, version: null }),
      acquire: decision({ allowed: false, reason: "revoked", message: "the rights were revoked", use: "acquire" }),
      records: [record({ state: "revoked", revoked_at: T, revoked_reason: "withdrawn" })],
      recordings: [],
    } as SourceDetail);
    r.provenance.mockResolvedValue([{ id: 1, entity: "rights", entity_id: "rr-1", action: "revoked",
      actor: "owner", details: { reason: "withdrawn" }, at: T }]);
    renderAt("/sources/src-1");
    const refused = await screen.findAllByText(/the rights were revoked/);
    expect(refused.length).toBeGreaterThan(0);
    expect(document.querySelector('[data-reason="revoked"]')).not.toBeNull();
    expect(await screen.findByText(/— withdrawn/)).toBeInTheDocument();
  });

  it("shows a load error", async () => {
    r.getSource.mockRejectedValue(new ApiError(404, "not_found", "Source not found"));
    r.provenance.mockResolvedValue([]);
    renderAt("/sources/nope");
    expect(await screen.findByRole("alert")).toHaveTextContent("Source not found");
  });
});

describe("Creators & rights", () => {
  it("attests a standing licence only after explicit confirmation", async () => {
    r.createRights.mockResolvedValue(record());
    renderAt("/creators");
    expect(await screen.findByText("No licence yet")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "New licence" }));
    const form = screen.getByRole("form", { name: "New licence" });
    const submit = within(form).getByRole("button", { name: "Attest licence" });
    expect(submit).toBeDisabled();
    await userEvent.type(within(form).getByRole("textbox", { name: /Attestation/ }),
      "The creator granted a written licence on 2026-11-20.");
    expect(submit).toBeDisabled();                                  // not yet confirmed
    await userEvent.click(within(form).getByRole("checkbox", { name: /I confirm/ }));
    await userEvent.click(submit);
    await waitFor(() => expect(r.createRights).toHaveBeenCalled());
    const arg = r.createRights.mock.calls[0]![0];
    expect(arg.coverage).toBe("creator");
    expect(arg.scope.v).toBe(1);
    expect(arg.scope.uses).toEqual(["acquire", "analyze", "edit", "publish"]);
    expect(arg.attestation_text).toContain("written licence");
  });

  it("lists licences with revoke and edit for the current version only", async () => {
    r.creatorRights.mockResolvedValue([
      record({ id: "rr-2", version: 2 }),
      record({ id: "rr-1", version: 1, state: "superseded", superseded_by_id: "rr-2" }),
    ]);
    renderAt("/creators");
    const v2 = await screen.findByTestId("record-rr-2");
    const v1 = screen.getByTestId("record-rr-1");
    expect(within(v2).getByRole("button", { name: "Revoke" })).toBeInTheDocument();
    expect(within(v1).queryByRole("button", { name: "Revoke" })).toBeNull();
    expect(within(v1).getByText("Superseded")).toBeInTheDocument();
  });

  it("shows an empty state when there are no creators", async () => {
    r.listCreators.mockResolvedValue(page([]));
    renderAt("/creators");
    expect(await screen.findByText("No creators yet")).toBeInTheDocument();
  });
});

describe("Games & channels", () => {
  it("creates a game and a brand channel from the dashboard (nothing is seeded)", async () => {
    r.createGame.mockResolvedValue({ id: "g-1", slug: "gta-vi", name: "GTA VI", active: true, created_at: T });
    r.createBrandChannel.mockResolvedValue({ id: "b-1", slug: "rumbleframe-youtube-shorts", name: "RumbleFrame",
      platform: "youtube_shorts", handle: null, monetized: false, active: true, created_at: T });
    renderAt("/catalog");
    expect(await screen.findByText("No games yet")).toBeInTheDocument();
    expect(screen.getByText("No brand channels yet")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Game name"), "GTA VI");
    await userEvent.click(screen.getByRole("button", { name: "Add game" }));
    await waitFor(() => expect(r.createGame).toHaveBeenCalledWith("gta-vi", "GTA VI"));
    await userEvent.type(screen.getByLabelText("Brand name"), "RumbleFrame");
    await userEvent.click(screen.getByRole("button", { name: "Add channel" }));
    await waitFor(() => expect(r.createBrandChannel).toHaveBeenCalledWith({
      slug: "rumbleframe-youtube-shorts", name: "RumbleFrame", platform: "youtube_shorts",
      handle: undefined, monetized: false }));
  });

  it("shows load errors", async () => {
    r.listGames.mockRejectedValue(new ApiError(0, "network_error", "Cannot reach the RIFTWIRE backend."));
    renderAt("/catalog");
    expect(await screen.findByText(/Could not load games/)).toBeInTheDocument();
  });
});
