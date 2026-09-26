import { useState } from "react";
import { useJobs, useSources, useDiscover, useScore } from "../api/hooks";
import { Loading, EmptyState, ErrorBanner } from "../components/Feedback";
import { ScoreBar } from "../components/ScoreBar";
import { StateBadge } from "../components/Badges";
import { Link } from "react-router-dom";

export function Jobs() {
  const [query, setQuery] = useState("");
  const [source, setSource] = useState("");
  const [minScore, setMinScore] = useState("");
  const { data: page, isLoading, error } = useJobs({
    query: query || undefined,
    sources: source ? [source] : undefined,
    min_score: minScore ? parseFloat(minScore) : undefined,
  });
  const { data: sources } = useSources();
  const discover = useDiscover();
  const score = useScore();

  // Discovery and scoring both run server-side to completion, so the click is
  // "in progress" for seconds. Both buttons say so and refuse further clicks,
  // rather than looking like a button that silently did nothing.
  const busy = discover.isPending || score.isPending;

  if (isLoading || !page) return <Loading />;
  return (
    <div>
      <h1 style={{ marginBottom: 24 }}>Jobs</h1>
      <div className="filters">
        <input placeholder="Search…" value={query} onChange={(e) => setQuery(e.target.value)} />
        <select aria-label="Source" value={source} onChange={(e) => setSource(e.target.value)}>
          <option value="">All sources</option>
          {(sources ?? []).map((src) => (
            <option key={src.slug} value={src.slug}>{src.slug}</option>
          ))}
        </select>
        <input type="number" placeholder="Min score" value={minScore} onChange={(e) => setMinScore(e.target.value)} step="0.05" min="0" max="1" />
        <button
          onClick={() => discover.mutate({ sources: source ? [source] : undefined })}
          disabled={busy}
        >
          {discover.isPending ? "Discovering…" : "Discover"}
        </button>
        <button className="secondary" onClick={() => score.mutate({})} disabled={busy}>
          {score.isPending ? "Scoring…" : "Score"}
        </button>
      </div>
      {discover.isError && <ErrorBanner message={describeError(discover.error)} />}
      {score.isError && <ErrorBanner message={describeError(score.error)} />}
      {discover.data && (
        <p style={{ marginBottom: 12, color: "var(--text-muted)" }}>
          {`${discover.data.jobs_discovered} discovered, ${discover.data.jobs_created} new`}
          {discover.data.failures.length ? ` · ${discover.data.failures.length} failure(s)` : ""}
        </p>
      )}
      {score.data && (
        <p style={{ marginBottom: 12, color: "var(--text-muted)" }}>
          {`${score.data.scored} scored`}
          {score.data.failed ? ` · ${score.data.failed} failed` : ""}
        </p>
      )}
      {page.items.length === 0 ? (
        <EmptyState
          title="No jobs yet"
          message="Run discovery to find jobs. It searches every enabled source, and you can narrow the run to the source selected above."
        />
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
      {error && <ErrorBanner message={describeError(error)} />}
      <p style={{ marginTop: 12, color: "var(--text-muted)" }}>{page.total} total · {page.items.length} shown</p>
    </div>
  );
}

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
