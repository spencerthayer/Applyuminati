/**
 * Rendering a source's options form from the JSON Schema its own plugin
 * supplies.
 *
 * A plugin publishes `options_schema` (a Pydantic `model_json_schema()`) on
 * `SourceInfo`, and that is the single description of what the plugin accepts.
 * Generating the form from it means a new source — or a new option on an
 * existing one — needs no change here at all.
 *
 * Only two shapes are rendered, because only two are declared by any plugin
 * today: a `string` becomes a text input, and an `array` of `string` becomes a
 * comma-separated input. Anything else is left out of the form *and* left
 * untouched on save (see `buildOptionsBody`), so an unsupported field can never
 * be silently deleted from a user's configuration.
 */

import { isRecord, readString, readStringArray } from "./guards";
import type { JsonObject } from "../api/types";

/** How one option is edited. */
export type OptionInputKind = "text" | "list";

export interface OptionField {
  /** The JSON Schema property name, i.e. the key sent in the options body. */
  name: string;
  label: string;
  kind: OptionInputKind;
  description: string;
  /** A `string | null` field: blank means "not set", not an empty string. */
  nullable: boolean;
}

/**
 * Split a comma-separated input into trimmed, non-empty values.
 *
 * `"a, b ,c"` becomes `["a", "b", "c"]` and `""` becomes `[]`, so clearing a
 * field is an empty list rather than a list holding one empty string.
 */
export function parseListInput(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter((item) => item !== "");
}

/** Render an existing option value back into its editable form. */
export function formatOptionValue(field: OptionField, value: unknown): string {
  if (field.kind === "list") return readStringArray({ v: value }, "v").join(", ");
  return typeof value === "string" ? value : "";
}

/** The fields this app can edit, in schema order. */
export function readOptionFields(schema: JsonObject | null | undefined): OptionField[] {
  if (!isRecord(schema)) return [];
  const properties = schema.properties;
  if (!isRecord(properties)) return [];

  const fields: OptionField[] = [];
  for (const [name, raw] of Object.entries(properties)) {
    if (!isRecord(raw)) continue;
    const field = describeField(name, raw);
    if (field) fields.push(field);
  }
  return fields;
}

function describeField(name: string, schema: Record<string, unknown>): OptionField | null {
  const label = readString(schema, "title", name);
  const description = readString(schema, "description", "");
  if (schema.type === "array" && isRecord(schema.items) && schema.items.type === "string") {
    return { name, label, kind: "list", description, nullable: false };
  }
  if (schema.type === "string") {
    return { name, label, kind: "text", description, nullable: false };
  }
  // A nullable string is declared as `anyOf: [{type: "string"}, {type: "null"}]`
  // with no top-level `type`, so it needs its own branch or a real option the
  // plugin accepts would be uneditable.
  const branches = Array.isArray(schema.anyOf) ? schema.anyOf.filter(isRecord) : [];
  const branchTypes = branches.map((branch) => branch.type);
  if (
    branchTypes.includes("string") &&
    branchTypes.every((type) => type === "string" || type === "null")
  ) {
    return { name, label, kind: "text", description, nullable: true };
  }
  return null;
}

/**
 * Build the options body for a save.
 *
 * Rendered fields overwrite the plugin's current options; every other key is
 * carried through untouched, so a field this app cannot render keeps whatever
 * value the user already had.
 */
export function buildOptionsBody(
  fields: OptionField[],
  draft: Record<string, string>,
  current: JsonObject,
): JsonObject {
  const body: JsonObject = { ...current };
  for (const field of fields) {
    const raw = draft[field.name] ?? "";
    if (field.kind === "list") {
      body[field.name] = parseListInput(raw);
    } else {
      body[field.name] = field.nullable && raw.trim() === "" ? null : raw;
    }
  }
  return body;
}
