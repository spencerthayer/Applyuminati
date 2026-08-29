import { useDashboard } from "../api/hooks";
import { Loading } from "../components/Feedback";
import { ScoreBar } from "../components/ScoreBar";

export function Dashboard() {
  const { data: dash, isLoading } = useDashboard();
  if (isLoading || !dash) return <Loading />;
  return (
    <div>
      <h1 style={{ marginBottom: 24 }}>Dashboard</h1>
      <div className="grid grid-4">
        <div className="card stat"><div className="num">{dash.total_jobs}</div><div className="label">Discovered</div></div>
        <div className="card stat"><div className="num">{dash.shortlisted}</div><div className="label">Shortlisted</div></div>
        <div className="card stat"><div className="num">{dash.ready}</div><div className="label">Ready</div></div>
        <div className="card stat"><div className="num">{dash.submitted}</div><div className="label">Submitted</div></div>
        <div className="card stat"><div className="num">{dash.needs_attention}</div><div className="label">Needs Attention</div></div>
        <div className="card stat"><div className="num">{dash.scored}</div><div className="label">Scored</div></div>
      </div>
      {dash.by_recommendation && Object.keys(dash.by_recommendation).length > 0 && (
        <div className="card">
          <h3>By Recommendation</h3>
          {Object.entries(dash.by_recommendation).map(([rec, count]) => (
            <div key={rec} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
              <span style={{ width: 80 }}>{rec}</span>
              <ScoreBar score={count} max={dash.total_jobs || 1} />
              <span style={{ color: "var(--text-muted)" }}>{count}</span>
            </div>
          ))}
        </div>
      )}
      {dash.by_source && Object.keys(dash.by_source).length > 0 && (
        <div className="card">
          <h3>By Source</h3>
          <table>
            <tbody>
              {Object.entries(dash.by_source).map(([src, count]) => (
                <tr key={src}><td>{src}</td><td>{count}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {dash.recent_activity && dash.recent_activity.length > 0 && (
        <div className="card">
          <h3>Recent Activity</h3>
          <table>
            <tbody>
              {dash.recent_activity.slice(0, 10).map((item, i) => (
                <tr key={i}><td>{new Date(item.at).toLocaleString()}</td><td>{item.summary}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {dash.latest_run && (
        <div className="card">
          <h3>Latest Run</h3>
          <p>{dash.latest_run.kind} — {dash.latest_run.state}</p>
          {dash.latest_run.failures.length > 0 && (
            <ul>{dash.latest_run.failures.map((f, i) => <li key={i}>{f}</li>)}</ul>
          )}
        </div>
      )}
    </div>
  );
}
