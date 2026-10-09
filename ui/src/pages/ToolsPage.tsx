import { useEffect, useMemo, useState, type KeyboardEvent } from "react";
import {
  Badge,
  Button,
  Caption1,
  createTableColumn,
  DataGrid,
  DataGridBody,
  DataGridCell,
  DataGridHeader,
  DataGridHeaderCell,
  DataGridRow,
  Field,
  SearchBox,
  Select,
  type TableColumnDefinition,
} from "@fluentui/react-components";
import { WrenchRegular, FilterDismissRegular } from "@fluentui/react-icons";
import { useNavigate } from "react-router";
import { listServers, listToolFunnels, listTools } from "../api/client";
import { DOMAINS, OPERATIONS, type MCPTool, type Operation, type ToolFunnel } from "../api/types";
import { FunnelBars } from "../components/analytics";
import { EmptyState, ErrorState, fmtInt, fmtMs, LoadingRow, OperationBadge, PageHeader, Pager, useCommonStyles } from "../components/common";
import { useDebounced } from "../hooks/useDebounced";
import { useLoader } from "../hooks/useLoader";
import { ToolDetailDrawer } from "./ToolDetailDrawer";

const PAGE_SIZE = 50;
type TriState = "" | "true" | "false";

interface Filters {
  q: string;
  domain: string;
  operation: "" | Operation;
  serverId: string;
  enabled: TriState;
  available: TriState;
}
const EMPTY: Filters = { q: "", domain: "", operation: "", serverId: "", enabled: "", available: "" };
const tri = (v: TriState) => (v === "" ? undefined : v === "true");
const NUMERIC_COLS = new Set(["version", "calls", "errors", "latency"]);
const ellipsis = { maxWidth: 360, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } as const;

