import { useState } from "react";
import {
  Badge,
  Body1,
  ToggleButton,
  Caption1,
  Field,
  makeStyles,
  Select,
  Subtitle1,
  Subtitle2,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  tokens,
} from "@fluentui/react-components";
import { DataBarVerticalRegular } from "@fluentui/react-icons";
import { getAnalyticsOverview, getAnalyticsSuggestions, listAgentProfiles, listToolFunnels } from "../api/client";
import type { AnalyticsKind, AnalyticsWindow, ToolFunnelSort, WastedTool } from "../api/types";
import {
  costSavedEstimate,
  fmtPct,
  FunnelBars,
  PositionChart,
  PRICE_RATE_ENV,
  StatCard,
  TIME_RATE_ENV,
  timeSavedEstimate,
  WindowPicker,
} from "../components/analytics";
import type { Estimate } from "../components/analytics";
import { EmptyState, ErrorState, fmtInt, fmtMs, fmtTime, LoadingRow, PageHeader, Pager, useCommonStyles } from "../components/common";
import { useLoader } from "../hooks/useLoader";
import { ToolDetailDrawer } from "./ToolDetailDrawer";
import { SkillDrawer } from "./SkillsPage";
import { kindOfId } from "../components/FeedbackThumbs";

const useStyles = makeStyles({
  cards: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))", gap: tokens.spacingHorizontalS },
  savings: { gridColumn: "span 2" },
  wasted: {
    padding: `${tokens.spacingVerticalM} ${tokens.spacingHorizontalL}`,
    border: `1px solid ${tokens.colorNeutralStroke2}`,
    borderLeft: `4px solid ${tokens.colorPaletteMarigoldBorderActive}`,
    borderRadius: tokens.borderRadiusMedium,
    backgroundColor: tokens.colorNeutralBackground1,
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalS,
    marginBottom: tokens.spacingVerticalL,
  },
  measured: {
    marginTop: tokens.spacingVerticalM,
    paddingTop: tokens.spacingVerticalS,
    borderTop: `1px solid ${tokens.colorNeutralStroke2}`,
  },
  section: { marginTop: tokens.spacingVerticalXL, display: "flex", flexDirection: "column", gap: tokens.spacingVerticalS },
  split: { display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: tokens.spacingHorizontalXL, alignItems: "start" },
  link: {
    background: "none",
    border: "none",
    padding: 0,
    cursor: "pointer",
    color: tokens.colorBrandForegroundLink,
    font: "inherit",
    fontWeight: tokens.fontWeightSemibold,
    textAlign: "left",
    ":hover": { textDecoration: "underline" },
  },
});

const PAGE = 25;
const SORTS: { value: ToolFunnelSort; label: string }[] = [
  { value: "surfaced", label: "Most surfaced" },
  { value: "exposedTokens", label: "Most context spent" },
  { value: "selectionRate", label: "Selection rate" },
  { value: "successRate", label: "Success rate" },
  { value: "failed", label: "Most failures" },
  { value: "toolName", label: "Name" },
];

function ToolName({ id, name, onOpen }: { id: string; name: string | null; onOpen: (id: string) => void }) {
  const s = useStyles();
  if (name == null) return <Caption1>deleted tool</Caption1>;
  return (
    <button type="button" className={s.link} onClick={() => onOpen(id)}>
      {name}
    </button>
  );
}

