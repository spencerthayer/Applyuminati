import { NavLink, Outlet } from "react-router-dom";
import { useHealth } from "../api/hooks";

export function Layout({ children }: { children: React.ReactNode }) {
  const { data: health } = useHealth();
  const status = health?.status ?? "—";

  return (
    <div className="app-layout">
      <aside className="sidebar">
        <h2>Applyuminati</h2>
        <nav>
          <NavLink to="/" end className={({ isActive }) => (isActive ? "active" : "")}>Dashboard</NavLink>
          <NavLink to="/jobs" className={({ isActive }) => (isActive ? "active" : "")}>Jobs</NavLink>
          <NavLink to="/profile" className={({ isActive }) => (isActive ? "active" : "")}>Profile</NavLink>
          <NavLink to="/settings" className={({ isActive }) => (isActive ? "active" : "")}>Settings</NavLink>
        </nav>
        <div style={{ padding: "16px", fontSize: 12, color: "var(--text-muted)" }}>
          API: {status}
        </div>
      </aside>
      <main className="main">{children ?? <Outlet />}</main>
    </div>
  );
}
