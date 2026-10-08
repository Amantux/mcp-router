import type { ReactNode } from "react";
import {
  Badge,
  Body1,
  Button,
  Caption1,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  makeStyles,
  Spinner,
  Title3,
  tokens,
} from "@fluentui/react-components";
import type { ExecutionOutcome, Operation, ServerStatus } from "../api/types";

export const useCommonStyles = makeStyles({
  num: { textAlign: "right", fontVariantNumeric: "tabular-nums", justifyContent: "flex-end" },
  mono: { fontFamily: tokens.fontFamilyMonospace, fontSize: tokens.fontSizeBase200 },
  muted: { color: tokens.colorNeutralForeground3 },
  toolbar: { display: "flex", flexWrap: "wrap", alignItems: "flex-end", gap: tokens.spacingHorizontalS, marginBottom: tokens.spacingVerticalS },
  section: { marginTop: tokens.spacingVerticalL },
});

const useHeaderStyles = makeStyles({
  row: { display: "flex", alignItems: "center", gap: tokens.spacingHorizontalM, margin: `${tokens.spacingVerticalS} 0 ${tokens.spacingVerticalM}` },
  spacer: { flex: 1 },
});

/** Title left, (one) primary action right. */
export function PageHeader({ title, meta, actions }: { title: string; meta?: ReactNode; actions?: ReactNode }) {
  const s = useHeaderStyles();
  return (
    <div className={s.row}>
      <Title3 as="h1">{title}</Title3>
      {meta}
      <div className={s.spacer} />
      {actions}
    </div>
  );
}

const useEmptyStyles = makeStyles({
  box: {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    gap: tokens.spacingVerticalS,
    padding: `${tokens.spacingVerticalXXL} ${tokens.spacingHorizontalL}`,
    textAlign: "center",
    border: `1px dashed ${tokens.colorNeutralStroke2}`,
    borderRadius: tokens.borderRadiusMedium,
    backgroundColor: tokens.colorNeutralBackground1,
  },
  icon: { fontSize: "32px", color: tokens.colorNeutralForeground3 },
});

export function EmptyState({ icon, title, body, action }: { icon?: ReactNode; title: string; body?: string; action?: ReactNode }) {
  const s = useEmptyStyles();
  return (
    <div className={s.box} role="status">
      {icon && <span className={s.icon}>{icon}</span>}
      <Body1 as="p" style={{ margin: 0, fontWeight: 600 }}>
        {title}
      </Body1>
      {body && (
        <Caption1 as="p" style={{ margin: 0, maxWidth: 520 }}>
          {body}
        </Caption1>
      )}
      {action}
    </div>
  );
}

export function LoadingRow({ label }: { label: string }) {
  return <Spinner size="tiny" label={label} labelPosition="after" style={{ justifyContent: "flex-start", padding: 8 }} />;
}

const STATUS_COLOR: Record<ServerStatus, "success" | "warning" | "danger" | "informative"> = {
  healthy: "success",
  degraded: "warning",
  offline: "danger",
  unknown: "informative",
};

export function StatusBadge({ status }: { status: ServerStatus }) {
  return (
    <Badge appearance="tint" color={STATUS_COLOR[status] ?? "informative"}>
      {status}
    </Badge>
  );
}

const OUTCOME_COLOR: Record<ExecutionOutcome, "success" | "warning" | "danger" | "severe" | "important"> = {
  ok: "success",
  denied: "danger",
  timeout: "warning",
  rate_limited: "severe",
  error: "danger",
};

export function OutcomeBadge({ outcome }: { outcome: ExecutionOutcome }) {
  return (
    <Badge appearance="tint" color={OUTCOME_COLOR[outcome] ?? "informative"}>
      {outcome.replace("_", " ")}
    </Badge>
  );
}

const OP_COLOR: Record<Operation, "brand" | "warning" | "danger" | "informative"> = {
  read: "brand",
  write: "warning",
  execute: "danger",
  unknown: "informative",
};

export function OperationBadge({ op }: { op: Operation }) {
  return (
    <Badge appearance="outline" color={OP_COLOR[op] ?? "informative"} size="small">
      {op}
    </Badge>
  );
}

const useJsonStyles = makeStyles({
  pre: {
    margin: 0,
    padding: tokens.spacingHorizontalS,
    maxHeight: "320px",
    overflow: "auto",
    fontFamily: tokens.fontFamilyMonospace,
    fontSize: tokens.fontSizeBase200,
    backgroundColor: tokens.colorNeutralBackground3,
    borderRadius: tokens.borderRadiusMedium,
    whiteSpace: "pre",
  },
});

export function JsonBlock({ value, label }: { value: unknown; label: string }) {
  const s = useJsonStyles();
  return (
    <pre className={s.pre} aria-label={label} tabIndex={0}>
      {JSON.stringify(value ?? {}, null, 2)}
    </pre>
  );
}

const usePagerStyles = makeStyles({
  row: { display: "flex", alignItems: "center", gap: tokens.spacingHorizontalS, justifyContent: "flex-end", marginTop: tokens.spacingVerticalS },
});

export function Pager({ offset, limit, total, onChange }: { offset: number; limit: number; total: number; onChange: (offset: number) => void }) {
  const s = usePagerStyles();
  if (total <= limit && offset === 0) return null;
  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + limit, total);
  return (
    <div className={s.row}>
      <Caption1 style={{ fontVariantNumeric: "tabular-nums" }}>
        {fmtInt(from)}–{fmtInt(to)} of {fmtInt(total)}
      </Caption1>
      <Button size="small" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}>
        Previous
      </Button>
      <Button size="small" disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}>
        Next
      </Button>
    </div>
  );
}

/** Confirmation that names the object and the consequence. */
export function ConfirmDialog(props: {
  open: boolean;
  title: string;
  body: ReactNode;
  confirmLabel: string;
  pendingLabel: string;
  pending: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <Dialog open={props.open} onOpenChange={(_, d) => !d.open && !props.pending && props.onCancel()}>
      <DialogSurface>
        <DialogBody>
          <DialogTitle>{props.title}</DialogTitle>
          <DialogContent>{props.body}</DialogContent>
          <DialogActions>
            <Button appearance="secondary" onClick={props.onCancel} disabled={props.pending}>
              Cancel
            </Button>
            <Button appearance="primary" onClick={props.onConfirm} disabled={props.pending}>
              {props.pending ? props.pendingLabel : props.confirmLabel}
            </Button>
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  );
}

// ------------------------------------------------------------- formatters
const intFmt = new Intl.NumberFormat("en-US");
export const fmtInt = (n: number | null | undefined) => (n == null ? "—" : intFmt.format(n));
export const fmtMs = (n: number | null | undefined) => (n == null ? "—" : `${n < 10 ? n.toFixed(1) : Math.round(n)} ms`);
export const fmtScore = (n: number) => n.toFixed(3);
export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "never";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
