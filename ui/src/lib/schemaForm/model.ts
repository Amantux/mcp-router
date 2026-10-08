/**
 * JSON Schema → form model. Pure functions, no React.
 *
 * Compiles a tool's `inputSchema` into a tree of FieldNodes the form can
 * render. Anything the form can't represent faithfully becomes a `raw` node
 * (a per-field JSON editor), and a root that isn't an object makes the whole
 * schema unrepresentable (the playground then stays in raw-JSON mode).
 * The form never invents a constraint the schema doesn't state; the backend's
 * validator stays authoritative.
 */
import type { JsonValue } from "../../api/types";

export type Scalar = string | number | boolean | null;

interface Base {
  title?: string;
  description?: string;
  default?: JsonValue;
}

export interface StringNode extends Base {
  kind: "string";
  format?: string;
  minLength?: number;
  maxLength?: number;
  pattern?: string;
}
export interface NumberNode extends Base {
  kind: "number" | "integer";
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
}
export interface BooleanNode extends Base {
  kind: "boolean";
}
export interface EnumNode extends Base {
  kind: "enum";
  options: Scalar[];
}
export type ScalarNode = StringNode | NumberNode | BooleanNode | EnumNode;
export interface ArrayNode extends Base {
  kind: "array";
  item: ScalarNode;
  minItems?: number;
  maxItems?: number;
  uniqueItems?: boolean;
}
export interface ObjectField {
  key: string;
  node: FieldNode;
  required: boolean;
}
export interface ObjectNode extends Base {
  kind: "object";
  fields: ObjectField[];
  /** False when the schema forbids keys beyond `properties`. */
  additionalProperties: boolean;
}
/** A field the form can't represent: edited as raw JSON. `reason` is shown to the user. */
export interface RawNode extends Base {
  kind: "raw";
  reason: string;
}
export type FieldNode = ScalarNode | ArrayNode | ObjectNode | RawNode;

export type CompiledSchema = { ok: true; root: ObjectNode } | { ok: false; reason: string };

type Schema = Record<string, unknown>;

const isObj = (v: unknown): v is Schema => v !== null && typeof v === "object" && !Array.isArray(v);
const isScalar = (v: unknown): v is Scalar => v === null || ["string", "number", "boolean"].includes(typeof v);
const num = (v: unknown): number | undefined => (typeof v === "number" && Number.isFinite(v) ? v : undefined);
const str = (v: unknown): string | undefined => (typeof v === "string" ? v : undefined);

function base(s: Schema): Base {
  const b: Base = {};
  if (typeof s.title === "string") b.title = s.title;
  if (typeof s.description === "string") b.description = s.description;
  if ("default" in s) b.default = s.default as JsonValue;
  return b;
}

/** Resolve a local JSON pointer ("#/$defs/Foo"). Undefined for remote or broken refs. */
function resolvePointer(root: Schema, ref: string): Schema | undefined {
  if (ref === "#") return root;
  if (!ref.startsWith("#/")) return undefined;
  let cur: unknown = root;
  for (const raw of ref.slice(2).split("/")) {
    const part = decodeURIComponent(raw).replace(/~1/g, "/").replace(/~0/g, "~");
    if (!isObj(cur) || !(part in cur)) return undefined;
    cur = cur[part];
  }
  return isObj(cur) ? cur : undefined;
}

const raw = (s: Schema, reason: string): RawNode => ({ ...base(s), kind: "raw", reason });

/** `anyOf/oneOf: [X, {type:"null"}]` → X (null is not offered by the form). */
function unwrapNullable(s: Schema): Schema | undefined {
  for (const k of ["anyOf", "oneOf"] as const) {
    const alts = s[k];
    if (!Array.isArray(alts)) continue;
    const nonNull = alts.filter((a) => !(isObj(a) && a.type === "null"));
    if (alts.length === 2 && nonNull.length === 1 && isObj(nonNull[0])) {
      const rest = { ...s };
      delete rest[k];
      return { ...nonNull[0], ...rest };
    }
  }
  return undefined;
}

