/**
 * Hand-written mirror of `src/applyuminati/api/schemas.py`.
 *
 * This file is the wire contract. Field names, optionality and enum members are
 * copied verbatim from the Pydantic models — a rename on either side is an
 * integration break, so keep the two in lockstep and keep the declaration order
 * identical to `schemas.py` so a diff is easy to eyeball.
 *
 * Conventions:
 * - Python `datetime` serialises to an ISO-8601 string -> `IsoDateTime`.
 * - Python `X | None = None` -> `field?: X | null` (FastAPI emits explicit
 *   `null`, and a field may be omitted entirely, so both are accepted).
 * - Python `dict[str, Any]` -> `JsonObject`; the loosely typed payloads
 *   (`matched_evidence`, `missing_requirements`, `locations`, `artifacts`,
 *   `targets`) get companion "view" interfaces further down for rendering.
 * - `StrEnum` -> string-literal union, never a TS `enum`.
 */

/** ISO-8601 timestamp, as produced by FastAPI for a `datetime`. */
export type IsoDateTime = string;

export type JsonValue = string | number | boolean | null | JsonValue[] | { [k: string]: JsonValue };

export type JsonObject = Record<string, unknown>;

// ---------------------------------------------------------------------------
// Enums (verbatim from applyuminati.core.*)
// ---------------------------------------------------------------------------

/** `applyuminati.core.registry.HealthState` */
export type HealthState = "healthy" | "degraded" | "unavailable" | "not_installed" | "unknown";

/** `applyuminati.core.settings.ExecutionMode` */
export type ExecutionMode =
  | "research_only"
  | "prepare_application"
  | "fill_no_submit"
  | "autonomous_submit";

/** `applyuminati.core.models.common.EmploymentType` */
export type EmploymentType =
  | "full_time"
  | "part_time"
  | "contract"
  | "contract_to_hire"
  | "temporary"
  | "internship"
  | "apprenticeship"
  | "volunteer"
  | "unknown";

/** `applyuminati.core.models.common.RemoteMode` */
export type RemoteMode = "remote" | "hybrid" | "onsite" | "unknown";

/** `applyuminati.core.models.common.SeniorityLevel` — ordered ladder. */
export type SeniorityLevel =
  | "intern"
  | "entry"
  | "junior"
  | "mid"
  | "senior"
  | "staff"
  | "principal"
  | "lead"
  | "manager"
  | "director"
  | "vp"
  | "executive"
  | "unknown";

/** `applyuminati.core.models.common.CompensationPeriod` */
export type CompensationPeriod = "hourly" | "daily" | "weekly" | "monthly" | "yearly";

/** `applyuminati.core.models.job.AtsVendor` */
export type AtsVendor =
  | "greenhouse"
  | "lever"
  | "ashby"
  | "workday"
  | "smartrecruiters"
  | "icims"
  | "taleo"
  | "successfactors"
  | "jobvite"
  | "bamboohr"
  | "recruitee"
  | "workable"
  | "teamtailor"
  | "eightfold"
  | "custom"
  | "unknown";

/** `applyuminati.core.models.job.SourceTier` — closeness to the employer. */
export type SourceTier = "direct_ats" | "employer_site" | "aggregator" | "derived";

/** `applyuminati.core.models.job.VerificationState` */
export type VerificationState = "unverified" | "live" | "closed" | "gone" | "unknown";

/** `applyuminati.core.models.scoring.Recommendation` */
export type Recommendation = "apply" | "investigate" | "skip";

/** `applyuminati.core.models.scoring.ScoreDimension` */
export type ScoreDimension =
  | "title_match"
  | "seniority_match"
  | "required_skills"
  | "preferred_skills"
  | "demonstrated_experience"
  | "domain_overlap"
  | "compensation"
  | "location"
  | "employment_type"
  | "work_authorization"
  | "user_preference";

/** `applyuminati.core.models.scoring.BlockerSeverity` */
export type BlockerSeverity = "hard" | "significant" | "minor";

/** `applyuminati.core.models.application.ApplicationState` */
export type ApplicationState =
  | "discovered"
  | "evaluating"
  | "skipped"
  | "shortlisted"
  | "preparing"
  | "ready"
  | "applying"
  | "submitted"
  | "confirmed"
  | "recruiter_contact"
  | "assessment"
  | "interview"
  | "follow_up"
  | "rejected"
  | "withdrawn"
  | "offer"
  | "accepted"
  | "closed"
  | "failed";

/** `applyuminati.core.strategy.Strictness` */
export type Strictness = "hard" | "soft" | "ignored";

