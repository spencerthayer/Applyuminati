import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import { Dashboard } from "./Dashboard";

const DASH = {
  total_jobs: 4,
  shortlisted: 1,
  ready: 1,
  submitted: 0,
  needs_attention: 0,
  scored: 3,
  unscored: 1,
  by_recommendation: {},
  by_source: {},
  by_application_state: {},
  recent_activity: [],
  latest_run: null,
};

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function fetchMock(): Mock<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>> {
  return globalThis.fetch as unknown as Mock<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  globalThis.fetch = vi.fn(async () => json(DASH)) as unknown as typeof fetch;
});

describe("Dashboard", () => {
  it("runs a discovery run from the landing page and says so while it runs", async () => {
    let release: (() => void) | undefined;
    const held = new Promise<Response>((resolve) => {
      release = () =>
        resolve(
          json({
            run_id: "run-1",
            state: "completed",
            jobs_discovered: 9,
            jobs_created: 5,
            jobs_merged: 0,
            failures: [],
          }),
        );
    });
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).startsWith("/api/v1/jobs/discover")) return held;
      return json(DASH);
    }) as unknown as typeof fetch;

    render(
      <QueryClientProvider client={makeClient()}>
        <Dashboard />
      </QueryClientProvider>,
    );

    await screen.findByText("Discovered");
    fireEvent.click(screen.getByRole("button", { name: "Discover" }));

    const button = await screen.findByRole("button", { name: "Discovering…" });
    expect(button).toBeDisabled();
    expect(
      fetchMock().mock.calls.some(([url]) => String(url).startsWith("/api/v1/jobs/discover")),
    ).toBe(true);

    release?.();
    await waitFor(() => expect(screen.getByRole("button", { name: "Discover" })).toBeEnabled());
    expect(await screen.findByText(/9 discovered/i)).toBeInTheDocument();
  });

  it("surfaces a failed discovery run rather than swallowing it", async () => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).startsWith("/api/v1/jobs/discover")) {
        return json({ detail: "no sources are enabled" }, 400);
      }
      return json(DASH);
    }) as unknown as typeof fetch;

    render(
      <QueryClientProvider client={makeClient()}>
        <Dashboard />
      </QueryClientProvider>,
    );

    await screen.findByText("Discovered");
    fireEvent.click(screen.getByRole("button", { name: "Discover" }));

    expect(await screen.findByText("no sources are enabled")).toBeInTheDocument();
  });
});
