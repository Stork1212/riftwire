# Interface direction after M2: a unified project workflow (proposal)

_Status: **direction for owner review, not approved work.** Nothing here is implemented
except the navigation grouping and the M2 building blocks named in §2. Written
2026-11-24 with the M2 review. M3 does not start until it is approved._

**Numbering.** In the approved proposal ([`PHASE2_PROPOSAL.md`](PHASE2_PROPOSAL.md) §3),
**M3 is "Permitted acquisition"**. The project workflow below is the interface those
milestones grow into:
- M3: acquisition methods;
- M4: signals;
- M5–M6: highlights;
- Phase 4: editing presets;
- Phase 5: publishing.

Each part becomes an active control only when its milestone delivers it.

## 1. Goal
One place to start work: **New project**. A project ties together:
- one source video (a pasted link or an imported file);
- its creator and rights;
- a game;
- the output channels (initially the owner-configured RumbleFrame channels);
- later, an editing preset.

The aim is a production system, not a manual clipping tool: every choice that can be made
automatically is pre-filled, and the owner confirms.

## 2. Workflow
| Step | What happens | Built in M2 (reusable) | Arrives with |
|---|---|---|---|
| 1. Source | Paste a link or drop a file | `LinkResolver`, Import modes, `/sources/resolve` | — |
| 2. Creator | Automatic match (Twitch login, known link, file hash); inline setup if missing | `matching` chain, inline creator setup | M3: official-API matching (YouTube channel id, title, duration) |
| 3. Rights | Shows the licence state and the **authorized uses**; refuses with the next step | `evaluate`, `authorized_uses`, actionable errors | — |
| 4. Footage | Permitted method only: the creator's file or the owner's platform download (upload) | upload-fed providers | M3: further *permitted* providers, each approved separately |
| 5. Game | Pre-selected from the source, or the owner's last choice | configurable games | — |
| 6. Output channels | Brand channels the source is assigned to, filtered by what the licence allows (platform, monetization) | brand channels, assignments, rights checks | Phase 5: publishing |
| 7. Editing preset | Choose a preset per output channel | — | Phase 4: hidden until presets exist |
| 8. Review & create | Summary with every rights decision; nothing runs without the owner's confirmation | — | M4–M6: analysis jobs (rights-gated) |

## 3. Rules for the interface
- **No misleading controls.** A feature that doesn't exist yet is not shown as an active
  button. It is either absent or described in plain text ("arrives with Phase 4"). The
  M2 Import page follows this: the Video URL path has no download button and says why.
- **Licence state is not authorization.** Badges say whether a licence is active; the
  *uses* it authorizes are shown separately. Edit and publish are always checked per
  clip, platform and brand channel.
- **Nothing brand-specific in code.** RumbleFrame is configuration: a brand channel the
  owner creates in Games & Channels. A future "default output channel" preference
  (proposed `brand_channels.is_default`, needs approval) pre-selects it, so RIFTWIRE can
  host other brands without code changes.
- **Navigation groups** (implemented in M2):
  - **Workspace:** Overview, Import, Sources, Recordings; later Projects.
  - **Management:** Creators & Rights, Games & Channels, Storage; later Presets.
  - **System:** Processing Queue, System.

## 4. Proposed data model (for approval with the milestone that needs it)
`projects`:
- `id`, `source_id`, `game_id`, `title`, `status` (draft, ready, archived);
- `created_at`, `updated_at`.

`project_outputs`:
- `project_id`, `brand_channel_id`;
- `preset_id` (nullable until Phase 4).

Every project action re-uses the M2 rights checks; no project field can override them.

## 5. Open questions for the owner
1. Should a project be created automatically when a link is pasted on Import, with the
   owner reviewing it afterwards, or only on explicit confirmation?
2. Default output channel: one global default, or one per game?
3. Should projects be introduced with M3 (acquisition) or with M4 (first analysis jobs)?