/** `applyuminati.core.strategy.RemotePreference` */
export type RemotePreference =
  | "remote_only"
  | "remote_preferred"
  | "hybrid_preferred"
  | "onsite_preferred"
  | "no_preference";

// ---------------------------------------------------------------------------
// SearchStrategy (applyuminati.core.strategy.SearchStrategy)
// ---------------------------------------------------------------------------

/**
 * The user's dial-set. Stored as exact numbers, never as vague labels — the UI
 * may render a slider, but what persists is `application_volume_bias = 0.5`.
 */
export interface SearchStrategy {
  name: string;

  // breadth / effort — all normalised 0..1
  depth_bias: number;
  application_volume_bias: number;
  title_exploration: number;

  // constraints
  compensation_strictness: Strictness;
  location_strictness: Strictness;
  remote_preference: RemotePreference;
  seniority_tolerance_levels: number;
  work_authorization_is_hard_blocker: boolean;

  // thresholds
  minimum_fit_score: number;
  minimum_evidence_confidence: number;
  skip_below_score: number;

  // volume limits
  max_applications_per_run: number;
  max_applications_per_day: number;
  max_jobs_per_source_per_run: number;

  // preferences
  preferred_industries: string[];
  excluded_industries: string[];
  preferred_companies: string[];
  excluded_companies: string[];

  // autonomy
  execution_mode: ExecutionMode;
  require_review_for_sensitive_questions: boolean;
  require_review_above_compensation_usd?: number | null;
}

// ---------------------------------------------------------------------------
// Pagination
// ---------------------------------------------------------------------------

/** Offset-paginated envelope. */
export interface Page<ItemT> {
  items: ItemT[];
  total: number;
  limit: number;
  offset: number;
}

// ---------------------------------------------------------------------------
// Authentication
// ---------------------------------------------------------------------------

/** `applyuminati.api.security.AuthStatus` */
export interface AuthStatus {
  /** False when authentication is switched off entirely. */
  required: boolean;
  /** False when no password has been set on the instance yet. */
  configured: boolean;
  authenticated: boolean;
  /** Present only when authenticated; echoed back in the CSRF header. */
  csrf_token?: string | null;
  expires_at?: number | null;
  listens_beyond_loopback: boolean;
}

/** `applyuminati.api.routers.inbox.InboxEntry` */
export type InterventionReason =
  | "authentication_required"
  | "captcha_required"
  | "mfa_required"
  | "identity_verification"
  | "legal_attestation"
  | "ambiguous_question"
  | "document_required"
  | "payment_or_fee"
  | "user_review"
  | "automation_blocked"
  | "unknown_interaction";

export type InterventionResolution =
  | "done_continue"
  | "skip_application"
  | "keep_control"
  | "answer"
  | "approve"
  | "reject"
  | "provide_document"
  | "cancel";

export interface InboxEntry {
  attempt_id: string;
  application_id: string;
  job_id: string;
  company?: string | null;
  title?: string | null;
  intervention_id: string;
  reason: InterventionReason;
  instruction: string;
  requires_browser_handoff: boolean;
  question_text?: string | null;
  browser_host_id?: string | null;
  browser_session_id?: string | null;
  task_space_id?: string | null;
  opened_at: IsoDateTime;
}

export interface ResolveInboxRequest {
  resolution: InterventionResolution;
  payload?: JsonObject;
}

