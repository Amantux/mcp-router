import { describe, expect, it } from "vitest";
import { compileSchema, describeConstraints, type FieldNode, type ObjectNode } from "./model";

function root(schema: unknown): ObjectNode {
  const c = compileSchema(schema);
  if (!c.ok) throw new Error(`expected representable, got: ${c.reason}`);
  return c.root;
}
const field = (r: ObjectNode, key: string): FieldNode => {
  const f = r.fields.find((x) => x.key === key);
  if (!f) throw new Error(`no field ${key}`);
  return f.node;
};

describe("compileSchema — scalars", () => {
  it("maps string/number/integer/boolean with their constraints and metadata", () => {
    const r = root({
      type: "object",
      properties: {
        q: { type: "string", title: "Query", description: "Search text", minLength: 2, maxLength: 80, pattern: "^[a-z]+$", format: "email" },
        ratio: { type: "number", minimum: 0, maximum: 1 },
        n: { type: "integer", exclusiveMinimum: 0, default: 10 },
        flag: { type: "boolean" },
      },
    });
    expect(field(r, "q")).toMatchObject({ kind: "string", title: "Query", description: "Search text", minLength: 2, maxLength: 80, pattern: "^[a-z]+$", format: "email" });
    expect(field(r, "ratio")).toMatchObject({ kind: "number", minimum: 0, maximum: 1 });
    expect(field(r, "n")).toMatchObject({ kind: "integer", exclusiveMinimum: 0, default: 10 });
    expect(field(r, "flag")).toMatchObject({ kind: "boolean" });
  });

  it("marks required fields from the `required` list only", () => {
    const r = root({ type: "object", properties: { a: { type: "string" }, b: { type: "string" } }, required: ["b", "not_a_prop"] });
    expect(r.fields.map((f) => [f.key, f.required])).toEqual([
      ["a", false],
      ["b", true],
    ]);
  });

  it("turns enum and const into a select; non-scalar enums stay raw", () => {
    const r = root({
      type: "object",
      properties: {
        state: { type: "string", enum: ["open", "closed", "all"] },
        mixed: { enum: [1, "two", true, null] },
        fixed: { const: "v1" },
        objs: { enum: [{ a: 1 }] },
        none: { enum: [] },
      },
    });
    expect(field(r, "state")).toMatchObject({ kind: "enum", options: ["open", "closed", "all"] });
    expect(field(r, "mixed")).toMatchObject({ kind: "enum", options: [1, "two", true, null] });
    expect(field(r, "fixed")).toMatchObject({ kind: "enum", options: ["v1"] });
    expect(field(r, "objs")).toMatchObject({ kind: "raw", reason: "enum of non-scalar values" });
    expect(field(r, "none").kind).toBe("raw");
  });

  it("unwraps nullable type unions and anyOf [X, null]; real unions stay raw", () => {
    const r = root({
      type: "object",
      properties: {
        a: { type: ["string", "null"] },
        b: { anyOf: [{ type: "integer", minimum: 1 }, { type: "null" }], description: "kept" },
        c: { type: ["string", "number"] },
        d: { oneOf: [{ type: "string" }, { type: "number" }] },
        e: { allOf: [{ type: "string" }] },
      },
    });
    expect(field(r, "a").kind).toBe("string");
    expect(field(r, "b")).toMatchObject({ kind: "integer", minimum: 1, description: "kept" });
    expect(field(r, "c")).toMatchObject({ kind: "raw", reason: "accepts several types (string, number)" });
    expect(field(r, "d")).toMatchObject({ kind: "raw", reason: "uses oneOf" });
    expect(field(r, "e")).toMatchObject({ kind: "raw", reason: "uses allOf" });
  });
});

describe("compileSchema — arrays", () => {
  it("represents arrays of scalars and of enums, with item constraints", () => {
    const r = root({
      type: "object",
      properties: {
        tags: { type: "array", items: { type: "string" }, minItems: 1, maxItems: 5, uniqueItems: true },
        ids: { type: "array", items: { type: "integer", minimum: 0 } },
        labels: { type: "array", items: { enum: ["bug", "feature"] } },
      },
    });
    expect(field(r, "tags")).toMatchObject({ kind: "array", item: { kind: "string" }, minItems: 1, maxItems: 5, uniqueItems: true });
    expect(field(r, "ids")).toMatchObject({ kind: "array", item: { kind: "integer", minimum: 0 } });
    expect(field(r, "labels")).toMatchObject({ kind: "array", item: { kind: "enum", options: ["bug", "feature"] } });
  });

  it("falls back to raw for arrays of objects, nested arrays, tuples, and undescribed items", () => {
    const r = root({
      type: "object",
      properties: {
        objs: { type: "array", items: { type: "object", properties: { a: { type: "string" } } } },
        grid: { type: "array", items: { type: "array", items: { type: "number" } } },
        tuple: { type: "array", items: [{ type: "string" }, { type: "number" }] },
        prefix: { type: "array", prefixItems: [{ type: "string" }] },
        bare: { type: "array" },
        anyItems: { type: "array", items: {} },
      },
    });
    expect(field(r, "objs")).toMatchObject({ kind: "raw", reason: "array of objects" });
    expect(field(r, "grid")).toMatchObject({ kind: "raw", reason: "array of arrays" });
    expect(field(r, "tuple")).toMatchObject({ kind: "raw", reason: "tuple arrays" });
    expect(field(r, "prefix")).toMatchObject({ kind: "raw", reason: "tuple arrays" });
    expect(field(r, "bare")).toMatchObject({ kind: "raw", reason: "array items are not described" });
    expect(field(r, "anyItems")).toMatchObject({ kind: "raw", reason: "array items: any JSON value is accepted" });
  });
});

