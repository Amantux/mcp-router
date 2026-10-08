import { useState } from "react";
import { Button, Caption1, Checkbox, Field, Input, Label, makeStyles, Select, Switch, Textarea, tokens } from "@fluentui/react-components";
import { ChevronDownRegular, ChevronRightRegular } from "@fluentui/react-icons";
import { describeConstraints, type FieldNode, type ObjectNode, type Scalar } from "./model";
import { emptyDraft, type Draft, type DraftObject, type FieldErrors } from "./draft";

const useStyles = makeStyles({
  form: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalM },
  group: {
    margin: 0,
    padding: `${tokens.spacingVerticalXS} ${tokens.spacingHorizontalM} ${tokens.spacingVerticalM}`,
    border: `1px solid ${tokens.colorNeutralStroke2}`,
    borderRadius: tokens.borderRadiusMedium,
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalM,
    minWidth: 0,
  },
  legend: { padding: 0 },
  groupMeta: { color: tokens.colorNeutralForeground3 },
  required: { color: tokens.colorPaletteRedForeground1, paddingLeft: "2px" },
  checkGroup: { display: "flex", flexDirection: "column", gap: "2px" },
  checks: { display: "flex", flexWrap: "wrap", gap: `0 ${tokens.spacingHorizontalM}` },
  mono: { fontFamily: tokens.fontFamilyMonospace, fontSize: tokens.fontSizeBase200 },
});

const optionLabel = (v: Scalar) => (v === null ? "null" : typeof v === "string" ? v : JSON.stringify(v));

function hintFor(node: FieldNode): string | undefined {
  const parts = [node.description, describeConstraints(node)].filter(Boolean);
  if (node.kind === "raw") parts.push(`Edited as JSON: ${node.reason}.`);
  return parts.length ? parts.join(" — ") : undefined;
}

interface FieldProps {
  name: string;
  node: FieldNode;
  required: boolean;
  path: string;
  draft: Draft;
  errors: FieldErrors;
  disabled?: boolean;
  onChange: (d: Draft) => void;
}

function ObjectGroup({ name, node, required, path, draft, errors, disabled, onChange }: FieldProps & { node: ObjectNode }) {
  const s = useStyles();
  const [open, setOpen] = useState(true);
  const d = (typeof draft === "object" && !Array.isArray(draft) ? draft : {}) as DraftObject;
  const label = node.title ?? name;
  return (
    <fieldset className={s.group} aria-label={label}>
      <legend className={s.legend}>
        <Button
          appearance="transparent"
          size="small"
          icon={open ? <ChevronDownRegular /> : <ChevronRightRegular />}
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
        >
          <strong>{label}</strong>
          {required && (
            <span className={s.required} aria-hidden>
              *
            </span>
          )}
        </Button>
        {!required && <Caption1 className={s.groupMeta}>optional group — leave blank to omit</Caption1>}
      </legend>
      {node.description && open && <Caption1>{node.description}</Caption1>}
      {errors[path] && <Caption1 style={{ color: tokens.colorPaletteRedForeground1 }}>{errors[path]}</Caption1>}
      {open && <Fields node={node} path={path} draft={d} errors={errors} disabled={disabled} onChange={onChange} />}
    </fieldset>
  );
}

function Fields({
  node,
  path,
  draft,
  errors,
  disabled,
  onChange,
}: {
  node: ObjectNode;
  path: string;
  draft: DraftObject;
  errors: FieldErrors;
  disabled?: boolean;
  onChange: (d: DraftObject) => void;
}) {
  return (
    <>
      {node.fields.map((f) => {
        const childPath = path ? `${path}.${f.key}` : f.key;
        return (
          <SchemaField
            key={f.key}
            name={f.key}
            node={f.node}
            required={f.required}
            path={childPath}
            draft={draft[f.key] ?? emptyDraft(f.node)}
            errors={errors}
            disabled={disabled}
            onChange={(v) => onChange({ ...draft, [f.key]: v })}
          />
        );
      })}
    </>
  );
}

