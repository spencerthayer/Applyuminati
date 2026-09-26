import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import { useApplyToJob, useDiscover, useJobs, useScore, useUpdatePreferences } from "./hooks";
import type { ApplyJobResponse, ProfileResponse } from "./types";

/** A client with retries off, so a failing assertion is not masked by backoff. */
function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function wrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

/** The `fetch` stub installed for one case, narrowed to its call records. */
function fetchMock(): Mock<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>> {
  return globalThis.fetch as unknown as Mock<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>;
}

/** The `RequestInit` of one recorded call. The client always sends one. */
function initOf(call: [RequestInfo | URL, RequestInit?] | undefined): RequestInit {
  return call?.[1] ?? {};
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("useApplyToJob", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse({ attempt_id: "att-1", state: "applying", driver: "playwright" }, 201),
    ) as unknown as typeof fetch;
  });

  it("POSTs the job id and the chosen execution mode as the body", async () => {
    const client = makeClient();
    const { result } = renderHook(
      () => useApplyToJob(),
      { wrapper: wrapper(client) },
    );

    result.current.mutate({ jobId: "job-1", mode: "fill_no_submit" });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const [url] = fetchMock().mock.calls[0];
    const init = initOf(fetchMock().mock.calls[0]);
    expect(url).toBe("/api/v1/jobs/job-1/apply");
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ mode: "fill_no_submit" }));
  });

  it("sends a null mode so the server applies its configured default", async () => {
    const client = makeClient();
    const { result } = renderHook(() => useApplyToJob(), { wrapper: wrapper(client) });

    result.current.mutate({ jobId: "job-1", mode: null });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const init = initOf(fetchMock().mock.calls[0]);
    expect(init.body).toBe(JSON.stringify({ mode: null }));
  });

  it("returns the server's attempt id, state and driver", async () => {
    const client = makeClient();
    const { result } = renderHook(() => useApplyToJob(), { wrapper: wrapper(client) });

    result.current.mutate({ jobId: "job-1", mode: null });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data as ApplyJobResponse).toEqual({
      attempt_id: "att-1",
      state: "applying",
      driver: "playwright",
    });
  });

  it("surfaces a 409 as a retryable-to-render ApiError rather than swallowing it", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse({ detail: "an application for this job is already in progress" }, 409),
    ) as unknown as typeof fetch;
    const client = makeClient();
    const { result } = renderHook(() => useApplyToJob(), { wrapper: wrapper(client) });

    result.current.mutate({ jobId: "job-1", mode: null });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toMatchObject({
      status: 409,
      message: "an application for this job is already in progress",
    });
  });
});

describe("useUpdatePreferences", () => {
  it("PUTs exactly the preference fields the server declares", async () => {
    const profile = { id: "p-1", label: "default" } as unknown as ProfileResponse;
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse(profile)) as unknown as typeof fetch;
    const client = makeClient();
    const { result } = renderHook(() => useUpdatePreferences(), { wrapper: wrapper(client) });

    result.current.mutate({
      titles: ["Staff Engineer"],
      locations: ["Remote"],
      minimum_compensation: 180000,
      compensation_currency: "USD",
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const [url] = fetchMock().mock.calls[0];
    const init = initOf(fetchMock().mock.calls[0]);
    expect(url).toBe("/api/v1/profile/preferences");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual({
      titles: ["Staff Engineer"],
      locations: ["Remote"],
      minimum_compensation: 180000,
      compensation_currency: "USD",
    });
  });
});

describe("useJobs", () => {
  it("sends the source and state filters under the parameter names the API declares", async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(jsonResponse({ items: [], total: 0, limit: 50, offset: 0 })) as unknown as typeof fetch;
    const client = makeClient();
    const { result } = renderHook(
      () => useJobs({ sources: ["greenhouse"], states: ["shortlisted"] }),
      { wrapper: wrapper(client) },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const [url] = fetchMock().mock.calls[0];
    expect(url).toBe("/api/v1/jobs?source=greenhouse&state=shortlisted");
  });
});

describe("useDiscover", () => {
  it("passes source restrictions as query parameters, which is where the API reads them", async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(jsonResponse({ id: "run-1", jobs_created: 3 })) as unknown as typeof fetch;
    const client = makeClient();
    const { result } = renderHook(() => useDiscover(), { wrapper: wrapper(client) });

    result.current.mutate({ sources: ["lever"], queries: ["platform engineer"] });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const [url] = fetchMock().mock.calls[0];
    expect(url).toBe("/api/v1/jobs/discover?sources=lever&queries=platform+engineer");
  });
});

describe("useScore", () => {
  it("passes job ids and the rescore flag as query parameters", async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(jsonResponse({ id: "run-2", scored: 1 })) as unknown as typeof fetch;
    const client = makeClient();
    const { result } = renderHook(() => useScore(), { wrapper: wrapper(client) });

    result.current.mutate({ job_ids: ["job-1"], rescore: true, limit: 25 });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const [url] = fetchMock().mock.calls[0];
    expect(url).toBe("/api/v1/jobs/score?job_ids=job-1&rescore=true&limit=25");
  });
});
