import { useEffect, useMemo, useState } from "react";
import {
  Badge,
  Body1,
  Button,
  Caption1,
  Field,
  makeStyles,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  mergeClasses,
  SearchBox,
  Select,
  Spinner,
  Subtitle2,
  Tab,
  TabList,
  Textarea,
  tokens,
} from "@fluentui/react-components";
import { PlayRegular, WrenchRegular } from "@fluentui/react-icons";
import { Link, useSearchParams } from "react-router";
import { activateSkill, ApiError, executeTool, getSkill, listSkills, getApproval, getMe, getTool, isAbort, listPrincipals, listServers, listTools } from "../api/client";
import { useAuth } from "../api/auth";
import type { Approval, ExecuteResult, SkillActivation, SkillDetail, JsonObject, MCPTool } from "../api/types";
import { ConfirmDialog, EmptyState, fmtMs, JsonBlock, LoadingRow, OperationBadge, PageHeader, useCommonStyles } from "../components/common";
import { useNotify } from "../components/Notifications";
import { useDebounced } from "../hooks/useDebounced";
import { useLoader } from "../hooks/useLoader";
import {
  compileSchema,
  fromArguments,
  initialDraft,
  mapServerErrors,
  SchemaForm,
  toArguments,
  type Draft,
  type FieldErrors,
} from "../lib/schemaForm";

const useStyles = makeStyles({
  layout: { display: "grid", gridTemplateColumns: "minmax(240px, 300px) minmax(0, 1fr)", gap: tokens.spacingHorizontalXL, alignItems: "start" },
  picker: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalS, minWidth: 0 },
  list: {
    listStyle: "none",
    margin: 0,
    padding: 0,
    maxHeight: "60vh",
    overflowY: "auto",
    border: `1px solid ${tokens.colorNeutralStroke2}`,
    borderRadius: tokens.borderRadiusMedium,
    backgroundColor: tokens.colorNeutralBackground1,
  },
  item: {
    display: "flex",
    flexDirection: "column",
    alignItems: "flex-start",
    width: "100%",
    padding: `6px ${tokens.spacingHorizontalS}`,
    border: "none",
    borderBottom: `1px solid ${tokens.colorNeutralStroke3}`,
    background: "none",
    textAlign: "left",
    cursor: "pointer",
    color: tokens.colorNeutralForeground1,
    ":hover": { backgroundColor: tokens.colorNeutralBackground1Hover },
  },
  itemActive: { backgroundColor: tokens.colorNeutralBackground1Selected },
  runner: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalM, minWidth: 0 },
  head: { display: "flex", alignItems: "center", gap: tokens.spacingHorizontalS, flexWrap: "wrap" },
  runRow: { display: "flex", alignItems: "center", gap: tokens.spacingHorizontalM, flexWrap: "wrap" },
  meta: { display: "flex", gap: tokens.spacingHorizontalL, flexWrap: "wrap", fontVariantNumeric: "tabular-nums" },
  json: { fontFamily: tokens.fontFamilyMonospace, fontSize: tokens.fontSizeBase200 },
  outcome: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalS },
});

const TERMINAL: Approval["status"][] = ["executed", "failed", "denied", "expired"];
export const APPROVAL_POLL_MS = 3000;

/** Who a playground run executes as. Shows only names, never credentials. */
export function RunAsLine({ impersonating }: { impersonating?: string } = {}) {
  const a = useAuth();
  let who: string;
  if (a.hasAgentKey)
    who = a.agentKeyRejected ? "agent key — refused by the backend; fix it in Connect" : a.agentId ? `agent “${a.agentId}” (agent key)` : "agent key (resolving agent…)";
  else if (a.hasAdminToken)
    who = impersonating
      ? `agent “${impersonating}” (admin-initiated — runs under that agent's own policy, and is audited as impersonation)`
      : "admin token — pick an agent below to run as (the backend refuses admin runs with no agent named)";
  else who = "no credential — in dev mode the backend runs this as agent “dev”";
  return (
    <Caption1 data-testid="run-as">
      Runs as <strong>{who}</strong>
    </Caption1>
  );
}

