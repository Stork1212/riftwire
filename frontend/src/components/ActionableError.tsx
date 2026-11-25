import { Link } from "react-router-dom";
import { ApiError } from "../services/api";

// Where the owner fixes each kind of rights refusal.
const FIX_LINK: Record<string, { to: string; label: string }> = {
  no_rights_record: { to: "/creators", label: "Open Creators & Rights" },
  not_attested: { to: "/creators", label: "Open Creators & Rights" },
  revoked: { to: "/creators", label: "Open Creators & Rights" },
  expired: { to: "/creators", label: "Open Creators & Rights" },
  not_yet_valid: { to: "/creators", label: "Open Creators & Rights" },
  creator_archived: { to: "/creators", label: "Open Creators & Rights" },
  scope_unsupported: { to: "/creators", label: "Open Creators & Rights" },
  use_not_in_scope: { to: "/creators", label: "Open Creators & Rights" },
  platform_permission_missing: { to: "/creators", label: "Add written platform permission" },
  creator_permission_needed: { to: "/creators", label: "Record the creator's permission" },
  acquisition_method_not_permitted: { to: "/creators", label: "Open Creators & Rights" },
  acquisition_method_unknown: { to: "/sources", label: "Open Sources" },
  creator_channel_required: { to: "/creators", label: "Set up the creator" },
  brand_channel_not_assigned: { to: "/sources", label: "Open Sources" },
};

/** An error with its reason and, for rights refusals, the next step and where to take it. */
export function ActionableError({ error, title = "Not possible" }: { error: unknown; title?: string }) {
  const e = error instanceof ApiError ? error : null;
  const msg = error instanceof Error ? error.message : String(error);
  const reason = e && typeof e.details.reason === "string" ? e.details.reason : e?.code;
  const fix = reason ? FIX_LINK[reason] : undefined;
  return (
    <div className="alert alert-bad" role="alert" data-reason={reason ?? undefined}>
      <strong>{title}.</strong> {msg}
      {fix && <> <Link to={fix.to}>{fix.label}</Link></>}
    </div>
  );
}
