import { useState } from "react";
import {
  Badge,
  Body1,
  Button,
  Caption1,
  Card,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Field,
  makeStyles,
  MessageBar,
  MessageBarBody,
  Radio,
  RadioGroup,
  Subtitle2,
  Textarea,
  tokens,
} from "@fluentui/react-components";
import { BranchForkRegular } from "@fluentui/react-icons";
import { acceptDedup, dismissDedup, getSkill, getTool, listDedupSuggestions, runDedupScan } from "../api/client";

// CONTRACT: wave-4 dedup suggestions may reference a skill as "skill:<id>" in toolAId/toolBId (no S2 notes yet).
const SKILL_PREFIX = "skill:";
const isSkillRef = (id: string) => id.startsWith(SKILL_PREFIX);
function loadSide(id: string, embedded: MCPTool | undefined, sig: AbortSignal): Promise<MCPTool | SkillDetail> {
  if (isSkillRef(id)) return getSkill(id.slice(SKILL_PREFIX.length), sig);
  return embedded ? Promise.resolve(embedded) : getTool(id, sig);
}
import type { DuplicateSuggestion, MCPTool, SkillDetail } from "../api/types";
import { EmptyState, fmtInt, fmtMs, JsonBlock, LoadingRow, OperationBadge, PageHeader, useCommonStyles } from "../components/common";
import { useNotify } from "../components/Notifications";
import { useLoader } from "../hooks/useLoader";

const useStyles = makeStyles({
  list: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalM, marginTop: tokens.spacingVerticalM },
  head: { display: "flex", alignItems: "center", gap: tokens.spacingHorizontalS, flexWrap: "wrap" },
  pair: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: tokens.spacingHorizontalM },
  side: {
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalXS,
    padding: tokens.spacingHorizontalS,
    border: `1px solid ${tokens.colorNeutralStroke2}`,
    borderRadius: tokens.borderRadiusMedium,
    minWidth: 0,
  },
  preferred: { border: `1px solid ${tokens.colorBrandStroke1}` },
  stats: { display: "flex", gap: tokens.spacingHorizontalM, fontVariantNumeric: "tabular-nums" },
  actions: { display: "flex", alignItems: "center", gap: tokens.spacingHorizontalS, flexWrap: "wrap" },
});

function ToolSide({ tool, label, preferred, kind }: { tool: MCPTool | SkillDetail | undefined; label: string; preferred: boolean; kind?: "tool" | "skill" }) {
  const s = useStyles();
  const c = useCommonStyles();
  if (!tool) return <div className={s.side}>Loading {label}…</div>;
  if (kind === "skill" && "sourceId" in tool)
    return (
      <div className={preferred ? `${s.side} ${s.preferred}` : s.side} aria-label={`${label}: ${tool.name}`}>
        <div className={s.head}>
          <Subtitle2>{tool.name}</Subtitle2>
          <Badge appearance="outline" size="small">
            skill
          </Badge>
          <OperationBadge op={tool.operation} />
          {preferred && (
            <Badge appearance="filled" color="brand" size="small">
              suggested preferred
            </Badge>
          )}
        </div>
        <Caption1 className={c.muted}>{tool.sourceName ?? tool.sourceId}</Caption1>
        <Body1>{tool.description || <span className={c.muted}>No description.</span>}</Body1>
        <div className={s.stats}>
          <Caption1>Body ~{fmtInt(tool.bodyTokensEst ?? null)} tokens</Caption1>
        </div>
      </div>
    );
  if ("sourceId" in tool) return null;
  return (
    <div className={preferred ? `${s.side} ${s.preferred}` : s.side} aria-label={`${label}: ${tool.name}`}>
      <div className={s.head}>
        <Subtitle2>{tool.name}</Subtitle2>
        {kind && (
          <Badge appearance="outline" size="small">
            tool
          </Badge>
        )}
        <OperationBadge op={tool.operation} />
        {preferred && (
          <Badge appearance="filled" color="brand" size="small">
            suggested preferred
          </Badge>
        )}
      </div>
      <Caption1 className={c.muted}>
        {tool.serverName ?? tool.serverId} · v{tool.version}
      </Caption1>
      <Body1>{tool.description || <span className={c.muted}>No description.</span>}</Body1>
      <div className={s.stats}>
        <Caption1>Calls {fmtInt(tool.callCount)}</Caption1>
        <Caption1>Errors {fmtInt(tool.errorCount)}</Caption1>
        <Caption1>Avg {fmtMs(tool.avgLatencyMs)}</Caption1>
      </div>
      <JsonBlock value={tool.inputSchema} label={`${tool.name} input schema`} />
    </div>
  );
}

