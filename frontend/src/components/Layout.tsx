import { NavLink, Outlet } from "react-router-dom";

interface NavItem { to: string; label: string; end?: boolean }

const NAV: { group: string; items: NavItem[] }[] = [
  { group: "Workspace", items: [
    { to: "/", label: "Overview", end: true },
    { to: "/import", label: "Import" },
    { to: "/sources", label: "Sources" },
    { to: "/recordings", label: "Recordings" },
  ] },
  { group: "Management", items: [
    { to: "/creators", label: "Creators & Rights" },
    { to: "/catalog", label: "Games & Channels" },
    { to: "/storage", label: "Storage" },
  ] },
  { group: "System", items: [
    { to: "/queue", label: "Processing Queue" },
    { to: "/system", label: "System" },
  ] },
];

export function Logo() {
  return (
    <div className="logo" aria-label="RIFTWIRE">
      <svg viewBox="0 0 32 32" width="26" height="26" aria-hidden="true">
        <rect width="32" height="32" rx="7" fill="var(--panel-2)" />
        <path d="M9 23 15 9h3l-3 7h6l-8 9 2-6z" fill="var(--accent)" />
      </svg>
      <span className="logo-word">RIFT<span>WIRE</span></span>
    </div>
  );
}

export function Layout() {
  return (
    <div className="shell">
      <aside className="sidebar">
        <Logo />
        <nav className="nav">
          {NAV.map((g) => (
            <div key={g.group} className="nav-group" role="group" aria-label={g.group}>
              <div className="nav-group-title">{g.group}</div>
              {g.items.map((n) => (
                <NavLink key={n.to} to={n.to} end={n.end}
                  className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
                  {n.label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-foot">Phase 2 · Sources &amp; Signals</div>
      </aside>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
