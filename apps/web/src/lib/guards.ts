/**
 * The app's canonical runtime type guards.
 *
 * Several API fields are declared `dict[str, Any]` on the Python side
 * (`matched_evidence`, `missing_requirements`, `locations`, `artifacts`,
 * `targets`). They arrive as genuinely unknown JSON, and this app ships no
 * schema validator by design, so the boundary is narrowed here once and
 * consumed through the readers in `src/lib/payload.ts`. Do not re-declare these
 * guards at call sites.
 */

/** Narrow to a plain JSON object. Fields stay `unknown` — check them too. */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Read `key` as a non-empty string, or return `fallback`.
 *
 * Used wherever the backend declares a field required but the payload is
 * untyped JSON: an absent or empty value must degrade to a readable default
 * rather than rendering "undefined" into the UI.
 */
export function readString(source: Record<string, unknown>, key: string, fallback = ""): string {
  const value = source[key];
  return typeof value === "string" && value !== "" ? value : fallback;
}

/** Read `key` as a string, or `null` when absent/blank. */
export function readOptionalString(
  source: Record<string, unknown>,
  key: string,
): string | null {
  const value = source[key];
  return typeof value === "string" && value !== "" ? value : null;
}

/** Read `key` as a finite number, or return `fallback`. */
export function readNumber(source: Record<string, unknown>, key: string, fallback: number): number {
  const value = source[key];
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

/** Read `key` as an array of strings, dropping non-string entries. */
export function readStringArray(source: Record<string, unknown>, key: string): string[] {
  const value = source[key];
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}
