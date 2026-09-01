/**
 * React Query bindings for the Applyuminati API.
 *
 * This is the only place that knows endpoint paths and cache-invalidation
 * relationships. Components call hooks; they never call `fetch` or construct a
 * URL. Keys are hierarchical (`["jobs", …]`) so a mutation can invalidate a
 * whole family with one call.
 *
 * Deliberately absent: any business logic. A mutation posts and invalidates —
 * it never recomputes a score, a dedup decision or a legal state transition
 * locally. The server is the single source of truth for all three.
 */

import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { ApiError, get, post, put } from "./client";
import type {
  ApplicationDetail,
  ApplicationState,
  ApplicationSummary,
  AuthStatus,
  BackendHealthResponse,
  DashboardResponse,
  DiscoverRequest,
  HealthResponse,
  InboxEntry,
  JobDetail,
  OpenBrowserResponse,
  JobSummary,
  Page,
  ProfileImportRequest,
  ResolveInboxRequest,
  ResolveInboxResponse,
  ProfileImportResponse,
  ProfileResponse,
  Recommendation,
  RunSummary,
  ScoreRequest,
  SettingsResponse,
  SourceInfo,
  SourceToggleRequest,
  StrategyUpdateRequest,
  TransitionRequest,
  VerificationState,
} from "./types";

// ---------------------------------------------------------------------------
// Filters
// ---------------------------------------------------------------------------

/** Sort columns the jobs endpoint accepts. Mirrors `JobRepository.list`. */
export type JobSort = "discovered_at" | "posted_at" | "freshness_days" | "fit_score" | "company" | "title";

/**
 * Jobs-table filter set, serialised straight into the query string.
 *
 * Field names match the API's query parameters exactly so there is no
 * translation layer to drift.
 */
export interface JobFilters {
  query?: string;
  sources?: string[];
  recommendation?: Recommendation | "";
  min_score?: number | null;
  states?: ApplicationState[];
  verification?: VerificationState | "";
  has_score?: boolean | null;
  sort?: JobSort;
  descending?: boolean;
  limit?: number;
  offset?: number;
}

export interface ApplicationFilters {
  states?: ApplicationState[];
  limit?: number;
  offset?: number;
}

// ---------------------------------------------------------------------------
// Query keys
// ---------------------------------------------------------------------------

export const queryKeys = {
  session: ["auth", "session"] as const,
  health: ["health"] as const,
  backendHealth: ["health", "backends"] as const,
  dashboard: ["dashboard"] as const,
  jobList: (filters: JobFilters) => ["jobs", "list", filters] as const,
  job: (jobId: string) => ["jobs", "detail", jobId] as const,
  profile: ["profile"] as const,
  sources: ["sources"] as const,
  applicationList: (filters: ApplicationFilters) => ["applications", "list", filters] as const,
  application: (applicationId: string) => ["applications", "detail", applicationId] as const,
  settings: ["settings"] as const,
  inbox: ["needs-you"] as const,
};

// ---------------------------------------------------------------------------
// Authentication
// ---------------------------------------------------------------------------

/**
 * Whether this browser holds a session.
 *
 * Every other query depends on this one being resolved, because an
 * unauthenticated app would otherwise fire a dozen requests that all 401 and
 * paint a wall of red banners over the login form. `App` gates on it.
 */
export function useSession(): UseQueryResult<AuthStatus, ApiError> {
  return useQuery<AuthStatus, ApiError>({
    queryKey: queryKeys.session,
    queryFn: ({ signal }) => get<AuthStatus>("/auth/session", undefined, signal),
    // A session outliving its cookie shows up as 401s elsewhere, so this is
    // re-checked periodically rather than trusted for the tab's whole lifetime.
    refetchInterval: 5 * 60_000,
    retry: false,
  });
}

