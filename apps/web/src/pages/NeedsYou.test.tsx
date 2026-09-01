import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { InboxEntry } from "../api/types";
import { NeedsYou } from "./NeedsYou";

vi.mock("../api/hooks", () => ({
  useInbox: () => ({
    data: [
      {
        attempt_id: "att-1",
        application_id: "app-1",
        job_id: "job-1",
        company: "Acme",
        title: "Engineer",
        intervention_id: "int-1",
        reason: "authentication_required",
        instruction: "Sign in, then return here.",
        requires_browser_handoff: true,
        task_space_id: "applyuminati:att-1",
        host_presence: "offline",
        opened_at: "2026-08-31T00:00:00Z",
      } satisfies InboxEntry,
    ],
    isLoading: false,
  }),
  useResolveInbox: () => ({ mutate: vi.fn(), isPending: false }),
  useOpenBrowser: () => ({ mutate: vi.fn(), isPending: false }),
}));

describe("NeedsYou", () => {
  it("shows live host presence and a real Open browser action", () => {
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <NeedsYou />
      </QueryClientProvider>,
    );
    expect(screen.getByText("Mac offline")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open browser" })).toBeInTheDocument();
    expect(screen.getByText("Browser task: applyuminati:att-1")).toBeInTheDocument();
  });
});
