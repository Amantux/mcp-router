import { describe, expect, it } from "vitest";
import { compileSchema, type ObjectNode } from "./model";
import { fromArguments, initialDraft, mapServerErrors, toArguments, type Draft, type DraftObject } from "./draft";

function root(schema: unknown): ObjectNode {
  const c = compileSchema(schema);
  if (!c.ok) throw new Error(c.reason);
  return c.root;
}

const ISSUES = root({
  type: "object",
  additionalProperties: false,
  required: ["repo", "state"],
  properties: {
    repo: { type: "string", minLength: 3 },
    state: { type: "string", enum: ["open", "closed"], default: "open" },
    limit: { type: "integer", minimum: 1, maximum: 100, default: 30 },
    includeDrafts: { type: "boolean" },
    archived: { type: "boolean", default: false },
    labels: { type: "array", items: { type: "string" } },
    kinds: { type: "array", items: { enum: ["bug", "feature", "chore"] } },
    since: {
      type: "object",
      required: ["days"],
      properties: { days: { type: "integer", minimum: 1 }, tz: { type: "string" } },
    },
    extra: { type: "object", additionalProperties: { type: "string" } },
  },
});

describe("initialDraft", () => {
  it("applies defaults (scalars, enum, boolean) and leaves the rest blank", () => {
    const d = initialDraft(ISSUES, true) as DraftObject;
    expect(d.state).toBe("0");
    expect(d.limit).toBe("30");
    expect(d.archived).toBe("false");
    expect(d.includeDrafts).toBe("");
    expect(d.repo).toBe("");
    expect(d.kinds).toEqual([]);
    expect(d.since).toEqual({ days: "", tz: "" });
    expect(d.extra).toBe("");
  });

  it("starts required booleans at false and preselects a single-option required enum", () => {
    const r = root({ type: "object", required: ["ok", "v"], properties: { ok: { type: "boolean" }, v: { const: "1" } } });
    expect(initialDraft(r)).toEqual({ ok: "false", v: "0" });
  });

  it("uses an object default for a nested group", () => {
    const r = root({ type: "object", properties: { o: { type: "object", properties: { a: { type: "string" } }, default: { a: "x" } } } });
    expect(initialDraft(r)).toEqual({ o: { a: "x" } });
  });
});

describe("toArguments (form → JSON)", () => {
  it("emits typed values, omits blank optional fields, and reports nothing when valid", () => {
    const d = initialDraft(ISSUES) as DraftObject;
    const { value, errors } = toArguments(ISSUES, { ...d, repo: "acme/api", labels: "p1\n\n p2 \n", kinds: ["2", "0"] });
    expect(errors).toEqual({});
    expect(value).toEqual({ repo: "acme/api", state: "open", limit: 30, archived: false, labels: ["p1", " p2 "], kinds: ["chore", "bug"] });
  });

  it("flags required fields, type errors and bounds at their dotted paths", () => {
    const d = initialDraft(ISSUES) as DraftObject;
    const { errors } = toArguments(ISSUES, { ...d, repo: "ab", state: "", limit: "1.5", since: { days: "0", tz: "" } });
    expect(errors).toEqual({
      repo: "Use at least 3 characters.",
      state: "Required.",
      limit: "Enter a whole number.",
      "since.days": "Must be at least 1.",
    });
  });

  it("omits an untouched optional nested group, but enforces its required children once used", () => {
    const d = initialDraft(ISSUES) as DraftObject;
    const blank = toArguments(ISSUES, { ...d, repo: "abc" });
    expect(blank.errors).toEqual({});
    expect(blank.value).not.toHaveProperty("since");
    const used = toArguments(ISSUES, { ...d, repo: "abc", since: { days: "", tz: "UTC" } });
    expect(used.errors).toEqual({ "since.days": "Required." });
    expect(used.value.since).toEqual({ tz: "UTC" });
  });

  it("form → JSON always yields a value even with invalid inputs (they are dropped)", () => {
    const d = initialDraft(ISSUES) as DraftObject;
    const { value, errors } = toArguments(ISSUES, { ...d, repo: "abc", limit: "lots" });
    expect(errors.limit).toBe("Enter a number.");
    expect(value).toEqual({ repo: "abc", state: "open", archived: false });
  });

  it("parses raw JSON fields and reports invalid JSON", () => {
    const d = initialDraft(ISSUES) as DraftObject;
    expect(toArguments(ISSUES, { ...d, repo: "abc", extra: '{"a":"b"}' }).value.extra).toEqual({ a: "b" });
    expect(toArguments(ISSUES, { ...d, repo: "abc", extra: "{nope" }).errors.extra).toBe("Not valid JSON.");
  });

  it("validates array items line by line, plus min/max/unique", () => {
    const r = root({
      type: "object",
      required: ["ids"],
      properties: { ids: { type: "array", items: { type: "integer" }, minItems: 2, maxItems: 3, uniqueItems: true } },
    });
    expect(toArguments(r, { ids: "1\nx\n2.5" }).errors.ids).toBe("Lines 2, 3: not a valid integer.");
    expect(toArguments(r, { ids: "1" }).errors.ids).toBe("Add at least 2 items.");
    expect(toArguments(r, { ids: "1\n1" }).errors.ids).toBe("Remove duplicate items.");
    expect(toArguments(r, { ids: "1\n2\n3\n4" }).errors.ids).toBe("Use at most 3 items.");
    // A required array left empty is sent as [] (key present), then minItems applies.
    expect(toArguments(r, { ids: "" })).toEqual({ value: { ids: [] }, errors: { ids: "Add at least 2 items." } });
  });

  it("parses boolean array items and checks string patterns", () => {
    const r = root({
      type: "object",
      properties: { flags: { type: "array", items: { type: "boolean" } }, code: { type: "string", pattern: "^[A-Z]{3}$" } },
    });
    expect(toArguments(r, { flags: "true\nfalse", code: "ABC" })).toEqual({ value: { flags: [true, false], code: "ABC" }, errors: {} });
    expect(toArguments(r, { flags: "", code: "abc" }).errors.code).toBe("Must match the pattern ^[A-Z]{3}$.");
  });

  it("leaves a pattern JS can't compile to the backend", () => {
    const r = root({ type: "object", properties: { x: { type: "string", pattern: "(?<!x" } } });
    expect(toArguments(r, { x: "anything" }).errors).toEqual({});
  });
});

