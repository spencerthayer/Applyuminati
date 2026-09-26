import { useState } from "react";
import { useParams, Link } from "react-router-dom";
import { useApplyToJob, useJob } from "../api/hooks";
import { Loading, ErrorBanner } from "../components/Feedback";
import { ScoreBar } from "../components/ScoreBar";
import { StateBadge } from "../components/Badges";
import type { ApiError } from "../api/client";
import { EXECUTION_MODES, type ExecutionMode, type MissingRequirementView } from "../api/types";

interface ApplyOutcome {
  message: string;
  hint: string;
  link: string;
  label: string;
}

function applyOutcome(error: ApiError, jobId: string): ApplyOutcome {
  // Both of these are ordinary outcomes a user hits, not bugs: one means the
  // work is already underway, the other that there is nothing to apply with yet.
  if (error.status === 409) {
    return {
      message: "An application for this job is already in progress.",
      hint: "Pick up where it left off rather than starting a second one.",
      link: `/applications?job=${encodeURIComponent(jobId)}`,
      label: "View the application",
    };
  }
  if (error.status === 400) {
    return {
      message: error.message,
      hint: "Applications are written from your career profile, so it has to exist first.",
      link: "/profile",
      label: "Import a profile",
    };
  }
  return {
    message: error.message,
    hint: error.recovery,
    link: "/applications",
    label: "Applications",
  };
}

export function JobDetail() {
  const { id } = useParams<{ id: string }>();
  const { data: job, isLoading, error } = useJob(id!);
  const apply = useApplyToJob();
  const [mode, setMode] = useState<"" | ExecutionMode>("");
  if (isLoading) return <Loading />;
  if (error) return <ErrorBanner message={String(error)} />;
  if (!job) return <ErrorBanner message="Job not found" />;
  const outcome = apply.error ? applyOutcome(apply.error, job.id) : null;
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
      <div className="card">
        <h3>Apply</h3>
        <div style={{ display: "flex", gap: 12, alignItems: "flex-end" }}>
          <div>
            <label htmlFor="execution-mode">Execution mode</label>
            <select
              id="execution-mode"
              value={mode}
              disabled={apply.isPending}
              onChange={(e) => setMode(e.target.value as "" | ExecutionMode)}
            >
              <option value="">Server default</option>
              {EXECUTION_MODES.map((value) => (
                <option key={value} value={value}>{value}</option>
              ))}
            </select>
          </div>
          <button onClick={() => apply.mutate({ jobId: job.id, mode: mode || null })} disabled={apply.isPending}>
            {apply.isPending ? "Applying…" : "Apply"}
          </button>
        </div>
        {apply.isSuccess && apply.data && (
          <p style={{ marginTop: 12 }}>
            Application started — attempt <strong>{apply.data.attempt_id}</strong>{" "}
            ({apply.data.state} via {apply.data.driver}).{" "}
            <Link to={`/applications?job=${encodeURIComponent(job.id)}`}>View the application</Link>
          </p>
        )}
        {outcome && (
          <div className="error" style={{ marginTop: 12 }}>
            <div>{outcome.message}</div>
            <div style={{ marginTop: 4 }}>{outcome.hint}</div>
            <div style={{ marginTop: 8 }}>
              <Link to={outcome.link}>{outcome.label}</Link>
            </div>
          </div>
        )}
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
