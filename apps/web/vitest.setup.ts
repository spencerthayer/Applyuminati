/**
 * Vitest setup: jest-dom matchers plus a hard guard against real network I/O.
 *
 * Every test in this suite must pass offline. `fetch` is replaced with a stub
 * that throws, so any test that forgets to mock a request fails loudly with a
 * useful message instead of hanging or silently hitting a dev server.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

beforeEach(() => {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    throw new Error(
      `unmocked fetch in test: ${String(input)} — stub globalThis.fetch for this case`,
    );
  }) as unknown as typeof fetch;
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