function ResultContent({ result }: { result: NonNullable<ExecuteResult["result"]> }) {
  const s = useStyles();
  return (
    <>
      {result.isError && (
        <MessageBar intent="warning">
          <MessageBarBody>
            <MessageBarTitle>The tool reported an error in its result</MessageBarTitle>
            The call completed, but the upstream tool marked its own output as an error.
          </MessageBarBody>
        </MessageBar>
      )}
      {(result.content ?? []).map((block, i) =>
        block.type === "text" && typeof block.text === "string" ? (
          <pre key={i} className={s.json} style={{ margin: 0, whiteSpace: "pre-wrap" }} aria-label={`Result block ${i + 1}`}>
            {prettyText(block.text)}
          </pre>
        ) : (
          <JsonBlock key={i} value={block} label={`Result block ${i + 1}`} />
        ),
      )}
      {result.structuredContent != null && <JsonBlock value={result.structuredContent} label="Structured result" />}
    </>
  );
}

/** Pretty-print text content that is itself JSON; leave anything else verbatim. */
function prettyText(t: string): string {
  const trimmed = t.trim();
  if (!(trimmed.startsWith("{") || trimmed.startsWith("["))) return t;
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2);
  } catch {
    return t;
  }
}

function ApprovalTracker({ approvalId, asAgent, pollMs }: { approvalId: string; asAgent: boolean; pollMs: number }) {
  const [approval, setApproval] = useState<Approval | null | undefined>(undefined);
  const [checkFailed, setCheckFailed] = useState(false);
  const [refused, setRefused] = useState(false);
  const done = refused || approval === null || (approval !== undefined && TERMINAL.includes(approval.status));

  useEffect(() => {
    if (done) return;
    const ctrl = new AbortController();
    const check = () =>
      getApproval(approvalId, asAgent, ctrl.signal)
        .then((a) => {
          setApproval(a ?? null);
          setCheckFailed(false);
        })
        .catch((e: unknown) => {
          if (isAbort(e)) return;
          // Credentials refused: retrying can't help, so stop polling and say so.
          if (e instanceof ApiError && (e.status === 401 || e.status === 403)) setRefused(true);
          else setCheckFailed(true);
        });
    void check();
    const id = window.setInterval(check, pollMs);
    return () => {
      ctrl.abort();
      window.clearInterval(id);
    };
  }, [approvalId, asAgent, pollMs, done]);

  if (refused)
    return (
      <MessageBar intent="error" data-testid="approval-refused">
        <MessageBarBody>
          The backend refused to show this approval's status for the current credentials. Check them in Connect, or follow it on the{" "}
          <Link to="/approvals">Approvals</Link> page.
        </MessageBarBody>
      </MessageBar>
    );
  if (approval === null)
    return (
      <MessageBar intent="info" data-testid="approval-gone">
        <MessageBarBody>This approval request no longer exists. It may have been cleaned up after it expired.</MessageBarBody>
      </MessageBar>
    );
  const status = approval?.status ?? "pending";
  if (status === "pending" || status === "executing")
    return (
      <MessageBar intent="warning" layout="multiline" data-testid="outcome-pending">
        <MessageBarBody>
          <MessageBarTitle>Waiting for approval</MessageBarTitle>
          A policy rule requires a human to approve this call. An admin can approve or deny it on the{" "}
          <Link to="/approvals">Approvals</Link> page. Requests expire after 10 minutes.{" "}
          <Spinner size="extra-tiny" style={{ display: "inline-flex" }} label={status === "executing" ? "Approved — running…" : "Checking status…"} labelPosition="after" />
          {checkFailed && " The last status check failed; retrying."}
        </MessageBarBody>
      </MessageBar>
    );
  const copy: Record<string, { intent: "success" | "error" | "warning"; title: string; body: string }> = {
    executed: { intent: "success", title: "Approved and executed", body: "An admin approved the call and it ran." },
    failed: { intent: "error", title: "Approved, but the call failed", body: "It was re-checked against current policy and schema, or the upstream call failed." },
    denied: { intent: "error", title: "Approval denied", body: "An admin denied this call. Nothing was executed." },
    expired: { intent: "warning", title: "Approval expired", body: "Nobody decided within 10 minutes. Nothing was executed; run it again to re-request." },
  };
  const c = copy[status];
  return (
    <>
      <MessageBar intent={c.intent} data-testid={`approval-${status}`}>
        <MessageBarBody>
          <MessageBarTitle>{c.title}</MessageBarTitle>
          {c.body}
        </MessageBarBody>
      </MessageBar>
      {approval?.resultPreview && <JsonBlock value={approval.resultPreview} label="Result preview" />}
    </>
  );
}

