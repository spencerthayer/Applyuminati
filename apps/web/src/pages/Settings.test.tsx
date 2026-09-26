import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import { Settings } from "./Settings";

const GREENHOUSE = {
  slug: "greenhouse",
  name: "Greenhouse",
  description: "",
  tier: "direct_ats",
  ats: "greenhouse",
  enabled: true,
  capabilities: [],
  requires_auth: false,
  blocking: "none",
  options: { boards: ["acme", "globex"], account: "acme" },
  options_schema: {
    title: "GreenhouseOptions",
    type: "object",
    properties: {
      boards: { items: { type: "string" }, title: "Boards", type: "array" },
      account: { title: "Account", description: "Board subdomain", type: "string" },
    },
  },
  last_run_jobs: 0,
  consecutive_failures: 0,
};

const LEVER = {
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
  options_schema: {
    title: "LeverOptions",
    type: "object",
    properties: {
      companies: { items: { type: "string" }, title: "Companies", type: "array" },
    },
  },
  last_run_jobs: 0,
  consecutive_failures: 0,
};

const LOCAL_FEED = {
  slug: "local_feed",
  name: "Local feed",
  description: "",
  tier: "derived",
  ats: "custom",
  enabled: false,
  capabilities: [],
  requires_auth: false,
  blocking: "none",
  options: {},
  options_schema: null,
  last_run_jobs: 0,
  consecutive_failures: 0,
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

function stubApi() {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/v1/sources/")) return json(GREENHOUSE);
    if (url.startsWith("/api/v1/sources")) return json([GREENHOUSE, LEVER, LOCAL_FEED]);
    if (url.startsWith("/api/v1/settings")) {
      return json({
        execution_mode: "autonomous_submit",
        data_dir: "/data",
        database: "sqlite",
        log_level: "info",
        llm_enabled: false,
        providers: [],
        browser_preferred: [],
        agents_enabled: false,
        agents_preferred: [],
        email_accounts: [],
        strategy: null,
      });
    }
    if (url.startsWith("/api/v1/health/backends")) {
      return json({ llm: [], browsers: [], agents: [], email: [] });
    }
    throw new Error(`unexpected request in Settings test: ${url}`);
  }) as unknown as typeof fetch;
}

function renderSettings() {
  return render(
    <QueryClientProvider client={makeClient()}>
      <Settings />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  stubApi();
});

describe("Settings source options", () => {
  it("builds the options form from the schema the plugin publishes", async () => {
    renderSettings();

    expect(await screen.findByLabelText("Boards")).toHaveValue("acme, globex");
    expect(screen.getByLabelText("Account")).toHaveValue("acme");
  });

  it("prefills from the options the source already has stored", async () => {
    renderSettings();

    expect(await screen.findByLabelText("Boards")).toHaveValue("acme, globex");
  });

  it("sends a comma-separated input as a trimmed list of strings", async () => {
    renderSettings();

    fireEvent.change(await screen.findByLabelText("Boards"), { target: { value: "a, b ,c" } });
    fireEvent.click(screen.getByRole("button", { name: "Save options" }));

    await waitFor(() =>
      expect(
        fetchMock().mock.calls.some(
          ([url]) => String(url) === "/api/v1/sources/greenhouse/enable",
        ),
      ).toBe(true),
    );
    const call = fetchMock().mock.calls.find(
      ([url]) => String(url) === "/api/v1/sources/greenhouse/enable",
    );
    expect(JSON.parse(String(initOf(call).body))).toEqual({
      options: { boards: ["a", "b", "c"], account: "acme" },
    });
  });

  it("sends an empty list when a list field is cleared", async () => {
    renderSettings();

    fireEvent.change(await screen.findByLabelText("Boards"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save options" }));

    await waitFor(() =>
      expect(
        fetchMock().mock.calls.some(
          ([url]) => String(url) === "/api/v1/sources/greenhouse/enable",
        ),
      ).toBe(true),
    );
    const call = fetchMock().mock.calls.find(
      ([url]) => String(url) === "/api/v1/sources/greenhouse/enable",
    );
    expect(JSON.parse(String(initOf(call).body)).options.boards).toEqual([]);
  });

  it("shows a disabled source's options read-only, and no form for a source with no schema", async () => {
    renderSettings();

    await screen.findByLabelText("Boards");
    expect(screen.getAllByRole("button", { name: "Save options" })).toHaveLength(1);
    expect(screen.getByLabelText("Companies")).toBeDisabled();
    expect(screen.getByText(/enable this source to configure its options/i)).toBeInTheDocument();
    // `local_feed` publishes no schema at all, so it gets no options section.
    expect(screen.queryByText("Local feed options")).not.toBeInTheDocument();
  });
});
