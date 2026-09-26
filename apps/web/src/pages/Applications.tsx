import { Link, useParams, useSearchParams } from "react-router-dom";
import { useState } from "react";
import { useApplication, useApplications, useTransitionApplication } from "../api/hooks";
import { Loading, ErrorBanner, EmptyState } from "../components/Feedback";
import { StateBadge } from "../components/Badges";
import { APPLICATION_STATES, type ApplicationState } from "../api/types";

const PAGE_SIZE = 50;

export function Applications() {
  const { id } = useParams<{ id: string }>();
  return id ? <ApplicationDetail applicationId={id} /> : <ApplicationList />;
}

function ApplicationList() {
  const [params, setParams] = useSearchParams();
  const [state, setState] = useState("");
  const [offset, setOffset] = useState(0);
  // A job's page links here with `?job=` after an apply attempt, so the user
  // lands on the application for the job they were just looking at.
  const jobId = params.get("job") ?? "";
  const { data: page, isLoading, error } = useApplications({
    states: state ? [state as ApplicationState] : undefined,
    limit: PAGE_SIZE,
    offset,
  });
  const visible = jobId
    ? (page?.items ?? []).filter((item) => item.job_id === jobId)
    : (page?.items ?? []);

  if (isLoading || !page) return <Loading />;
  return (
    <div>
      <h1 style={{ marginBottom: 24 }}>Applications</h1>
      <div className="filters">
        <select
          aria-label="State"
          value={state}
          onChange={(e) => {
            setState(e.target.value);
            setOffset(0);
          }}
        >
          <option value="">All states</option>
          {APPLICATION_STATES.map((value) => (
            <option key={value} value={value}>{value}</option>
          ))}
        </select>
        {jobId && (
          <button
            className="secondary"
            onClick={() => {
              const next = new URLSearchParams(params);
              next.delete("job");
              setParams(next);
            }}
          >
            Clear job filter
          </button>
        )}
      </div>
      {error && <ErrorBanner message={error.message} />}
      {visible.length === 0 ? (
        <EmptyState
          title="No applications"
          message="Applying to a job from its detail page creates one."
        />
      ) : (
        <table>
          <thead>
            <tr><th>Job</th><th>Company</th><th>State</th><th>Score</th><th>Attention</th><th>Updated</th></tr>
          </thead>
          <tbody>
            {visible.map((item) => (
              <tr key={item.id}>
                <td><Link to={`/applications/${item.id}`}>{item.job_title}</Link></td>
                <td>{item.company}</td>
                <td><StateBadge state={item.state} /></td>
                <td>{item.fit_score != null ? item.fit_score.toFixed(2) : "—"}</td>
                <td>{item.needs_attention ? "yes" : "—"}</td>
                <td>{new Date(item.updated_at).toLocaleDateString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p style={{ marginTop: 12, color: "var(--text-muted)" }}>
        {page.total} total · {page.items.length} shown
      </p>
      <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
        <button className="secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
          Previous
        </button>
        <button
          className="secondary"
          disabled={offset + PAGE_SIZE >= page.total}
          onClick={() => setOffset(offset + PAGE_SIZE)}
        >
          Next
        </button>
      </div>
    </div>
  );
}

function ApplicationDetail({ applicationId }: { applicationId: string }) {
  const { data: application, isLoading, error } = useApplication(applicationId);
  const transition = useTransitionApplication();

  if (isLoading) return <Loading />;
  if (error) return <ErrorBanner message={error.message} />;
  if (!application) return <ErrorBanner message="Application not found" />;

  return (
    <div>
      <Link to="/applications">← Back to Applications</Link>
      <h1 style={{ marginTop: 12 }}>{application.job_title}</h1>
      <p style={{ color: "var(--text-muted)", marginBottom: 16 }}>
        {application.company} · <StateBadge state={application.state} />
      </p>

      <div className="card">
        <h3>Move this application</h3>
        {application.allowed_transitions.length === 0 ? (
          <p style={{ color: "var(--text-muted)", margin: 0 }}>
            No further transitions are available from {application.state}.
          </p>
        ) : (
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {application.allowed_transitions.map((next) => (
              <button
                key={next}
                className="secondary"
                disabled={transition.isPending}
                onClick={() =>
                  transition.mutate({ applicationId: application.id, to_state: next, jobId: application.job_id })
                }
              >
                {`Move to ${next}`}
              </button>
            ))}
          </div>
        )}
        {transition.isError && <ErrorBanner message={transition.error.message} />}
      </div>

      <div className="card">
        <h3>Details</h3>
        <p>Job: <Link to={`/jobs/${application.job_id}`}>{application.job_title}</Link></p>
        <p>External reference: {application.external_reference ?? "—"}</p>
        <p>Submitted: {application.submitted_at ? new Date(application.submitted_at).toLocaleString() : "—"}</p>
        {application.notes && <p>Notes: {application.notes}</p>}
      </div>

      <div className="card">
        <h3>History</h3>
        <table>
          <thead><tr><th>When</th><th>From</th><th>To</th><th>Actor</th><th>Reason</th></tr></thead>
          <tbody>
            {application.events.map((event) => (
              <tr key={event.id}>
                <td>{new Date(event.occurred_at).toLocaleString()}</td>
                <td>{event.from_state ?? "—"}</td>
                <td>{event.to_state ?? "—"}</td>
                <td>{event.actor}</td>
                <td>{event.message ?? event.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {application.artifacts.length > 0 && (
        <div className="card">
          <h3>Artifacts</h3>
          <ul>
            {application.artifacts.map((artifact, index) => (
              <li key={index}>{Object.keys(artifact).join(", ")}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
