import { describe, expect, it } from "vitest";

import type { JsonObject } from "../api/types";
import { buildOptionsBody, formatOptionValue, parseListInput, readOptionFields } from "./options";

/**
 * Two plugin schemas, verbatim in shape: Greenhouse (`list[str]` plus a plain
 * `str`) and LocalFeed (`list[str]` plus a `str | null`, which Pydantic emits
 * as `anyOf` with no top-level `type`).
 */
const SCHEMA: JsonObject = {
  title: "GreenhouseOptions",
  type: "object",
  properties: {
    boards: { items: { type: "string" }, title: "Boards", type: "array" },
    account: { title: "Account", description: "Board subdomain", type: "string" },
    retries: { title: "Retries", type: "integer" },
  },
};

const LOCAL_FEED_SCHEMA: JsonObject = {
  title: "LocalFeedOptions",
  type: "object",
  properties: {
    paths: { items: { format: "path", type: "string" }, title: "Paths", type: "array" },
    default_company: {
      anyOf: [{ type: "string" }, { type: "null" }],
      default: null,
      title: "Default Company",
    },
  },
};

describe("parseListInput", () => {
  it("trims whitespace around every comma-separated value", () => {
    expect(parseListInput("a, b ,c")).toEqual(["a", "b", "c"]);
  });

  it("treats an empty input as an empty list, not a list with one blank", () => {
    expect(parseListInput("")).toEqual([]);
  });

  it("drops blank entries left by trailing or doubled commas", () => {
    expect(parseListInput("a,,b,  ,")).toEqual(["a", "b"]);
  });

  it("keeps a single value untouched", () => {
    expect(parseListInput("  greenhouse  ")).toEqual(["greenhouse"]);
  });
});

describe("readOptionFields", () => {
  it("renders a string as a text field and an array of string as a list field", () => {
    const fields = readOptionFields(SCHEMA);
    expect(fields).toEqual([
      { name: "boards", label: "Boards", kind: "list", description: "", nullable: false },
      { name: "account", label: "Account", kind: "text", description: "Board subdomain", nullable: false },
    ]);
  });

  it("skips a property whose type this app cannot render", () => {
    expect(readOptionFields(SCHEMA).map((f) => f.name)).not.toContain("retries");
  });

  it("renders a nullable string as a text field, the way Pydantic declares it", () => {
    expect(readOptionFields(LOCAL_FEED_SCHEMA)).toEqual([
      { name: "paths", label: "Paths", kind: "list", description: "", nullable: false },
      {
        name: "default_company",
        label: "Default Company",
        kind: "text",
        description: "",
        nullable: true,
      },
    ]);
  });

  it("returns nothing for a missing, null or shapeless schema", () => {
    expect(readOptionFields(undefined)).toEqual([]);
    expect(readOptionFields(null)).toEqual([]);
    expect(readOptionFields({ type: "object" })).toEqual([]);
  });
});

describe("formatOptionValue", () => {
  it("renders a stored list back as comma-separated text", () => {
    const [boards] = readOptionFields(SCHEMA);
    expect(formatOptionValue(boards, ["a", "b"])).toBe("a, b");
  });

  it("renders an empty stored list as an empty input", () => {
    const [boards] = readOptionFields(SCHEMA);
    expect(formatOptionValue(boards, [])).toBe("");
  });

  it("renders a stored string as-is and anything else as empty", () => {
    const [, account] = readOptionFields(SCHEMA);
    expect(formatOptionValue(account, "acme")).toBe("acme");
    expect(formatOptionValue(account, 7)).toBe("");
  });
});

describe("buildOptionsBody", () => {
  it("sends a parsed list and a plain string for the rendered fields", () => {
    const fields = readOptionFields(SCHEMA);
    const body = buildOptionsBody(fields, { boards: "a, b ,c", account: "acme" }, {});
    expect(body).toEqual({ boards: ["a", "b", "c"], account: "acme" });
  });

  it("sends an empty list for a cleared list field", () => {
    const fields = readOptionFields(SCHEMA);
    expect(buildOptionsBody(fields, { boards: "", account: "" }, { boards: ["old"] })).toEqual({
      boards: [],
      account: "",
    });
  });

  it("sends null, not an empty string, for a cleared nullable string", () => {
    const fields = readOptionFields(LOCAL_FEED_SCHEMA);
    expect(buildOptionsBody(fields, { paths: "/a.json, /b.json", default_company: "" }, {})).toEqual({
      paths: ["/a.json", "/b.json"],
      default_company: null,
    });
  });

  it("keeps a typed value for a nullable string", () => {
    const fields = readOptionFields(LOCAL_FEED_SCHEMA);
    expect(
      buildOptionsBody(fields, { paths: "", default_company: "Acme" }, {}).default_company,
    ).toBe("Acme");
  });

  it("carries through options for fields this app does not render", () => {
    const fields = readOptionFields(SCHEMA);
    const body = buildOptionsBody(fields, { boards: "a", account: "acme" }, {
      retries: 3,
      board: "kept",
    });
    expect(body).toEqual({ retries: 3, board: "kept", boards: ["a"], account: "acme" });
  });
});