function compileNode(s: unknown, root: Schema, refs: string[]): FieldNode {
  if (s === true || (isObj(s) && Object.keys(s).length === 0)) return { kind: "raw", reason: "any JSON value is accepted" };
  if (!isObj(s)) return { kind: "raw", reason: "the schema for this field is not an object" };

  if (typeof s.$ref === "string") {
    const ref = s.$ref;
    if (refs.includes(ref)) return raw(s, "recursive schema");
    const target = resolvePointer(root, ref);
    if (!target) return raw(s, `unresolvable reference ${ref}`);
    const merged = { ...target, ...s };
    delete merged.$ref;
    return compileNode(merged, root, [...refs, ref]);
  }

  const nullable = unwrapNullable(s);
  if (nullable) return compileNode(nullable, root, refs);
  for (const k of ["anyOf", "oneOf", "allOf", "not", "if"]) {
    if (k in s) return raw(s, `uses ${k}`);
  }

  if ("const" in s) {
    return isScalar(s.const) ? { ...base(s), kind: "enum", options: [s.const] } : raw(s, "constant is not a scalar");
  }
  if (Array.isArray(s.enum)) {
    if (s.enum.length === 0) return raw(s, "empty enum");
    return s.enum.every(isScalar) ? { ...base(s), kind: "enum", options: s.enum as Scalar[] } : raw(s, "enum of non-scalar values");
  }

  let type = s.type;
  if (Array.isArray(type)) {
    const nonNull = type.filter((t) => t !== "null");
    if (nonNull.length !== 1) return raw(s, `accepts several types (${type.join(", ")})`);
    type = nonNull[0];
  }
  if (type === undefined) {
    if (isObj(s.properties)) type = "object";
    else if ("items" in s) type = "array";
    else return raw(s, "no type is declared");
  }

  switch (type) {
    case "string":
      return {
        ...base(s),
        kind: "string",
        format: str(s.format),
        minLength: num(s.minLength),
        maxLength: num(s.maxLength),
        pattern: str(s.pattern),
      };
    case "number":
    case "integer":
      return {
        ...base(s),
        kind: type,
        minimum: num(s.minimum),
        maximum: num(s.maximum),
        exclusiveMinimum: num(s.exclusiveMinimum),
        exclusiveMaximum: num(s.exclusiveMaximum),
      };
    case "boolean":
      return { ...base(s), kind: "boolean" };
    case "array": {
      if (Array.isArray(s.items) || "prefixItems" in s) return raw(s, "tuple arrays");
      if (!("items" in s)) return raw(s, "array items are not described");
      const item = compileNode(s.items, root, refs);
      if (item.kind === "object" || item.kind === "array" || item.kind === "raw")
        return raw(s, item.kind === "raw" ? `array items: ${item.reason}` : `array of ${item.kind}s`);
      return {
        ...base(s),
        kind: "array",
        item,
        minItems: num(s.minItems),
        maxItems: num(s.maxItems),
        uniqueItems: s.uniqueItems === true ? true : undefined,
      };
    }
    case "object": {
      const props = isObj(s.properties) ? s.properties : {};
      const addl = s.additionalProperties;
      if (Object.keys(props).length === 0 && (isObj(addl) && Object.keys(addl).length > 0)) return raw(s, "free-form map of values");
      if ("patternProperties" in s && Object.keys(props).length === 0) return raw(s, "keys defined by pattern");
      const required = new Set(Array.isArray(s.required) ? s.required.filter((r): r is string => typeof r === "string") : []);
      return {
        ...base(s),
        kind: "object",
        fields: Object.entries(props).map(([key, child]) => ({ key, node: compileNode(child, root, refs), required: required.has(key) })),
        additionalProperties: addl !== false,
      };
    }
    case "null":
      return raw(s, "only accepts null");
    default:
      return raw(s, `unknown type ${String(type)}`);
  }
}

/**
 * Compile a tool input schema. The root must be an object (MCP tools take a
 * named-argument object); anything else can only be edited as raw JSON.
 */
export function compileSchema(schema: unknown): CompiledSchema {
  if (schema === undefined || schema === null || (isObj(schema) && Object.keys(schema).length === 0)) {
    // No schema at all: MCP treats this as "no declared arguments".
    return { ok: true, root: { kind: "object", fields: [], additionalProperties: true } };
  }
  if (!isObj(schema)) return { ok: false, reason: "the input schema is not a JSON object" };
  const node = compileNode(schema, schema, []);
  if (node.kind === "object") return { ok: true, root: node };
  if (node.kind === "raw") return { ok: false, reason: node.reason };
  return { ok: false, reason: `the tool takes a bare ${node.kind}, not named arguments` };
}

/** Short, human hint text for a field's format and constraints (never enforced beyond what the schema says). */
export function describeConstraints(node: FieldNode): string {
  const parts: string[] = [];
  if (node.kind === "string") {
    if (node.format) parts.push(`Format: ${node.format}${FORMAT_EXAMPLES[node.format] ? ` (e.g. ${FORMAT_EXAMPLES[node.format]})` : ""}`);
    if (node.minLength != null && node.maxLength != null) parts.push(`${node.minLength}–${node.maxLength} characters`);
    else if (node.minLength != null) parts.push(`at least ${node.minLength} characters`);
    else if (node.maxLength != null) parts.push(`at most ${node.maxLength} characters`);
    if (node.pattern) parts.push(`pattern ${node.pattern}`);
  } else if (node.kind === "number" || node.kind === "integer") {
    if (node.kind === "integer") parts.push("whole number");
    const lo = node.minimum != null ? `≥ ${node.minimum}` : node.exclusiveMinimum != null ? `> ${node.exclusiveMinimum}` : null;
    const hi = node.maximum != null ? `≤ ${node.maximum}` : node.exclusiveMaximum != null ? `< ${node.exclusiveMaximum}` : null;
    if (lo) parts.push(lo);
    if (hi) parts.push(hi);
  } else if (node.kind === "array") {
    if (node.item.kind !== "enum") parts.push(`one ${node.item.kind} per line`);
    if (node.minItems != null) parts.push(`at least ${node.minItems}`);
    if (node.maxItems != null) parts.push(`at most ${node.maxItems}`);
    if (node.uniqueItems) parts.push("no duplicates");
  }
  return parts.join(" · ");
}

const FORMAT_EXAMPLES: Record<string, string> = {
  "date-time": "2026-10-08T14:30:00Z",
  date: "2026-10-08",
  time: "14:30:00",
  email: "ops@example.com",
  uri: "https://example.com/path",
  url: "https://example.com/path",
  uuid: "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  ipv4: "192.0.2.1",
  hostname: "api.example.com",
};
