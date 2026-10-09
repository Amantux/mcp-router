import { useMemo, useState } from "react";
import {
  Badge,
  Button,
  Caption1,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Field,
  Input,
  makeStyles,
  MessageBar,
  MessageBarBody,
  Select,
  SpinButton,
  Subtitle2,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  tokens,
} from "@fluentui/react-components";
import { AddRegular, CopyRegular, DeleteRegular, EditRegular, KeyResetRegular, ShieldKeyholeRegular } from "@fluentui/react-icons";
import { createPrincipal, createRule, deletePrincipal, deleteRule, listPrincipals, listRules, listServers, listSkillSources, rotatePrincipalKey, updatePrincipal, updateRule } from "../api/client";
import { CEILINGS, type CreatedPrincipal, type OperationCeiling, type PolicyRule, type Principal } from "../api/types";
import { ConfirmDialog, EmptyState, ErrorState, fmtInt, fmtTime, LoadingRow, PageHeader, useCommonStyles } from "../components/common";
import { useNotify } from "../components/Notifications";
import { useLoader } from "../hooks/useLoader";

const useStyles = makeStyles({
  section: { marginTop: tokens.spacingVerticalXL, marginBottom: tokens.spacingVerticalS, display: "flex", alignItems: "center", gap: tokens.spacingHorizontalM },
  ruleForm: { display: "flex", flexWrap: "wrap", alignItems: "flex-end", gap: tokens.spacingHorizontalS, marginBottom: tokens.spacingVerticalS },
  form: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalM },
  keyRow: { display: "flex", gap: tokens.spacingHorizontalS },
});

function CreatePrincipalDialog({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (p: CreatedPrincipal) => void }) {
  const s = useStyles();
  const notify = useNotify();
  const [agentId, setAgentId] = useState("");
  const [maxTools, setMaxTools] = useState(8);
  const [maxSkills, setMaxSkills] = useState(3);
  const [maxServers, setMaxServers] = useState("");
  const [error, setError] = useState<string>();
  const [serversError, setServersError] = useState<string>();
  const [pending, setPending] = useState(false);
  const submit = async () => {
    const id = agentId.trim();
    if (!id) {
      setError("Enter the agent id this key will authenticate.");
      return;
    }
    setError(undefined);
    const servers = maxServers.trim() === "" ? null : Number(maxServers);
    if (servers !== null && !(Number.isInteger(servers) && servers >= 1 && servers <= 1000)) {
      setServersError("Enter a whole number from 1 to 1000, or leave it blank for no limit.");
      return;
    }
    setServersError(undefined);
    setPending(true);
    try {
      const created = await createPrincipal({ agentId: id, maxTools, maxServers: servers, maxSkills });
      setAgentId("");
      setMaxServers("");
      onCreated(created);
    } catch (e) {
      notify.error(`Create principal “${id}”`, e);
    } finally {
      setPending(false);
    }
  };
  return (
    <Dialog open={open} onOpenChange={(_, d) => !d.open && !pending && onClose()}>
      <DialogSurface>
        <form
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <DialogBody>
            <DialogTitle>Create agent principal</DialogTitle>
            <DialogContent className={s.form}>
              <Field label="Agent id" required validationMessage={error}>
                <Input value={agentId} onChange={(_, d) => setAgentId(d.value)} />
              </Field>
              <Field label="Max tools exposed per request">
                <SpinButton value={maxTools} min={1} max={64} onChange={(_, d) => d.value != null && setMaxTools(d.value)} />
              </Field>
              <Field label="Max skills exposed per request" hint="0 = this agent is never offered skills.">
                <SpinButton value={maxSkills} min={0} max={64} onChange={(_, d) => d.value != null && setMaxSkills(d.value)} />
              </Field>
              <Field label="Max distinct servers per request" hint="Blank = no limit." validationMessage={serversError}>
                <Input type="number" min={1} max={1000} value={maxServers} onChange={(_, d) => setMaxServers(d.value)} />
              </Field>
              <Caption1>New principals can't use any tool until you add an allow rule (deny by default).</Caption1>
            </DialogContent>
            <DialogActions>
              <Button appearance="secondary" onClick={onClose} disabled={pending}>
                Cancel
              </Button>
              <Button appearance="primary" type="submit" disabled={pending}>
                {pending ? "Creating…" : "Create principal"}
              </Button>
            </DialogActions>
          </DialogBody>
        </form>
      </DialogSurface>
    </Dialog>
  );
}

