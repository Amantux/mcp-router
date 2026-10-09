import { useState } from "react";
import {
  Badge,
  Button,
  Caption1,
  Field,
  Input,
  Select,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
} from "@fluentui/react-components";
import { FilterDismissRegular, HistoryRegular } from "@fluentui/react-icons";
import { Link as RouterLink } from "react-router";
import { listExecutions } from "../api/client";
import { FeedbackThumbs, kindOfId } from "../components/FeedbackThumbs";
import type { ExecutionOutcome } from "../api/types";
import { EmptyState, ErrorState, fmtInt, fmtMs, fmtTime, LoadingRow, OutcomeBadge, PageHeader, Pager, useCommonStyles } from "../components/common";
import { useDebounced } from "../hooks/useDebounced";
import { useLoader } from "../hooks/useLoader";

const PAGE_SIZE = 50;
const OUTCOMES: ExecutionOutcome[] = ["ok", "denied", "timeout", "rate_limited", "error"];

export function ExecutionsPage() {
  const c = useCommonStyles();
  const [agentId, setAgentId] = useState("");
  const [outcome, setOutcome] = useState<"" | ExecutionOutcome>("");
  const [offset, setOffset] = useState(0);
  const agent = useDebounced(agentId.trim(), 200);
  const execs = useLoader(
    "Load execution history",
    (sig) => listExecutions({ agentId: agent || undefined, outcome: outcome || undefined, limit: PAGE_SIZE, offset }, sig),
    [agent, outcome, offset],
  );
  const filtered = agentId.trim() !== "" || outcome !== "";
  const clear = () => {
    setAgentId("");
    setOutcome("");
    setOffset(0);
  };
  const page = execs.data;
  const items = page?.items ?? [];

  return (
    <>
      <PageHeader title="Execution history" meta={page && <Caption1 className={c.muted}>{fmtInt(page.total)} records</Caption1>} />
      <div className={c.toolbar} role="search">
        <Field label="Agent id">
          <Input
            value={agentId}
            onChange={(_, d) => {
              setAgentId(d.value);
              setOffset(0);
            }}
          />
        </Field>
        <Field label="Outcome">
          <Select
            value={outcome}
            onChange={(_, d) => {
              setOutcome(d.value as "" | ExecutionOutcome);
              setOffset(0);
            }}
          >
            <option value="">Any</option>
            {OUTCOMES.map((o) => (
              <option key={o} value={o}>
                {o.replace("_", " ")}
              </option>
            ))}
          </Select>
        </Field>
        {filtered && (
          <Button icon={<FilterDismissRegular />} onClick={clear}>
            Clear filters
          </Button>
        )}
      </div>
      {execs.loading && !page ? (
        <LoadingRow label="Loading executions…" />
      ) : execs.failed && !execs.data ? (
        <ErrorState what="Executions" onRetry={execs.reload} />
      ) : items.length === 0 && !execs.failed ? (
        filtered ? (
          <EmptyState icon={<FilterDismissRegular />} title="No executions match these filters" action={<Button onClick={clear}>Clear filters</Button>} />
        ) : (
          <EmptyState
            icon={<HistoryRegular />}
            title="No tool executions recorded yet"
            body="Every execution attempt through the gateway — allowed or refused — is audited here once agents start calling tools."
          />
        )
      ) : (
        <>
          <Table size="small" aria-label="Executions">
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Time</TableHeaderCell>
                <TableHeaderCell>Agent</TableHeaderCell>
                <TableHeaderCell>Tool</TableHeaderCell>
                <TableHeaderCell>Server</TableHeaderCell>
                <TableHeaderCell>Outcome</TableHeaderCell>
                <TableHeaderCell className={c.num}>Latency</TableHeaderCell>
                <TableHeaderCell>Detail</TableHeaderCell>
                <TableHeaderCell>Feedback</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((x) => (
                <TableRow key={x.id}>
                  <TableCell>{fmtTime(x.createdAt)}</TableCell>
                  <TableCell>{x.agentId}</TableCell>
                  <TableCell>{x.toolName ?? (x.toolId ? <span className={c.mono}>{x.toolId.slice(0, 8)}</span> : "—")}</TableCell>
                  <TableCell>{x.serverName ?? (x.serverId ? <span className={c.mono}>{x.serverId.slice(0, 8)}</span> : "—")}</TableCell>
                  <TableCell>
                    <OutcomeBadge outcome={x.outcome} />
                    {x.initiatedBy === "admin" && (
                      <Badge size="small" appearance="tint" color="important" title="Admin-initiated run, impersonating this agent">
                        admin
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell className={c.num}>{fmtMs(x.latencyMs)}</TableCell>
                  <TableCell>
                    <Caption1>{x.detail || "—"}</Caption1>
                  </TableCell>
                  <TableCell>
                    <FeedbackThumbs
                      routeRequestId={x.routeRequestId}
                      target={{ kind: x.toolId ? kindOfId(x.toolId) : "tool", id: x.toolId, name: x.toolName }}
                      label={x.toolName ?? x.toolId ?? "this execution"}
                    />
                    {x.routeRequestId && (
                      <RouterLink to={`/lens?routeRequestId=${encodeURIComponent(x.routeRequestId)}&agentId=${encodeURIComponent(x.agentId)}`}>
                        Open in lens
                      </RouterLink>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {page && <Pager offset={page.offset} limit={PAGE_SIZE} total={page.total} onChange={setOffset} />}
        </>
      )}
    </>
  );
}
