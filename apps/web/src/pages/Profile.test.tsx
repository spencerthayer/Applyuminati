import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import { Profile } from "./Profile";

const PROFILE = {
  id: "p-1",
  label: "default",
  resume: {},
  name: "Spencer",
  headline: "Engineer",
  email: "spencer@example.com",
  counts: { claims: 12 },
  targets: {
    titles: ["Staff Engineer"],
    seniority: "senior",
    remote_modes: ["remote"],
    employment_types: ["full_time"],
    locations: [{ raw: "Remote", city: null, region: null, country: null, postal_code: null, country_code: null }],
  },
  strategy: {
    depth_bias: 0.5,
    application_volume_bias: 0.5,
    title_exploration: 0.5,
    minimum_fit_score: 0.6,
    strictness: "soft",
    remote_preference: "remote_only",
    company_size_preference: "any",
    industry_preferences: [],
    freshness_days: 30,
  },
  claim_levels: { expert: 4 },
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
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

function renderProfile() {
  return render(
    <QueryClientProvider client={makeClient()}>
      <Profile />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  globalThis.fetch = vi.fn(async () => json(PROFILE)) as unknown as typeof fetch;
});

describe("Profile preferences", () => {
  it("seeds the editor from the profile's stored targets", async () => {
    renderProfile();

    expect(await screen.findByLabelText("Target titles")).toHaveValue("Staff Engineer");
    expect(screen.getByLabelText("Locations")).toHaveValue("Remote");
    expect(screen.getByLabelText("Seniority")).toHaveValue("senior");
  });

  it("submits only the preference fields the server declares", async () => {
    renderProfile();

    fireEvent.change(await screen.findByLabelText("Target titles"), {
      target: { value: "Staff Engineer, Principal Engineer" },
    });
    fireEvent.change(screen.getByLabelText("Locations"), { target: { value: "Remote, London" } });
    fireEvent.change(screen.getByLabelText("Seniority"), { target: { value: "staff" } });
    fireEvent.change(screen.getByLabelText("Minimum compensation"), { target: { value: "180000" } });
    fireEvent.change(screen.getByLabelText("Compensation currency"), { target: { value: "GBP" } });
    fireEvent.click(screen.getByLabelText("hybrid"));
    fireEvent.click(screen.getByRole("button", { name: "Save preferences" }));

    await waitFor(() =>
      expect(
        fetchMock().mock.calls.some(([url]) => String(url) === "/api/v1/profile/preferences"),
      ).toBe(true),
    );
    const call = fetchMock().mock.calls.find(
      ([url]) => String(url) === "/api/v1/profile/preferences",
    );
    expect(initOf(call).method).toBe("PUT");
    expect(JSON.parse(String(initOf(call).body))).toEqual({
      titles: ["Staff Engineer", "Principal Engineer"],
      locations: ["Remote", "London"],
      seniority: "staff",
      minimum_compensation: 180000,
      compensation_currency: "GBP",
      remote_modes: ["remote", "hybrid"],
      employment_types: ["full_time"],
    });
  });

  it("sends an empty list when every title is cleared", async () => {
    renderProfile();

    fireEvent.change(await screen.findByLabelText("Target titles"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save preferences" }));

    await waitFor(() =>
      expect(
        fetchMock().mock.calls.some(([url]) => String(url) === "/api/v1/profile/preferences"),
      ).toBe(true),
    );
    const call = fetchMock().mock.calls.find(
      ([url]) => String(url) === "/api/v1/profile/preferences",
    );
    expect(JSON.parse(String(initOf(call).body)).titles).toEqual([]);
  });

  it("reports a rejected save", async () => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith("/preferences")) return json({ detail: "no active profile" }, 404);
      return json(PROFILE);
    }) as unknown as typeof fetch;
    renderProfile();

    fireEvent.click(await screen.findByRole("button", { name: "Save preferences" }));

    expect(await screen.findByText("no active profile")).toBeInTheDocument();
  });
});
