import type { Page } from "../types/api";
import type {
  Acquired,
  AcquisitionMethodInfo,
  BrandChannel,
  Creator,
  CreatorChannel,
  Decision,
  Evidence,
  Game,
  ProvenanceEvent,
  Resolved,
  RightsInput,
  RightsRecord,
  Scope,
  Source,
  SourceDetail,
  Use,
} from "../types/rights";
import { qs, request } from "./api";

const json = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { Accept: "application/json", "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
const enc = encodeURIComponent;

export const rightsApi = {
  // catalog
  listGames: () => request<Game[]>("/api/v1/games"),
  createGame: (slug: string, name: string) => request<Game>("/api/v1/games", json("POST", { slug, name })),
  updateGame: (id: string, patch: Partial<Pick<Game, "name" | "active">>) =>
    request<Game>(`/api/v1/games/${enc(id)}`, json("PATCH", patch)),
  listBrandChannels: () => request<BrandChannel[]>("/api/v1/brand-channels"),
  createBrandChannel: (b: { slug: string; name: string; platform: string; handle?: string; monetized: boolean }) =>
    request<BrandChannel>("/api/v1/brand-channels", json("POST", b)),
  updateBrandChannel: (id: string, patch: Partial<Pick<BrandChannel, "name" | "monetized" | "active">>) =>
    request<BrandChannel>(`/api/v1/brand-channels/${enc(id)}`, json("PATCH", patch)),

  // creators
  listCreators: (includeArchived = false) =>
    request<Page<Creator>>(`/api/v1/creators${qs({ include_archived: includeArchived ? "true" : undefined, limit: 200 })}`),
  createCreator: (c: { display_name: string; relationship: string; contact?: string }) =>
    request<Creator>("/api/v1/creators", json("POST", c)),
  updateCreator: (id: string, patch: { archived?: boolean; display_name?: string }) =>
    request<Creator>(`/api/v1/creators/${enc(id)}`, json("PATCH", patch)),
  addChannel: (creatorId: string, c: { platform: string; handle?: string; platform_channel_id?: string }) =>
    request<CreatorChannel>(`/api/v1/creators/${enc(creatorId)}/channels`, json("POST", c)),
  creatorRights: (creatorId: string) => request<RightsRecord[]>(`/api/v1/creators/${enc(creatorId)}/rights`),

  // rights
  createRights: (r: RightsInput) => request<RightsRecord>("/api/v1/rights", json("POST", r)),
  supersedeRights: (id: string, body: { scope: Scope; attestation_text: string; valid_until?: string | null }) =>
    request<RightsRecord>(`/api/v1/rights/${enc(id)}/supersede`, json("POST", body)),
  revokeRights: (id: string, reason: string) =>
    request<RightsRecord>(`/api/v1/rights/${enc(id)}/revoke`, json("POST", { reason })),
  addEvidenceNote: (id: string, kind: string, reference: string) =>
    request<Evidence>(`/api/v1/rights/${enc(id)}/evidence${qs({ kind, reference })}`, { method: "POST" }),
  addEvidenceFile: (id: string, kind: string, file: File, reference?: string) =>
    request<Evidence>(`/api/v1/rights/${enc(id)}/evidence${qs({ kind, reference, filename: file.name })}`, {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/octet-stream" },
      body: file,
    }),

  // sources
  acquisitionMethods: () => request<AcquisitionMethodInfo[]>("/api/v1/acquisition-methods"),
  resolve: (url: string) => request<Resolved>("/api/v1/sources/resolve", json("POST", { url })),
  listSources: (p: { status?: string; limit?: number; offset?: number } = {}) =>
    request<Page<Source>>(`/api/v1/sources${qs({ ...p })}`),
  createSource: (s: { url?: string; kind?: string; creator_channel_id?: string; game_id?: string;
    title_user?: string; acquisition_method?: string; brand_channel_ids?: string[] }) =>
    request<Source>("/api/v1/sources", json("POST", s)),
  getSource: (id: string) => request<SourceDetail>(`/api/v1/sources/${enc(id)}`),
  updateSource: (id: string, patch: { status?: string; game_id?: string; clear_game?: boolean; title_user?: string }) =>
    request<Source>(`/api/v1/sources/${enc(id)}`, json("PATCH", patch)),
  setAssignments: (id: string, brand_channel_ids: string[]) =>
    request<Source>(`/api/v1/sources/${enc(id)}/assignments`, json("PUT", { brand_channel_ids })),
  acquireSource: (id: string) =>
    request<Acquired>(`/api/v1/sources/${enc(id)}/acquire`, { method: "POST" }),
  attachRecording: (id: string, recording_id: string, acquisition_method?: string) =>
    request<Source>(`/api/v1/sources/${enc(id)}/recordings`,
      json("POST", acquisition_method ? { recording_id, acquisition_method } : { recording_id })),
  rightsCheck: (id: string, use: Use, p: { platform?: string; brand_channel_id?: string; clip_ms?: number } = {}) =>
    request<Decision>(`/api/v1/sources/${enc(id)}/rights-check${qs({ use, ...p })}`),
  provenance: (id: string) => request<ProvenanceEvent[]>(`/api/v1/sources/${enc(id)}/provenance`),
};