/** PATCH a principal's exposure caps. Blank max servers = no limit (sent as null). */
function EditLimitsDialog({ principal, onClose, onSaved }: { principal: Principal | null; onClose: () => void; onSaved: () => void }) {
  const s = useStyles();
  const notify = useNotify();
  const [maxTools, setMaxTools] = useState(principal?.maxTools ?? 8);
  const [maxSkills, setMaxSkills] = useState(principal?.maxSkills ?? 3);
  const [maxServers, setMaxServers] = useState(principal?.maxServers != null ? String(principal.maxServers) : "");
  const [error, setError] = useState<string>();
  const [pending, setPending] = useState(false);
  if (!principal) return null;
  const submit = async () => {
    const servers = maxServers.trim() === "" ? null : Number(maxServers);
    if (servers !== null && !(Number.isInteger(servers) && servers >= 1 && servers <= 1000)) {
      setError("Enter a whole number from 1 to 1000, or leave it blank for no limit.");
      return;
    }
    setError(undefined);
    setPending(true);
    try {
      await updatePrincipal(principal.id, { maxTools, maxSkills, maxServers: servers });
      notify.success(`Updated limits for “${principal.agentId}”`);
      onSaved();
      onClose();
    } catch (e) {
      notify.error(`Update limits for “${principal.agentId}”`, e);
    } finally {
      setPending(false);
    }
  };
  return (
    <Dialog open onOpenChange={(_, d) => !d.open && !pending && onClose()}>
      <DialogSurface>
        <form
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <DialogBody>
            <DialogTitle>Limits for “{principal.agentId}”</DialogTitle>
            <DialogContent className={s.form}>
              <Field label="Max tools exposed per request">
                <SpinButton value={maxTools} min={1} max={64} onChange={(_, d) => d.value != null && setMaxTools(d.value)} />
              </Field>
              <Field label="Max skills exposed per request" hint="0 = this agent is never offered skills.">
                <SpinButton value={maxSkills} min={0} max={64} onChange={(_, d) => d.value != null && setMaxSkills(d.value)} />
              </Field>
              <Field label="Max distinct servers per request" hint="Blank = no limit." validationMessage={error}>
                <Input type="number" min={1} max={1000} value={maxServers} onChange={(_, d) => setMaxServers(d.value)} />
              </Field>
            </DialogContent>
            <DialogActions>
              <Button appearance="secondary" onClick={onClose} disabled={pending}>
                Cancel
              </Button>
              <Button appearance="primary" type="submit" disabled={pending}>
                {pending ? "Saving…" : "Save limits"}
              </Button>
            </DialogActions>
          </DialogBody>
        </form>
      </DialogSurface>
    </Dialog>
  );
}

/**
 * PATCH a rule's target, name glob, ceiling and approval. Its kind stays as created
 * (the backend has no field for it). `targets` = the servers (tool rule) or skill
 * sources (skill rule) it can point at; serverId is sent only when it changed.
 */
