/**
 * Minimal typed `fetch` wrapper.
 *
 * Every request uses a *relative* URL under `/api/v1`. That is deliberate and
 * load-bearing: the production bundle is served from the same origin as the API,
 * so relative URLs make the app work behind any hostname, port or reverse-proxy
 * prefix with no rebuild. There is no `VITE_API_URL` and there must never be one.
 *
 * The other job of this module is honest errors. The backend speaks one error
 * envelope (`ErrorResponse`: code/category/message/recovery/retryable/details)
 * and this wrapper turns it back into a typed `ApiError`, so the UI can always
 * tell the user *why* something failed and what to do next.
 */

import { isRecord, readString } from "../lib/guards";
import type { ErrorResponse, JsonObject } from "./types";

/** Every endpoint lives under this prefix. Relative on purpose. */
export const API_BASE = "/api/v1";

/** A value acceptable as a query-string parameter. */
export type QueryValue = string | number | boolean | null | undefined | readonly string[];

export type QueryParams = Record<string, QueryValue>;

/**
 * A failed API call, carrying the backend's error envelope.
 *
 * `category` and `recovery` are the Python `FailureCategory` / `RecoveryHint`
 * values, so the UI renders actionable text without duplicating the backend's
 * failure taxonomy.
 */
export class ApiError extends Error {
  readonly code: string;
  readonly category: string;
  readonly recovery: string;
  readonly retryable: boolean;
  readonly details: JsonObject;
  /** HTTP status, or 0 when the request never reached the server. */
  readonly status: number;

  constructor(init: {
    code: string;
    category: string;
    message: string;
    recovery: string;
    retryable?: boolean;
    details?: JsonObject;
    status?: number;
  }) {
    super(init.message);
    this.name = "ApiError";
    this.code = init.code;
    this.category = init.category;
    this.recovery = init.recovery;
    this.retryable = init.retryable ?? false;
    this.details = init.details ?? {};
    this.status = init.status ?? 0;
  }
}

/**
 * Serialise query parameters.
 *
 * `null`, `undefined` and `""` are dropped so "no filter" never becomes "filter
 * by empty string". Arrays repeat the key (`?states=ready&states=offer`), which
 * is what FastAPI expects for a `list[...]` query parameter.
 */
export function buildQuery(params: QueryParams | undefined): string {
  if (!params) return "";
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "") continue;
    if (Array.isArray(value)) {
      for (const item of value) {
        if (item !== "") search.append(key, item);
      }
    } else {
      search.append(key, String(value));
    }
  }
  const query = search.toString();
  return query ? `?${query}` : "";
}

/**
 * Recognise an `ErrorResponse` envelope.
 *
 * `code` and `message` discriminate it: FastAPI's own validation errors carry
 * neither, so they fall through to the generic path in `translateError`.
 */
function asErrorResponse(value: unknown): ErrorResponse | null {
  if (!isRecord(value)) return null;
  if (typeof value.code !== "string" || typeof value.message !== "string") return null;
  return {
    code: value.code,
    category: readString(value, "category", "unknown"),
    message: value.message,
    recovery: readString(value, "recovery", "report_bug"),
    retryable: value.retryable === true,
    details: isRecord(value.details) ? value.details : {},
  };
}

/** Render FastAPI's `422` validation payload as one readable line. */
function formatValidationDetail(detail: unknown[]): string {
  return detail
    .map((entry) => {
      if (!isRecord(entry)) return String(entry);
      const loc = Array.isArray(entry.loc) ? entry.loc.join(".") : "";
      const message = readString(entry, "msg", "invalid value");
      return loc ? `${loc}: ${message}` : message;
    })
    .join("; ");
}

/**
 * Translate any error body into an `ApiError`.
 *
 * Four shapes are accepted, in order of preference: the bare `ErrorResponse`
 * envelope; that envelope nested under FastAPI's `detail` key
 * (`HTTPException(detail=err.to_dict())`); a `422` validation list; and finally
 * anything else, including a plain string or a non-JSON body — reported as-is
 * rather than flattened into "unknown error".
 */
export function translateError(status: number, body: unknown, statusText = ""): ApiError {
  const direct = asErrorResponse(body);
  if (direct) return new ApiError({ ...direct, status });

  if (isRecord(body)) {
    const nested = asErrorResponse(body.detail);
    if (nested) return new ApiError({ ...nested, status });

    if (Array.isArray(body.detail)) {
      return new ApiError({
        code: "http.unprocessable_entity",
        category: "configuration",
        message: formatValidationDetail(body.detail),
        recovery: "fix_configuration",
        details: { detail: body.detail },
        status,
      });
    }
    if (typeof body.detail === "string" && body.detail !== "") {
      return new ApiError({
        code: `http.${status}`,
        category: status >= 500 ? "transient_network" : "unknown",
        message: body.detail,
        recovery: status >= 500 ? "retry_after_backoff" : "report_bug",
        retryable: status >= 500,
        status,
      });
    }
  }

  const text = typeof body === "string" ? body.trim() : "";
  return new ApiError({
    code: `http.${status}`,
    category: status >= 500 ? "transient_network" : "unknown",
    message: text || statusText || `request failed with status ${status}`,
    recovery: status >= 500 ? "retry_after_backoff" : "report_bug",
    retryable: status >= 500,
    status,
  });
}

/** Read a response body as JSON, falling back to the raw text. */
async function readBody(response: Response): Promise<unknown> {
  const raw = await response.text();
  if (raw === "") return null;
  try {
    return JSON.parse(raw) as unknown;
  } catch {
    return raw;
  }
}

interface RequestOptions {
  params?: QueryParams;
  body?: unknown;
  signal?: AbortSignal;
}

type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

async function request<T>(method: Method, path: string, options: RequestOptions = {}): Promise<T> {
  const url = `${API_BASE}${path}${buildQuery(options.params)}`;
  const hasBody = options.body !== undefined;

  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers: {
        Accept: "application/json",
        ...(hasBody ? { "Content-Type": "application/json" } : {}),
      },
      body: hasBody ? JSON.stringify(options.body) : undefined,
      signal: options.signal,
    });
  } catch (error) {
    // Transport-level failure: API down, unreachable, or DNS/CORS problem.
    // An abort is the caller's own doing, so it propagates untranslated.
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError({
      code: "network.unreachable",
      category: "transient_network",
      message: `cannot reach the Applyuminati API at ${url}`,
      recovery: "retry_after_backoff",
      retryable: true,
      details: { cause: error instanceof Error ? error.message : String(error) },
      status: 0,
    });
  }

  if (!response.ok) {
    throw translateError(response.status, await readBody(response), response.statusText);
  }
  if (response.status === 204) return undefined as T;
  return (await readBody(response)) as T;
}

export function get<T>(path: string, params?: QueryParams, signal?: AbortSignal): Promise<T> {
  return request<T>("GET", path, { params, signal });
}

export function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>("POST", path, { body: body ?? {} });
}

export function put<T>(path: string, body?: unknown): Promise<T> {
  return request<T>("PUT", path, { body: body ?? {} });
}

export function patch<T>(path: string, body?: unknown): Promise<T> {
  return request<T>("PATCH", path, { body: body ?? {} });
}
