import { useState } from "react";
import { useJobs } from "../api/hooks";
import { Loading, EmptyState } from "../components/Feedback";
import { ScoreBar } from "../components/ScoreBar";
import { StateBadge } from "../components/Badges";
import { Link } from "react-router-dom";

export function Jobs() {
  const [query, setQuery] = useState("");
  const [source, setSource] = useState("");
  const [minScore, setMinScore] = useState("");
  const { data: page, isLoading } = useJobs({
    query: query || undefined,
    sources: source ? [source] : undefined,
    min_score: minScore ? parseFloat(minScore) : undefined,
  });
  if (isLoading || !page) return <Loading />;
  return (
    <div>
      <h1 style={{ marginBottom: 24 }}>Jobs</h1>
      <div className="filters">
        <input placeholder="Search…" value={query} onChange={(e) => setQuery(e.target.value)} />
        <select value={source} onChange={(e) => setSource(e.target.value)}>
          <option value="">All sources</option>
        </select>
        <input type="number" placeholder="Min score" value={minScore} onChange={(e) => setMinScore(e.target.value)} step="0.05" min="0" max="1" />
      </div>
      {page.items.length === 0 ? (
        <EmptyState title="No jobs yet" message="Run discovery to find jobs: use the CLI command 'applyuminati jobs discover' or enable a source in Settings." />
      ) : (
        <table>
          <thead><tr><th>Title</th><th>Company</th><th>Location</th><th>Source(s)</th><th>Score</th><th>Rec.</th><th>State</th></tr></thead>
          <tbody>
            {page.items.map((job) => (
              <tr key={job.id}>
                <td><Link to={`/jobs/${job.id}`}>{job.title}</Link></td>
                <td>{job.company}</td>
                <td>{job.location}</td>
                <td>{job.sources.join(", ")}</td>
                <td>{job.fit_score != null ? <ScoreBar score={job.fit_score} /> : "—"}</td>
                <td>{job.recommendation ?? "—"}</td>
                <td>{job.application_state ? <StateBadge state={job.application_state} /> : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p style={{ marginTop: 12, color: "var(--text-muted)" }}>{page.total} total · {page.items.length} shown</p>
    </div>
  );
}
