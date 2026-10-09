import { useEffect, useRef, useState } from "react";
import {
  Badge,
  Button,
  Caption1,
  Field,
  makeStyles,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  ProgressBar,
  Select,
  Slider,
  Subtitle2,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  Textarea,
  tokens,
} from "@fluentui/react-components";
import { EyeRegular } from "@fluentui/react-icons";
import { isAbort, listPrincipals, simulateAgent } from "../api/client";
import type { BudgetClamp, SimulateRequest, SimulateResponse } from "../api/types";
import { EmptyState, fmtMs, fmtScore, LoadingRow, PageHeader, useCommonStyles } from "../components/common";
import { useNotify } from "../components/Notifications";
import { FeedbackThumbs } from "../components/FeedbackThumbs";
import { useSearchParams } from "react-router";
import { useDebounced } from "../hooks/useDebounced";
import { useLoader } from "../hooks/useLoader";

const useStyles = makeStyles({
  layout: { display: "grid", gridTemplateColumns: "minmax(260px, 340px) minmax(0, 1fr)", gap: tokens.spacingHorizontalXL, alignItems: "start" },
  form: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalM },
  result: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalM, minWidth: 0 },
  readout: { display: "flex", gap: tokens.spacingHorizontalL, flexWrap: "wrap", fontVariantNumeric: "tabular-nums" },
  bar: { display: "flex", alignItems: "center", gap: tokens.spacingHorizontalS, minWidth: "180px" },
  clamps: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: tokens.spacingHorizontalM },
  clamp: {
    padding: tokens.spacingHorizontalS,
    border: `1px solid ${tokens.colorNeutralStroke2}`,
    borderRadius: tokens.borderRadiusMedium,
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalXS,
    fontVariantNumeric: "tabular-nums",
  },
  track: { position: "relative", height: "10px", borderRadius: "5px", backgroundColor: tokens.colorNeutralBackground5 },
  fill: { position: "absolute", left: 0, top: 0, bottom: 0, borderRadius: "5px", backgroundColor: tokens.colorBrandBackground },
  marker: { position: "absolute", top: "-3px", bottom: "-3px", width: "2px", backgroundColor: tokens.colorPaletteRedBorderActive },
  stages: { display: "flex", flexWrap: "wrap", alignItems: "center", gap: tokens.spacingHorizontalXS, fontVariantNumeric: "tabular-nums" },
  sliderRow: { display: "flex", alignItems: "center", gap: tokens.spacingHorizontalS },
});

export const TOOL_LIMIT = 64; // PrincipalIn.max_tools le=64
const SERVER_LIMIT = 16;
const SKILL_LIMIT = 10;
const BUDGET_LABEL: Record<string, string> = { maxTools: "Max tools", maxServers: "Max servers", maxSkills: "Max skills" };
const BY_LABEL: Record<string, string> = { principal: "the agent's own cap", global: "the global cap (MCPR_MAX_EXPOSED_*)" };

/** Requested vs applied for one budget: a track scaled to the largest term, filled to `applied`, red marker at `requested`. */
export function ClampView({ clamp }: { clamp: BudgetClamp }) {
  const s = useStyles();
  const name = BUDGET_LABEL[clamp.budget] ?? clamp.budget;
  const terms = [clamp.requested, clamp.principal, clamp.globalCap, clamp.applied].filter((v): v is number => v != null);
  const scale = Math.max(1, ...terms);
  const pct = (v: number) => `${Math.min(100, (v / scale) * 100)}%`;
  const fmt = (v: number | null) => (v == null ? "∞" : String(v));
  const clamped = clamp.clampedBy != null;
  return (
    <div className={s.clamp} data-testid={`clamp-${clamp.budget}`} aria-label={`${name} budget`}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <strong>{name}</strong>
        {clamped ? (
          <Badge appearance="tint" color="warning" size="small">
            clamped
          </Badge>
        ) : (
          <Badge appearance="tint" color="success" size="small">
            as requested
          </Badge>
        )}
      </div>
      <Caption1 data-testid={`clamp-${clamp.budget}-summary`}>
        requested {clamp.requested == null ? "default" : clamp.requested} → applied <strong>{fmt(clamp.applied)}</strong>
      </Caption1>
      <div className={s.track} role="img" aria-label={`${name}: requested ${fmt(clamp.requested)}, applied ${fmt(clamp.applied)}`}>
        {clamp.applied != null && <div className={s.fill} style={{ width: pct(clamp.applied) }} data-testid={`clamp-${clamp.budget}-fill`} />}
        {clamp.requested != null && <div className={s.marker} style={{ left: pct(clamp.requested) }} />}
      </div>
      <Caption1>
        agent cap {fmt(clamp.principal)} · global cap {fmt(clamp.globalCap)}
      </Caption1>
      {clamped && <Caption1 data-testid={`clamp-${clamp.budget}-by`}>Limited by {BY_LABEL[String(clamp.clampedBy)] ?? clamp.clampedBy}.</Caption1>}
    </div>
  );
}

