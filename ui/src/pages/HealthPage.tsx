import {
  Badge,
  Caption1,
  Card,
  CardHeader,
  Link,
  makeStyles,
  ProgressBar,
  Subtitle2,
  Text,
  tokens,
} from "@fluentui/react-components";
import { getHealthz, getModelsHealth, METRICS_URL } from "../api/client";
import type { ModelsHealth } from "../api/types";
import { fmtInt, fmtMs, LoadingRow, PageHeader, useCommonStyles, ErrorState } from "../components/common";
import { useLoader } from "../hooks/useLoader";
import { useVisiblePolling } from "../hooks/useVisiblePolling";

const POLL_MS = 12_000;

const useStyles = makeStyles({
  grid: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))", gap: tokens.spacingHorizontalM },
  kv: { display: "grid", gridTemplateColumns: "auto 1fr", gap: `2px ${tokens.spacingHorizontalM}`, fontVariantNumeric: "tabular-nums" },
  big: { fontSize: tokens.fontSizeBase600, fontWeight: tokens.fontWeightSemibold, fontVariantNumeric: "tabular-nums" },
  section: { marginTop: tokens.spacingVerticalL, marginBottom: tokens.spacingVerticalS },
});

function MemoryBar({ used, total, label }: { used?: number; total?: number; label: string }) {
  if (used == null) return <Caption1>{label}: —</Caption1>;
  return (
    <div>
      <Caption1 style={{ fontVariantNumeric: "tabular-nums" }}>
        {label}: {fmtInt(Math.round(used))} MB{total ? ` / ${fmtInt(Math.round(total))} MB` : ""}
      </Caption1>
      {total ? <ProgressBar value={used / total} max={1} thickness="large" color={used / total > 0.85 ? "warning" : "brand"} /> : null}
    </div>
  );
}

function BackendCard({
  title,
  info,
  label,
}: {
  title: string;
  info: { kind: string | null; endpointHost: string | null; deployment: string | null } | null | undefined;
  label: { name: string; value: string | null };
}) {
  const s = useStyles();
  if (!info) return null;
  return (
    <Card size="small" aria-label={title}>
      <CardHeader
        header={<Subtitle2>{title}</Subtitle2>}
        action={<Badge appearance="tint">{info.kind ?? "unknown"}</Badge>}
      />
      <div className={s.kv}>
        <Caption1>{label.name}</Caption1>
        <Text weight="semibold">{info.deployment ?? label.value ?? "—"}</Text>
        <Caption1>Endpoint</Caption1>
        <Text weight="semibold">{info.endpointHost ?? "local"}</Text>
      </div>
    </Card>
  );
}

function SystemCards({ h }: { h: ModelsHealth }) {
  const s = useStyles();
  return (
    <div className={s.grid}>
      <Card size="small">
        <CardHeader header={<Subtitle2>Compute</Subtitle2>} />
        <div className={s.kv}>
          <Caption1>Device</Caption1>
          <Text weight="semibold">{h.device}</Text>
          <Caption1>Mode</Caption1>
          <Text weight="semibold">{h.mode}</Text>
        </div>
      </Card>
      <Card size="small">
        <CardHeader header={<Subtitle2>GPU</Subtitle2>} description={<Caption1>{h.gpu?.name ?? "No GPU detected — CPU fallback"}</Caption1>} />
        {h.gpu ? (
          <>
            <MemoryBar label="VRAM" used={h.gpu.memoryUsedMb} total={h.gpu.memoryTotalMb} />
            {h.gpu.utilizationPct != null && <Caption1>Utilization {h.gpu.utilizationPct.toFixed(0)}%</Caption1>}
          </>
        ) : null}
      </Card>
      <Card size="small">
        <CardHeader header={<Subtitle2>Memory</Subtitle2>} />
        <MemoryBar label="Process RSS" used={h.memory?.rssMb} />
        <MemoryBar label="System" used={h.memory?.systemUsedMb} total={h.memory?.systemTotalMb} />
      </Card>
      {(h.routingP50Ms != null || h.routingP95Ms != null) && (
        <Card size="small">
          <CardHeader header={<Subtitle2>Routing latency</Subtitle2>} />
          <div className={s.kv}>
            <Caption1>p50</Caption1>
            <span className={s.big}>{fmtMs(h.routingP50Ms)}</span>
            <Caption1>p95</Caption1>
            <span className={s.big}>{fmtMs(h.routingP95Ms)}</span>
          </div>
        </Card>
      )}
      <BackendCard
        title="Decision backend"
        info={h.decisionBackend}
        label={{ name: "Model", value: h.decisionBackend?.model ?? null }}
      />
      <BackendCard
        title="Embedding backend"
        info={h.embeddingBackend}
        label={{ name: "Model", value: h.embeddingBackend?.name ?? null }}
      />
    </div>
  );
}

export function HealthPage() {
  const s = useStyles();
  const c = useCommonStyles();
  const health = useLoader("Load model health", (sig) => getModelsHealth(sig), []);
  const healthz = useLoader("Check /healthz", (sig) => getHealthz(sig), []);
  useVisiblePolling(() => {
    health.refresh();
    healthz.refresh();
  }, POLL_MS);

  const ok = !healthz.failed && healthz.data?.status === "ok";
  const h = health.data;
  return (
    <>
      <PageHeader
        title="Models & health"
        meta={<Caption1 className={c.muted}>Refreshes every 12 s while this tab is visible</Caption1>}
        actions={
          <Link href={METRICS_URL} target="_blank" rel="noreferrer">
            Raw Prometheus metrics
          </Link>
        }
      />
      <div className={s.grid}>
        <Card size="small" aria-label="Service health">
          <CardHeader header={<Subtitle2>Service (/healthz)</Subtitle2>} />
          {healthz.loading && !healthz.data && !healthz.failed ? (
            <LoadingRow label="Checking…" />
          ) : (
            <Badge appearance="filled" color={ok ? "success" : "danger"}>
              {ok ? "healthy — database reachable" : "failing"}
            </Badge>
          )}
        </Card>
      </div>
      <Subtitle2 as="h2" className={s.section} block>
        System
      </Subtitle2>
      {health.loading && !h ? <LoadingRow label="Loading model health…" /> : h ? <SystemCards h={h} /> : <ErrorState what="Model health" onRetry={health.reload} />}
      <Subtitle2 as="h2" className={s.section} block>
        Loaded models
      </Subtitle2>
      {h && (
        <div className={s.grid}>
          {(h.models ?? []).length === 0 ? (
            <Caption1>No models reported.</Caption1>
          ) : (
            h.models.map((m) => (
              <Card key={`${m.kind}:${m.name}`} size="small">
                <CardHeader
                  header={<Subtitle2>{m.name}</Subtitle2>}
                  description={<Caption1>{m.kind}</Caption1>}
                  action={
                    <Badge appearance="tint" color={m.loaded ? "success" : "warning"}>
                      {m.loaded ? "loaded" : "not loaded"}
                    </Badge>
                  }
                />
                <div className={s.kv}>
                  {m.backend && (
                    <>
                      <Caption1>Backend</Caption1>
                      <Caption1>{m.backend}</Caption1>
                    </>
                  )}
                  {m.device && (
                    <>
                      <Caption1>Device</Caption1>
                      <Caption1>{m.device}</Caption1>
                    </>
                  )}
                  {m.version && (
                    <>
                      <Caption1>Version</Caption1>
                      <Caption1 className={c.mono}>{m.version}</Caption1>
                    </>
                  )}
                </div>
              </Card>
            ))
          )}
        </div>
      )}
    </>
  );
}
