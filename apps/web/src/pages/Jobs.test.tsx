import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import { Jobs } from "./Jobs";

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function renderJobs(client: QueryClient) {
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Jobs />
      </MemoryRouter>
    </QueryClientProvider>,
  );
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

const PAGE = {
  items: [
    {
      id: "job-1",
      title: "Staff Engineer",
      company: "Acme",
      location: "Remote",
      remote_mode: "remote",
      employment_type: "full_time",
      seniority: "staff",
      ats: "greenhouse",
      sources: ["greenhouse"],
      canonical_url: "https://acme.example/jobs/1",
      discovered_at: "2026-09-01T00:00:00Z",
      freshness_days: 1,
      verification: "live",
      fit_score: 0.9,
      recommendation: "apply",
      application_state: null,
      duplicate_source_count: 0,
    },
  ],
  total: 1,
  limit: 50,
  offset: 0,
};

const SOURCES = [
  {
    slug: "greenhouse",
    name: "Greenhouse",
    description: "",
    tier: "direct_ats",
    ats: "greenhouse",
    enabled: true,
    capabilities: [],
    requires_auth: false,
    blocking: "none",
    options: {},
    last_run_jobs: 0,
    consecutive_failures: 0,
  },
  {
    slug: "lever",
    name: "Lever",
    description: "",
    tier: "direct_ats",
    ats: "lever",
    enabled: false,
    capabilities: [],
    requires_auth: false,
    blocking: "none",
    options: {},
    last_run_jobs: 0,
    consecutive_failures: 0,
  },
];

/** A fetch stub that answers the page's own endpoints, plus a held discover run. */
function stubApi(options: { jobs?: unknown; holdDiscover?: boolean } = {}) {
  const jobs = options.jobs ?? PAGE;
  let releaseDiscover: (() => void) | undefined;
  const held = new Promise<Response>((resolve) => {
    releaseDiscover = () =>
      resolve(json({ run_id: "run-1", state: "completed", jobs_discovered: 12, jobs_created: 4, jobs_merged: 1, failures: [] }));
  });

  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/v1/jobs/discover")) {
      if (options.holdDiscover) return held;
      return json({ run_id: "run-1", state: "completed", jobs_discovered: 12, jobs_created: 4, jobs_merged: 1, failures: [] });
    }
    if (url.startsWith("/api/v1/jobs/score")) {
      return json({ run_id: "run-2", state: "completed", scored: 7, failed: 0, failures: [] });
    }
    if (url.startsWith("/api/v1/sources")) return json(SOURCES);
    if (url.startsWith("/api/v1/jobs")) return json(jobs);
    throw new Error(`unexpected request in Jobs test: ${url}`);
  }) as unknown as typeof fetch;

  return { release: () => releaseDiscover?.() };
}

beforeEach(() => {
  globalThis.fetch = vi.fn(async () => new Response("null", { status: 200 })) as unknown as typeof fetch;
});

describe("Jobs", () => {
  it("populates the source filter from the registered sources", async () => {
    stubApi();
    renderJobs(makeClient());

    const select = await screen.findByRole("combobox", { name: /source/i });
    const labels = within(select).getAllByRole("option").map((option) => option.textContent);
    expect(labels).toEqual(["All sources", "greenhouse", "lever"]);
  });

  it("filters the job list by the chosen source", async () => {
    stubApi();
      renderJobs(makeClient());

    await screen.findByText("Staff Engineer");
    fireEvent.change(screen.getByRole("combobox", { name: /source/i }), { target: { value: "lever" } });

    await waitFor(() =>
      expect(fetchMock().mock.calls.some(([url]) => String(url).includes("source=lever"))).toBe(true),
    );
  });

  it("runs discovery, shows progress while it runs, and reports what it found", async () => {
    const api = stubApi({ holdDiscover: true });
      renderJobs(makeClient());

    await screen.findByText("Staff Engineer");
    fireEvent.click(screen.getByRole("button", { name: "Discover" }));

    const button = await screen.findByRole("button", { name: "Discovering…" });
    expect(button).toBeDisabled();
    expect(
      fetchMock().mock.calls.some(([url]) => String(url).startsWith("/api/v1/jobs/discover")),
    ).toBe(true);

    api.release();

    await waitFor(() => expect(screen.getByRole("button", { name: "Discover" })).toBeEnabled());
    expect(await screen.findByText(/12 discovered/i)).toBeInTheDocument();
  });

  it("restricts a discovery run to the source currently selected", async () => {
    stubApi();
      renderJobs(makeClient());

    await screen.findByText("Staff Engineer");
    fireEvent.change(screen.getByRole("combobox", { name: /source/i }), { target: { value: "greenhouse" } });
    fireEvent.click(screen.getByRole("button", { name: "Discover" }));

    await waitFor(() =>
      expect(
        fetchMock().mock.calls.some(([url]) =>
          String(url).startsWith("/api/v1/jobs/discover?sources=greenhouse"),
        ),
      ).toBe(true),
    );
  });

  it("scores discovered jobs and reports the count", async () => {
    stubApi();
      renderJobs(makeClient());

    await screen.findByText("Staff Engineer");
    fireEvent.click(screen.getByRole("button", { name: "Score" }));

    await waitFor(() =>
      expect(
        fetchMock().mock.calls.some(([url]) => String(url).startsWith("/api/v1/jobs/score")),
      ).toBe(true),
    );
    expect(await screen.findByText(/7 scored/i)).toBeInTheDocument();
  });

  it("offers discovery from the page rather than telling the user to use the CLI", async () => {
    stubApi({ jobs: { items: [], total: 0, limit: 50, offset: 0 } });
    renderJobs(makeClient());

    const empty = await screen.findByText("No jobs yet");
    expect(empty.parentElement?.textContent).not.toContain("applyuminati jobs discover");
    expect(screen.getByRole("button", { name: "Discover" })).toBeInTheDocument();
  });
});
