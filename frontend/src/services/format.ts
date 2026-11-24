export function formatBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`;
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  const total = Math.round(ms / 1000);
  if (ms < 1000) return `${(ms / 1000).toFixed(1)}s`;
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (x: number) => String(x).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

export function formatTotalDuration(ms: number): string {
  const totalMin = Math.round(ms / 60000);
  if (ms > 0 && totalMin === 0) return `${Math.round(ms / 1000)}s`;
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? "—"
    : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function formatResolution(w: number | null, h: number | null): string {
  return w && h ? `${w}×${h}` : "—";
}

export function formatFps(fps: number | null, rational: string | null, vfr: boolean | null): string {
  if (!fps) return "—";
  const v = Number.isInteger(fps) ? String(fps) : fps.toFixed(3).replace(/0+$/, "");
  return `${v} fps${rational && !Number.isInteger(fps) ? ` (${rational})` : ""}${vfr ? " · VFR" : ""}`;
}