/** The actionable element: tools that cost context on every turn but are rarely chosen. */
export function WastedExposure({
  items,
  minSurfaced,
  maxSelectionRate,
  onOpen,
}: {
  items: WastedTool[];
  minSurfaced: number;
  maxSelectionRate: number;
  onOpen: (id: string) => void;
}) {
  const s = useStyles();
  const c = useCommonStyles();
  const sorted = [...items].sort((a, b) => b.exposedTokens - a.exposedTokens);
  const total = sorted.reduce((n, w) => n + w.exposedTokens, 0);
  return (
    <section className={s.wasted} aria-label="Wasted exposure">
      <Subtitle1 as="h2">Wasted exposure</Subtitle1>
      <Body1>
        {sorted.length === 0
          ? "No tool is surfaced often and rarely chosen. Agents use what they are shown."
          : `${sorted.length} tool${sorted.length === 1 ? " was" : "s were"} shown at least ${minSurfaced} times but selected at most ${fmtPct(maxSelectionRate, 0)} of the time, spending ${fmtInt(total)} tokens of agent context.`}
      </Body1>
      {sorted.length > 0 && (
        <>
          <Caption1>
            Suggestions only, nothing changes automatically. Sharpen the tool's description, fix its classification, or disable it from its
            detail drawer.
          </Caption1>
          <Table size="small" aria-label="Wasted exposure">
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Tool</TableHeaderCell>
                <TableHeaderCell>Server</TableHeaderCell>
                <TableHeaderCell className={c.num}>Surfaced</TableHeaderCell>
                <TableHeaderCell className={c.num}>Selected</TableHeaderCell>
                <TableHeaderCell className={c.num}>Selection rate</TableHeaderCell>
                <TableHeaderCell className={c.num}>Context spent (tokens)</TableHeaderCell>
                <TableHeaderCell className={c.num}>Unhelpful</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {sorted.map((w) => (
                <TableRow key={w.toolId}>
                  <TableCell>
                    <ToolName id={w.toolId} name={w.toolName} onOpen={onOpen} />
                    {w.kind && (
                      <Badge appearance="tint" size="small" style={{ marginLeft: 4 }} color={w.kind === "skill" ? "brand" : "informative"}>
                        {w.kind}
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell>{w.serverName ?? "—"}</TableCell>
                  <TableCell className={c.num}>{fmtInt(w.surfaced)}</TableCell>
                  <TableCell className={c.num}>{fmtInt(w.selected)}</TableCell>
                  <TableCell className={c.num}>{fmtPct(w.selectionRate)}</TableCell>
                  <TableCell className={c.num}>
                    <strong>{fmtInt(w.exposedTokens)}</strong>
                  </TableCell>
                  <TableCell className={c.num}>{w.unhelpful == null ? "—" : fmtInt(w.unhelpful)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </>
      )}
    </section>
  );
}

/** An estimate card: value + its assumption inline, or "Not configured" (never a bare 0). */
function EstimateCard({ label, est, env, testId }: { label: string; est: Estimate | null; env: string; testId: string }) {
  if (!est) return <StatCard testId={testId} label={label} value="—" sub={`Not configured — set ${env}`} />;
  return <StatCard testId={testId} label={label} value={est.value} sub={`estimate ${est.basis}`} />;
}

export function AnalyticsPage() {
  const s = useStyles();
  const c = useCommonStyles();
  const [win, setWin] = useState<AnalyticsWindow>("7d");
  const [sort, setSort] = useState<ToolFunnelSort>("surfaced");
  const [offset, setOffset] = useState(0);
  const [kind, setKind] = useState<AnalyticsKind>("all");
  const [openId, setOpenId] = useState<string | null>(null);
  const [openSkillId, setOpenSkillId] = useState<string | null>(null);
  // Funnel rows carry "skill:<id>" for skills: those open the skill drawer, not the tool one.
  const open = (id: string) => (kindOfId(id) === "skill" ? setOpenSkillId(id.slice("skill:".length)) : setOpenId(id));

  const overview = useLoader("Load analytics overview", (sig) => getAnalyticsOverview(win, sig), [win]);
  const suggestions = useLoader("Load analytics suggestions", (sig) => getAnalyticsSuggestions(win, sig), [win]);
  const agents = useLoader("Load agent analytics", (sig) => listAgentProfiles(win, sig), [win]);
  const tools = useLoader(
    "Load tool funnels",
    (sig) => listToolFunnels({ window: win, sort, order: sort === "toolName" ? "asc" : "desc", limit: PAGE, offset, kind }, sig),
    [win, sort, offset, kind],
  );

  const o = overview.data;
  const pickWindow = (w: AnalyticsWindow) => {
    setWin(w);
    setOffset(0);
  };
  const header = <PageHeader title="Analytics" actions={<WindowPicker value={win} onChange={pickWindow} />} />;

  if (overview.loading && !o)
    return (
      <>
        {header}
        <LoadingRow label="Loading analytics…" />
      </>
    );
  if (!o)
    return (
      <>
        {header}
        <ErrorState what="Analytics" onRetry={overview.reload} />
      </>
    );
  if (o.routing.decisions === 0)
    return (
      <>
        {header}
        <EmptyState
          icon={<DataBarVerticalRegular />}
          title="No routing data in this window yet"
          body="Analytics accrue once agents start routing. Every /route call and MCP tools/list exposure is counted, so connect an agent (or run the agent lens) and check back."
        />
      </>
    );

  const e = o.contextEconomy;
  const sg = suggestions.data;
  return (
    <>
      {header}
      {sg && <WastedExposure items={sg.wastedExposure} minSurfaced={sg.minSurfaced} maxSelectionRate={sg.maxSelectionRate} onOpen={open} />}

      <div className={s.cards} aria-label="Overview">
        <div className={s.savings}>
          <StatCard
            testId="card-savings"
            label="Context savings"
            value={fmtPct(e.savings)}
            sub={`${fmtInt(e.tokensNotSent)} tokens not sent: ${fmtInt(e.exposedTokens)} exposed of ${fmtInt(e.catalogTokens)} in authorized catalogs, over ${fmtInt(e.servedDecisions)} served decisions (${fmtInt(e.unscoredDecisions)} unscored, ${fmtInt(e.noMatchDecisions)} no-match excluded). Estimate: ${e.estimator}.${o.skills && o.skills.bodyTokensNotSent > 0 ? ` Plus ${fmtInt(o.skills.bodyTokensNotSent)} skill-body tokens not sent (bodies load only on activation).` : ""}`}
          />
        </div>
        {o.skills && (
          <StatCard
            testId="card-skills"
            label="Skill activation"
            value={fmtPct(o.skills.activationRate)}
            sub={`${fmtInt(o.skills.activated)} of ${fmtInt(o.skills.surfaced)} surfaced skills activated; ${fmtInt(o.skills.bodyTokensNotSent)} body tokens not sent`}
          />
        )}
        <StatCard testId="card-selection" label="Selection rate" value={fmtPct(o.funnel.selectionRate)} sub={`${fmtInt(o.funnel.selected)} of ${fmtInt(o.funnel.surfaced)} surfaced`} />
        <StatCard testId="card-nomatch" label="No-match rate" value={fmtPct(o.routing.noMatchRate)} sub={`${fmtInt(o.routing.noMatch)} of ${fmtInt(o.routing.decisions)} decisions`} />
        <StatCard testId="card-fallback" label="Fallback rate" value={fmtPct(o.routing.fallbackRate)} sub={`${fmtInt(o.routing.fallback)} deterministic fallback rankings`} />
        <StatCard testId="card-denial" label="Denial rate" value={fmtPct(o.executions.denialRate)} sub={`${fmtInt(o.executions.denied)} of ${fmtInt(o.executions.attempts)} calls`} />
        <StatCard testId="card-latency" label="Routing latency" value={`${fmtMs(o.routing.latencyP50Ms)}`} sub={`p50 · p95 ${fmtMs(o.routing.latencyP95Ms)}`} />
        <EstimateCard testId="card-time-saved" label="Est. time saved" est={timeSavedEstimate(e)} env={TIME_RATE_ENV} />
        <EstimateCard testId="card-cost-saved" label="Est. cost saved" est={costSavedEstimate(e)} env={PRICE_RATE_ENV} />
        {o.feedback && (
          <StatCard
            testId="card-feedback"
            label="Feedback"
            value={fmtPct(o.feedback.helpfulRate)}
            sub={`helpful · ${o.feedback.items == null ? "—" : fmtInt(o.feedback.items)} items · ${fmtPct(o.feedback.coverage)} of decisions covered`}
          />
        )}
      </div>

      {o.measured && (
        // MEASURED (recorded per row), deliberately set apart from the estimates above.
        <div className={`${s.cards} ${s.measured}`} aria-label="Measured latency">
          <StatCard
            testId="card-measured-route"
            label="Measured route latency"
            value={fmtMs(o.measured.routeLatencyP50Ms)}
            sub={`measured · p50 · p95 ${fmtMs(o.measured.routeLatencyP95Ms)}`}
          />
          <StatCard
            testId="card-measured-exec"
            label="Measured execution latency"
            value={fmtMs(o.measured.executionLatencyP50Ms)}
            sub={`measured · p50 · p95 ${fmtMs(o.measured.executionLatencyP95Ms)}`}
          />
        </div>
      )}

      <div className={s.section}>
        <div className={s.split}>
          <div>
            <Subtitle2 as="h2">Position bias</Subtitle2>
            {o.positionCurve.length === 0 ? <Caption1 className={c.muted}>No selections recorded yet.</Caption1> : <PositionChart points={o.positionCurve} />}
          </div>
          <div>
            <Subtitle2 as="h2">Funnel</Subtitle2>
            <FunnelBars label="All tools" surfaced={o.funnel.surfaced} selected={o.funnel.selected} succeeded={o.funnel.succeeded} />
            <Caption1 className={c.muted}>
              Attribution covers {fmtPct(o.executions.attributionCoverage)} of calls; {fmtInt(o.executions.offFunnelSelections)} calls were to tools
              that weren't surfaced.
            </Caption1>
          </div>
        </div>
      </div>

      <section className={s.section} aria-label="Tools">
        <div style={{ display: "flex", alignItems: "flex-end", gap: 12 }}>
          <Subtitle2 as="h2">Tools</Subtitle2>
          <div role="group" aria-label="Kind" style={{ display: "flex", gap: 4 }}>
            {(["all", "tool", "skill"] as const).map((k) => (
              <ToggleButton
                key={k}
                size="small"
                checked={kind === k}
                onClick={() => {
                  setKind(k);
                  setOffset(0);
                }}
              >
                {k === "all" ? "All" : k === "tool" ? "Tools" : "Skills"}
              </ToggleButton>
            ))}
          </div>
          <div style={{ flex: 1 }} />
          <Field label="Sort">
            <Select
              value={sort}
              onChange={(_, d) => {
                setSort(d.value as ToolFunnelSort);
                setOffset(0);
              }}
            >
              {SORTS.map((x) => (
                <option key={x.value} value={x.value}>
                  {x.label}
                </option>
              ))}
            </Select>
          </Field>
        </div>
        {tools.loading && !tools.data ? (
          <LoadingRow label="Loading tool funnels…" />
        ) : tools.failed && !tools.data ? (
          <ErrorState what="Tool funnels" onRetry={tools.reload} />
        ) : (tools.data?.items.length ?? 0) === 0 ? (
          <Caption1 className={c.muted}>No tool was surfaced in this window.</Caption1>
        ) : (
          <>
            <Table size="small" aria-label="Tool funnels">
              <TableHeader>
                <TableRow>
                  <TableHeaderCell>Tool</TableHeaderCell>
                  <TableHeaderCell>Server</TableHeaderCell>
                  <TableHeaderCell style={{ width: 220 }}>Surfaced → selected → succeeded</TableHeaderCell>
                  <TableHeaderCell className={c.num}>Selection</TableHeaderCell>
                  <TableHeaderCell className={c.num}>Success</TableHeaderCell>
                  <TableHeaderCell className={c.num}>Avg rank</TableHeaderCell>
                  <TableHeaderCell className={c.num}>Context (tokens)</TableHeaderCell>
                  <TableHeaderCell className={c.num}>Helpful</TableHeaderCell>
                  <TableHeaderCell className={c.num}>Unhelpful</TableHeaderCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {tools.data!.items.map((t) => (
                  <TableRow key={t.toolId}>
                    <TableCell>
                      <ToolName id={t.toolId} name={t.toolName} onOpen={open} />
                    </TableCell>
                    <TableCell>{t.serverName ?? "—"}</TableCell>
                    <TableCell>
                      <FunnelBars label={t.toolName ?? t.toolId} surfaced={t.surfaced} selected={t.selected} succeeded={t.succeeded} />
                    </TableCell>
                    <TableCell className={c.num}>{fmtPct(t.selectionRate)}</TableCell>
                    <TableCell className={c.num}>{fmtPct(t.successRate)}</TableCell>
                    <TableCell className={c.num}>{t.avgRank == null ? "—" : t.avgRank.toFixed(1)}</TableCell>
                    <TableCell className={c.num}>{fmtInt(t.exposedTokens)}</TableCell>
                    <TableCell className={c.num}>{t.feedbackHelpful == null ? "—" : fmtInt(t.feedbackHelpful)}</TableCell>
                    <TableCell className={c.num}>{t.feedbackUnhelpful == null ? "—" : fmtInt(t.feedbackUnhelpful)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <Pager offset={offset} limit={PAGE} total={tools.data!.total} onChange={setOffset} />
          </>
        )}
      </section>

      <section className={s.section} aria-label="Agents">
        <Subtitle2 as="h2">Agents</Subtitle2>
        {(agents.data ?? []).length === 0 ? (
          <Caption1 className={c.muted}>No agent routed in this window.</Caption1>
        ) : (
          <Table size="small" aria-label="Agent analytics">
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Agent</TableHeaderCell>
                <TableHeaderCell className={c.num}>Decisions</TableHeaderCell>
                <TableHeaderCell className={c.num}>Selection</TableHeaderCell>
                <TableHeaderCell className={c.num}>No-match</TableHeaderCell>
                <TableHeaderCell className={c.num}>Fallback</TableHeaderCell>
                <TableHeaderCell className={c.num}>Denials</TableHeaderCell>
                <TableHeaderCell className={c.num}>Avg exposed</TableHeaderCell>
                <TableHeaderCell className={c.num}>Context savings</TableHeaderCell>
                <TableHeaderCell className={c.num}>p95</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {agents.data!.map((a) => (
                <TableRow key={a.agentId}>
                  <TableCell>
                    <strong>{a.agentId}</strong>
                  </TableCell>
                  <TableCell className={c.num}>{fmtInt(a.decisions)}</TableCell>
                  <TableCell className={c.num}>{fmtPct(a.selectionRate)}</TableCell>
                  <TableCell className={c.num}>{fmtPct(a.noMatchRate)}</TableCell>
                  <TableCell className={c.num}>{fmtPct(a.fallbackRate)}</TableCell>
                  <TableCell className={c.num}>
                    {fmtInt(a.denied)} ({fmtPct(a.denialRate)})
                  </TableCell>
                  <TableCell className={c.num}>{a.avgSurfacedPerDecision == null ? "—" : a.avgSurfacedPerDecision.toFixed(1)}</TableCell>
                  <TableCell className={c.num}>{fmtPct(a.contextEconomy.savings)}</TableCell>
                  <TableCell className={c.num}>{fmtMs(a.latencyP95Ms)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </section>

      <section className={s.section} aria-label="Staleness">
        <Subtitle2 as="h2">Possibly stale</Subtitle2>
        <Caption1>Suggestions only: nothing is disabled automatically. Review these and disable what you no longer need.</Caption1>
        {sg && sg.staleTools.length === 0 && sg.neverRoutedServers.length === 0 ? (
          <Caption1 className={c.muted}>Every tool older than {sg.staleDays} days has been surfaced recently.</Caption1>
        ) : (
          sg && (
            <div className={s.split}>
              <Table size="small" aria-label="Stale tools">
                <TableHeader>
                  <TableRow>
                    <TableHeaderCell>Tool not surfaced in {sg.staleDays} days</TableHeaderCell>
                    <TableHeaderCell>Server</TableHeaderCell>
                    <TableHeaderCell>Last surfaced</TableHeaderCell>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {sg.staleTools.map((t) => (
                    <TableRow key={t.toolId}>
                      <TableCell>
                        <ToolName id={t.toolId} name={t.toolName} onOpen={open} />
                      </TableCell>
                      <TableCell>{t.serverName}</TableCell>
                      <TableCell>{fmtTime(t.lastSurfacedAt)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              <Table size="small" aria-label="Never-routed servers">
                <TableHeader>
                  <TableRow>
                    <TableHeaderCell>Server never routed to</TableHeaderCell>
                    <TableHeaderCell className={c.num}>Tools</TableHeaderCell>
                    <TableHeaderCell>Registered</TableHeaderCell>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {sg.neverRoutedServers.map((v) => (
                    <TableRow key={v.serverId}>
                      <TableCell>{v.serverName}</TableCell>
                      <TableCell className={c.num}>{fmtInt(v.toolCount)}</TableCell>
                      <TableCell>{fmtTime(v.createdAt)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )
        )}
      </section>
      <ToolDetailDrawer toolId={openId} onClose={() => setOpenId(null)} onChanged={() => tools.refresh()} />
      <SkillDrawer skillId={openSkillId} onClose={() => setOpenSkillId(null)} />
    </>
  );
}
