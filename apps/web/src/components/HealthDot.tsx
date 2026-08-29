import type { HealthState } from "../api/types";

const dotClasses: Record<string, string> = {
  healthy: "healthy",
  degraded: "degraded",
  unavailable: "unavailable",
  not_installed: "not_installed",
  unknown: "unknown",
};

export function HealthDot({ state }: { state: HealthState }) {
  const cls = dotClasses[state] ?? "unknown";
  return <span className={`health-dot ${cls}`} title={state} />;
}