/** Justification is mandatory: the backend rejects an empty one, and the UI refuses to send it. */
export function DismissDialog({
  open,
  pairLabel,
  onCancel,
  onDismiss,
}: {
  open: boolean;
  pairLabel: string;
  onCancel: () => void;
  onDismiss: (justification: string) => Promise<void>;
}) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string>();
  const [pending, setPending] = useState(false);
  const submit = async () => {
    const j = text.trim();
    if (!j) {
      setError("Explain why these are not duplicates — the justification is kept with the decision.");
      return;
    }
    setPending(true);
    try {
      await onDismiss(j);
      setText("");
      setError(undefined);
    } finally {
      setPending(false);
    }
  };
  return (
    <Dialog open={open} onOpenChange={(_, d) => !d.open && !pending && onCancel()}>
      <DialogSurface>
        <form
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <DialogBody>
            <DialogTitle>Dismiss suggestion {pairLabel}?</DialogTitle>
            <DialogContent>
              <Field label="Justification" required validationMessage={error}>
                <Textarea
                  value={text}
                  onChange={(_, d) => {
                    setText(d.value);
                    if (d.value.trim()) setError(undefined);
                  }}
                  rows={3}
                  placeholder="e.g. Different auth scopes; one targets GitHub Enterprise"
                />
              </Field>
            </DialogContent>
            <DialogActions>
              <Button appearance="secondary" onClick={onCancel} disabled={pending}>
                Cancel
              </Button>
              <Button appearance="primary" type="submit" disabled={pending}>
                {pending ? "Dismissing…" : "Dismiss suggestion"}
              </Button>
            </DialogActions>
          </DialogBody>
        </form>
      </DialogSurface>
    </Dialog>
  );
}