function EditRuleDialog({
  rule,
  target,
  targets,
  onClose,
  onSaved,
}: {
  rule: PolicyRule | null;
  target: string;
  targets: { id: string; name: string }[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const s = useStyles();
  const notify = useNotify();
  const [targetId, setTargetId] = useState(rule?.serverId ?? "");
  const [toolName, setToolName] = useState(rule?.toolName ?? "");
  const [ceiling, setCeiling] = useState<OperationCeiling>(rule?.maxOperation ?? "read");
  const [approval, setApproval] = useState(rule?.requiresApproval ?? false);
  const [pending, setPending] = useState(false);
  if (!rule) return null;
  const kind = rule.resourceKind === "skill" ? "skill" : "tool";
  const submit = async () => {
    setPending(true);
    try {
      const retarget = targetId !== (rule.serverId ?? "") ? { serverId: targetId || null } : {};
      await updateRule(rule.id, { ...retarget, toolName: toolName.trim() || null, maxOperation: ceiling, requiresApproval: approval });
      notify.success(`Updated the ${kind} rule for “${rule.agentId}” on ${target}`);
      onSaved();
      onClose();
    } catch (e) {
      notify.error(`Update rule for “${rule.agentId}”`, e);
    } finally {
      setPending(false);
    }
  };
  return (
    <Dialog open onOpenChange={(_, d) => !d.open && !pending && onClose()}>
      <DialogSurface>
        <form
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <DialogBody>
            <DialogTitle>
              Edit {kind} rule for “{rule.agentId}” on {target}
            </DialogTitle>
            <DialogContent className={s.form}>
              <Field label={kind === "skill" ? "Skill source" : "Server"}>
                <Select value={targetId} onChange={(_, d) => setTargetId(d.value)}>
                  <option value="">{kind === "skill" ? "Any source" : "Any server"}</option>
                  {/* A target that is no longer listed stays selectable, so saving other fields can't move the rule. */}
                  {rule.serverId && !targets.some((t) => t.id === rule.serverId) && <option value={rule.serverId}>{target}</option>}
                  {targets.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label={kind === "skill" ? "Skill name" : "Tool name"} hint="Blank = any; glob allowed">
                <Input value={toolName} onChange={(_, d) => setToolName(d.value)} />
              </Field>
              <Field label="Operation ceiling" hint="read < write < execute">
                <Select value={ceiling} onChange={(_, d) => setCeiling(d.value as OperationCeiling)}>
                  {CEILINGS.map((o) => (
                    <option key={o} value={o}>
                      up to {o}
                    </option>
                  ))}
                </Select>
              </Field>
              <Switch checked={approval} onChange={(_, d) => setApproval(d.checked)} label="Requires approval" />
            </DialogContent>
            <DialogActions>
              <Button appearance="secondary" onClick={onClose} disabled={pending}>
                Cancel
              </Button>
              <Button appearance="primary" type="submit" disabled={pending}>
                {pending ? "Saving…" : "Save rule"}
              </Button>
            </DialogActions>
          </DialogBody>
        </form>
      </DialogSurface>
    </Dialog>
  );
}

/**
 * Confirm, rotate, then show the new key, all in ONE dialog: stacking a key reveal on a
 * closing confirmation leaves two modals fighting over focus and aria-hidden.
 */
function RotateKeyDialog({ principal, onClose }: { principal: Principal | null; onClose: () => void }) {
  const s = useStyles();
  const c = useCommonStyles();
  const notify = useNotify();
  const [pending, setPending] = useState(false);
  const [key, setKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  if (!principal) return null;
  const rotate = async () => {
    setPending(true);
    try {
      setKey((await rotatePrincipalKey(principal.id)).apiKey);
    } catch (e) {
      notify.error(`Rotate the key for “${principal.agentId}”`, e);
    } finally {
      setPending(false);
    }
  };
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(key ?? "");
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  return (
    <Dialog open modalType={key ? "alert" : "modal"} onOpenChange={(_, d) => !d.open && !pending && !key && onClose()}>
      <DialogSurface>
        <DialogBody>
          <DialogTitle>{key ? `New API key for “${principal.agentId}”` : `Rotate the key for “${principal.agentId}”?`}</DialogTitle>
          <DialogContent className={s.form}>
            {key ? (
              <>
                <MessageBar intent="warning">
                  <MessageBarBody>The old key no longer works. Copy this one now: it is stored hashed and cannot be shown again.</MessageBarBody>
                </MessageBar>
                <div className={s.keyRow}>
                  <Input readOnly value={key} className={c.mono} style={{ flex: 1 }} aria-label="API key" onFocus={(e) => e.target.select()} />
                  <Button icon={<CopyRegular />} onClick={() => void copy()}>
                    {copied ? "Copied" : "Copy"}
                  </Button>
                </div>
              </>
            ) : (
              "The current key stops working immediately. The new key is shown once; update every client that uses this agent."
            )}
          </DialogContent>
          <DialogActions>
            {key ? (
              <Button appearance="primary" onClick={onClose}>
                I've stored the key
              </Button>
            ) : (
              <>
                <Button appearance="secondary" onClick={onClose} disabled={pending}>
                  Cancel
                </Button>
                <Button appearance="primary" onClick={() => void rotate()} disabled={pending}>
                  {pending ? "Rotating…" : "Rotate key"}
                </Button>
              </>
            )}
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  );
}

type PrincipalAction = { kind: "disable" | "delete"; p: Principal } | null;

/** Shows the freshly minted key exactly once. It is never stored in UI state beyond this dialog. */
export function KeyRevealDialog({ created, onClose }: { created: Pick<CreatedPrincipal, "agentId" | "apiKey"> | null; onClose: () => void }) {
  const s = useStyles();
  const c = useCommonStyles();
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    if (!created) return;
    try {
      await navigator.clipboard.writeText(created.apiKey);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  return (
    <Dialog open={created !== null} modalType="alert">
      <DialogSurface>
        <DialogBody>
          <DialogTitle>API key for “{created?.agentId}”</DialogTitle>
          <DialogContent className={s.form}>
            <MessageBar intent="warning">
              <MessageBarBody>
                Copy this key now. It is stored hashed and cannot be retrieved again. If it's lost, rotate the key on this page.
              </MessageBarBody>
            </MessageBar>
            <div className={s.keyRow}>
              <Input readOnly value={created?.apiKey ?? ""} className={c.mono} style={{ flex: 1 }} aria-label="API key" onFocus={(e) => e.target.select()} />
              <Button icon={<CopyRegular />} onClick={() => void copy()}>
                {copied ? "Copied" : "Copy"}
              </Button>
            </div>
          </DialogContent>
          <DialogActions>
            <Button
              appearance="primary"
              onClick={() => {
                setCopied(false);
                onClose();
              }}
            >
              I've stored the key
            </Button>
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  );
}

export function PolicyPage() {
  const s = useStyles();
  const c = useCommonStyles();
  const notify = useNotify();
  const principals = useLoader("Load principals", (sig) => listPrincipals(sig), []);
  const rules = useLoader("Load policy rules", (sig) => listRules(sig), []);
  const servers = useLoader("Load servers", (sig) => listServers(sig), []);
  const sources = useLoader("Load skill sources", (sig) => listSkillSources(sig), []);
  const serverNames = useMemo(() => new Map((servers.data ?? []).map((x) => [x.id, x.name])), [servers.data]);
  const sourceNames = useMemo(() => new Map((sources.data ?? []).map((x) => [x.id, x.name])), [sources.data]);

  const [createOpen, setCreateOpen] = useState(false);
  const [revealed, setRevealed] = useState<Pick<CreatedPrincipal, "agentId" | "apiKey"> | null>(null);
  const [pAction, setPAction] = useState<PrincipalAction>(null);
  const [limitsFor, setLimitsFor] = useState<Principal | null>(null);
  const [rotateFor, setRotateFor] = useState<Principal | null>(null);
  const [editRule, setEditRule] = useState<PolicyRule | null>(null);
  const [delRule, setDelRule] = useState<PolicyRule | null>(null);
  const [acting, setActing] = useState(false);

  const ruleTarget = (r: PolicyRule) =>
    r.serverId ? ((r.resourceKind === "skill" ? sourceNames : serverNames).get(r.serverId) ?? r.serverId.slice(0, 8)) : r.resourceKind === "skill" ? "any source" : "any server";
  const ruleLabel = (r: PolicyRule) => `${r.agentId} · ${r.resourceKind === "skill" ? "skill" : "tool"} · ${ruleTarget(r)} · ${r.toolName ?? "any"}`;

  const act = async (what: string, fn: () => Promise<unknown>, ok: string, after: () => void) => {
    setActing(true);
    try {
      await fn();
      notify.success(ok);
      after();
    } catch (e) {
      notify.error(what, e);
    } finally {
      setActing(false);
    }
  };

  const setEnabled = (p: Principal, enabled: boolean) =>
    void act(`${enabled ? "Enable" : "Disable"} “${p.agentId}”`, () => updatePrincipal(p.id, { enabled }), `${enabled ? "Enabled" : "Disabled"} “${p.agentId}”`, () => {
      setPAction(null);
      principals.refresh();
    });

  const confirmPrincipal = () => {
    if (!pAction) return;
    const { kind, p } = pAction;
    if (kind === "disable") return setEnabled(p, false);
    void act(`Delete “${p.agentId}”`, () => deletePrincipal(p.id), `Deleted “${p.agentId}” and its rules`, () => {
      setPAction(null);
      principals.refresh();
      rules.refresh();
    });
  };

  const [ruleAgent, setRuleAgent] = useState("");
  const [ruleKind, setRuleKind] = useState<"tool" | "skill">("tool");
  const [ruleServer, setRuleServer] = useState("");
  const [ruleTool, setRuleTool] = useState("");
  const [ruleCeiling, setRuleCeiling] = useState<OperationCeiling>("read");
  const [ruleApproval, setRuleApproval] = useState(false);
  const [ruleError, setRuleError] = useState<string>();
  const [rulePending, setRulePending] = useState(false);

  const addRule = async () => {
    if (!ruleAgent) {
      setRuleError("Choose the agent this rule allows.");
      return;
    }
    setRuleError(undefined);
    setRulePending(true);
    try {
      await createRule({
        agentId: ruleAgent,
        resourceKind: ruleKind,
        serverId: ruleServer || null,
        toolName: ruleTool.trim() || null,
        maxOperation: ruleCeiling,
        requiresApproval: ruleApproval,
      });
      notify.success(`Added ${ruleCeiling} ${ruleKind} rule for “${ruleAgent}”`);
      setRuleTool("");
      rules.refresh();
    } catch (e) {
      notify.error(`Add rule for “${ruleAgent}”`, e);
    } finally {
      setRulePending(false);
    }
  };

  const plist = principals.data ?? [];
  const rlist = rules.data ?? [];
  return (
    <>
      <PageHeader
        title="Policy"
        meta={<Caption1 className={c.muted}>Deny by default — an agent can only use tools an allow rule covers.</Caption1>}
        actions={
          <Button appearance="primary" icon={<AddRegular />} onClick={() => setCreateOpen(true)}>
            Create principal
          </Button>
        }
      />
      <div className={s.section}>
        <Subtitle2 as="h2">Principals</Subtitle2>
        {principals.data && <Caption1 className={c.muted}>{fmtInt(plist.length)}</Caption1>}
      </div>
      {principals.loading && !principals.data ? (
        <LoadingRow label="Loading principals…" />
      ) : principals.failed && !principals.data ? (
        <ErrorState what="Principals" onRetry={principals.reload} />
      ) : plist.length === 0 && !principals.failed ? (
        <EmptyState
          icon={<ShieldKeyholeRegular />}
          title="No agent principals yet"
          body="Create a principal to issue an API key for an agent. Until keys exist, gateway auth is disabled (dev mode only)."
          action={<Button onClick={() => setCreateOpen(true)}>Create principal</Button>}
        />
      ) : (
        <Table size="small" aria-label="Principals">
          <TableHeader>
            <TableRow>
              <TableHeaderCell>Agent id</TableHeaderCell>
              <TableHeaderCell>Status</TableHeaderCell>
              <TableHeaderCell className={c.num}>Max tools</TableHeaderCell>
              <TableHeaderCell className={c.num}>Max skills</TableHeaderCell>
              <TableHeaderCell className={c.num}>Max servers</TableHeaderCell>
              <TableHeaderCell className={c.num}>Rules</TableHeaderCell>
              <TableHeaderCell>Created</TableHeaderCell>
              <TableHeaderCell aria-label="Actions" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {plist.map((p) => (
              <TableRow key={p.id}>
                <TableCell>
                  <strong>{p.agentId}</strong>
                </TableCell>
                <TableCell>
                  <Switch
                    aria-label={`${p.enabled ? "Disable" : "Enable"} ${p.agentId}`}
                    checked={p.enabled}
                    disabled={acting}
                    label={p.enabled ? "enabled" : "disabled"}
                    onChange={(_, d) => (d.checked ? setEnabled(p, true) : setPAction({ kind: "disable", p }))}
                  />
                </TableCell>
                <TableCell className={c.num}>{p.maxTools}</TableCell>
                <TableCell className={c.num}>{p.maxSkills ?? "—"}</TableCell>
                <TableCell className={c.num}>{p.maxServers ?? <span className={c.muted}>no limit</span>}</TableCell>
                <TableCell className={c.num}>{rlist.filter((r) => r.agentId === p.agentId).length}</TableCell>
                <TableCell>{fmtTime(p.createdAt)}</TableCell>
                <TableCell>
                  <span style={{ display: "flex", gap: 4 }}>
                    <Button size="small" appearance="subtle" icon={<EditRegular />} aria-label={`Edit limits for ${p.agentId}`} onClick={() => setLimitsFor(p)} />
                    <Button size="small" appearance="subtle" icon={<KeyResetRegular />} aria-label={`Rotate key for ${p.agentId}`} onClick={() => setRotateFor(p)} />
                    <Button size="small" appearance="subtle" icon={<DeleteRegular />} aria-label={`Delete ${p.agentId}`} onClick={() => setPAction({ kind: "delete", p })} />
                  </span>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <div className={s.section}>
        <Subtitle2 as="h2">Allow rules</Subtitle2>
        {rules.data && <Caption1 className={c.muted}>{fmtInt(rlist.length)}</Caption1>}
      </div>
      <form
        noValidate
        className={s.ruleForm}
        aria-label="Add rule"
        onSubmit={(e) => {
          e.preventDefault();
          void addRule();
        }}
      >
        <Field label="Agent" required validationMessage={ruleError}>
          <Select value={ruleAgent} onChange={(_, d) => setRuleAgent(d.value)}>
            <option value="">Choose…</option>
            {plist.map((p) => (
              <option key={p.id} value={p.agentId}>
                {p.agentId}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Kind">
          <Select
            value={ruleKind}
            onChange={(_, d) => {
              setRuleKind(d.value as "tool" | "skill");
              setRuleServer(""); // a server id is not a skill source id
            }}
          >
            <option value="tool">Tools</option>
            <option value="skill">Skills</option>
          </Select>
        </Field>
        <Field label={ruleKind === "skill" ? "Skill source" : "Server"}>
          <Select value={ruleServer} onChange={(_, d) => setRuleServer(d.value)}>
            <option value="">{ruleKind === "skill" ? "Any source" : "Any server"}</option>
            {(ruleKind === "skill" ? (sources.data ?? []) : (servers.data ?? [])).map((x) => (
              <option key={x.id} value={x.id}>
                {x.name}
              </option>
            ))}
          </Select>
        </Field>
        <Field label={ruleKind === "skill" ? "Skill name" : "Tool name"} hint="Blank = any; glob allowed">
          <Input value={ruleTool} onChange={(_, d) => setRuleTool(d.value)} placeholder="list_*" />
        </Field>
        <Field label="Operation ceiling" hint="read < write < execute">
          <Select value={ruleCeiling} onChange={(_, d) => setRuleCeiling(d.value as OperationCeiling)}>
            {CEILINGS.map((o) => (
              <option key={o} value={o}>
                up to {o}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Approval">
          <Switch checked={ruleApproval} onChange={(_, d) => setRuleApproval(d.checked)} label="Requires approval" />
        </Field>
        <Button type="submit" disabled={rulePending}>
          {rulePending ? "Adding…" : "Add rule"}
        </Button>
      </form>
      {rules.loading && !rules.data ? (
        <LoadingRow label="Loading rules…" />
      ) : rules.failed && !rules.data ? (
        <ErrorState what="Policy rules" onRetry={rules.reload} />
      ) : rlist.length === 0 && !rules.failed ? (
        <Caption1>No allow rules — every agent is currently denied all tools.</Caption1>
      ) : (
        <Table size="small" aria-label="Rules">
          <TableHeader>
            <TableRow>
              <TableHeaderCell>Agent</TableHeaderCell>
              <TableHeaderCell>Kind</TableHeaderCell>
              <TableHeaderCell>Server / source</TableHeaderCell>
              <TableHeaderCell>Name</TableHeaderCell>
              <TableHeaderCell>Ceiling</TableHeaderCell>
              <TableHeaderCell>Approval</TableHeaderCell>
              <TableHeaderCell>Created</TableHeaderCell>
              <TableHeaderCell aria-label="Actions" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {rlist.map((r) => (
              <TableRow key={r.id}>
                <TableCell>{r.agentId}</TableCell>
                <TableCell>{r.resourceKind === "skill" ? "skill" : "tool"}</TableCell>
                <TableCell>
                  {r.serverId ? (
                    ((r.resourceKind === "skill" ? sourceNames : serverNames).get(r.serverId) ?? <span className={c.mono}>{r.serverId.slice(0, 8)}</span>)
                  ) : (
                    <span className={c.muted}>any</span>
                  )}
                </TableCell>
                <TableCell>{r.toolName ? <span className={c.mono}>{r.toolName}</span> : <span className={c.muted}>any</span>}</TableCell>
                <TableCell>{r.maxOperation}</TableCell>
                <TableCell>{r.requiresApproval ? <Badge appearance="tint" color="warning">required</Badge> : "—"}</TableCell>
                <TableCell>{fmtTime(r.createdAt)}</TableCell>
                <TableCell>
                  <span style={{ display: "flex", gap: 4 }}>
                    <Button size="small" appearance="subtle" icon={<EditRegular />} aria-label={`Edit rule ${ruleLabel(r)}`} onClick={() => setEditRule(r)} />
                    <Button size="small" appearance="subtle" icon={<DeleteRegular />} aria-label={`Delete rule ${ruleLabel(r)}`} onClick={() => setDelRule(r)} />
                  </span>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <CreatePrincipalDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(p) => {
          setCreateOpen(false);
          setRevealed(p);
          principals.refresh();
        }}
      />
      <KeyRevealDialog created={revealed} onClose={() => setRevealed(null)} />
      <RotateKeyDialog key={rotateFor?.id ?? "none"} principal={rotateFor} onClose={() => setRotateFor(null)} />
      <EditLimitsDialog key={limitsFor?.id ?? "none"} principal={limitsFor} onClose={() => setLimitsFor(null)} onSaved={() => principals.refresh()} />
      <EditRuleDialog
        key={editRule?.id ?? "none"}
        rule={editRule}
        target={editRule ? ruleTarget(editRule) : ""}
        targets={(editRule?.resourceKind === "skill" ? sources.data : servers.data) ?? []}
        onClose={() => setEditRule(null)}
        onSaved={() => rules.refresh()}
      />
      <ConfirmDialog
        open={pAction !== null}
        title={pAction?.kind === "disable" ? `Disable “${pAction.p.agentId}”?` : `Delete “${pAction?.p.agentId ?? ""}”?`}
        body={
          pAction?.kind === "disable"
            ? "Its key stops authenticating until you re-enable it. Its rules are kept."
            : `Its key stops working and its ${fmtInt(rlist.filter((r) => r.agentId === pAction?.p.agentId).length)} rule(s) are deleted with it, so a later agent with the same id starts with no access. It can't be undone.`
        }
        confirmLabel={pAction?.kind === "disable" ? "Disable agent" : "Delete agent"}
        pendingLabel={pAction?.kind === "disable" ? "Disabling…" : "Deleting…"}
        pending={acting}
        onConfirm={confirmPrincipal}
        onCancel={() => setPAction(null)}
      />
      <ConfirmDialog
        open={delRule !== null}
        title={delRule ? `Delete rule ${ruleLabel(delRule)}?` : ""}
        body={`“${delRule?.agentId ?? ""}” loses whatever this rule allowed (deny by default). Other rules for the agent are unchanged.`}
        confirmLabel="Delete rule"
        pendingLabel="Deleting…"
        pending={acting}
        onConfirm={() =>
          delRule &&
          void act(`Delete rule for “${delRule.agentId}”`, () => deleteRule(delRule.id), `Deleted a rule for “${delRule.agentId}”`, () => {
            setDelRule(null);
            rules.refresh();
          })
        }
        onCancel={() => setDelRule(null)}
      />
    </>
  );
}