export interface ResolveInboxResponse {
  attempt_id: string;
  workflow_state: string;
  open_intervention?: string | null;
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

export interface ComponentHealth {
  name: string;
  kind: string;
  state: HealthState;
  detail: string;
  facts: JsonObject;
  latency_ms?: number | null;
}

export interface HealthResponse {
  status: string;
  version: string;
  database_ok: boolean;
  schema_version?: string | null;
  execution_mode: string;
  profile_configured: boolean;
  enabled_sources: string[];
  checked_at: IsoDateTime;
}

/** Availability of every registered backend, grouped by extension point. */
export interface BackendHealthResponse {
  sources: ComponentHealth[];
  llm: ComponentHealth[];
  browsers: ComponentHealth[];
  agents: ComponentHealth[];
  email: ComponentHealth[];
  /** Plugins that failed to import, surfaced rather than hidden. */
  load_errors: string[];
}

// ---------------------------------------------------------------------------
// Profile
// ---------------------------------------------------------------------------

export interface ClaimSummary {
  id: string;
  statement: string;
  level: string;
  tags: string[];
  provenance_kinds: string[];
}

export interface ProfileResponse {
  id: string;
  label: string;
  /** Raw JSON Resume document, exactly as it round-trips. */
  resume: JsonObject;
  name?: string | null;
  headline?: string | null;
  email?: string | null;
  counts: Record<string, number>;
  targets: JsonObject;
  strategy: SearchStrategy;
  claim_levels: Record<string, number>;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
}

export interface ProfileImportRequest {
  /** A JSON Resume document. */
  resume: JsonObject;
  label?: string;
  /** Replace the existing profile rather than failing when one exists. */
  replace?: boolean;
}

export interface ProfileImportResponse {
  profile: ProfileResponse;
  claims_created: number;
  metrics_extracted: number;
  /** Fields the importer could not interpret. Reported, never dropped. */
  warnings: string[];
}

export interface PreferencesUpdateRequest {
  titles?: string[] | null;
  locations?: string[] | null;
  remote_modes?: RemoteMode[] | null;
  employment_types?: EmploymentType[] | null;
  seniority?: SeniorityLevel | null;
  minimum_compensation?: number | null;
  compensation_currency?: string | null;
  strategy?: SearchStrategy | null;
}

// ---------------------------------------------------------------------------
// Sources
// ---------------------------------------------------------------------------

export interface SourceInfo {
  slug: string;
  name: string;
  description: string;
  tier: SourceTier;
  ats: AtsVendor;
  enabled: boolean;
  capabilities: string[];
  requires_auth: boolean;
  blocking: string;
  health?: ComponentHealth | null;
  /** Plugin-specific options currently configured, secrets removed. */
  options: JsonObject;
  /** JSON Schema for this plugin's options, so the UI can render a form. */
  options_schema?: JsonObject | null;
  last_run_at?: IsoDateTime | null;
  last_run_jobs: number;
  consecutive_failures: number;
}

export interface SourceToggleRequest {
  options?: JsonObject | null;
}

// ---------------------------------------------------------------------------
// Jobs
// ---------------------------------------------------------------------------

export interface JobSourceInfo {
  source: string;
  tier: SourceTier;
  url: string;
  source_job_id: string;
  first_seen_at: IsoDateTime;
  last_seen_at: IsoDateTime;
  confidence: number;
}

export interface ScoreDimensionInfo {
  dimension: ScoreDimension;
  score: number;
  weight: number;
  confidence: number;
  rationale: string;
  llm_adjusted: boolean;
}

export interface FitScoreInfo {
  id: string;
  overall: number;
  confidence: number;
  recommendation: Recommendation;
  explanation: string;
  baseline_overall?: number | null;
  scorer_version: string;
  llm_provider?: string | null;
  llm_model?: string | null;
  dimensions: ScoreDimensionInfo[];
  matched_evidence: JsonObject[];
  missing_requirements: JsonObject[];
  uncertainties: string[];
  scored_at: IsoDateTime;
}

/** Row shape for the jobs table. */
export interface JobSummary {
  id: string;
  title: string;
  company: string;
  location: string;
  remote_mode: RemoteMode;
  employment_type: EmploymentType;
  seniority: SeniorityLevel;
  ats: AtsVendor;
  sources: string[];
  canonical_url: string;
  apply_url?: string | null;
  compensation?: string | null;
  posted_at?: IsoDateTime | null;
  discovered_at: IsoDateTime;
  /** Days since any source last saw the posting. */
  freshness_days: number;
  verification: VerificationState;
  fit_score?: number | null;
  recommendation?: Recommendation | null;
  application_state?: ApplicationState | null;
  duplicate_source_count: number;
}

/** Everything the job detail page needs, in one request. */
export interface JobDetail extends JobSummary {
  description?: string | null;
  requirements: string[];
  preferred_qualifications: string[];
  skills: string[];
  locations: JsonObject[];
  source_records: JobSourceInfo[];
  score?: FitScoreInfo | null;
  merged_job_ids: string[];
  /** Transitions currently legal for the linked application. */
  available_actions: string[];
}

export interface DiscoverRequest {
  /** Restrict to these source slugs. Empty means "every enabled source". */
  sources?: string[];
  /** Override the profile's target titles for this run. */
  queries?: string[];
  locations?: string[];
  /** Run synchronously and return the finished run. */
  wait?: boolean;
}

export interface ScoreRequest {
  job_ids?: string[];
  /** Re-score jobs that already have a score. */
  rescore?: boolean;
  /** Run the optional LLM enrichment pass on top of the deterministic score. */
  use_llm?: boolean;
  limit?: number;
  wait?: boolean;
}

// ---------------------------------------------------------------------------
// Applications
// ---------------------------------------------------------------------------

export interface ApplicationEventInfo {
  id: string;
  occurred_at: IsoDateTime;
  from_state?: ApplicationState | null;
  to_state?: ApplicationState | null;
  actor: string;
  actor_detail?: string | null;
  reason: string;
  message?: string | null;
  failure_category?: string | null;
}

export interface ApplicationSummary {
  id: string;
  job_id: string;
  job_title: string;
  company: string;
  state: ApplicationState;
  fit_score?: number | null;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
  submitted_at?: IsoDateTime | null;
  needs_attention: boolean;
}

export interface ApplicationDetail extends ApplicationSummary {
  external_reference?: string | null;
  notes?: string | null;
  events: ApplicationEventInfo[];
  artifacts: JsonObject[];
  allowed_transitions: ApplicationState[];
}

export interface TransitionRequest {
  to_state: ApplicationState;
  reason?: string;
  message?: string | null;
}

// ---------------------------------------------------------------------------
// Runs and dashboard
// ---------------------------------------------------------------------------

export interface RunSummary {
  id: string;
  kind: string;
  state: string;
  started_at: IsoDateTime;
  finished_at?: IsoDateTime | null;
  duration_seconds?: number | null;
  stats: Record<string, number>;
  failures: string[];
  triggered_by: string;
}

export interface ActivityItem {
  at: IsoDateTime;
  kind: string;
  summary: string;
  job_id?: string | null;
  application_id?: string | null;
}

export interface DashboardResponse {
  total_jobs: number;
  shortlisted: number;
  ready: number;
  submitted: number;
  needs_attention: number;
  scored: number;
  unscored: number;
  by_recommendation: Record<string, number>;
  by_source: Record<string, number>;
  by_application_state: Record<string, number>;
  recent_activity: ActivityItem[];
  latest_run?: RunSummary | null;
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

export interface ProviderInfo {
  name: string;
  kind: string;
  enabled: boolean;
  base_url?: string | null;
  default_model?: string | null;
  fast_model?: string | null;
  /** Never the key itself. */
  has_api_key: boolean;
  health?: ComponentHealth | null;
}

export interface SettingsResponse {
  execution_mode: string;
  data_dir: string;
  database: string;
  log_level: string;
  llm_enabled: boolean;
  default_provider?: string | null;
  providers: ProviderInfo[];
  browser_preferred: string[];
  agents_enabled: boolean;
  agents_preferred: string[];
  email_accounts: string[];
  strategy: SearchStrategy;
}

export interface StrategyUpdateRequest {
  strategy?: SearchStrategy | null;
  /** Materialise a named preset into concrete values. */
  preset?: string | null;
}

/** Uniform error envelope. Mirrors `ApplyuminatiError.to_dict`. */
export interface ErrorResponse {
  code: string;
  category: string;
  message: string;
  recovery: string;
  retryable: boolean;
  details: JsonObject;
}

// ---------------------------------------------------------------------------
// Views over loosely typed payloads
//
// The wire types above keep `dict[str, Any]` as `JsonObject`, exactly as
// declared. These interfaces describe the shapes the backend actually puts in
// those dicts (from the corresponding domain models) and are used only by the
// safe readers in `src/lib/payload.ts` — never asserted blindly.
// ---------------------------------------------------------------------------

/** Shape of a `matched_evidence` entry (`core.models.scoring.MatchedEvidence`). */
export interface MatchedEvidenceView {
  requirement: string;
  claim_id: string | null;
  excerpt: string | null;
  strength: number;
}

/** Shape of a `missing_requirements` entry (`core.models.scoring.MissingRequirement`). */
export interface MissingRequirementView {
  requirement: string;
  severity: BlockerSeverity;
  partially_evidenced: boolean;
  note: string | null;
}

/** Shape of a `locations` entry (`core.models.common.Location`). */
export interface LocationView {
  raw: string | null;
  city: string | null;
  region: string | null;
  country: string | null;
  postal_code: string | null;
  country_code: string | null;
}

/** Shape of `ProfileResponse.targets` (`core.models.profile.JobTargets`). */
export interface JobTargetsView {
  titles: string[];
  anti_titles: string[];
  seniority: SeniorityLevel;
  industries: string[];
  excluded_industries: string[];
  locations: LocationView[];
  remote_modes: RemoteMode[];
  employment_types: EmploymentType[];
  desired_skills: string[];
  avoided_skills: string[];
}