export function ToolsPage() {
  const c = useCommonStyles();
  const navigate = useNavigate();
  const [filters, setFilters] = useState<Filters>(EMPTY);
  const [offset, setOffset] = useState(0);
  const [openId, setOpenId] = useState<string | null>(null);
  const q = useDebounced(filters.q, 200);

  // Funnel column: best-effort (an instance without analytics just shows "—"), so no error toast.
  const [funnels, setFunnels] = useState<Map<string, ToolFunnel>>(new Map());
  useEffect(() => {
    const ctrl = new AbortController();
    listToolFunnels({ window: "7d", limit: 500, offset: 0 }, ctrl.signal)
      .then((p) => setFunnels(new Map(p.items.map((f) => [f.toolId, f]))))
      .catch(() => {});
    return () => ctrl.abort();
  }, []);

  const servers = useLoader("Load servers", (sig) => listServers(sig), []);
  const serverNames = useMemo(() => new Map((servers.data ?? []).map((s) => [s.id, s.name])), [servers.data]);

  const tools = useLoader(
    "Load tools",
    (sig) =>
      listTools(
        {
          q: q.trim() || undefined,
          domain: filters.domain || undefined,
          operation: filters.operation || undefined,
          serverId: filters.serverId || undefined,
          enabled: tri(filters.enabled),
          available: tri(filters.available),
          limit: PAGE_SIZE,
          offset,
        },
        sig,
      ),
    [q, filters.domain, filters.operation, filters.serverId, filters.enabled, filters.available, offset],
  );

  const set = <K extends keyof Filters>(k: K, v: Filters[K]) => {
    setFilters((f) => ({ ...f, [k]: v }));
    setOffset(0);
  };
  const clear = () => {
    setFilters(EMPTY);
    setOffset(0);
  };
  const filtered = Object.values(filters).some((v) => v !== "");

  const columns = useMemo<TableColumnDefinition<MCPTool>[]>(
    () => [
      createTableColumn<MCPTool>({
        columnId: "name",
        renderHeaderCell: () => "Tool",
        renderCell: (t) => (
          <div style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
            <strong>{t.name}</strong>
            <Caption1 className={c.muted} style={ellipsis} title={t.description}>
              {t.description}
            </Caption1>
          </div>
        ),
      }),
      createTableColumn<MCPTool>({
        columnId: "server",
        renderHeaderCell: () => "Server",
        renderCell: (t) => t.serverName ?? serverNames.get(t.serverId) ?? t.serverId.slice(0, 8),
      }),
      createTableColumn<MCPTool>({
        columnId: "domain",
        renderHeaderCell: () => "Domain",
        renderCell: (t) => t.domain ?? <span className={c.muted}>—</span>,
      }),
      createTableColumn<MCPTool>({
        columnId: "operation",
        renderHeaderCell: () => "Operation",
        renderCell: (t) => <OperationBadge op={t.operation} />,
      }),
      createTableColumn<MCPTool>({
        columnId: "state",
        renderHeaderCell: () => "State",
        renderCell: (t) => (
          <span style={{ display: "flex", gap: 4 }}>
            {!t.enabled && (
              <Badge size="small" appearance="tint" color="danger">
                disabled
              </Badge>
            )}
            {t.available === false && (
              <Badge size="small" appearance="tint" color="warning">
                unavailable
              </Badge>
            )}
            {t.enabled && t.available !== false && (
              <Badge size="small" appearance="tint" color="success">
                enabled
              </Badge>
            )}
          </span>
        ),
      }),
      createTableColumn<MCPTool>({ columnId: "version", renderHeaderCell: () => "Version", renderCell: (t) => t.version }),
      createTableColumn<MCPTool>({ columnId: "calls", renderHeaderCell: () => "Calls", renderCell: (t) => fmtInt(t.callCount) }),
      createTableColumn<MCPTool>({ columnId: "errors", renderHeaderCell: () => "Errors", renderCell: (t) => fmtInt(t.errorCount) }),
      createTableColumn<MCPTool>({ columnId: "latency", renderHeaderCell: () => "Average latency", renderCell: (t) => fmtMs(t.avgLatencyMs) }),
      createTableColumn<MCPTool>({
        columnId: "funnel",
        renderHeaderCell: () => "Funnel (7d)",
        renderCell: (t) => {
          const f = funnels.get(t.id);
          return f && f.surfaced > 0 ? <FunnelBars label={`${t.name} funnel`} surfaced={f.surfaced} selected={f.selected} succeeded={f.succeeded} /> : <span className={c.muted}>—</span>;
        },
      }),
    ],
    [c, serverNames, funnels],
  );

  const page = tools.data;
  const items = page?.items ?? [];

  return (
    <>
      <PageHeader
        title="Tools"
        meta={page && <Caption1 className={c.muted}>{fmtInt(page.total)} {filtered ? "matching" : "catalogued"}</Caption1>}
      />
      <div className={c.toolbar} role="search">
        <Field label="Search">
          <SearchBox value={filters.q} onChange={(_, d) => set("q", d.value)} placeholder="Name, description, tag" style={{ minWidth: 240 }} />
        </Field>
        <Field label="Domain">
          <Select value={filters.domain} onChange={(_, d) => set("domain", d.value)}>
            <option value="">Any</option>
            {DOMAINS.map((d) => (
              <option key={d}>{d}</option>
            ))}
          </Select>
        </Field>
        <Field label="Operation">
          <Select value={filters.operation} onChange={(_, d) => set("operation", d.value as Filters["operation"])}>
            <option value="">Any</option>
            {OPERATIONS.map((o) => (
              <option key={o}>{o}</option>
            ))}
          </Select>
        </Field>
        <Field label="Server">
          <Select value={filters.serverId} onChange={(_, d) => set("serverId", d.value)}>
            <option value="">Any</option>
            {(servers.data ?? []).map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Enabled">
          <Select value={filters.enabled} onChange={(_, d) => set("enabled", d.value as TriState)}>
            <option value="">Any</option>
            <option value="true">Enabled</option>
            <option value="false">Disabled</option>
          </Select>
        </Field>
        <Field label="Availability">
          <Select value={filters.available} onChange={(_, d) => set("available", d.value as TriState)}>
            <option value="">Any</option>
            <option value="true">Available</option>
            <option value="false">Unavailable</option>
          </Select>
        </Field>
        {filtered && (
          <Button icon={<FilterDismissRegular />} onClick={clear}>
            Clear filters
          </Button>
        )}
      </div>

      {tools.loading && !page ? (
        <LoadingRow label="Loading tools…" />
      ) : tools.failed && !tools.data ? (
        <ErrorState what="Tools" onRetry={tools.reload} />
      ) : items.length === 0 && !tools.failed ? (
        filtered ? (
          <EmptyState
            icon={<FilterDismissRegular />}
            title="No tools match these filters"
            body="Try a broader search term or clear one of the filters."
            action={<Button onClick={clear}>Clear filters</Button>}
          />
        ) : (
          <EmptyState
            icon={<WrenchRegular />}
            title="The tool catalog is empty"
            body="Tools appear here once a registered server has been discovered. Register a server, or refresh an existing one to rediscover its tools."
            action={<Button onClick={() => navigate("/servers")}>Go to servers</Button>}
          />
        )
      ) : (
        <>
          <DataGrid items={items} columns={columns} getRowId={(t: MCPTool) => t.id} size="small" aria-label="Tools" focusMode="row_unstable">
            <DataGridHeader>
              <DataGridRow>
                {({ renderHeaderCell, columnId }) => (
                  <DataGridHeaderCell className={NUMERIC_COLS.has(String(columnId)) ? c.num : undefined}>{renderHeaderCell()}</DataGridHeaderCell>
                )}
              </DataGridRow>
            </DataGridHeader>
            <DataGridBody<MCPTool>>
              {({ item, rowId }) => (
                <DataGridRow<MCPTool>
                  key={rowId}
                  style={{ cursor: "pointer" }}
                  onClick={() => setOpenId(item.id)}
                  onKeyDown={(e: KeyboardEvent) => {
                    if (e.target !== e.currentTarget || (e.key !== "Enter" && e.key !== " ")) return;
                    e.preventDefault(); // Space would scroll the page
                    setOpenId(item.id);
                  }}
                >
                  {({ renderCell, columnId }) => (
                    <DataGridCell className={NUMERIC_COLS.has(String(columnId)) ? c.num : undefined}>{renderCell(item)}</DataGridCell>
                  )}
                </DataGridRow>
              )}
            </DataGridBody>
          </DataGrid>
          {page && <Pager offset={page.offset} limit={PAGE_SIZE} total={page.total} onChange={setOffset} />}
        </>
      )}
      <ToolDetailDrawer toolId={openId} onClose={() => setOpenId(null)} onChanged={() => tools.refresh()} />
    </>
  );
}