export function useLogin(): UseMutationResult<AuthStatus, ApiError, { password: string }> {
  const client = useQueryClient();
  return useMutation<AuthStatus, ApiError, { password: string }>({
    mutationFn: (body) => post<AuthStatus>("/auth/login", body),
    onSuccess: (status) => {
      client.setQueryData(queryKeys.session, status);
      // Nothing was fetchable before this point, so the whole cache is stale.
      void client.invalidateQueries();
    },
  });
}

export function useLogout(): UseMutationResult<AuthStatus, ApiError, void> {
  const client = useQueryClient();
  return useMutation<AuthStatus, ApiError, void>({
    mutationFn: () => post<AuthStatus>("/auth/logout"),
    onSuccess: (status) => {
      // Cleared rather than invalidated: refetching after logout would only
      // produce 401s, and leaving the data cached would leave it on screen.
      client.clear();
      client.setQueryData(queryKeys.session, status);
    },
  });
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

/** Liveness plus the facts the nav bar shows. Polled, so it stays honest. */
export function useHealth(): UseQueryResult<HealthResponse, ApiError> {
  return useQuery<HealthResponse, ApiError>({
    queryKey: queryKeys.health,
    queryFn: ({ signal }) => get<HealthResponse>("/health", undefined, signal),
    refetchInterval: 30_000,
    staleTime: 10_000,
  });
}

/** Per-backend availability, used by the settings page. */
export function useBackendHealth(): UseQueryResult<BackendHealthResponse, ApiError> {
  return useQuery<BackendHealthResponse, ApiError>({
    queryKey: queryKeys.backendHealth,
    queryFn: ({ signal }) => get<BackendHealthResponse>("/health/backends", undefined, signal),
    staleTime: 60_000,
  });
}

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------

export function useDashboard(): UseQueryResult<DashboardResponse, ApiError> {
  return useQuery<DashboardResponse, ApiError>({
    queryKey: queryKeys.dashboard,
    queryFn: ({ signal }) => get<DashboardResponse>("/dashboard", undefined, signal),
  });
}

// ---------------------------------------------------------------------------
// Jobs
// ---------------------------------------------------------------------------

export function useJobs(filters: JobFilters): UseQueryResult<Page<JobSummary>, ApiError> {
  return useQuery<Page<JobSummary>, ApiError>({
    queryKey: queryKeys.jobList(filters),
    queryFn: ({ signal }) =>
      get<Page<JobSummary>>(
        "/jobs",
        {
          query: filters.query,
          sources: filters.sources,
          recommendation: filters.recommendation,
          min_score: filters.min_score,
          states: filters.states,
          verification: filters.verification,
          has_score: filters.has_score,
          sort: filters.sort,
          descending: filters.descending,
          limit: filters.limit,
          offset: filters.offset,
        },
        signal,
      ),
    // Keeps the previous page visible while the next one loads, so filtering
    // does not flash an empty table and re-trigger the empty state.
    placeholderData: keepPreviousData,
  });
}

export function useJob(jobId: string | undefined): UseQueryResult<JobDetail, ApiError> {
  return useQuery<JobDetail, ApiError>({
    queryKey: queryKeys.job(jobId ?? ""),
    queryFn: ({ signal }) => get<JobDetail>(`/jobs/${encodeURIComponent(jobId as string)}`, undefined, signal),
    enabled: Boolean(jobId),
  });
}

/** Kick off a discovery run across the enabled sources. */
export function useDiscover(): UseMutationResult<RunSummary, ApiError, DiscoverRequest> {
  const client = useQueryClient();
  return useMutation<RunSummary, ApiError, DiscoverRequest>({
    mutationFn: (body) => post<RunSummary>("/jobs/discover", body),
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: ["jobs"] }),
        client.invalidateQueries({ queryKey: queryKeys.dashboard }),
        client.invalidateQueries({ queryKey: queryKeys.sources }),
      ]);
    },
  });
}

