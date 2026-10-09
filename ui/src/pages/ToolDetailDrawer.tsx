import { useState } from "react";
import {
  Badge,
  Tab,
  TabList,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  Body1,
  Button,
  Caption1,
  DrawerBody,
  DrawerHeader,
  DrawerHeaderTitle,
  makeStyles,
  OverlayDrawer,
  Subtitle2,
  tokens,
} from "@fluentui/react-components";
import { DismissRegular, PlayRegular } from "@fluentui/react-icons";
import { Link } from "react-router";
import { getTool, getToolAnalytics, setToolEnabled } from "../api/client";
import { useNotify } from "../components/Notifications";
import type { AnalyticsWindow, MCPTool, ToolVersion } from "../api/types";
import { fmtPct, FunnelBars, PositionChart, StatCard, WindowPicker } from "../components/analytics";
import { ErrorState, fmtInt, fmtMs, fmtTime, JsonBlock, LoadingRow, OperationBadge, useCommonStyles } from "../components/common";
import { useLoader } from "../hooks/useLoader";
import { ClassificationEditor } from "./ClassificationEditor";

const useStyles = makeStyles({
  section: { marginTop: tokens.spacingVerticalL, display: "flex", flexDirection: "column", gap: tokens.spacingVerticalS },
  stats: { display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: tokens.spacingHorizontalS },
  stat: {
    padding: tokens.spacingHorizontalS,
    border: `1px solid ${tokens.colorNeutralStroke2}`,
    borderRadius: tokens.borderRadiusMedium,
    display: "flex",
    flexDirection: "column",
  },
  statValue: { fontSize: tokens.fontSizeBase500, fontWeight: tokens.fontWeightSemibold, fontVariantNumeric: "tabular-nums" },
  timeline: { listStyle: "none", margin: 0, padding: 0, borderLeft: `2px solid ${tokens.colorNeutralStroke2}` },
  tlItem: { position: "relative", padding: `0 0 ${tokens.spacingVerticalS} ${tokens.spacingHorizontalM}` },
  dot: {
    position: "absolute",
    left: "-6px",
    top: "4px",
    width: "10px",
    height: "10px",
    borderRadius: "50%",
    backgroundColor: tokens.colorNeutralForeground3,
  },
  badges: { display: "flex", gap: tokens.spacingHorizontalXS, flexWrap: "wrap", alignItems: "center" },
});

const CHANGE_COLOR: Record<ToolVersion["changeKind"], "success" | "warning" | "informative" | "danger" | "brand"> = {
  added: "success",
  schema: "warning",
  metadata: "informative",
  removed: "danger",
  restored: "brand",
};

export function VersionTimeline({ versions }: { versions: ToolVersion[] }) {
  const s = useStyles();
  const c = useCommonStyles();
  if (versions.length === 0) return <Caption1 className={c.muted}>No recorded versions.</Caption1>;
  const sorted = [...versions].sort((a, b) => b.version - a.version);
  return (
    <ol className={s.timeline} aria-label="Version history">
      {sorted.map((v) => (
        <li key={v.id ?? v.version} className={s.tlItem}>
          <span className={s.dot} />
          <div className={s.badges}>
            <strong style={{ fontVariantNumeric: "tabular-nums" }}>v{v.version}</strong>
            <Badge appearance="tint" size="small" color={CHANGE_COLOR[v.changeKind] ?? "informative"}>
              {v.changeKind}
            </Badge>
            <Caption1>{fmtTime(v.recordedAt)}</Caption1>
          </div>
          <Caption1 className={c.mono}>schema {v.schemaHash.slice(0, 12)}</Caption1>
        </li>
      ))}
    </ol>
  );
}