const STATUS_COPY: Record<string, { intent: "error" | "warning"; title: string; body: string }> = {
  denied: { intent: "error", title: "Denied by policy", body: "No policy rule lets this identity run this tool. Nothing was executed." },
  rate_limited: { intent: "warning", title: "Rate limit reached", body: "This agent made too many calls in the last minute. Wait a moment, then run it again." },
  unavailable: { intent: "warning", title: "Tool unavailable", body: "The tool or its server is disabled or offline. Check the Servers page, then retry." },
  invalid_args: { intent: "error", title: "The tool's schema rejected these arguments", body: "Fix the highlighted fields and run it again." },
  timeout: { intent: "warning", title: "The tool didn't answer in time", body: "The upstream call hit the execution timeout. It may still have had side effects upstream." },
  error: { intent: "error", title: "The tool call failed", body: "The upstream server returned an error." },
  cancelled: { intent: "warning", title: "Call cancelled", body: "The call was cancelled before it finished." },
};

/** Honest rendering of every execution outcome, plus the audit id and latency. */
export function ExecutionOutcomeView({
  tool,
  outcome,
  asAgent,
  generalErrors = [],
  pollMs = APPROVAL_POLL_MS,
}: {
  tool: Pick<MCPTool, "name">;
  outcome: ExecuteResult & { roundTripMs?: number };
  asAgent: boolean;
  generalErrors?: string[];
  pollMs?: number;
}) {
  const s = useStyles();
  const c = useCommonStyles();
  const copy = STATUS_COPY[outcome.status];
  return (
    <section className={s.outcome} aria-label="Run outcome" data-testid={`outcome-${outcome.status}`}>
      <Subtitle2 as="h2">Outcome</Subtitle2>
      {outcome.status === "ok" ? (
        <>
          <MessageBar intent="success">
            <MessageBarBody>
              <MessageBarTitle>Ran {tool.name}</MessageBarTitle>
            </MessageBarBody>
          </MessageBar>
          {outcome.result ? <ResultContent result={outcome.result} /> : <Caption1>The tool returned no content.</Caption1>}
        </>
      ) : outcome.status === "pending_approval" ? (
        outcome.approvalId ? (
          <ApprovalTracker approvalId={outcome.approvalId} asAgent={asAgent} pollMs={pollMs} />
        ) : (
          <MessageBar intent="warning">
            <MessageBarBody>
              <MessageBarTitle>Waiting for approval</MessageBarTitle>
              The backend didn't return an approval id, so this page can't track it. Check the <Link to="/approvals">Approvals</Link> page.
            </MessageBarBody>
          </MessageBar>
        )
      ) : (
        <MessageBar intent={copy?.intent ?? "error"} layout="multiline">
          <MessageBarBody>
            <MessageBarTitle>{copy?.title ?? `Outcome: ${outcome.status}`}</MessageBarTitle>
            {copy?.body}
            {outcome.detail && (
              <>
                <br />
                <span data-testid="outcome-detail">Reason: {outcome.detail}</span>
              </>
            )}
            {generalErrors.length > 0 && (
              <ul style={{ margin: "4px 0 0", paddingLeft: 16 }}>
                {generalErrors.map((e) => (
                  <li key={e}>{e}</li>
                ))}
              </ul>
            )}
          </MessageBarBody>
        </MessageBar>
      )}
      <div className={s.meta}>
        <Caption1>
          Audit record{" "}
          {outcome.recordId ? (
            <Link to={`/executions`} className={c.mono} data-testid="audit-id">
              {outcome.recordId}
            </Link>
          ) : (
            "—"
          )}
        </Caption1>
        {outcome.latencyMs != null && (
          <Caption1>
            Latency <strong>{fmtMs(outcome.latencyMs)}</strong>
          </Caption1>
        )}
        {outcome.roundTripMs != null && (
          <Caption1>
            Round trip <strong>{fmtMs(outcome.roundTripMs)}</strong> (measured in the browser)
          </Caption1>
        )}
      </div>
    </section>
  );
}

