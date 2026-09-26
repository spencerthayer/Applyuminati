import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

const applications = [
  {
    id: "app-1",
    job_id: "job-1",
    job_title: "Staff Engineer",
    company: "Acme",
    state: "shortlisted",
    fit_score: 0.9,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-02T00:00:00Z",
    submitted_at: null,
    needs_attention: false,
  },
];

vi.mock("./api/hooks", () => ({
  useSession: () => ({ data: { required: false, authenticated: true }, isPending: false }),
  useHealth: () => ({ data: { status: "ok" } }),
  useDashboard: () => ({ data: undefined, isLoading: false }),
  useDiscover: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null, data: undefined }),
  useInbox: () => ({ data: [] }),
  useLogout: () => ({ mutate: vi.fn(), isPending: false }),
  useApplications: () => ({
    data: { items: applications, total: 1, limit: 50, offset: 0 },
    isLoading: false,
  }),
  useApplication: () => ({ data: undefined, isLoading: false }),
  useTransitionApplication: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
}));

beforeEach(() => {
  globalThis.fetch = vi.fn(async () => new Response("null", { status: 200 })) as unknown as typeof fetch;
});

afterEach(() => {
  globalThis.history.pushState({}, "", "/");
});

describe("App routing", () => {
  it("serves the applications page at /applications", async () => {
    globalThis.history.pushState({}, "", "/applications");
    render(
      <QueryClientProvider client={new QueryClient()}>
        <App />
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("heading", { name: "Applications" })).toBeInTheDocument();
    expect(screen.getByText("Staff Engineer")).toBeInTheDocument();
  });

  it("offers an Applications entry in the navigation", async () => {
    globalThis.history.pushState({}, "", "/");
    render(
      <QueryClientProvider client={new QueryClient()}>
        <App />
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("link", { name: "Applications" })).toHaveAttribute(
      "href",
      "/applications",
    );
  });
});