export function PairCard({ sug, onResolved }: { sug: DuplicateSuggestion; onResolved: () => void }) {
  const s = useStyles();
  const c = useCommonStyles();
  const notify = useNotify();
  const a = useLoader("Load duplicate tool", (sig) => loadSide(sug.toolAId, sug.toolA, sig), [sug.id]);
  const b = useLoader("Load duplicate tool", (sig) => loadSide(sug.toolBId, sug.toolB, sig), [sug.id]);
  const [preferred, setPreferred] = useState(sug.preferredToolId ?? sug.toolAId);
  // Kind badges appear only when a skill is involved, so tool↔tool pairs read as before.
  const kinds: ["tool" | "skill", "tool" | "skill"] | undefined =
    isSkillRef(sug.toolAId) || isSkillRef(sug.toolBId) ? [isSkillRef(sug.toolAId) ? "skill" : "tool", isSkillRef(sug.toolBId) ? "skill" : "tool"] : undefined;
  const [accepting, setAccepting] = useState(false);
  const [dismissOpen, setDismissOpen] = useState(false);
  const nameA = a.data?.name ?? "tool A";
  const nameB = b.data?.name ?? "tool B";
  const pairLabel = `“${nameA}” ↔ “${nameB}”`;

  const accept = async () => {
    setAccepting(true);
    try {
      // The toast reports what the backend stored, not what the radio says.
      const saved = (await acceptDedup(sug.id, preferred)).preferredToolId;
      const savedName = saved === sug.toolAId ? nameA : saved === sug.toolBId ? nameB : undefined;
      notify.success(savedName ? `Marked “${savedName}” as preferred over its duplicate` : `Accepted suggestion ${pairLabel}`);
      onResolved();
    } catch (e) {
      notify.error(`Accept suggestion ${pairLabel}`, e);
    } finally {
      setAccepting(false);
    }
  };

  const dismiss = async (justification: string) => {
    try {
      await dismissDedup(sug.id, justification);
      notify.success(`Dismissed suggestion ${pairLabel}`);
      setDismissOpen(false);
      onResolved();
    } catch (e) {
      notify.error(`Dismiss suggestion ${pairLabel}`, e);
    }
  };

  return (
    <Card size="small" aria-label={`Duplicate suggestion ${pairLabel}`}>
      <div className={s.head}>
        <Badge appearance="tint" color={sug.similarity >= 0.9 ? "danger" : "warning"}>
          {(sug.similarity * 100).toFixed(1)}% similar
        </Badge>
        <Body1>{sug.rationale || <span className={c.muted}>No rationale recorded.</span>}</Body1>
      </div>
      <div className={s.pair}>
        <ToolSide tool={a.data} label="Tool A" preferred={sug.preferredToolId === sug.toolAId} kind={kinds?.[0]} />
        <ToolSide tool={b.data} label="Tool B" preferred={sug.preferredToolId === sug.toolBId} kind={kinds?.[1]} />
      </div>
      <div className={s.actions}>
        <RadioGroup layout="horizontal" value={preferred} onChange={(_, d) => setPreferred(d.value)} aria-label="Preferred tool">
          <Radio value={sug.toolAId} label={`Prefer ${nameA}`} />
          <Radio value={sug.toolBId} label={`Prefer ${nameB}`} />
        </RadioGroup>
        <Button appearance="primary" disabled={accepting || !a.data || !b.data} onClick={() => void accept()}>
          {accepting ? "Accepting…" : "Accept preferred"}
        </Button>
        <Button disabled={accepting} onClick={() => setDismissOpen(true)}>
          Dismiss…
        </Button>
      </div>
      <DismissDialog open={dismissOpen} pairLabel={pairLabel} onCancel={() => setDismissOpen(false)} onDismiss={dismiss} />
    </Card>
  );
}

export function DuplicatesPage() {
  const s = useStyles();
  const c = useCommonStyles();
  const notify = useNotify();
  const sugs = useLoader("Load duplicate suggestions", (sig) => listDedupSuggestions("open", sig), []);
  const [scanning, setScanning] = useState(false);
  const scan = async () => {
    setScanning(true);
    try {
      await runDedupScan();
      notify.success("Duplicate scan finished");
      sugs.refresh();
    } catch (e) {
      notify.error("Run duplicate scan", e);
    } finally {
      setScanning(false);
    }
  };
  const list = sugs.data ?? [];
  return (
    <>
      <PageHeader
        title="Duplicate review"
        meta={sugs.data && <Caption1 className={c.muted}>{fmtInt(list.length)} open</Caption1>}
        actions={
          <Button appearance="primary" disabled={scanning} onClick={() => void scan()}>
            {scanning ? "Scanning…" : "Run duplicate scan"}
          </Button>
        }
      />
      <MessageBar intent="info">
        <MessageBarBody>
          Suggestions are advisory. Accepting records which tool is preferred; dismissing records why they differ.
          Nothing is ever auto-disabled or deleted — disable a tool yourself from the catalog if you want it gone.
        </MessageBarBody>
      </MessageBar>
      {sugs.loading && !sugs.data ? (
        <LoadingRow label="Loading suggestions…" />
      ) : list.length === 0 && !sugs.failed ? (
        <div className={s.list}>
          <EmptyState
            icon={<BranchForkRegular />}
            title="No open duplicate suggestions"
            body="The scanner compares descriptions, input schemas and capability classes across servers. Run a scan after adding servers."
          />
        </div>
      ) : (
        <div className={s.list}>
          {list.map((sug) => (
            <PairCard key={sug.id} sug={sug} onResolved={() => sugs.refresh()} />
          ))}
        </div>
      )}
    </>
  );
}