/** Score (or re-score) discovered jobs against the active profile. */
export function useScore(): UseMutationResult<RunSummary, ApiError, ScoreRequest> {
  const client = useQueryClient();
  return useMutation<RunSummary, ApiError, ScoreRequest>({
    mutationFn: (body) => post<RunSummary>("/jobs/score", body),
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: ["jobs"] }),
        client.invalidateQueries({ queryKey: queryKeys.dashboard }),
      ]);
    },
  });
}

// ---------------------------------------------------------------------------
// Profile
// ---------------------------------------------------------------------------

/**
 * The active career profile, or `null` when none has been imported yet.
 *
 * "No profile" is a normal first-run state, not an error, so a 404 is mapped to
 * `null` instead of surfacing a red banner on every page.
 */
export function useProfile(): UseQueryResult<ProfileResponse | null, ApiError> {
  return useQuery<ProfileResponse | null, ApiError>({
    queryKey: queryKeys.profile,
    queryFn: async ({ signal }) => {
      try {
        return await get<ProfileResponse>("/profile", undefined, signal);
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }
    },
  });
}

export function useImportResume(): UseMutationResult<
  ProfileImportResponse,
  ApiError,
  ProfileImportRequest
> {
  const client = useQueryClient();
  return useMutation<ProfileImportResponse, ApiError, ProfileImportRequest>({
    mutationFn: (body) => post<ProfileImportResponse>("/profile/import", body),
    onSuccess: async () => {
      // A new profile invalidates every score, so the whole job family and the
      // dashboard counters have to be refetched, not just the profile.
      await Promise.all([
        client.invalidateQueries({ queryKey: queryKeys.profile }),
        client.invalidateQueries({ queryKey: queryKeys.settings }),
        client.invalidateQueries({ queryKey: ["jobs"] }),
        client.invalidateQueries({ queryKey: queryKeys.dashboard }),
        client.invalidateQueries({ queryKey: queryKeys.health }),
      ]);
    },
  });
}

// ---------------------------------------------------------------------------
// Sources
// ---------------------------------------------------------------------------

/** Registered source plugins. A bare array — this list is never paginated. */
export function useSources(): UseQueryResult<SourceInfo[], ApiError> {
  return useQuery<SourceInfo[], ApiError>({
    queryKey: queryKeys.sources,
    queryFn: ({ signal }) => get<SourceInfo[]>("/sources", undefined, signal),
  });
}

export interface ToggleSourceInput extends SourceToggleRequest {
  slug: string;
  enabled: boolean;
}

export function useToggleSource(): UseMutationResult<SourceInfo, ApiError, ToggleSourceInput> {
  const client = useQueryClient();
  return useMutation<SourceInfo, ApiError, ToggleSourceInput>({
    mutationFn: ({ slug, enabled, options }) =>
      post<SourceInfo>(
        `/sources/${encodeURIComponent(slug)}/${enabled ? "enable" : "disable"}`,
        { options: options ?? null },
      ),
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: queryKeys.sources }),
        client.invalidateQueries({ queryKey: queryKeys.health }),
        client.invalidateQueries({ queryKey: queryKeys.backendHealth }),
      ]);
    },
  });
}

// ---------------------------------------------------------------------------
// Applications
// ---------------------------------------------------------------------------

export function useApplications(
  filters: ApplicationFilters,
): UseQueryResult<Page<ApplicationSummary>, ApiError> {
  return useQuery<Page<ApplicationSummary>, ApiError>({
    queryKey: queryKeys.applicationList(filters),
    queryFn: ({ signal }) =>
      get<Page<ApplicationSummary>>(
        "/applications",
        { states: filters.states, limit: filters.limit, offset: filters.offset },
        signal,
      ),
    placeholderData: keepPreviousData,
  });
}

export function useApplication(
  applicationId: string | undefined,
): UseQueryResult<ApplicationDetail, ApiError> {
  return useQuery<ApplicationDetail, ApiError>({
    queryKey: queryKeys.application(applicationId ?? ""),
    queryFn: ({ signal }) =>
      get<ApplicationDetail>(
        `/applications/${encodeURIComponent(applicationId as string)}`,
        undefined,
        signal,
      ),
    enabled: Boolean(applicationId),
  });
}

