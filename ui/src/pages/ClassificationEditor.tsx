import { useState } from "react";
import { Button, Caption1, Field, Input, makeStyles, Select, tokens } from "@fluentui/react-components";
import { updateClassification } from "../api/client";
import { DOMAINS, OPERATIONS, type ClassificationUpdate, type MCPTool, type Operation } from "../api/types";
import { useNotify } from "../components/Notifications";

const useStyles = makeStyles({
  grid: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: `${tokens.spacingVerticalS} ${tokens.spacingHorizontalM}` },
  full: { gridColumn: "1 / -1" },
  actions: { display: "flex", alignItems: "center", gap: tokens.spacingHorizontalM, marginTop: tokens.spacingVerticalS },
});

const splitList = (v: string) =>
  v
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
const NONE = "__none__";

/** Anything classifiable: a tool or a skill (same wire shape). */
export interface Classifiable {
  id: string;
  name: string;
  domain?: string | null;
  operation: Operation;
  tags?: string[];
  requiredScopes?: string[];
  classificationReviewed?: boolean;
}

/** `save` defaults to the tools PATCH; skills pass their own endpoint. */
export function ClassificationEditor<T extends Classifiable = MCPTool>({
  tool,
  onSaved,
  save: saveFn = updateClassification as unknown as (id: string, body: ClassificationUpdate) => Promise<T>,
}: {
  tool: T;
  onSaved: (t: T) => void;
  save?: (id: string, body: ClassificationUpdate) => Promise<T>;
}) {
  const s = useStyles();
  const notify = useNotify();
  const [domain, setDomain] = useState<string>(tool.domain ?? NONE);
  const [operation, setOperation] = useState<Operation>(tool.operation);
  const [tags, setTags] = useState((tool.tags ?? []).join(", "));
  const [scopes, setScopes] = useState((tool.requiredScopes ?? []).join(", "));
  const [pending, setPending] = useState(false);

  const domainOptions: string[] = [...DOMAINS];
  if (tool.domain && !domainOptions.includes(tool.domain)) domainOptions.push(tool.domain);

  const dirty =
    (domain === NONE ? null : domain) !== (tool.domain ?? null) ||
    operation !== tool.operation ||
    splitList(tags).join(",") !== (tool.tags ?? []).join(",") ||
    splitList(scopes).join(",") !== (tool.requiredScopes ?? []).join(",");

  const save = async () => {
    setPending(true);
    try {
      const updated = await saveFn(tool.id, {
        domain: domain === NONE ? null : domain,
        operation,
        tags: splitList(tags),
        requiredScopes: splitList(scopes),
      });
      notify.success(`Saved classification for “${tool.name}”`);
      onSaved(updated);
    } catch (e) {
      notify.error(`Save classification for “${tool.name}”`, e);
    } finally {
      setPending(false);
    }
  };

  return (
    <form
      noValidate
      aria-label="Classification"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <div className={s.grid}>
        <Field label="Domain">
          <Select value={domain} onChange={(_, d) => setDomain(d.value)}>
            <option value={NONE}>(unclassified)</option>
            {domainOptions.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Operation" hint="Policy ceilings compare against this.">
          <Select value={operation} onChange={(_, d) => setOperation(d.value as Operation)}>
            {OPERATIONS.map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Tags" hint="Comma-separated." className={s.full}>
          <Input value={tags} onChange={(_, d) => setTags(d.value)} />
        </Field>
        <Field label="Required scopes" hint="Comma-separated." className={s.full}>
          <Input value={scopes} onChange={(_, d) => setScopes(d.value)} />
        </Field>
      </div>
      <div className={s.actions}>
        <Button appearance="primary" type="submit" disabled={!dirty || pending}>
          {pending ? "Saving…" : "Save classification"}
        </Button>
        <Caption1>{tool.classificationReviewed ? "Reviewed by an admin." : "AI-generated — not yet reviewed. Saving marks it reviewed."}</Caption1>
      </div>
    </form>
  );
}