/** Funnel tab: this tool's surfaced → selected → succeeded, its position curve, and what it competes with. */
export function ToolFunnelPanel({ toolId, kind = "tool" }: { toolId: string; kind?: "tool" | "skill" }) {
  const s = useStyles();
  const c = useCommonStyles();
  // 7d by default: the same window as the Tools table's "Funnel (7d)" column (HS-U-028).
  const [win, setWin] = useState<AnalyticsWindow>("7d");
  const a = useLoader("Load tool analytics", (sig) => getToolAnalytics(toolId, win, sig), [toolId, win]);
  const d = a.data;
  return (
    <>
      <section className={s.section}>
        <WindowPicker value={win} onChange={setWin} />
      </section>
      {a.loading && !d ? (
        <LoadingRow label="Loading funnel…" />
      ) : !d && kind === "skill" ? (
        <Caption1 className={c.muted}>No funnel data for this skill yet. It appears once agents are routed to it.</Caption1>
      ) : !d ? (
        <ErrorState what="Funnel data" onRetry={a.reload} />
      ) : d.tool.surfaced === 0 ? (
        <Caption1 className={c.muted}>This {kind} wasn't surfaced to any agent in this window. Analytics accrue once agents start routing.</Caption1>
      ) : (
        <>
          <section className={s.section} aria-label="Tool funnel">
            <Subtitle2 as="h2">Funnel</Subtitle2>
            <Caption1 as="p" className={c.muted} style={{ margin: 0 }}>
              Surfaced: routing offered it to an agent. Selected: the agent then tried to call it (once per offer). Succeeded: a call returned
              ok. Average rank: its mean position in the offered list (1 = top). Context spent: estimated tokens its definition used in agents'
              context while offered.
            </Caption1>
            <FunnelBars label={d.tool.toolName ?? toolId} surfaced={d.tool.surfaced} selected={d.tool.selected} succeeded={d.tool.succeeded} />
            <div className={s.stats}>
              <StatCard label="Selection rate" value={fmtPct(d.tool.selectionRate)} />
              <StatCard label="Success rate" value={fmtPct(d.tool.successRate)} />
              <StatCard label="Average rank" value={d.tool.avgRank == null ? "—" : d.tool.avgRank.toFixed(1)} />
              <StatCard label="Context spent" value={fmtInt(d.tool.exposedTokens)} sub="tokens" />
            </div>
          </section>
          {d.positionCurve.length > 0 && (
            <section className={s.section}>
              <Subtitle2 as="h2">Selection by rank</Subtitle2>
              <PositionChart points={d.positionCurve} label={`Position bias for ${d.tool.toolName ?? toolId}`} />
            </section>
          )}
          {d.coSurfaced.length > 0 && (
            <section className={s.section}>
              <Subtitle2 as="h2">Shown alongside</Subtitle2>
              <Table size="small" aria-label="Co-surfaced tools">
                <TableHeader>
                  <TableRow>
                    <TableHeaderCell>Tool</TableHeaderCell>
                    <TableHeaderCell className={c.num}>Shown together</TableHeaderCell>
                    <TableHeaderCell className={c.num}>This one picked</TableHeaderCell>
                    <TableHeaderCell className={c.num}>The other picked</TableHeaderCell>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {d.coSurfaced.map((o) => (
                    <TableRow key={o.toolId}>
                      <TableCell>
                        {o.toolName ?? "deleted tool"} <Caption1 className={c.muted}>{o.serverName ?? ""}</Caption1>
                      </TableCell>
                      <TableCell className={c.num}>{fmtInt(o.coSurfaced)}</TableCell>
                      <TableCell className={c.num}>{fmtInt(o.thisSelected)}</TableCell>
                      <TableCell className={c.num}>{fmtInt(o.otherSelected)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </section>
          )}
        </>
      )}
    </>
  );
}

export function ToolDetailDrawer({ toolId, onClose, onChanged }: { toolId: string | null; onClose: () => void; onChanged: (t: MCPTool) => void }) {
  return (
    <OverlayDrawer open={toolId !== null} position="end" size="large" onOpenChange={(_, d) => !d.open && onClose()}>
      {/* Keyed on the id: a different tool never shows the previous one's data or tab. */}
      {toolId !== null && <ToolDetailContent key={toolId} toolId={toolId} onClose={onClose} onChanged={onChanged} />}
    </OverlayDrawer>
  );
}

function ToolDetailContent({ toolId, onClose, onChanged }: { toolId: string; onClose: () => void; onChanged: (t: MCPTool) => void }) {
  const s = useStyles();
  const c = useCommonStyles();
  const detail = useLoader("Load tool details", (sig) => getTool(toolId, sig), [toolId]);
  const t = detail.data;
  const calls = t?.callCount ?? 0;
  const [tab, setTab] = useState<"details" | "funnel">("details");
  const notify = useNotify();
  const [toggling, setToggling] = useState(false);
  // One tool, reversible, no confirm: disabling the whole server is the confirmed bulk action.
  const toggle = async (tool: MCPTool) => {
    setToggling(true);
    try {
      const updated = await setToolEnabled(tool.id, !tool.enabled);
      notify.success(`${tool.enabled ? "Disabled" : "Enabled"} tool “${tool.name}”${tool.enabled ? ": agents no longer see it" : ""}`);
      onChanged(updated);
      detail.refresh();
    } catch (e) {
      notify.error(`${tool.enabled ? "Disable" : "Enable"} tool “${tool.name}”`, e);
    } finally {
      setToggling(false);
    }
  };
  const errRate = calls > 0 ? `${(((t?.errorCount ?? 0) / calls) * 100).toFixed(1)}%` : "—";

  return (
    <>
      <DrawerHeader>
        <DrawerHeaderTitle action={<Button appearance="subtle" aria-label="Close" icon={<DismissRegular />} onClick={onClose} />}>
          {t ? t.name : "Tool"}
        </DrawerHeaderTitle>
        {t && (
          <div className={s.badges}>
            <Caption1>{t.serverName ?? t.serverId}</Caption1>
            <OperationBadge op={t.operation} />
            {t.domain && <Badge appearance="outline" size="small">{t.domain}</Badge>}
            {!t.enabled && <Badge color="danger" appearance="tint" size="small">disabled</Badge>}
            {t.available === false && <Badge color="warning" appearance="tint" size="small">unavailable</Badge>}
            <Caption1 className={c.muted}>v{t.version}</Caption1>
            <Link to={`/playground?tool=${encodeURIComponent(t.id)}`} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              <PlayRegular /> Try in playground
            </Link>
            <Button size="small" disabled={toggling} onClick={() => void toggle(t)}>
              {toggling ? (t.enabled ? "Disabling…" : "Enabling…") : t.enabled ? "Disable tool" : "Enable tool"}
            </Button>
          </div>
        )}
        <TabList size="small" selectedValue={tab} onTabSelect={(_, d) => setTab(d.value as "details" | "funnel")}>
          <Tab value="details">Details</Tab>
          <Tab value="funnel">Funnel</Tab>
        </TabList>
      </DrawerHeader>
      <DrawerBody>
        {detail.loading && !t ? (
          <LoadingRow label="Loading tool…" />
        ) : !t ? (
          <ErrorState what="Tool details" onRetry={detail.reload} />
        ) : tab === "funnel" ? (
          <ToolFunnelPanel toolId={t.id} />
        ) : (
          <>
            <section className={s.section}>
              <Subtitle2 as="h2">Description</Subtitle2>
              <Body1>{t.description || <span className={c.muted}>No description provided by the server.</span>}</Body1>
            </section>
            <section className={s.section}>
              <Subtitle2 as="h2">Classification</Subtitle2>
              <ClassificationEditor
                key={`${t.id}:${t.version}:${t.domain}:${t.operation}:${(t.tags ?? []).join()}:${(t.requiredScopes ?? []).join()}`}
                tool={t}
                onSaved={(u) => {
                  onChanged(u);
                  detail.refresh();
                }}
              />
            </section>
            <section className={s.section}>
              <Subtitle2 as="h2">Usage</Subtitle2>
              <div className={s.stats}>
                <div className={s.stat}>
                  <Caption1>Calls</Caption1>
                  <span className={s.statValue}>{fmtInt(t.callCount)}</span>
                </div>
                <div className={s.stat}>
                  <Caption1>Errors</Caption1>
                  <span className={s.statValue}>{fmtInt(t.errorCount)}</span>
                </div>
                <div className={s.stat}>
                  <Caption1>Error rate</Caption1>
                  <span className={s.statValue}>{errRate}</span>
                </div>
                <div className={s.stat}>
                  <Caption1>Avg latency</Caption1>
                  <span className={s.statValue}>{fmtMs(t.avgLatencyMs)}</span>
                </div>
              </div>
            </section>
            <section className={s.section}>
              <Subtitle2 as="h2">Input schema</Subtitle2>
              <Caption1 className={c.mono}>sha256 {t.schemaHash}</Caption1>
              <JsonBlock value={t.inputSchema} label="Input schema" />
            </section>
            <section className={s.section}>
              <Subtitle2 as="h2">Version history</Subtitle2>
              <VersionTimeline versions={t.versions} />
            </section>
          </>
        )}
      </DrawerBody>
    </>
  );
}
