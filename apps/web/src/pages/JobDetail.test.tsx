import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import { JobDetail } from "./JobDetail";

const JOB = {
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
  description: "Do the work.",
  requirements: [],
  preferred_qualifications: [],
  skills: [],
  locations: [],
  source_records: [],
  score: null,
  merged_job_ids: [],
  available_actions: [],
};

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function renderDetail(client: QueryClient) {
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/jobs/job-1"]}>
        <Routes>
          <Route path="/jobs/:id" element={<JobDetail />} />
          <Route path="/applications" element={<h1>Applications</h1>} />
          <Route path="/profile" element={<h1>Profile</h1>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function fetchMock(): Mock<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>> {
  return globalThis.fetch as unknown as Mock<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>;
}

/** The `RequestInit` of one recorded call. The client always sends one. */
function initOf(call: [RequestInfo | URL, RequestInit?] | undefined): RequestInit {
  return call?.[1] ?? {};
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  globalThis.fetch = vi.fn(async () => json(JOB)) as unknown as typeof fetch;
});

describe("JobDetail", () => {
  it("starts an application with the execution mode the user chose", async () => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith("/apply")) {
        return json({ attempt_id: "att-9", state: "applying", driver: "playwright" }, 201);
      }
      return json(JOB);
    }) as unknown as typeof fetch;
    renderDetail(makeClient());

    await screen.findByRole("heading", { name: "Staff Engineer" });
    fireEvent.change(screen.getByRole("combobox", { name: /execution mode/i }), {
      target: { value: "fill_no_submit" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() =>
      expect(
        fetchMock().mock.calls.some(([url]) => String(url) === "/api/v1/jobs/job-1/apply"),
      ).toBe(true),
    );
    const call = fetchMock().mock.calls.find(([url]) => String(url) === "/api/v1/jobs/job-1/apply");
    expect(initOf(call).method).toBe("POST");
    expect(initOf(call).body).toBe(JSON.stringify({ mode: "fill_no_submit" }));
    expect(await screen.findByText("att-9")).toBeInTheDocument();
    expect(screen.getByText(/application started/i)).toBeInTheDocument();
  });

  it("sends a null mode when the user defers to the server default", async () => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith("/apply")) {
        return json({ attempt_id: "att-1", state: "applying", driver: "playwright" }, 201);
      }
      return json(JOB);
    }) as unknown as typeof fetch;
    renderDetail(makeClient());

    await screen.findByRole("heading", { name: "Staff Engineer" });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() =>
      expect(
        fetchMock().mock.calls.some(([url]) => String(url) === "/api/v1/jobs/job-1/apply"),
      ).toBe(true),
    );
    const call = fetchMock().mock.calls.find(([url]) => String(url) === "/api/v1/jobs/job-1/apply");
    expect(initOf(call).body).toBe(JSON.stringify({ mode: null }));
  });

  it("shows that the application is already in progress and links to it on a 409", async () => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith("/apply")) {
        return json({ detail: "an application for this job is already in progress" }, 409);
      }
      return json(JOB);
    }) as unknown as typeof fetch;
    renderDetail(makeClient());

    await screen.findByRole("heading", { name: "Staff Engineer" });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    expect(
      await screen.findByText(/an application for this job is already in progress/i),
    ).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /view the application/i });
    expect(link).toHaveAttribute("href", "/applications?job=job-1");
  });

  it("tells the user to import a profile when applying returns a 400", async () => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith("/apply")) {
        return json({ detail: "import a career profile before applying" }, 400);
      }
      return json(JOB);
    }) as unknown as typeof fetch;
    renderDetail(makeClient());

    await screen.findByRole("heading", { name: "Staff Engineer" });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    expect(await screen.findByText(/import a career profile before applying/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /import a profile/i })).toHaveAttribute("href", "/profile");
  });
});
