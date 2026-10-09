/** Small, library-free analytics visuals: stat tiles, funnel bars, position-bias bars. */
import type { ReactNode } from "react";
import { Caption1, makeStyles, Tab, TabList, tokens } from "@fluentui/react-components";
import type { AnalyticsWindow, RankPoint } from "../api/types";
import { fmtInt } from "./common";

export const WINDOWS: { value: AnalyticsWindow; label: string }[] = [
  { value: "7d", label: "7 days" },
  { value: "30d", label: "30 days" },
  { value: "90d", label: "90 days" },
];

/** Fraction (0..1) → "12.3%"; null (no denominator yet) → "—". */
export const fmtPct = (r: number | null | undefined, digits = 1) => (r == null ? "—" : `${(r * 100).toFixed(digits)}%`);

const useStyles = makeStyles({
  card: {
    padding: `${tokens.spacingVerticalS} ${tokens.spacingHorizontalM}`,
    border: `1px solid ${tokens.colorNeutralStroke2}`,
    borderRadius: tokens.borderRadiusMedium,
    backgroundColor: tokens.colorNeutralBackground1,
    display: "flex",
    flexDirection: "column",
    gap: "2px",
    minWidth: 0,
  },
  value: { fontSize: tokens.fontSizeBase600, lineHeight: tokens.lineHeightBase600, fontWeight: tokens.fontWeightSemibold, fontVariantNumeric: "tabular-nums" },
  sub: { color: tokens.colorNeutralForeground3, fontVariantNumeric: "tabular-nums" },
  funnel: { display: "flex", flexDirection: "column", gap: "2px", minWidth: "120px" },
  funnelRow: { display: "grid", gridTemplateColumns: "1fr 52px", alignItems: "center", gap: tokens.spacingHorizontalXS },
  track: { height: "6px", borderRadius: "3px", backgroundColor: tokens.colorNeutralBackground4, overflow: "hidden" },
  chart: { display: "flex", alignItems: "flex-end", gap: "2px", height: "96px", padding: `0 0 ${tokens.spacingVerticalXS}`, borderBottom: `1px solid ${tokens.colorNeutralStroke2}` },
  col: { flex: 1, display: "flex", flexDirection: "column", justifyContent: "flex-end", height: "100%", minWidth: "10px" },
  colBar: { backgroundColor: tokens.colorBrandBackground, borderRadius: "4px 4px 0 0", minHeight: "1px" },
  axis: { display: "flex", gap: "2px" },
  axisLabel: { flex: 1, textAlign: "center", minWidth: "10px", color: tokens.colorNeutralForeground3, fontVariantNumeric: "tabular-nums" },
});

export function StatCard({ label, value, sub, testId }: { label: string; value: ReactNode; sub?: ReactNode; testId?: string }) {
  const s = useStyles();
  return (
    <div className={s.card} data-testid={testId}>
      <Caption1>{label}</Caption1>
      <span className={s.value}>{value}</span>
      {sub && <Caption1 className={s.sub}>{sub}</Caption1>}
    </div>
  );
}

const STAGE_FILL = [tokens.colorBrandBackground2Pressed, tokens.colorBrandBackground, tokens.colorPaletteGreenBackground3];

/**
 * Surfaced → selected → succeeded, each bar scaled to `surfaced` (one hue
 * family; the "succeeded" stage carries success semantics). Labels carry the
 * counts so identity never depends on colour.
 */
export function FunnelBars({ surfaced, selected, succeeded, label }: { surfaced: number; selected: number; succeeded: number; label: string }) {
  const s = useStyles();
  const rows: [string, number][] = [
    ["surfaced", surfaced],
    ["selected", selected],
    ["succeeded", succeeded],
  ];
  return (
    <div className={s.funnel} role="img" aria-label={`${label}: surfaced ${surfaced}, selected ${selected}, succeeded ${succeeded}`}>
      {rows.map(([name, n], i) => (
        <div key={name} className={s.funnelRow} title={`${name}: ${fmtInt(n)}`}>
          <div className={s.track}>
            <div
              data-testid={`funnel-${name}`}
              style={{ width: `${surfaced > 0 ? Math.min(100, (n / surfaced) * 100) : 0}%`, height: "100%", borderRadius: 3, backgroundColor: STAGE_FILL[i] }}
            />
          </div>
          <Caption1 style={{ fontVariantNumeric: "tabular-nums", textAlign: "right" }}>{fmtInt(n)}</Caption1>
        </div>
      ))}
    </div>
  );
}

