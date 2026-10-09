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
import { AddRegular, CopyRegular, ShieldKeyholeRegular } from "@fluentui/react-icons";
import { createPrincipal, createRule, listPrincipals, listRules, listServers, listSkillSources } from "../api/client";
import { CEILINGS, type CreatedPrincipal, type OperationCeiling } from "../api/types";
import { EmptyState, ErrorState, fmtInt, fmtTime, LoadingRow, PageHeader, useCommonStyles } from "../components/common";
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

/** Shows the freshly minted key exactly once. It is never stored in UI state beyond this dialog. */
export function KeyRevealDialog({ created, onClose }: { created: CreatedPrincipal | null; onClose: () => void }) {
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
                Copy this key now. It is stored hashed and cannot be retrieved again — if it's lost, create a new principal.
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
  const [revealed, setRevealed] = useState<CreatedPrincipal | null>(null);

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
            </TableRow>
          </TableHeader>
          <TableBody>
            {plist.map((p) => (
              <TableRow key={p.id}>
                <TableCell>
                  <strong>{p.agentId}</strong>
                </TableCell>
                <TableCell>
                  <Badge appearance="tint" color={p.enabled ? "success" : "danger"}>
                    {p.enabled ? "enabled" : "disabled"}
                  </Badge>
                </TableCell>
                <TableCell className={c.num}>{p.maxTools}</TableCell>
                <TableCell className={c.num}>{p.maxSkills ?? "—"}</TableCell>
                <TableCell className={c.num}>{p.maxServers ?? <span className={c.muted}>no limit</span>}</TableCell>
                <TableCell className={c.num}>{rlist.filter((r) => r.agentId === p.agentId).length}</TableCell>
                <TableCell>{fmtTime(p.createdAt)}</TableCell>
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
    </>
  );
}