export function SchemaField(props: FieldProps) {
  const s = useStyles();
  const { name, node, required, path, draft, errors, disabled, onChange } = props;
  if (node.kind === "object") return <ObjectGroup {...props} node={node} />;
  const text = typeof draft === "string" ? draft : "";
  const label = node.title ?? name;
  const common = { label, required, hint: hintFor(node), validationMessage: errors[path] };

  switch (node.kind) {
    case "string":
      return (
        <Field {...common}>
          <Input value={text} disabled={disabled} onChange={(_, d) => onChange(d.value)} />
        </Field>
      );
    case "number":
    case "integer":
      return (
        <Field {...common}>
          <Input value={text} inputMode={node.kind === "integer" ? "numeric" : "decimal"} disabled={disabled} onChange={(_, d) => onChange(d.value)} />
        </Field>
      );
    case "boolean":
      return required ? (
        <Field {...common}>
          <Switch checked={text === "true"} disabled={disabled} onChange={(_, d) => onChange(d.checked ? "true" : "false")} label={text === "true" ? "true" : "false"} />
        </Field>
      ) : (
        <Field {...common}>
          <Select value={text} disabled={disabled} onChange={(_, d) => onChange(d.value)}>
            <option value="">— not set —</option>
            <option value="true">true</option>
            <option value="false">false</option>
          </Select>
        </Field>
      );
    case "enum":
      return (
        <Field {...common}>
          <Select value={text} disabled={disabled} onChange={(_, d) => onChange(d.value)}>
            <option value="">{required ? "— choose —" : "— not set —"}</option>
            {node.options.map((o, i) => (
              <option key={i} value={String(i)}>
                {optionLabel(o)}
              </option>
            ))}
          </Select>
        </Field>
      );
    case "array":
      if (node.item.kind === "enum") {
        const selected = new Set(Array.isArray(draft) ? draft : []);
        const opts = node.item.options;
        // Not wrapped in <Field>: Field would label every checkbox with the field's label.
        return (
          <div role="group" aria-label={label} className={s.checkGroup}>
            <Label required={required}>{label}</Label>
            <div className={s.checks}>
              {opts.map((o, i) => (
                <Checkbox
                  key={i}
                  label={optionLabel(o)}
                  checked={selected.has(String(i))}
                  disabled={disabled}
                  onChange={(_, d) => {
                    const next = new Set(selected);
                    if (d.checked) next.add(String(i));
                    else next.delete(String(i));
                    // Keep option order stable regardless of click order.
                    onChange(opts.map((_o, j) => String(j)).filter((j) => next.has(j)));
                  }}
                />
              ))}
            </div>
            {common.hint && <Caption1 className={s.groupMeta}>{common.hint}</Caption1>}
            {common.validationMessage && <Caption1 style={{ color: tokens.colorPaletteRedForeground1 }}>{common.validationMessage}</Caption1>}
          </div>
        );
      }
      return (
        <Field {...common}>
          <Textarea value={text} rows={3} resize="vertical" disabled={disabled} onChange={(_, d) => onChange(d.value)} placeholder="One item per line" />
        </Field>
      );
    case "raw":
      return (
        <Field {...common}>
          <Textarea value={text} rows={4} resize="vertical" className={s.mono} disabled={disabled} onChange={(_, d) => onChange(d.value)} placeholder="JSON value" />
        </Field>
      );
  }
}

/** Renders a compiled root object schema as a form. Controlled: the caller owns the draft. */
export function SchemaForm({
  root,
  draft,
  errors,
  disabled,
  onChange,
}: {
  root: ObjectNode;
  draft: Draft;
  errors: FieldErrors;
  disabled?: boolean;
  onChange: (d: Draft) => void;
}) {
  const s = useStyles();
  if (root.fields.length === 0)
    return <Caption1>{root.additionalProperties ? "This tool declares no arguments." : "This tool takes no arguments."}</Caption1>;
  const d = (typeof draft === "object" && !Array.isArray(draft) ? draft : {}) as DraftObject;
  return (
    <div className={s.form} role="group" aria-label="Arguments">
      <Fields node={root} path="" draft={d} errors={errors} disabled={disabled} onChange={onChange} />
      {errors[""] && <Caption1 style={{ color: tokens.colorPaletteRedForeground1 }}>{errors[""]}</Caption1>}
    </div>
  );
}
