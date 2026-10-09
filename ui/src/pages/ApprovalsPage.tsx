import { useState } from "react";
import {
  Badge,
  Button,
  Caption1,
  Field,
  Select,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
} from "@fluentui/react-components";
import { CheckmarkCircleRegular } from "@fluentui/react-icons";
import { approveApproval, denyApproval, listApprovals } from "../api/client";
import type { Approval, ApprovalStatus } from "../api/types";
import { ConfirmDialog, EmptyState, ErrorState, fmtTime, JsonBlock, LoadingRow, PageHeader, useCommonStyles } from "../components/common";
import { useNotify } from "../components/Notifications";
import { useLoader } from "../hooks/useLoader";
import { useVisiblePolling } from "../hooks/useVisiblePolling";

const STATUSES: ApprovalStatus[] = ["pending", "executing", "executed", "failed", "denied", "expired"];
const COLOR: Record<ApprovalStatus, "warning" | "informative" | "success" | "danger" | "subtle"> = {
  pending: "warning",
  executing: "informative",
  executed: "success",
  failed: "danger",
  denied: "danger",
  expired: "subtle",
};

type Pending = { approval: Approval; action: "approve" | "deny" } | null;

export function ApprovalsPage() {
  const c = useCommonStyles();
  const notify = useNotify();
  const [status, setStatus] = useState<"" | ApprovalStatus>("pending");
  const [confirm, setConfirm] = useState<Pending>(null);
  const [busy, setBusy] = useState(false);
  const list = useLoader("Load approvals", (sig) => listApprovals(status || undefined, sig), [status]);
  useVisiblePolling(list.refresh, 10_000);
  const items = list.data ?? [];

  const decide = async () => {
    if (!confirm) return;
    const { approval, action } = confirm;
    const tool = approval.summary.tool ?? approval.toolId;
    setBusy(true);
    try {
      const res = action === "approve" ? await approveApproval(approval.id) : await denyApproval(approval.id);
      notify.success(action === "approve" ? `Approved ${tool} for ${approval.agentId} — ${res.status}` : `Denied ${tool} for ${approval.agentId}`);
      setConfirm(null);
      list.refresh();
    } catch (e) {
      notify.error(`${action === "approve" ? "Approve" : "Deny"} ${tool}`, e);
    } finally {
      setBusy(false);
    }
  };

  const target = confirm ? (confirm.approval.summary.tool ?? confirm.approval.toolId) : "";
  return (
    <>
      <PageHeader title="Approvals" />
      <div className={c.toolbar}>
        <Field label="Status">
          <Select value={status} onChange={(_, d) => setStatus(d.value as "" | ApprovalStatus)}>
            <option value="">Any</option>
            {STATUSES.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </Select>
        </Field>
      </div>
      {list.loading && !list.data ? (
        <LoadingRow label="Loading approvals…" />
      ) : list.failed && !list.data ? (
        <ErrorState what="Approvals" onRetry={list.reload} />
      ) : items.length === 0 && !list.failed ? (
        <EmptyState
          icon={<CheckmarkCircleRegular />}
          title={status === "pending" ? "Nothing is waiting for approval" : "No approvals match"}
          body="Calls to tools whose policy rule requires approval wait here for an admin. Requests expire after 10 minutes."
        />
      ) : (
        <Table size="small" aria-label="Approvals">
          <TableHeader>
            <TableRow>
              <TableHeaderCell>Tool</TableHeaderCell>
              <TableHeaderCell>Agent</TableHeaderCell>
              <TableHeaderCell>Status</TableHeaderCell>
              <TableHeaderCell>Arguments (redacted)</TableHeaderCell>
              <TableHeaderCell>Requested</TableHeaderCell>
              <TableHeaderCell>Expires</TableHeaderCell>
              <TableHeaderCell aria-label="Actions" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.map((a) => (
              <TableRow key={a.id}>
                <TableCell>
                  <strong>{a.summary.tool ?? a.toolId}</strong>
                  {a.summary.operation && <Caption1 className={c.muted}> · {String(a.summary.operation)}</Caption1>}
                </TableCell>
                <TableCell>{a.agentId}</TableCell>
                <TableCell>
                  <Badge appearance="tint" color={COLOR[a.status] ?? "informative"}>
                    {a.status}
                  </Badge>
                </TableCell>
                <TableCell style={{ maxWidth: 320 }}>
                  <JsonBlock value={a.summary.arguments ?? {}} label={`Arguments for ${a.summary.tool ?? a.toolId}`} />
                </TableCell>
                <TableCell>{fmtTime(a.createdAt)}</TableCell>
                <TableCell>{fmtTime(a.expiresAt)}</TableCell>
                <TableCell>
                  {a.status === "pending" && (
                    <span style={{ display: "flex", gap: 4 }}>
                      <Button size="small" aria-label={`Approve ${a.summary.tool ?? a.toolId} for ${a.agentId}`} onClick={() => setConfirm({ approval: a, action: "approve" })}>
                        Approve
                      </Button>
                      <Button size="small" aria-label={`Deny ${a.summary.tool ?? a.toolId} for ${a.agentId}`} onClick={() => setConfirm({ approval: a, action: "deny" })}>
                        Deny
                      </Button>
                    </span>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <ConfirmDialog
        open={confirm !== null}
        title={confirm?.action === "approve" ? `Approve ${target} for ${confirm?.approval.agentId}?` : `Deny ${target} for ${confirm?.approval.agentId}?`}
        body={
          confirm?.action === "approve"
            ? "The call runs immediately, once, after policy and the tool's schema are re-checked against current state."
            : "The call is dropped and never runs. The agent sees it as denied."
        }
        confirmLabel={confirm?.action === "approve" ? "Approve and run" : "Deny"}
        pendingLabel={confirm?.action === "approve" ? "Approving…" : "Denying…"}
        pending={busy}
        onConfirm={() => void decide()}
        onCancel={() => setConfirm(null)}
      />
    </>
  );
}
