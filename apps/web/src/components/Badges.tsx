import type { ApplicationState, Recommendation, VerificationState } from "../api/types";

const colors: Record<string, string> = {
  apply: "badge-green",
  investigate: "badge-yellow",
  skip: "badge-red",
  shortlisted: "badge-blue",
  submitted: "badge-green",
  ready: "badge-blue",
  preparing: "badge-muted",
  discovered: "badge-muted",
  rejected: "badge-red",
  offer: "badge-green",
  interview: "badge-green",
  needs_attention: "badge-red",
  failed: "badge-red",
  live: "badge-green",
  unverified: "badge-muted",
  gone: "badge-red",
  closed: "badge-muted",
};

export function StateBadge({ state }: { state: string }) {
  const cls = colors[state] ?? "badge-muted";
  return <span className={`badge ${cls}`}>{state}</span>;
}

export function RecBadge({ rec }: { rec: Recommendation }) {
  const cls = colors[rec] ?? "badge-muted";
  return <span className={`badge ${cls}`}>{rec}</span>;
}

export function VerifyBadge({ state }: { state: VerificationState }) {
  const cls = colors[state] ?? "badge-muted";
  return <span className={`badge ${cls}`}>{state}</span>;
}

export function AppStateBadge({ state }: { state: ApplicationState }) {
  return <StateBadge state={state} />;
}
