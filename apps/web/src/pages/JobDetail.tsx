import { useParams, Link } from "react-router-dom";
import { useJob } from "../api/hooks";
import { Loading, ErrorBanner } from "../components/Feedback";
import { ScoreBar } from "../components/ScoreBar";
import { StateBadge } from "../components/Badges";
import type { MissingRequirementView } from "../api/types";

export function JobDetail() {
  const { id } = useParams<{ id: string }>();
  const { data: job, isLoading, error } = useJob(id!);
  if (isLoading) return <Loading />;
  if (error) return <ErrorBanner message={String(error)} />;
  if (!job) return <ErrorBanner message="Job not found" />;
  return (
    <div>
      <Link to="/jobs">← Back to Jobs</Link>
      <h1 style={{ marginTop: 12 }}>{job.title}</h1>
      <p style={{ color: "var(--text-muted)", marginBottom: 16 }}>{job.company} · {job.location} · {job.remote_mode}</p>
      <div style={{ display: "flex", gap: 12, marginBottom: 16 }}>
        {job.recommendation && <StateBadge state={job.recommendation} />}
        {job.application_state && <StateBadge state={job.application_state} />}
        <span className="badge badge-muted">{job.verification}</span>
      </div>
      {job.score && (
        <div className="card">
          <h3>Fit Score: {(job.score.overall * 100).toFixed(0)}% (confidence {(job.score.confidence * 100).toFixed(0)}%)</h3>
          <div className="dimensions">
            {job.score.dimensions.map((d) => (
              <div key={d.dimension} className="dimension">
                <div className="header">
                  <span className="name">{d.dimension}</span>
                  <span className="score">{(d.score * 100).toFixed(0)}% · w={d.weight.toFixed(2)}{d.llm_adjusted ? " (LLM)" : ""}</span>
                </div>
                <ScoreBar score={d.score} />
                <div className="rationale">{d.rationale}</div>
              </div>
            ))}
          </div>
          {job.score.missing_requirements.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <h3>Missing Requirements</h3>
              <ul>{job.score.missing_requirements.map((m, i) => {
                const req = m as unknown as MissingRequirementView;
                return (
                  <li key={i}><span className={`badge ${req.severity === "hard" ? "badge-red" : "badge-yellow"}`}>{req.severity}</span> {req.requirement}</li>
                );
              })}</ul>
            </div>
          )}
          {job.score.uncertainties.length > 0 && (
            <div style={{ marginTop: 12 }}><h3>Uncertainties</h3><ul>{job.score.uncertainties.map((u, i) => <li key={i}>{u}</li>)}</ul></div>
          )}
        </div>
      )}
      <div className="card">
        <h3>Source Provenance</h3>
        <ul className="provenance">
          {job.source_records.map((src, i) => (
            <li key={i}>
              <span className={`badge ${src.tier === "direct_ats" ? "badge-green" : "badge-muted"}`}>{src.tier}</span>{" "}
              {src.source} — first seen {new Date(src.first_seen_at).toLocaleDateString()}, last seen {new Date(src.last_seen_at).toLocaleDateString()}
            </li>
          ))}
        </ul>
      </div>
      {job.description && (
        <div className="card">
          <h3>Description</h3>
          <div className="description">{job.description}</div>
        </div>
      )}
    </div>
  );
}