export interface TransitionInput extends TransitionRequest {
  applicationId: string;
  /** Job whose detail view triggered this, so its cache entry is refreshed. */
  jobId?: string;
}

/**
 * Move an application to a new state.
 *
 * The set of legal target states comes from the server
 * (`allowed_transitions` / `available_actions`); this hook never validates a
 * transition locally.
 */
export function useTransitionApplication(): UseMutationResult<
  ApplicationDetail,
  ApiError,
  TransitionInput
> {
  const client = useQueryClient();
  return useMutation<ApplicationDetail, ApiError, TransitionInput>({
    mutationFn: ({ applicationId, to_state, reason, message }) =>
      post<ApplicationDetail>(
        `/applications/${encodeURIComponent(applicationId)}/transition`,
        { to_state, reason, message },
      ),
    onSuccess: async (_result, input) => {
      await Promise.all([
        client.invalidateQueries({ queryKey: ["applications"] }),
        client.invalidateQueries({ queryKey: ["jobs"] }),
        client.invalidateQueries({ queryKey: queryKeys.dashboard }),
        ...(input.jobId
          ? [client.invalidateQueries({ queryKey: queryKeys.job(input.jobId) })]
          : []),
      ]);
    },
  });
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

export function useInbox(): UseQueryResult<InboxEntry[], ApiError> {
  return useQuery<InboxEntry[], ApiError>({
    queryKey: queryKeys.inbox,
    queryFn: ({ signal }) => get<InboxEntry[]>("/needs-you", undefined, signal),
    refetchInterval: 15_000,
  });
}

export function useOpenBrowser(): UseMutationResult<OpenBrowserResponse, ApiError, string> {
  const client = useQueryClient();
  return useMutation<OpenBrowserResponse, ApiError, string>({
    mutationFn: (attemptId) => post<OpenBrowserResponse>(`/needs-you/${attemptId}/open-browser`, {}),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: queryKeys.inbox });
    },
  });
}

export function useResolveInbox(): UseMutationResult<
  ResolveInboxResponse,
  ApiError,
  { attemptId: string; interventionId: string; body: ResolveInboxRequest }
> {
  const client = useQueryClient();
  return useMutation<
    ResolveInboxResponse,
    ApiError,
    { attemptId: string; interventionId: string; body: ResolveInboxRequest }
  >({
    mutationFn: ({ attemptId, interventionId, body }) =>
      post<ResolveInboxResponse>(`/needs-you/${attemptId}/${interventionId}`, body),
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: queryKeys.inbox }),
        client.invalidateQueries({ queryKey: queryKeys.dashboard }),
      ]);
    },
  });
}

export function useSettings(): UseQueryResult<SettingsResponse, ApiError> {
  return useQuery<SettingsResponse, ApiError>({
    queryKey: queryKeys.settings,
    queryFn: ({ signal }) => get<SettingsResponse>("/settings", undefined, signal),
  });
}

/**
 * Persist the strategy dial-set, or materialise a named preset.
 *
 * Thresholds change which jobs are shortlisted, so the job family and the
 * dashboard counters are invalidated alongside settings.
 */
export function useUpdateStrategy(): UseMutationResult<
  SettingsResponse,
  ApiError,
  StrategyUpdateRequest
> {
  const client = useQueryClient();
  return useMutation<SettingsResponse, ApiError, StrategyUpdateRequest>({
    mutationFn: (body) => put<SettingsResponse>("/settings/strategy", body),
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: queryKeys.settings }),
        client.invalidateQueries({ queryKey: queryKeys.profile }),
        client.invalidateQueries({ queryKey: ["jobs"] }),
        client.invalidateQueries({ queryKey: queryKeys.dashboard }),
      ]);
    },
  });
}
