import { describe, expect, it, vi } from "vitest";

import { ApiError, buildQuery, translateError, get, post } from "./client";

describe("buildQuery", () => {
  it("returns an empty string for no params", () => {
    expect(buildQuery(undefined)).toBe("");
    expect(buildQuery({})).toBe("");
  });

  it("drops null, undefined, and empty-string values", () => {
    expect(buildQuery({ a: null, b: undefined, c: "" })).toBe("");
  });

  it("serialises scalars", () => {
    expect(buildQuery({ q: "engineer", limit: 10, active: true })).toBe(
      "?q=engineer&limit=10&active=true",
    );
  });

  it("repeats the key for array values, skipping empty entries", () => {
    const query = buildQuery({ states: ["ready", "", "offer"] });
    expect(query).toBe("?states=ready&states=offer");
  });
});

describe("translateError", () => {
  it("recognises a direct ErrorResponse envelope", () => {
    const err = translateError(409, {
      code: "duplicate_action",
      category: "duplicate_action",
      message: "already applied",
      recovery: "report_bug",
      retryable: false,
    });
    expect(err).toBeInstanceOf(ApiError);
    expect(err.code).toBe("duplicate_action");
    expect(err.status).toBe(409);
    expect(err.message).toBe("already applied");
  });

  it("recognises an ErrorResponse nested under `detail`", () => {
    const err = translateError(422, {
      detail: { code: "configuration.bad_input", message: "bad field", category: "configuration" },
    });
    expect(err.code).toBe("configuration.bad_input");
    expect(err.message).toBe("bad field");
  });

  it("formats FastAPI's array-of-validation-errors `detail`", () => {
    const err = translateError(422, {
      detail: [{ loc: ["body", "title"], msg: "field required" }],
    });
    expect(err.code).toBe("http.unprocessable_entity");
    expect(err.message).toBe("body.title: field required");
  });

  it("uses a string `detail` as the message, marking 5xx retryable", () => {
    const err = translateError(503, { detail: "backend unavailable" });
    expect(err.retryable).toBe(true);
    expect(err.category).toBe("transient_network");
    expect(err.message).toBe("backend unavailable");
  });

  it("falls back to status text or a generic message", () => {
    const err = translateError(500, null, "Internal Server Error");
    expect(err.message).toBe("Internal Server Error");
    const err2 = translateError(404, null);
    expect(err2.message).toBe("request failed with status 404");
  });
});

describe("request transport failures", () => {
  it("translates a rejected fetch into a retryable ApiError with status 0", async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(get("/health")).rejects.toMatchObject({
      code: "network.unreachable",
      status: 0,
      retryable: true,
    });
  });

  it("re-throws an AbortError untranslated", async () => {
    const abort = new DOMException("aborted", "AbortError");
    globalThis.fetch = vi.fn().mockRejectedValue(abort);
    await expect(get("/health")).rejects.toBe(abort);
  });

  it("parses a successful JSON response", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const result = await get<{ status: string }>("/health");
    expect(result).toEqual({ status: "ok" });
  });

  it("sends a JSON body and Content-Type header for POST", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ id: "1" }), { status: 200 }));
    globalThis.fetch = fetchMock;
    await post("/jobs", { title: "Engineer" });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ title: "Engineer" }));
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });

  it("throws a translated ApiError for a non-ok response", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "not found" }), {
        status: 404,
        statusText: "Not Found",
      }),
    );
    await expect(get("/jobs/missing")).rejects.toMatchObject({ status: 404, message: "not found" });
  });
});
