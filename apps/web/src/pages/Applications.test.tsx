import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import { Applications } from "./Applications";

const SUMMARY = {
  id: "app-1",
  job_id: "job-1",
  job_title: "Staff Engineer",
  company: "Acme",
  state: "shortlisted" as const,
  fit_score: 0.9,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-02T00:00:00Z",
  submitted_at: null,
  needs_attention: false,
};

const OTHER = {
  ...SUMMARY,
  id: "app-2",
  job_id: "job-2",
  job_title: "Platform Engineer",
  company: "Globex",
  state: "interview" as const,
};

const DETAIL = {
  ...SUMMARY,
  external_reference: null,
  notes: null,
  events: [
    {
      id: "ev-1",
      occurred_at: "2026-09-01T00:00:00Z",
      from_state: null,
      to_state: "discovered" as const,
      actor: "user",
      actor_detail: null,
      reason: "job discovered",
      message: null,
      failure_category: null,
    },
  ],
  artifacts: [],
  allowed_transitions: ["ready", "skipped", "withdrawn", "needs_attention"],
};

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function renderApplications(entry: string) {
  return render(
    <QueryClientProvider client={makeClient()}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/applications" element={<Applications />} />
          <Route path="/applications/:id" element={<Applications />} />
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

function stubApi(items: unknown[] = [SUMMARY, OTHER], detail: unknown = DETAIL) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const detailMatch = url.match(/^\/api\/v1\/applications\/([^/?]+)/);
    if (detailMatch) {
      if (detailMatch[1] === "app-1") return json(detail);
      return json({ ...(detail as typeof DETAIL), ...OTHER });
    }
    if (url.startsWith("/api/v1/applications")) {
      return json({ items, total: items.length, limit: 50, offset: 0 });
    }
    throw new Error(`unexpected request in Applications test: ${url}`);
  }) as unknown as typeof fetch;
}

beforeEach(() => {
  stubApi();
});

describe("Applications", () => {
  it("lists applications from the server", async () => {
    renderApplications("/applications");
    expect(await screen.findByText("Staff Engineer")).toBeInTheDocument();
    expect(screen.getByText("Platform Engineer")).toBeInTheDocument();
    const row = screen.getByRole("link", { name: "Platform Engineer" }).closest("tr");
    expect(within(row as HTMLElement).getByText("interview")).toBeInTheDocument();
  });

  it("filters the list to one job when arriving from a job's apply action", async () => {
    renderApplications("/applications?job=job-2");
    expect(await screen.findByText("Platform Engineer")).toBeInTheDocument();
    expect(screen.queryByText("Staff Engineer")).not.toBeInTheDocument();
  });

  it("opens a detail view for the selected application", async () => {
    renderApplications("/applications");
    fireEvent.click(await screen.findByRole("link", { name: "Staff Engineer" }));

    expect(await screen.findByText("job discovered")).toBeInTheDocument();
  });

  it("offers exactly the transitions the server says are legal", async () => {
    renderApplications("/applications/app-1");

    expect(await screen.findByRole("button", { name: "Move to ready" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move to skipped" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move to withdrawn" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move to needs_attention" })).toBeInTheDocument();
    // `interview` is legal from `interview`'s neighbours, not from `shortlisted`.
    expect(screen.queryByRole("button", { name: "Move to interview" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Move to applied/ })).not.toBeInTheDocument();
  });

  it("posts the chosen target state when a transition is clicked", async () => {
    renderApplications("/applications/app-1");

    fireEvent.click(await screen.findByRole("button", { name: "Move to withdrawn" }));

    await waitFor(() =>
      expect(
        fetchMock().mock.calls.some(
          ([url]) => String(url) === "/api/v1/applications/app-1/transition",
        ),
      ).toBe(true),
    );
    const call = fetchMock().mock.calls.find(
      ([url]) => String(url) === "/api/v1/applications/app-1/transition",
    );
    expect(initOf(call).method).toBe("POST");
    expect(initOf(call).body).toBe(JSON.stringify({ to_state: "withdrawn" }));
  });

  it("reports a transition the server refuses", async () => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/transition")) return json({ detail: "illegal transition" }, 422);
      if (url.match(/^\/api\/v1\/applications\/[^/?]+/)) return json(DETAIL);
      return json({ items: [SUMMARY, OTHER], total: 2, limit: 50, offset: 0 });
    }) as unknown as typeof fetch;
    renderApplications("/applications/app-1");

    fireEvent.click(await screen.findByRole("button", { name: "Move to ready" }));

    expect(await screen.findByText("illegal transition")).toBeInTheDocument();
  });
});