/** Rank vs selection rate, as columns. A steep drop after rank 1–2 means agents mostly take the top pick. */
export function PositionChart({ points, label = "Position bias" }: { points: RankPoint[]; label?: string }) {
  const s = useStyles();
  const pts = [...points].sort((a, b) => a.rank - b.rank);
  const max = Math.max(0.0001, ...pts.map((p) => p.rate ?? 0));
  return (
    <figure style={{ margin: 0 }} aria-label={label}>
      <div className={s.chart}>
        {pts.map((p) => (
          <div key={p.rank} className={s.col} title={`Rank ${p.rank}: ${fmtPct(p.rate)} selected (${fmtInt(p.selected)} of ${fmtInt(p.shown)} shown)`}>
            <div className={s.colBar} data-testid={`rank-${p.rank}`} style={{ height: `${((p.rate ?? 0) / max) * 100}%` }} />
          </div>
        ))}
      </div>
      <div className={s.axis} aria-hidden>
        {pts.map((p) => (
          <Caption1 key={p.rank} className={s.axisLabel}>
            {p.rank}
          </Caption1>
        ))}
      </div>
      <figcaption>
        <Caption1>
          Rank in the exposed list → share of times the tool at that rank was selected. Peak {fmtPct(max === 0.0001 ? null : max)}.
        </Caption1>
      </figcaption>
    </figure>
  );
}

export function WindowPicker({ value, onChange }: { value: AnalyticsWindow; onChange: (w: AnalyticsWindow) => void }) {
  return (
    <TabList size="small" selectedValue={value} onTabSelect={(_, d) => onChange(d.value as AnalyticsWindow)} aria-label="Time window">
      {WINDOWS.map((w) => (
        <Tab key={w.value} value={w.value}>
          {w.label}
        </Tab>
      ))}
    </TabList>
  );
}

// ---------------------------------------------------------------- estimates
// An estimate is NEVER shown without its basis: each helper returns the value
// together with the assumption it was derived from, or null when the operator
// has not configured the rate (the UI then says so instead of rendering 0).

export const TIME_RATE_ENV = "MCPR_PREFILL_MS_PER_1K_TOKENS";
export const PRICE_RATE_ENV = "MCPR_PRICE_PER_1K_INPUT_TOKENS";

export interface Estimate {
  value: string;
  basis: string;
}

export function fmtDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const sec = ms / 1000;
  if (sec < 60) return `${sec.toFixed(1)} s`;
  if (sec < 3600) return `${(sec / 60).toFixed(1)} min`;
  return `${(sec / 3600).toFixed(1)} h`;
}

function fmtMoney(amount: number, currency: string | null | undefined): string {
  if (currency) {
    try {
      return new Intl.NumberFormat(undefined, { style: "currency", currency, maximumFractionDigits: 4 }).format(amount);
    } catch {
      // not an ISO 4217 code: fall through to "<amount> <code>"
    }
  }
  const n = amount.toLocaleString(undefined, { maximumFractionDigits: 4 });
  return currency ? `${n} ${currency}` : n;
}

export function timeSavedEstimate(e: {
  estimatedTimeSavedMs?: number | null;
  assumptions?: { prefillMsPer1kTokens: number | null } | null;
}): Estimate | null {
  const rate = e.assumptions?.prefillMsPer1kTokens;
  if (e.estimatedTimeSavedMs == null || rate == null) return null;
  return { value: fmtDuration(e.estimatedTimeSavedMs), basis: `at ${rate} ms / 1K tokens` };
}

export function costSavedEstimate(e: {
  estimatedCostSaved?: number | null;
  currency?: string | null;
  assumptions?: { pricePer1kInputTokens: number | null } | null;
}): Estimate | null {
  const rate = e.assumptions?.pricePer1kInputTokens;
  if (e.estimatedCostSaved == null || rate == null) return null;
  return { value: fmtMoney(e.estimatedCostSaved, e.currency), basis: `at ${fmtMoney(rate, e.currency)} / 1K input tokens` };
}