describe("fromArguments (JSON → form, best effort)", () => {
  it("round-trips every representable value: toArguments(fromArguments(v)) === v", () => {
    const values = [
      { repo: "acme/api", state: "closed" },
      { repo: "acme/api", state: "open", limit: 5, includeDrafts: true, labels: ["a", "b c"], kinds: ["feature"] },
      { repo: "abc", state: "open", since: { days: 7, tz: "UTC" }, extra: { k: "v" } },
    ];
    for (const v of values) {
      const d = fromArguments(ISSUES, v);
      expect(d.ok).toBe(true);
      expect(toArguments(ISSUES, (d as { draft: Draft }).draft)).toEqual({ value: v, errors: {} });
    }
  });

  it("refuses keys the form doesn't have, naming them (additionalProperties false or not)", () => {
    expect(fromArguments(ISSUES, { repo: "abc", state: "open", token: "x" })).toEqual({ ok: false, reason: 'arguments: "token" is not a field in the form' });
    expect(fromArguments(ISSUES, { repo: "abc", since: { days: 1, offset: 2 } })).toEqual({ ok: false, reason: 'since: "offset" is not a field in the form' });
  });

  it("refuses type mismatches and values the form would lose", () => {
    expect(fromArguments(ISSUES, { limit: "30" })).toMatchObject({ ok: false, reason: "limit: expected a number" });
    expect(fromArguments(ISSUES, { limit: 2.5 })).toMatchObject({ ok: false, reason: "limit: expected a whole number" });
    expect(fromArguments(ISSUES, { state: "merged" })).toMatchObject({ ok: false, reason: "state: value is not one of the listed options" });
    expect(fromArguments(ISSUES, { repo: "" })).toMatchObject({ ok: false });
    expect(fromArguments(ISSUES, { labels: ["two\nlines"] })).toMatchObject({ ok: false, reason: "labels: a list item contains a line break" });
    expect(fromArguments(ISSUES, { labels: [] })).toMatchObject({ ok: false });
    expect(fromArguments(ISSUES, { kinds: ["bug", "bug"] })).toMatchObject({ ok: false });
    expect(fromArguments(ISSUES, { since: {} })).toMatchObject({ ok: false });
    expect(fromArguments(ISSUES, { includeDrafts: null })).toMatchObject({ ok: false });
    expect(fromArguments(ISSUES, [1, 2])).toEqual({ ok: false, reason: "arguments must be a JSON object" });
  });

  it("missing keys become blank fields (not defaults)", () => {
    const d = fromArguments(ISSUES, { repo: "abc" });
    expect(d.ok && (d.draft as DraftObject).limit).toBe("");
  });
});

describe("mapServerErrors (backend invalid_args → form paths)", () => {
  it("maps '$.a.b: keyword' to field paths and keeps root/unknown errors general", () => {
    const { fields, general } = mapServerErrors(["$.limit: maximum", "$.since.days: type", "$.labels[2]: type", "$: additionalProperties", "weird"]);
    expect(fields).toEqual({ limit: "Above the maximum.", "since.days": "Wrong type for this field.", labels: "Wrong type for this field." });
    expect(general).toEqual(["Arguments: Contains a field the tool doesn't accept.", "weird"]);
  });
});