/**
 * `routeRequestId` (from `?routeRequestId=`, set by an Executions row's "Open in lens"
 * link) is the REAL routing decision being rated. Thumbs render only when it is
 * present: a plain lens run is a simulation with no decision to attach feedback to.
 */
export function LensResult({ result, showFiltered, routeRequestId }: { result: SimulateResponse; showFiltered: boolean; routeRequestId?: string | null }) {
  const s = useStyles();
  const c = useCommonStyles();
  const ranked = [...result.tools].sort((a, b) => b.score - a.score);
  const rankedSkills = [...result.skills].sort((a, b) => b.score - a.score);
  return (
    <section className={s.result} aria-label="Lens result">
      <div className={s.readout}>
        <Caption1>
          Agent <strong>{result.agentId}</strong>
        </Caption1>
        <Caption1>
          Latency <strong>{fmtMs(result.latencyMs)}</strong>
        </Caption1>
        {result.candidates != null && <Caption1>{result.candidates} candidates considered</Caption1>}
        {result.requestId && <Caption1 className={c.mono}>request {result.requestId}</Caption1>}
      </div>
      {result.fallbackUsed && (
        <MessageBar intent="warning" data-testid="fallback-banner">
          <MessageBarBody>
            <MessageBarTitle>Deterministic fallback used</MessageBarTitle>
            The decision model was unavailable or too slow, so this ranking comes from the heuristic scorer, not calibrated probabilities.
          </MessageBarBody>
        </MessageBar>
      )}
      {result.clamps.length > 0 && (
        <div>
          <Subtitle2 as="h2">Budgets</Subtitle2>
          <div className={s.clamps}>
            {result.clamps.map((cl) => (
              <ClampView key={cl.budget} clamp={cl} />
            ))}
          </div>
        </div>
      )}
      {result.stages.length > 0 && (
        <div className={s.stages} aria-label="Pipeline stages">
          {result.stages.map((st, i) => (
            <span key={`${st.stage}-${i}`}>
              {i > 0 && "→ "}
              <Badge appearance="outline">
                {st.stage}: {st.before} → {st.after}
                {st.before > st.after ? ` (−${st.before - st.after})` : ""}
                {st.prunedByKind
                  ? ` [${Object.entries(st.prunedByKind)
                      .map(([k, n]) => `${n} ${k}${n === 1 ? "" : "s"}`)
                      .join(", ")}]`
                  : ""}
              </Badge>
            </span>
          ))}
        </div>
      )}
      <div>
        <Subtitle2 as="h2">Tools exposed to the agent</Subtitle2>
        {result.noMatch ? (
          <MessageBar intent="info" data-testid="no-match-banner">
            <MessageBarBody>
              <MessageBarTitle>No match</MessageBarTitle>
              No tool this agent may use is relevant enough, so it would receive an empty tool list. Check its policy rules, or whether a
              server that covers this task is registered and enabled.
            </MessageBarBody>
          </MessageBar>
        ) : (
          <Table size="small" aria-label="Exposed tools">
            <TableHeader>
              <TableRow>
                <TableHeaderCell className={c.num} style={{ width: 40 }}>
                  #
                </TableHeaderCell>
                <TableHeaderCell>Tool</TableHeaderCell>
                <TableHeaderCell>Server</TableHeaderCell>
                <TableHeaderCell>Score</TableHeaderCell>
                {routeRequestId && <TableHeaderCell>Feedback</TableHeaderCell>}
              </TableRow>
            </TableHeader>
            <TableBody>
              {ranked.map((t, i) => (
                <TableRow key={`${t.serverName}/${t.toolName}`}>
                  <TableCell className={c.num}>{i + 1}</TableCell>
                  <TableCell>
                    <strong>{t.toolName}</strong>
                  </TableCell>
                  <TableCell>{t.serverName}</TableCell>
                  <TableCell>
                    <div className={s.bar}>
                      <ProgressBar value={Math.max(0, Math.min(1, t.score))} max={1} thickness="large" style={{ flex: 1 }} aria-label={`${t.toolName} score`} />
                      <span className={c.num} style={{ width: 48 }}>
                        {fmtScore(t.score)}
                      </span>
                    </div>
                  </TableCell>
                  {routeRequestId && (
                    <TableCell>
                      <FeedbackThumbs routeRequestId={routeRequestId} target={{ kind: "tool", name: t.toolName }} label={t.toolName} />
                    </TableCell>
                  )}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>
      <div aria-label="Skills section">
        <Subtitle2 as="h2">
          Skills surfaced{result.maxSkillsApplied != null ? ` (up to ${result.maxSkillsApplied})` : ""}
        </Subtitle2>
        {rankedSkills.length === 0 ? (
          <Caption1 className={c.muted}>No skill was surfaced for this task.</Caption1>
        ) : (
          <Table size="small" aria-label="Surfaced skills">
            <TableHeader>
              <TableRow>
                <TableHeaderCell className={c.num} style={{ width: 40 }}>
                  #
                </TableHeaderCell>
                <TableHeaderCell>Skill</TableHeaderCell>
                <TableHeaderCell>Source</TableHeaderCell>
                <TableHeaderCell className={c.num}>Body tokens</TableHeaderCell>
                <TableHeaderCell>Score</TableHeaderCell>
                {routeRequestId && <TableHeaderCell>Feedback</TableHeaderCell>}
              </TableRow>
            </TableHeader>
            <TableBody>
              {rankedSkills.map((k, i) => (
                <TableRow key={`${k.source}/${k.skill}`}>
                  <TableCell className={c.num}>{i + 1}</TableCell>
                  <TableCell>
                    <strong>{k.skill}</strong>
                  </TableCell>
                  <TableCell>{k.source}</TableCell>
                  <TableCell className={c.num}>~{k.bodyTokensEst.toLocaleString()}</TableCell>
                  <TableCell>
                    <div className={s.bar}>
                      <ProgressBar value={Math.max(0, Math.min(1, k.score))} max={1} thickness="large" style={{ flex: 1 }} aria-label={`${k.skill} score`} />
                      <span className={c.num} style={{ width: 48 }}>
                        {fmtScore(k.score)}
                      </span>
                    </div>
                  </TableCell>
                  {routeRequestId && (
                    <TableCell>
                      <FeedbackThumbs routeRequestId={routeRequestId} target={{ kind: "skill", id: k.skillId, name: k.skill }} label={k.skill} />
                    </TableCell>
                  )}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>
      {showFiltered && (
        <div aria-label="Filtered out">
          <Subtitle2 as="h2">Filtered out</Subtitle2>
          <Caption1 as="p" style={{ margin: "4px 0" }}>
            Admin-only view. The agent never sees these tools or the reasons.
          </Caption1>
          {result.filtered.length === 0 ? (
            <Caption1 className={c.muted}>Nothing was filtered out, or the backend didn't report filter diagnostics.</Caption1>
          ) : (
            <Table size="small" aria-label="Filtered tools">
              <TableHeader>
                <TableRow>
                  <TableHeaderCell>Name</TableHeaderCell>
                  <TableHeaderCell>Kind</TableHeaderCell>
                  <TableHeaderCell>Server / source</TableHeaderCell>
                  <TableHeaderCell>Stage</TableHeaderCell>
                  <TableHeaderCell>Reason</TableHeaderCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {result.filtered.map((f, i) => (
                  <TableRow key={`${f.serverName}/${f.toolName}/${i}`}>
                    <TableCell>{f.toolName}</TableCell>
                    <TableCell>
                      <Badge appearance="tint" color={f.kind === "skill" ? "brand" : "informative"}>{f.kind ?? "tool"}</Badge>
                    </TableCell>
                    <TableCell>{f.serverName}</TableCell>
                    <TableCell>{f.stage ?? "policy"}</TableCell>
                    <TableCell>{f.reason}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </div>
      )}
    </section>
  );
}

export function LensPage({ debounceMs = 300 }: { debounceMs?: number }) {
  const s = useStyles();
  const notify = useNotify();
  const principals = useLoader("Load agents", (sig) => listPrincipals(sig), []);
  // Router state, not window.location: "Open in lens" while already on /lens must re-read it.
  const [params] = useSearchParams();
  const routeRequestId = params.get("routeRequestId");
  const linkedAgent = params.get("agentId");
  const [agentId, setAgentId] = useState(linkedAgent ?? "");
  useEffect(() => {
    if (linkedAgent) setAgentId(linkedAgent);
  }, [linkedAgent]);
  const [query, setQuery] = useState("");
  const [maxTools, setMaxTools] = useState(8);
  const [maxServers, setMaxServers] = useState(0); // 0 = don't request a server cap
  const [maxSkills, setMaxSkills] = useState(0); // 0 = don't request a skills cap
  const [showFiltered, setShowFiltered] = useState(true);
  const [errors, setErrors] = useState<{ agentId?: string; query?: string }>({});
  const [pending, setPending] = useState(false);
  const [result, setResult] = useState<SimulateResponse | null>(null);
  const last = useRef<{ agentId: string; query: string } | null>(null);
  const inflight = useRef<AbortController | null>(null);
  // Debounce primitives (an object literal would be a new value every render).
  const dTools = useDebounced(maxTools, debounceMs);
  const dServers = useDebounced(maxServers, debounceMs);
  const dSkills = useDebounced(maxSkills, debounceMs);

  const simulate = async (base: { agentId: string; query: string }, b: { maxTools: number; maxServers: number; maxSkills: number }) => {
    inflight.current?.abort();
    const ctrl = new AbortController();
    inflight.current = ctrl;
    const body: SimulateRequest = { agentId: base.agentId, query: base.query, maxTools: b.maxTools };
    if (b.maxServers > 0) body.maxServers = b.maxServers;
    if (b.maxSkills > 0) body.maxSkills = b.maxSkills;
    setPending(true);
    try {
      setResult(await simulateAgent(body, ctrl.signal));
      last.current = base;
    } catch (e) {
      if (!isAbort(e)) notify.error(`Simulate routing for “${base.agentId}”`, e);
    } finally {
      if (inflight.current === ctrl) setPending(false);
    }
  };

  // Live budget sliders: once a lens is showing, re-query (debounced) with the new budgets.
  useEffect(() => {
    if (last.current) void simulate(last.current, { maxTools: dTools, maxServers: dServers, maxSkills: dSkills });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-run only when the debounced budgets change
  }, [dTools, dServers, dSkills]);

  useEffect(() => () => inflight.current?.abort(), []);

  // Sliders re-query only the lens on screen; once the agent or query is edited, wait for an explicit submit.
  useEffect(() => {
    if (last.current && (last.current.agentId !== agentId || last.current.query !== query.trim())) last.current = null;
  }, [agentId, query]);

  const submit = () => {
    const e: typeof errors = {};
    if (!agentId) e.agentId = "Choose the agent whose view you want to see.";
    if (!query.trim()) e.query = "Describe the task the agent is trying to do.";
    setErrors(e);
    if (Object.keys(e).length) return;
    void simulate({ agentId, query: query.trim() }, { maxTools, maxServers, maxSkills });
  };

  const list = principals.data ?? [];
  return (
    <>
      <PageHeader title="Agent lens" />
      <div className={s.layout}>
        <form
          noValidate
          className={s.form}
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <Field label="Agent" required validationMessage={errors.agentId} hint="Routing runs under this agent's policy and budgets. Nothing is exposed to it.">
            <Select value={agentId} onChange={(_, d) => setAgentId(d.value)}>
              <option value="">{principals.loading ? "Loading agents…" : "— choose an agent —"}</option>
              {list.map((p) => (
                <option key={p.id} value={p.agentId}>
                  {p.agentId}
                  {p.enabled ? "" : " (disabled)"} · cap {p.maxTools}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Task query" required validationMessage={errors.query}>
            <Textarea value={query} onChange={(_, d) => setQuery(d.value)} rows={4} resize="vertical" placeholder="List open pull requests on the api repo" />
          </Field>
          <Field label={`Requested max tools: ${maxTools}`} hint="Ask for more than the caps allow to see the clamp.">
            <Slider min={1} max={TOOL_LIMIT} value={maxTools} onChange={(_, d) => setMaxTools(d.value)} aria-label="Requested max tools" />
          </Field>
          <Field label={`Requested max servers: ${maxServers === 0 ? "no limit requested" : maxServers}`}>
            <Slider min={0} max={SERVER_LIMIT} value={maxServers} onChange={(_, d) => setMaxServers(d.value)} aria-label="Requested max servers" />
          </Field>
          <Field label={`Requested max skills: ${maxSkills === 0 ? "no limit requested" : maxSkills}`} hint="Can only lower the agent's and global skills caps.">
            <Slider min={0} max={SKILL_LIMIT} value={maxSkills} onChange={(_, d) => setMaxSkills(d.value)} aria-label="Requested max skills" />
          </Field>
          <Switch checked={showFiltered} onChange={(_, d) => setShowFiltered(d.checked)} label="Show filtered-out tools" />
          <div>
            <Button appearance="primary" type="submit" disabled={pending}>
              {pending ? "Simulating…" : "Show agent's view"}
            </Button>
          </div>
        </form>
        <div>
          {result ? (
            <LensResult result={result} showFiltered={showFiltered} routeRequestId={routeRequestId} />
          ) : pending ? (
            <LoadingRow label="Simulating…" />
          ) : principals.data && list.length === 0 ? (
            <EmptyState
              icon={<EyeRegular />}
              title="No agents yet"
              body="The lens shows what a specific agent would be offered. Create an agent principal on the Policy page first."
            />
          ) : (
            <EmptyState
              icon={<EyeRegular />}
              title="See the catalog through an agent's eyes"
              body="Pick an agent and describe a task. The lens shows the tools it would be offered, how its budgets clamp the list, and what policy filtered out."
            />
          )}
        </div>
      </div>
    </>
  );
}
