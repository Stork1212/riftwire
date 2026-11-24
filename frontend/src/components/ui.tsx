import type { ReactNode } from "react";

type Tone = "ok" | "warn" | "bad" | "info" | "muted" | "accent";

const STATUS_TONE: Record<string, Tone> = {
  READY: "ok",
  COMPLETED: "ok",
  RUNNING: "info",
  PROCESSING: "info",
  STAGED: "muted",
  PENDING: "muted",
  RETRY_WAIT: "warn",
  STOPPING: "warn",
  UNRESPONSIVE: "bad",
  FAILED: "bad",
  CANCELLED: "muted",
  STOPPED: "muted",
  NEVER_STARTED: "muted",
  ONLINE: "ok",
  OFFLINE: "bad",
};

const STATUS_LABEL: Record<string, string> = {
  RETRY_WAIT: "Retry wait",
  NEVER_STARTED: "Not started",
};

export function StatusBadge({ status }: { status: string }) {
  const tone = STATUS_TONE[status] ?? "muted";
  const label = STATUS_LABEL[status] ?? status.charAt(0) + status.slice(1).toLowerCase();
  return (
    <span className={`badge badge-${tone}`} data-status={status}>
      <span className="badge-dot" aria-hidden="true" />
      {label}
    </span>
  );
}

export function ProgressBar({ value, label, tone = "accent" }: { value: number; label: string; tone?: Tone }) {
  const v = Math.max(0, Math.min(100, value));
  return (
    <div className="progress" role="progressbar" aria-label={label} aria-valuenow={Math.round(v)}
      aria-valuemin={0} aria-valuemax={100}>
      <div className={`progress-fill tone-${tone}`} style={{ width: `${v}%` }} />
    </div>
  );
}

export function StatCard({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: ReactNode; tone?: Tone }) {
  return (
    <div className={`stat ${tone ? `stat-${tone}` : ""}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {hint !== undefined && <div className="stat-hint">{hint}</div>}
    </div>
  );
}

/** Page title row: title, optional description and eyebrow (e.g. a back link), and
 *  actions aligned to the right. Every page uses it, so spacing is identical everywhere. */
export function PageHeader({ title, description, eyebrow, actions }: {
  title: ReactNode; description?: ReactNode; eyebrow?: ReactNode; actions?: ReactNode;
}) {
  return (
    <div className="page-head">
      <div>
        {eyebrow && <div className="eyebrow">{eyebrow}</div>}
        <h1 className="wrap-anywhere">{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="toolbar">{actions}</div>}
    </div>
  );
}

/** A labelled form control with optional help text underneath. */
export function Field({ label, hint, children }: { label: ReactNode; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="muted small">{hint}</span>}
    </label>
  );
}

export function Panel({ title, actions, children }: { title?: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="panel">
      {(title || actions) && (
        <header className="panel-head">
          {title && <h2>{title}</h2>}
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <div className="empty-title">{title}</div>
      {children && <div className="empty-body">{children}</div>}
    </div>
  );
}

export function ErrorBanner({ error, title = "Something went wrong" }: { error: Error | string; title?: string }) {
  const msg = typeof error === "string" ? error : error.message;
  return (
    <div className="alert alert-bad" role="alert">
      <strong>{title}.</strong> {msg}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return <div className="loading" aria-busy="true">{label}</div>;
}

export function Pagination({ total, limit, offset, onChange }: {
  total: number; limit: number; offset: number; onChange: (offset: number) => void;
}) {
  if (total <= limit) return null;
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.ceil(total / limit);
  return (
    <nav className="pager" aria-label="Pagination">
      <button className="btn btn-ghost" disabled={offset === 0}
        onClick={() => onChange(Math.max(0, offset - limit))}>← Prev</button>
      <span>Page {page} of {pages} · {total} total</span>
      <button className="btn btn-ghost" disabled={offset + limit >= total}
        onClick={() => onChange(offset + limit)}>Next →</button>
    </nav>
  );
}
