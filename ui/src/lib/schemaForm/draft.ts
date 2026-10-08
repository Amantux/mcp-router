/**
 * Form state ("draft") ⇄ JSON arguments. Pure functions, no React.
 *
 * Draft shapes, by node kind:
 *   string / number / integer → the text in the input ("" = unset)
 *   boolean                   → "" | "true" | "false"
 *   enum                      → index into options as text ("" = unset)
 *   array of enum             → selected option indices (string[])
 *   array of other scalars    → text, one item per line
 *   object                    → { [key]: Draft }
 *   raw                       → JSON text ("" = unset)
 *
 * Two directions:
 *   - toArguments(form → JSON) ALWAYS produces a value (unparseable inputs are
 *     dropped) plus a path-keyed error map; the form blocks Run on errors.
 *   - fromArguments(JSON → form) is best-effort: if the value holds anything
 *     the form can't show faithfully it returns a reason and the caller keeps
 *     the raw JSON.
 * Error keys are dotted paths ("filters.limit"); "" is the root object.
 */
import type { JsonObject, JsonValue } from "../../api/types";
import type { ArrayNode, FieldNode, ObjectNode, Scalar, ScalarNode } from "./model";

export type Draft = string | string[] | DraftObject;
export interface DraftObject {
  [key: string]: Draft;
}
export type FieldErrors = Record<string, string>;

const join = (path: string, key: string) => (path ? `${path}.${key}` : key);