describe("compileSchema — objects", () => {
  it("compiles nested objects recursively, keeping additionalProperties:false", () => {
    const r = root({
      type: "object",
      additionalProperties: false,
      properties: {
        filter: {
          type: "object",
          required: ["repo"],
          additionalProperties: false,
          properties: {
            repo: { type: "string" },
            window: { type: "object", properties: { days: { type: "integer" } } },
          },
        },
      },
    });
    expect(r.additionalProperties).toBe(false);
    const f = field(r, "filter") as ObjectNode;
    expect(f).toMatchObject({ kind: "object", additionalProperties: false });
    expect(f.fields.map((x) => [x.key, x.required, x.node.kind])).toEqual([
      ["repo", true, "string"],
      ["window", false, "object"],
    ]);
    expect((f.fields[1].node as ObjectNode).fields[0].node.kind).toBe("integer");
  });

  it("infers object/array type from properties/items when `type` is missing", () => {
    const r = root({ properties: { a: { properties: { b: { type: "string" } } }, c: { items: { type: "string" } } } });
    expect(field(r, "a").kind).toBe("object");
    expect(field(r, "c").kind).toBe("array");
  });

  it("treats free-form maps and pattern-keyed objects as raw", () => {
    const r = root({
      type: "object",
      properties: {
        headers: { type: "object", additionalProperties: { type: "string" } },
        byPattern: { type: "object", patternProperties: { "^x-": { type: "string" } } },
        open: { type: "object" },
      },
    });
    expect(field(r, "headers")).toMatchObject({ kind: "raw", reason: "free-form map of values" });
    expect(field(r, "byPattern")).toMatchObject({ kind: "raw", reason: "keys defined by pattern" });
    // An object with no declared properties is an (empty) group, not raw.
    expect(field(r, "open")).toMatchObject({ kind: "object", fields: [] });
  });
});

describe("compileSchema — $ref", () => {
  it("resolves local $defs/definitions refs, merging sibling keywords", () => {
    const r = root({
      type: "object",
      $defs: { Repo: { type: "string", pattern: "^[\\w-]+/[\\w-]+$" } },
      definitions: { Page: { type: "object", properties: { size: { type: "integer" } } } },
      properties: { repo: { $ref: "#/$defs/Repo", description: "owner/name" }, page: { $ref: "#/definitions/Page" } },
    });
    expect(field(r, "repo")).toMatchObject({ kind: "string", pattern: "^[\\w-]+/[\\w-]+$", description: "owner/name" });
    expect(field(r, "page").kind).toBe("object");
  });

  it("stops at recursive, remote, and broken refs with a raw field", () => {
    const r = root({
      type: "object",
      $defs: { Node: { type: "object", properties: { child: { $ref: "#/$defs/Node" } } } },
      properties: {
        tree: { $ref: "#/$defs/Node" },
        remote: { $ref: "https://example.com/schema.json" },
        broken: { $ref: "#/$defs/Missing" },
      },
    });
    const tree = field(r, "tree") as ObjectNode;
    expect(tree.fields[0].node).toMatchObject({ kind: "raw", reason: "recursive schema" });
    expect(field(r, "remote")).toMatchObject({ kind: "raw", reason: "unresolvable reference https://example.com/schema.json" });
    expect(field(r, "broken").kind).toBe("raw");
  });
});

describe("compileSchema — root", () => {
  it("accepts a missing or empty schema as 'no arguments'", () => {
    expect(compileSchema(undefined)).toEqual({ ok: true, root: { kind: "object", fields: [], additionalProperties: true } });
    expect(compileSchema({})).toMatchObject({ ok: true });
    expect(compileSchema({ type: "object", properties: {} })).toMatchObject({ ok: true, root: { fields: [] } });
  });

  it("makes the whole schema unrepresentable when the root isn't an object", () => {
    expect(compileSchema({ type: "string" })).toEqual({ ok: false, reason: "the tool takes a bare string, not named arguments" });
    expect(compileSchema({ oneOf: [{ type: "object" }, { type: "string" }] })).toEqual({ ok: false, reason: "uses oneOf" });
    expect(compileSchema({ type: "object", additionalProperties: { type: "number" } })).toEqual({ ok: false, reason: "free-form map of values" });
    expect(compileSchema([1])).toEqual({ ok: false, reason: "the input schema is not a JSON object" });
  });
});

describe("describeConstraints", () => {
  it("states format and constraints as text", () => {
    const r = root({
      type: "object",
      properties: {
        when: { type: "string", format: "date-time" },
        n: { type: "integer", minimum: 1, maximum: 100 },
        name: { type: "string", minLength: 3, maxLength: 20 },
        ids: { type: "array", items: { type: "integer" }, maxItems: 3, uniqueItems: true },
      },
    });
    expect(describeConstraints(field(r, "when"))).toBe("Format: date-time (e.g. 2026-10-08T14:30:00Z)");
    expect(describeConstraints(field(r, "n"))).toBe("whole number · ≥ 1 · ≤ 100");
    expect(describeConstraints(field(r, "name"))).toBe("3–20 characters");
    expect(describeConstraints(field(r, "ids"))).toBe("one integer per line · at most 3 · no duplicates");
  });
});
