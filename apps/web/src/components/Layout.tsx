import { NavLink, Outlet } from "react-router-dom";
import { useHealth, useInbox, useLogout, useSession } from "../api/hooks";

export function Layout({ children }: { children: React.ReactNode }) {
  const { data: health } = useHealth();
  const { data: session } = useSession();
  const { data: inbox } = useInbox();
  const logout = useLogout();
  const status = health?.status ?? "—";
  const waiting = inbox?.length ?? 0;

  return (
    <div className="app-layout">
      <aside className="sidebar">
        <h2>Applyuminati</h2>
        <nav>
          <NavLink to="/" end className={({ isActive }) => (isActive ? "active" : "")}>Dashboard</NavLink>
          <NavLink to="/needs-you" className={({ isActive }) => (isActive ? "active" : "")}>
            Needs you{waiting ? ` (${waiting})` : ""}
          </NavLink>
          <NavLink to="/jobs" className={({ isActive }) => (isActive ? "active" : "")}>Jobs</NavLink>
          <NavLink to="/profile" className={({ isActive }) => (isActive ? "active" : "")}>Profile</NavLink>
          <NavLink to="/settings" className={({ isActive }) => (isActive ? "active" : "")}>Settings</NavLink>
        </nav>
        <div style={{ padding: "16px", fontSize: 12, color: "var(--text-muted)" }}>
          <div>API: {status}</div>
          {session?.required ? (
            <button
              type="button"
              className="link-button"
              onClick={() => logout.mutate()}
              disabled={logout.isPending}
            >
              Sign out
            </button>
          ) : null}
        </div>
      </aside>
      <main className="main">{children ?? <Outlet />}</main>
    </div>
  );
}