function deepEqual(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/** Empty draft for a node, ignoring defaults (used for JSON→form of a missing key). */
export function emptyDraft(node: FieldNode): Draft {
  switch (node.kind) {
    case "object":
      return Object.fromEntries(node.fields.map((f) => [f.key, emptyDraft(f.node)]));
    case "array":
      return node.item.kind === "enum" ? [] : "";
    default:
      return "";
  }
}

/** Initial draft: schema defaults applied; required booleans start false; a single-option required enum is preselected. */
export function initialDraft(node: FieldNode, required = false): Draft {
  if (node.default !== undefined) {
    const fromDefault = draftFor(node, node.default);
    if (fromDefault.ok) return fromDefault.draft;
  }
  switch (node.kind) {
    case "object":
      return Object.fromEntries(node.fields.map((f) => [f.key, initialDraft(f.node, f.required)]));
    case "boolean":
      return required ? "false" : "";
    case "enum":
      return required && node.options.length === 1 ? "0" : "";
    default:
      return emptyDraft(node);
  }
}

// ------------------------------------------------------------ form → JSON
interface Out {
  value: JsonValue | undefined;
}

function parseScalar(node: ScalarNode, text: string): { ok: true; value: Scalar } | { ok: false; error: string } {
  const t = text.trim();
  switch (node.kind) {
    case "string":
      return { ok: true, value: text };
    case "number":
    case "integer": {
      const n = t === "" ? Number.NaN : Number(t);
      if (!Number.isFinite(n)) return { ok: false, error: "Enter a number." };
      if (node.kind === "integer" && !Number.isInteger(n)) return { ok: false, error: "Enter a whole number." };
      return { ok: true, value: n };
    }
    case "boolean":
      if (t === "true" || t === "false") return { ok: true, value: t === "true" };
      return { ok: false, error: "Enter true or false." };
    case "enum": {
      const i = Number(t);
      if (Number.isInteger(i) && i >= 0 && i < node.options.length) return { ok: true, value: node.options[i] };
      return { ok: false, error: "Pick one of the listed values." };
    }
  }
}

function checkScalar(node: ScalarNode, v: Scalar): string | undefined {
  if (node.kind === "string" && typeof v === "string") {
    if (node.minLength != null && v.length < node.minLength) return `Use at least ${node.minLength} characters.`;
    if (node.maxLength != null && v.length > node.maxLength) return `Use at most ${node.maxLength} characters.`;
    if (node.pattern) {
      let re: RegExp | null = null;
      try {
        re = new RegExp(node.pattern, "u");
      } catch {
        re = null; // a pattern JS can't compile is left to the backend validator
      }
      if (re && !re.test(v)) return `Must match the pattern ${node.pattern}.`;
    }
  }
  if ((node.kind === "number" || node.kind === "integer") && typeof v === "number") {
    if (node.minimum != null && v < node.minimum) return `Must be at least ${node.minimum}.`;
    if (node.maximum != null && v > node.maximum) return `Must be at most ${node.maximum}.`;
    if (node.exclusiveMinimum != null && v <= node.exclusiveMinimum) return `Must be greater than ${node.exclusiveMinimum}.`;
    if (node.exclusiveMaximum != null && v >= node.exclusiveMaximum) return `Must be less than ${node.exclusiveMaximum}.`;
  }
  return undefined;
}

function arrayOut(node: ArrayNode, draft: Draft, path: string, required: boolean, errors: FieldErrors): Out {
  const items: Scalar[] = [];
  if (node.item.kind === "enum") {
    const opts = node.item.options;
    for (const idx of Array.isArray(draft) ? draft : []) {
      const i = Number(idx);
      if (Number.isInteger(i) && i >= 0 && i < opts.length) items.push(opts[i]);
    }
  } else {
    const lines = (typeof draft === "string" ? draft : "").split("\n");
    const bad: number[] = [];
    lines.forEach((line, n) => {
      if (line.trim() === "") return;
      const p = parseScalar(node.item, node.item.kind === "string" ? line : line.trim());
      if (p.ok) {
        const e = checkScalar(node.item, p.value);
        if (e) bad.push(n + 1);
        else items.push(p.value);
      } else bad.push(n + 1);
    });
    if (bad.length) errors[path] = `Line${bad.length > 1 ? "s" : ""} ${bad.join(", ")}: not a valid ${node.item.kind}${describeItemRule(node)}.`;
  }
  if (items.length === 0 && !required) return { value: undefined };
  if (node.minItems != null && items.length < node.minItems) errors[path] ??= `Add at least ${node.minItems} item${node.minItems === 1 ? "" : "s"}.`;
  if (node.maxItems != null && items.length > node.maxItems) errors[path] ??= `Use at most ${node.maxItems} items.`;
  if (node.uniqueItems && new Set(items.map((i) => JSON.stringify(i))).size !== items.length) errors[path] ??= "Remove duplicate items.";
  return { value: items };
}

function describeItemRule(node: ArrayNode): string {
  const it = node.item;
  if (it.kind === "string" && (it.minLength != null || it.maxLength != null || it.pattern)) return " (check length/pattern)";
  if ((it.kind === "number" || it.kind === "integer") && (it.minimum != null || it.maximum != null)) return " (check range)";
  return "";
}

function nodeOut(node: FieldNode, draft: Draft, path: string, required: boolean, errors: FieldErrors): Out {
  switch (node.kind) {
    case "object":
      return objectOut(node, draft, path, required, errors);
    case "array":
      return arrayOut(node, draft, path, required, errors);
    case "raw": {
      const text = typeof draft === "string" ? draft.trim() : "";
      if (text === "") {
        if (required) errors[path] = "Required.";
        return { value: undefined };
      }
      try {
        return { value: JSON.parse(text) as JsonValue };
      } catch {
        errors[path] = "Not valid JSON.";
        return { value: undefined };
      }
    }
    default: {
      const text = typeof draft === "string" ? draft : "";
      if (text.trim() === "" && !(node.kind === "string" && text !== "")) {
        if (required) errors[path] = "Required.";
        return { value: undefined };
      }
      const p = parseScalar(node, text);
      if (!p.ok) {
        errors[path] = p.error;
        return { value: undefined };
      }
      const e = checkScalar(node, p.value);
      if (e) errors[path] = e;
      return { value: p.value };
    }
  }
}

function objectOut(node: ObjectNode, draft: Draft, path: string, required: boolean, errors: FieldErrors): Out {
  const d = (typeof draft === "object" && !Array.isArray(draft) ? draft : {}) as DraftObject;
  const childErrors: FieldErrors = {};
  const out: JsonObject = {};
  for (const f of node.fields) {
    const r = nodeOut(f.node, d[f.key] ?? emptyDraft(f.node), join(path, f.key), f.required, childErrors);
    if (r.value !== undefined) out[f.key] = r.value;
  }
  // An optional nested object the user left entirely blank is omitted, and its
  // children's "Required." errors don't apply (they only bind once it's used).
  if (!required && Object.keys(out).length === 0) {
    const onlyRequired = Object.values(childErrors).every((m) => m === "Required.");
    if (onlyRequired) return { value: undefined };
  }
  Object.assign(errors, childErrors);
  return { value: out };
}

/** Form → JSON arguments. Always yields an object; `errors` is empty when the form is valid. */
export function toArguments(root: ObjectNode, draft: Draft): { value: JsonObject; errors: FieldErrors } {
  const errors: FieldErrors = {};
  const r = objectOut(root, draft, "", true, errors);
  return { value: (r.value ?? {}) as JsonObject, errors };
}

// ------------------------------------------------------------ JSON → form
export type FromResult = { ok: true; draft: Draft } | { ok: false; reason: string };

const fail = (path: string, why: string): FromResult => ({ ok: false, reason: `${path || "arguments"}: ${why}` });

function draftFor(node: FieldNode, value: JsonValue, path = ""): FromResult {
  switch (node.kind) {
    case "raw":
      return { ok: true, draft: JSON.stringify(value, null, 2) };
    case "string":
      if (typeof value !== "string") return fail(path, "expected text");
      if (value === "") return fail(path, "an empty string can't be told apart from an unset field");
      return { ok: true, draft: value };
    case "number":
    case "integer":
      if (typeof value !== "number") return fail(path, "expected a number");
      if (node.kind === "integer" && !Number.isInteger(value)) return fail(path, "expected a whole number");
      return { ok: true, draft: String(value) };
    case "boolean":
      if (typeof value !== "boolean") return fail(path, "expected true or false");
      return { ok: true, draft: String(value) };
    case "enum": {
      const i = node.options.findIndex((o) => deepEqual(o, value));
      return i < 0 ? fail(path, "value is not one of the listed options") : { ok: true, draft: String(i) };
    }
    case "array": {
      if (!Array.isArray(value)) return fail(path, "expected a list");
      if (node.item.kind === "enum") {
        const opts = node.item.options;
        const idx: string[] = [];
        for (const v of value) {
          const i = opts.findIndex((o) => deepEqual(o, v));
          if (i < 0) return fail(path, "a list item is not one of the listed options");
          if (idx.includes(String(i))) return fail(path, "a repeated option can't be shown as a checkbox");
          idx.push(String(i));
        }
        return { ok: true, draft: idx };
      }
      if (value.length === 0) return fail(path, "an empty list can't be told apart from an unset field");
      const lines: string[] = [];
      for (const v of value) {
        const d = draftFor(node.item, v, path);
        if (!d.ok) return d;
        const text = d.draft as string;
        if (text.includes("\n")) return fail(path, "a list item contains a line break");
        if (node.item.kind === "string" && text.trim() !== text) return fail(path, "a list item has leading/trailing spaces");
        lines.push(text);
      }
      return { ok: true, draft: lines.join("\n") };
    }
    case "object": {
      if (value === null || typeof value !== "object" || Array.isArray(value)) return fail(path, "expected an object");
      const known = new Set(node.fields.map((f) => f.key));
      const extra = Object.keys(value).find((k) => !known.has(k));
      if (extra !== undefined) return fail(path, `"${extra}" is not a field in the form`);
      const out: DraftObject = {};
      for (const f of node.fields) {
        const v = (value as JsonObject)[f.key];
        if (v === undefined) {
          out[f.key] = emptyDraft(f.node);
          continue;
        }
        const d = draftFor(f.node, v, join(path, f.key));
        if (!d.ok) return d;
        out[f.key] = d.draft;
      }
      if (path && Object.keys(value).length === 0) return fail(path, "an empty object can't be told apart from an unset group");
      return { ok: true, draft: out };
    }
  }
}

/** JSON → form, best effort. On failure the caller keeps raw JSON and shows `reason`. */
export function fromArguments(root: ObjectNode, value: unknown): FromResult {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return { ok: false, reason: "arguments must be a JSON object" };
  return draftFor(root, value as JsonValue);
}

/**
 * Map backend invalid_args errors ("$.a.b: type", "$: additionalProperties")
 * onto form paths. Unmapped ones (root-level or unknown) are returned separately.
 */
export function mapServerErrors(errors: string[]): { fields: FieldErrors; general: string[] } {
  const fields: FieldErrors = {};
  const general: string[] = [];
  for (const e of errors) {
    const m = /^\$((?:\.[^:]+?|\[\d+\])*)\s*:\s*(.+)$/.exec(e.trim());
    if (!m) {
      general.push(e);
      continue;
    }
    const path = m[1].replace(/\[\d+\]/g, "").replace(/^\./, "");
    const msg = KEYWORD_COPY[m[2].trim()] ?? `Rejected by the tool's schema (${m[2].trim()}).`;
    if (!path) general.push(`Arguments: ${msg}`);
    else fields[path] = fields[path] ? fields[path] : msg;
  }
  return { fields, general };
}

const KEYWORD_COPY: Record<string, string> = {
  type: "Wrong type for this field.",
  required: "A required field inside this group is missing.",
  enum: "Not one of the allowed values.",
  const: "Not the allowed value.",
  minimum: "Below the minimum.",
  maximum: "Above the maximum.",
  exclusiveMinimum: "Below the minimum.",
  exclusiveMaximum: "Above the maximum.",
  minLength: "Too short.",
  maxLength: "Too long.",
  pattern: "Doesn't match the required pattern.",
  format: "Doesn't match the required format.",
  minItems: "Too few items.",
  maxItems: "Too many items.",
  uniqueItems: "Contains duplicate items.",
  additionalProperties: "Contains a field the tool doesn't accept.",
};
