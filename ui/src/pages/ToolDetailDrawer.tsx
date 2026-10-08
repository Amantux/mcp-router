import {
  Badge,
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
import { DismissRegular } from "@fluentui/react-icons";
import { getTool } from "../api/client";
import type { MCPTool, ToolVersion } from "../api/types";
import { fmtInt, fmtMs, fmtTime, JsonBlock, LoadingRow, OperationBadge, useCommonStyles } from "../components/common";
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

export function ToolDetailDrawer({ toolId, onClose, onChanged }: { toolId: string | null; onClose: () => void; onChanged: (t: MCPTool) => void }) {
  const s = useStyles();
  const c = useCommonStyles();
  const detail = useLoader("Load tool details", (sig) => (toolId ? getTool(toolId, sig) : Promise.resolve(undefined)), [toolId]);
  const t = toolId ? detail.data : undefined;
  const calls = t?.callCount ?? 0;
  const errRate = calls > 0 ? `${(((t?.errorCount ?? 0) / calls) * 100).toFixed(1)}%` : "—";

  return (
    <OverlayDrawer open={toolId !== null} position="end" size="large" onOpenChange={(_, d) => !d.open && onClose()}>
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
          </div>
        )}
      </DrawerHeader>
      <DrawerBody>
        {detail.loading && !t ? (
          <LoadingRow label="Loading tool…" />
        ) : !t ? (
          <Caption1>Tool details couldn't be loaded.</Caption1>
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
    </OverlayDrawer>
  );
}