type Mode = "form" | "json";

function ToolRunner({ tool }: { tool: MCPTool }) {
  const s = useStyles();
  const c = useCommonStyles();
  const notify = useNotify();
  const auth = useAuth();
  const compiled = useMemo(() => compileSchema(tool.inputSchema), [tool.inputSchema]);
  const root = compiled.ok ? compiled.root : null;
  const [mode, setMode] = useState<Mode>(root ? "form" : "json");
  const [draft, setDraft] = useState<Draft>(() => (root ? initialDraft(root) : {}));
  const [jsonText, setJsonText] = useState(() => JSON.stringify(root ? toArguments(root, initialDraft(root)).value : {}, null, 2));
  const [notice, setNotice] = useState<string | null>(null);
  const [jsonError, setJsonError] = useState<string>();
  // Admin-only session: the backend requires an agentId to impersonate, so the
  // run button stays disabled until one is picked (see routes_execute.py).
  const adminOnly = auth.hasAdminToken && !auth.hasAgentKey;
  const [runAs, setRunAs] = useState("");
  const [agents, setAgents] = useState<string[] | null>(null);
  useEffect(() => {
    if (!adminOnly) return;
    const ctl = new AbortController();
    listPrincipals(ctl.signal)
      .then((ps) => setAgents(ps.filter((p) => p.enabled).map((p) => p.agentId)))
      .catch((e) => {
        if (!isAbort(e)) setAgents([]);
      });
    return () => ctl.abort();
  }, [adminOnly]);
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const [serverErrors, setServerErrors] = useState<FieldErrors>({});
  const [generalErrors, setGeneralErrors] = useState<string[]>([]);
  const [outcome, setOutcome] = useState<(ExecuteResult & { roundTripMs: number }) | null>(null);
  const [running, setRunning] = useState(false);
  const [confirmArgs, setConfirmArgs] = useState<JsonObject | null>(null);
  const asAgent = auth.hasAgentKey;

  const switchMode = (next: Mode) => {
    if (next === mode) return;
    setNotice(null);
    if (next === "json") {
      // form → JSON always works: invalid inputs are simply left out.
      if (root) setJsonText(JSON.stringify(toArguments(root, draft).value, null, 2));
      setMode("json");
      return;
    }
    if (!root) return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(jsonText || "{}");
    } catch {
      setNotice("This isn't valid JSON, so it stays in raw mode. Fix the JSON, then switch to the form.");
      return;
    }
    const back = fromArguments(root, parsed);
    if (!back.ok) {
      setNotice(`The form can't represent these arguments (${back.reason}), so they stay as raw JSON.`);
      return;
    }
    setDraft(back.draft);
    setClientErrors({});
    setServerErrors({});
    setMode("form");
  };

  const collect = (): JsonObject | null => {
    if (mode === "form" && root) {
      const { value, errors } = toArguments(root, draft);
      setClientErrors(errors);
      return Object.keys(errors).length ? null : value;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(jsonText || "{}");
    } catch {
      setJsonError("Not valid JSON.");
      return null;
    }
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      setJsonError("Arguments must be a JSON object, like {\"name\": \"value\"}.");
      return null;
    }
    setJsonError(undefined);
    return parsed as JsonObject;
  };

  const run = async (args: JsonObject) => {
    setConfirmArgs(null);
    setRunning(true);
    setOutcome(null);
    setServerErrors({});
    setGeneralErrors([]);
    try {
      const res = await executeTool(tool.id, args, undefined, adminOnly && runAs ? { agentId: runAs } : {});
      if (res.status === "invalid_args") {
        const mapped = mapServerErrors(res.errors ?? []);
        if (mode === "form") {
          setServerErrors(mapped.fields);
          setGeneralErrors(mapped.general);
        } else setGeneralErrors([...Object.entries(mapped.fields).map(([p, m]) => `${p}: ${m}`), ...mapped.general]);
      }
      setOutcome(res);
    } catch (e) {
      notify.error(`Run ${tool.name}`, e);
    } finally {
      setRunning(false);
    }
  };

  const submit = () => {
    const args = collect();
    if (!args) return;
    if (tool.operation === "read") void run(args);
    else setConfirmArgs(args); // write/execute/unknown tools act on real systems: confirm first
  };

  const errors = { ...clientErrors, ...serverErrors };
  const who = auth.hasAgentKey
    ? auth.agentId
      ? `agent “${auth.agentId}”`
      : "the agent key"
    : auth.hasAdminToken
      ? runAs
        ? `agent “${runAs}” (admin-initiated)`
        : "the admin token"
      : "dev mode";

  return (
    <section className={s.runner} aria-label={`Run ${tool.name}`}>
      <div className={s.head}>
        <Subtitle2 as="h2">{tool.name}</Subtitle2>
        <Caption1 className={c.muted}>{tool.serverName ?? tool.serverId}</Caption1>
        <OperationBadge op={tool.operation} />
        {!tool.enabled && (
          <Badge size="small" appearance="tint" color="danger">
            disabled
          </Badge>
        )}
      </div>
      {tool.description && <Body1>{tool.description}</Body1>}
      <RunAsLine impersonating={adminOnly && runAs ? runAs : undefined} />
      {adminOnly && (
        <Field label="Run as agent" hint="Admin-initiated runs execute under the chosen agent's own policy and are audited as impersonation.">
          <Select data-testid="run-as-picker" value={runAs} onChange={(_, d) => setRunAs(d.value)}>
            <option value="">Choose an agent…</option>
            {(agents ?? []).map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </Select>
        </Field>
      )}
      {!compiled.ok && (
        <MessageBar intent="info" data-testid="schema-raw-only">
          <MessageBarBody>The input schema can't be shown as a form ({compiled.reason}). Edit the arguments as JSON.</MessageBarBody>
        </MessageBar>
      )}
      <TabList selectedValue={mode} onTabSelect={(_, d) => switchMode(d.value as Mode)} size="small">
        <Tab value="form" disabled={!root}>
          Form
        </Tab>
        <Tab value="json">Raw JSON</Tab>
      </TabList>
      {notice && (
        <MessageBar intent="warning" data-testid="raw-notice">
          <MessageBarBody>{notice}</MessageBarBody>
        </MessageBar>
      )}
      {mode === "form" && root ? (
        <SchemaForm
          root={root}
          draft={draft}
          errors={errors}
          disabled={running}
          onChange={(d) => {
            setDraft(d);
            setServerErrors({});
          }}
        />
      ) : (
        <Field label="Arguments (JSON)" validationMessage={jsonError}>
          <Textarea
            className={s.json}
            value={jsonText}
            rows={10}
            resize="vertical"
            disabled={running}
            onChange={(_, d) => {
              setJsonText(d.value);
              setJsonError(undefined);
            }}
          />
        </Field>
      )}
      <div className={s.runRow}>
        <Button appearance="primary" icon={<PlayRegular />} disabled={running || (adminOnly && !runAs)} onClick={submit}>
          {running ? "Running…" : "Run tool"}
        </Button>
        {tool.operation !== "read" && <Caption1 className={c.muted}>This is a {tool.operation} tool: you'll be asked to confirm.</Caption1>}
      </div>
      {outcome && <ExecutionOutcomeView tool={tool} outcome={outcome} asAgent={asAgent} generalErrors={generalErrors} />}
      <ConfirmDialog
        open={confirmArgs !== null}
        title={`Run ${tool.name} as ${who}?`}
        body={
          <>
            This calls the real <strong>{tool.name}</strong> tool on <strong>{tool.serverName ?? "its server"}</strong>. It is classified as a{" "}
            <strong>{tool.operation}</strong> tool, so it may change data upstream. Policy, approval rules and the audit log apply as for any agent call.
          </>
        }
        confirmLabel={`Run ${tool.name}`}
        pendingLabel="Running…"
        pending={running}
        onConfirm={() => confirmArgs && void run(confirmArgs)}
        onCancel={() => setConfirmArgs(null)}
      />
    </section>
  );
}

/** Activation failures the backend reports as HTTP status, mapped onto the shared outcome renderer. */
const ACTIVATION_STATUS: Record<
  number,
  { status: ExecuteResult["status"]; detail: string }
> = {
  403: {
    status: "denied",
    detail: "Policy denied this activation for the chosen identity.",
  },
  404: {
    status: "unavailable",
    detail: "This skill is not routed to the chosen identity.",
  },
  429: {
    status: "rate_limited",
    detail:
      "Activation rate limit reached for this identity. Try again shortly.",
  },
};

function SkillActivator({ skill }: { skill: SkillDetail }) {
  const s = useStyles();
  const c = useCommonStyles();
  const notify = useNotify();
  const auth = useAuth();
  // Same Run-as rules as tools: an admin-only session must name the agent it activates as.
  const adminOnly = auth.hasAdminToken && !auth.hasAgentKey;
  const [runAs, setRunAs] = useState("");
  const [agents, setAgents] = useState<string[] | null>(null);
  useEffect(() => {
    if (!adminOnly) return;
    const ctl = new AbortController();
    listPrincipals(ctl.signal)
      .then((ps) =>
        setAgents(ps.filter((p) => p.enabled).map((p) => p.agentId)),
      )
      .catch((e) => {
        if (!isAbort(e)) setAgents([]);
      });
    return () => ctl.abort();
  }, [adminOnly]);
  const [confirming, setConfirming] = useState(false);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<SkillActivation | null>(null);
  const [failure, setFailure] = useState<ExecuteResult | null>(null);
  const who = auth.hasAgentKey
    ? auth.agentId
      ? `agent “${auth.agentId}”`
      : "the agent key"
    : auth.hasAdminToken
      ? runAs
        ? `agent “${runAs}” (admin-initiated)`
        : "the admin token"
      : "dev mode";

  const activate = async () => {
    setConfirming(false);
    setRunning(true);
    setResult(null);
    setFailure(null);
    try {
      setResult(
        await activateSkill(
          skill.id,
          adminOnly && runAs ? { agentId: runAs } : {},
        ),
      );
    } catch (e) {
      const mapped =
        e instanceof ApiError ? ACTIVATION_STATUS[e.status] : undefined;
      if (mapped) setFailure({ ...mapped, recordId: null });
      else notify.error(`Activate ${skill.name}`, e);
    } finally {
      setRunning(false);
    }
  };

  return (
    <section className={s.runner} aria-label={`Activate ${skill.name}`}>
      <div className={s.head}>
        <Subtitle2 as="h2">{skill.name}</Subtitle2>
        <OperationBadge op={skill.operation} />
      </div>
      {skill.description && <Body1>{skill.description}</Body1>}
      <RunAsLine impersonating={adminOnly && runAs ? runAs : undefined} />
      {adminOnly && (
        <Field
          label="Activate as agent"
          hint="Admin-initiated activations run under the chosen agent's own policy and are audited as impersonation."
        >
          <Select
            data-testid="skill-run-as-picker"
            value={runAs}
            onChange={(_, d) => setRunAs(d.value)}
          >
            <option value="">Choose an agent…</option>
            {(agents ?? []).map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </Select>
        </Field>
      )}
      <div>
        <Button
          appearance="primary"
          icon={<PlayRegular />}
          disabled={running || (adminOnly && !runAs)}
          onClick={() =>
            skill.operation === "execute"
              ? setConfirming(true)
              : void activate()
          }
        >
          {running ? "Activating…" : `Activate as ${who}`}
        </Button>
      </div>
      {failure && (
        <ExecutionOutcomeView
          tool={skill}
          outcome={failure}
          asAgent={auth.hasAgentKey}
        />
      )}
      {result && (
        <section
          className={s.outcome}
          aria-label="Activation result"
          data-testid="activation-ok"
        >
          <MessageBar intent="success">
            <MessageBarBody>
              <MessageBarTitle>Activated {skill.name}</MessageBarTitle>
              {result.recordId
                ? `Audit record ${result.recordId}`
                : "No audit record id returned."}
            </MessageBarBody>
          </MessageBar>
          <Subtitle2 as="h3">Body</Subtitle2>
          {/* Skill bodies are untrusted text: a text node in <pre>, never Markdown/HTML. */}
          <pre aria-label="Skill body" className={c.muted}>
            {result.body}
          </pre>
          <Subtitle2 as="h3">Resources</Subtitle2>
          {result.resources.length === 0 ? (
            <Caption1>No resources.</Caption1>
          ) : (
            <ul aria-label="Skill resources">
              {result.resources.map((r) => (
                <li key={r.path}>
                  {r.path} · {r.kind} · {r.size} B
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
      <ConfirmDialog
        open={confirming}
        title={`Activate ${skill.name} as ${who}?`}
        body={
          <>
            <strong>{skill.name}</strong> is classified as an{" "}
            <strong>execute</strong> skill: its instructions may tell an agent
            to run scripts or act on real systems. Activation is audited and
            subject to policy as for any agent.
          </>
        }
        confirmLabel={`Activate ${skill.name}`}
        pendingLabel="Activating…"
        pending={running}
        onConfirm={() => void activate()}
        onCancel={() => setConfirming(false)}
      />
    </section>
  );
}

function SkillsPlayground() {
  const s = useStyles();
  const c = useCommonStyles();
  const [params, setParams] = useSearchParams();
  const selectedId = params.get("skill");
  const [query, setQuery] = useState("");
  const q = useDebounced(query, 200);
  const skills = useLoader(
    "Load skills",
    (sig) =>
      listSkills(
        { q: q.trim() || undefined, enabled: true, limit: 50, offset: 0 },
        sig,
      ),
    [q],
  );
  const detail = useLoader(
    "Load skill",
    (sig) =>
      selectedId ? getSkill(selectedId, sig) : Promise.resolve(undefined),
    [selectedId],
  );
  const items = skills.data?.items ?? [];
  const selected =
    selectedId && detail.data?.id === selectedId ? detail.data : undefined;
  return (
    <div className={s.layout}>
      <div className={s.picker}>
        <Field label="Find a skill">
          <SearchBox
            value={query}
            onChange={(_, d) => setQuery(d.value)}
            placeholder="Name or description"
          />
        </Field>
        {skills.loading && !skills.data ? (
          <LoadingRow label="Loading skills…" />
        ) : items.length === 0 ? (
          <Caption1>
            {q
              ? "No enabled skills match."
              : "No enabled skills yet. Add a skill source first."}
          </Caption1>
        ) : (
          <ul className={s.list} aria-label="Skills">
            {items.map((k) => (
              <li key={k.id}>
                <button
                  type="button"
                  className={mergeClasses(
                    s.item,
                    k.id === selectedId && s.itemActive,
                  )}
                  aria-current={k.id === selectedId ? "true" : undefined}
                  onClick={() => setParams({ skill: k.id })}
                >
                  <strong>{k.name}</strong>
                  <Caption1 className={c.muted}>
                    {k.sourceName ?? ""} · {k.operation}
                  </Caption1>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        {!selectedId ? (
          <EmptyState
            icon={<WrenchRegular />}
            title="Pick a skill to activate it"
            body="Activation returns the skill's body and resource list exactly as an agent would receive them, through the same policy and audit path."
          />
        ) : !selected ? (
          detail.loading ? (
            <LoadingRow label="Loading skill…" />
          ) : (
            <Caption1>That skill couldn't be loaded.</Caption1>
          )
        ) : (
          <SkillActivator key={selected.id} skill={selected} />
        )}
      </div>
    </div>
  );
}

export function PlaygroundPage() {
  const s = useStyles();
  const c = useCommonStyles();
  const auth = useAuth();
  const [params, setParams] = useSearchParams();
  const selectedId = params.get("tool");
  const [serverId, setServerId] = useState("");
  const [query, setQuery] = useState("");
  const q = useDebounced(query, 200);

  // Resolve the agent id behind the agent key, so the page can say who runs the tool.
  useEffect(() => {
    if (!auth.hasAgentKey || auth.agentId || auth.agentKeyRejected) return;
    const ctrl = new AbortController();
    getMe(ctrl.signal).catch(() => {});
    return () => ctrl.abort();
  }, [auth.hasAgentKey, auth.agentId, auth.agentKeyRejected]);

  const servers = useLoader("Load servers", (sig) => listServers(sig), []);
  const tools = useLoader(
    "Load tools",
    (sig) => listTools({ q: q.trim() || undefined, serverId: serverId || undefined, enabled: true, limit: 50, offset: 0 }, sig),
    [q, serverId],
  );
  const detail = useLoader("Load tool", (sig) => (selectedId ? getTool(selectedId, sig) : Promise.resolve(undefined)), [selectedId]);
  const items = tools.data?.items ?? [];
  const selected = selectedId && detail.data?.id === selectedId ? detail.data : undefined;

  const tab = params.has("skill") || params.get("tab") === "skills" ? "skills" : "tools";

  return (
    <>
      <PageHeader title="Playground" />
      <TabList selectedValue={tab} onTabSelect={(_, d) => setParams(d.value === "skills" ? { tab: "skills" } : {})} aria-label="Playground kind">
        <Tab value="tools">Tools</Tab>
        <Tab value="skills">Skills</Tab>
      </TabList>
      {tab === "skills" ? (
        <SkillsPlayground />
      ) : (
        <div className={s.layout}>
        <div className={s.picker}>
          <Field label="Server">
            <Select value={serverId} onChange={(_, d) => setServerId(d.value)}>
              <option value="">All servers</option>
              {(servers.data ?? []).map((sv) => (
                <option key={sv.id} value={sv.id}>
                  {sv.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Find a tool">
            <SearchBox value={query} onChange={(_, d) => setQuery(d.value)} placeholder="Name or description" />
          </Field>
          {tools.loading && !tools.data ? (
            <LoadingRow label="Loading tools…" />
          ) : items.length === 0 ? (
            <Caption1>{q || serverId ? "No enabled tools match." : "No enabled tools yet. Register a server first."}</Caption1>
          ) : (
            <ul className={s.list} aria-label="Tools">
              {items.map((t) => (
                <li key={t.id}>
                  <button
                    type="button"
                    className={mergeClasses(s.item, t.id === selectedId && s.itemActive)}
                    aria-current={t.id === selectedId ? "true" : undefined}
                    onClick={() => setParams({ tool: t.id })}
                  >
                    <strong>{t.name}</strong>
                    <Caption1 className={c.muted}>
                      {t.serverName ?? ""} · {t.operation}
                    </Caption1>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div>
          {!selectedId ? (
            <EmptyState
              icon={<WrenchRegular />}
              title="Pick a tool to try it"
              body="The argument form is generated from the tool's input schema. Runs go through the same policy, approval and audit path as an agent's call."
            />
          ) : !selected ? (
            detail.loading ? (
              <LoadingRow label="Loading tool…" />
            ) : (
              <Caption1>That tool couldn't be loaded.</Caption1>
            )
          ) : (
            <ToolRunner key={`${selected.id}:${selected.schemaHash}`} tool={selected} />
          )}
        </div>
        </div>
      )}
    </>
  );
}
