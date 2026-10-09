import { useState } from "react";
import {
  Button,
  Caption1,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  Tooltip,
} from "@fluentui/react-components";
import { AddRegular, ArrowSyncRegular, ServerRegular } from "@fluentui/react-icons";
import { listServers, refreshServer, setServerEnabled } from "../api/client";
import type { MCPServer } from "../api/types";
import { ConfirmDialog, EmptyState, ErrorState, fmtInt, fmtTime, LoadingRow, PageHeader, StatusBadge, useCommonStyles } from "../components/common";
import { useNotify } from "../components/Notifications";
import { useLoader } from "../hooks/useLoader";
import { RegisterServerDialog } from "./RegisterServerDialog";

function endpointLabel(s: MCPServer): string {
  if (s.transport === "stdio") return (s.stdioCommand ?? []).join(" ") || "—";
  return s.endpoint ?? "—";
}

export function ServersPage() {
  const c = useCommonStyles();
  const notify = useNotify();
  const servers = useLoader("Load servers", (sig) => listServers(sig), []);
  const [registerOpen, setRegisterOpen] = useState(false);
  const [refreshing, setRefreshing] = useState<Set<string>>(new Set());
  const [toggling, setToggling] = useState<string | null>(null);
  const [confirmDisable, setConfirmDisable] = useState<MCPServer | null>(null);

  const doRefresh = async (srv: MCPServer) => {
    setRefreshing((r) => new Set(r).add(srv.id));
    try {
      const updated = await refreshServer(srv.id);
      const n = updated?.toolCount;
      notify.success(`Refreshed “${srv.name}”${n != null ? ` — ${fmtInt(n)} tools` : ""}`);
      servers.refresh();
    } catch (e) {
      notify.error(`Refresh server “${srv.name}”`, e);
    } finally {
      setRefreshing((r) => {
        const next = new Set(r);
        next.delete(srv.id);
        return next;
      });
    }
  };

  const doToggle = async (srv: MCPServer, enabled: boolean) => {
    setToggling(srv.id);
    try {
      await setServerEnabled(srv.id, enabled);
      notify.success(`${enabled ? "Enabled" : "Disabled"} server “${srv.name}”`);
      servers.refresh();
    } catch (e) {
      notify.error(`${enabled ? "Enable" : "Disable"} server “${srv.name}”`, e);
    } finally {
      setToggling(null);
      setConfirmDisable(null);
    }
  };

  const list = servers.data ?? [];
  return (
    <>
      <PageHeader
        title="Servers"
        meta={servers.data && <Caption1 className={c.muted}>{fmtInt(list.length)} registered</Caption1>}
        actions={
          <Button appearance="primary" icon={<AddRegular />} onClick={() => setRegisterOpen(true)}>
            Register server
          </Button>
        }
      />
      {servers.loading && !servers.data ? (
        <LoadingRow label="Loading servers…" />
      ) : servers.failed && !servers.data ? (
        <ErrorState what="Servers" onRetry={servers.reload} />
      ) : list.length === 0 && !servers.failed ? (
        <EmptyState
          icon={<ServerRegular />}
          title="No MCP servers registered yet"
          body="Register a stdio command or a Streamable HTTP endpoint. The router calls tools/list on it and catalogs every tool it finds."
          action={<Button onClick={() => setRegisterOpen(true)}>Register server</Button>}
        />
      ) : (
        <Table size="small" aria-label="Servers">
          <TableHeader>
            <TableRow>
              <TableHeaderCell>Name</TableHeaderCell>
              <TableHeaderCell>Transport</TableHeaderCell>
              <TableHeaderCell>Endpoint</TableHeaderCell>
              <TableHeaderCell>Status</TableHeaderCell>
              <TableHeaderCell className={c.num}>Tools</TableHeaderCell>
              <TableHeaderCell>Last discovered</TableHeaderCell>
              <TableHeaderCell>Enabled</TableHeaderCell>
              <TableHeaderCell aria-label="Actions" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {list.map((srv) => {
              const busy = refreshing.has(srv.id);
              return (
                <TableRow key={srv.id}>
                  <TableCell>
                    <strong>{srv.name}</strong>
                    {srv.version && <Caption1 className={c.muted}> v{srv.version}</Caption1>}
                  </TableCell>
                  <TableCell>{srv.transport}</TableCell>
                  <TableCell>
                    <Tooltip content={endpointLabel(srv)} relationship="description">
                      <span className={c.mono} style={{ display: "inline-block", maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", verticalAlign: "bottom" }}>
                        {endpointLabel(srv)}
                      </span>
                    </Tooltip>
                  </TableCell>
                  <TableCell>
                    <StatusBadge status={srv.status} />
                  </TableCell>
                  <TableCell className={c.num}>{fmtInt(srv.toolCount)}</TableCell>
                  <TableCell>{fmtTime(srv.lastDiscoveredAt)}</TableCell>
                  <TableCell>
                    <Switch
                      aria-label={`${srv.enabled ? "Disable" : "Enable"} ${srv.name}`}
                      checked={srv.enabled}
                      disabled={toggling === srv.id}
                      onChange={(_, d) => (d.checked ? void doToggle(srv, true) : setConfirmDisable(srv))}
                    />
                  </TableCell>
                  <TableCell>
                    <Button size="small" icon={<ArrowSyncRegular />} disabled={busy} onClick={() => void doRefresh(srv)}>
                      {busy ? "Refreshing…" : "Refresh"}
                    </Button>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      )}
      <RegisterServerDialog open={registerOpen} onClose={() => setRegisterOpen(false)} onRegistered={() => servers.refresh()} />
      <ConfirmDialog
        open={confirmDisable !== null}
        title={`Disable server “${confirmDisable?.name ?? ""}”?`}
        body={
          <>
            Its {confirmDisable?.toolCount != null ? `${fmtInt(confirmDisable.toolCount)} ` : ""}tools will stop being routed or exposed to
            agents until you re-enable it. Nothing is deleted; the catalog and history are kept.
          </>
        }
        confirmLabel="Disable server"
        pendingLabel="Disabling…"
        pending={toggling !== null}
        onConfirm={() => confirmDisable && void doToggle(confirmDisable, false)}
        onCancel={() => setConfirmDisable(null)}
      />
    </>
  );
}
