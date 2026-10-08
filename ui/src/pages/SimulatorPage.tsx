import { useState } from "react";
import {
  Button,
  Caption1,
  Field,
  Input,
  makeStyles,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  ProgressBar,
  SpinButton,
  Subtitle2,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  Textarea,
  tokens,
} from "@fluentui/react-components";
import { simulateRoute } from "../api/client";
import type { RouteResponse } from "../api/types";
import { fmtMs, fmtScore, PageHeader, useCommonStyles } from "../components/common";
import { useNotify } from "../components/Notifications";

const useStyles = makeStyles({
  layout: { display: "grid", gridTemplateColumns: "minmax(280px, 380px) 1fr", gap: tokens.spacingHorizontalXL, alignItems: "start" },
  form: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalM },
  result: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalS, minWidth: 0 },
  readout: { display: "flex", gap: tokens.spacingHorizontalL, flexWrap: "wrap", fontVariantNumeric: "tabular-nums" },
  bar: { display: "flex", alignItems: "center", gap: tokens.spacingHorizontalS, minWidth: "180px" },
  impact: { margin: 0, paddingLeft: tokens.spacingHorizontalL },
});

const MAX_TOOLS_LIMIT = 20;

/** Catalog impact: which servers the routed tools came from, most-used first. */
export function serverImpact(res: RouteResponse): [string, number][] {
  const counts = new Map<string, number>();
  for (const t of res.tools) counts.set(t.serverName, (counts.get(t.serverName) ?? 0) + 1);
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

export function RouteResultView({ result }: { result: RouteResponse }) {
  const s = useStyles();
  const c = useCommonStyles();
  const noMatch = result.noMatch === true || result.tools.length === 0;
  const ranked = [...result.tools].sort((a, b) => b.score - a.score);
  const impact = serverImpact(result);
  return (
    <section className={s.result} aria-label="Routing result">
      <div className={s.readout}>
        <Caption1>
          Latency <strong>{fmtMs(result.latencyMs)}</strong>
        </Caption1>
        {result.modelVersion && <Caption1>Model {result.modelVersion}</Caption1>}
        <Caption1 className={c.mono}>request {result.requestId}</Caption1>
      </div>
      {result.fallbackUsed && (
        <MessageBar intent="warning" data-testid="fallback-banner">
          <MessageBarBody>
            <MessageBarTitle>Deterministic fallback used</MessageBarTitle>
            The decision model was unavailable or not confident, so these scores are heuristic, not calibrated probabilities.
          </MessageBarBody>
        </MessageBar>
      )}
      {noMatch ? (
        <MessageBar intent="info" data-testid="no-match-banner">
          <MessageBarBody>
            <MessageBarTitle>No match</MessageBarTitle>
            No eligible tool is relevant enough for this query, so the agent would receive an empty tool list. Check the
            agent's policy rules, or whether a server that covers this task is registered and enabled.
          </MessageBarBody>
        </MessageBar>
      ) : (
        <>
          <Table size="small" aria-label="Ranked tools">
            <TableHeader>
              <TableRow>
                <TableHeaderCell className={c.num} style={{ width: 40 }}>
                  #
                </TableHeaderCell>
                <TableHeaderCell>Tool</TableHeaderCell>
                <TableHeaderCell>Server</TableHeaderCell>
                <TableHeaderCell>Score</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {ranked.map((t, i) => (
                <TableRow key={`${t.serverName}/${t.toolName}`}>
                  <TableCell className={c.num}>{i + 1}</TableCell>
                  <TableCell>
                    <strong>{t.toolName}</strong>
                  </TableCell>
                  <TableCell>{t.serverName}</TableCell>
                  <TableCell>
                    <div className={s.bar}>
                      <ProgressBar value={Math.max(0, Math.min(1, t.score))} max={1} thickness="large" style={{ flex: 1 }} aria-label={`${t.toolName} score`} />
                      <span className={c.num} style={{ width: 48 }}>
                        {fmtScore(t.score)}
                      </span>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <div aria-label="Catalog impact">
            <Subtitle2 as="h2">Catalog impact</Subtitle2>
            <Caption1 as="p" style={{ margin: "4px 0" }}>
              Exposing these tools would involve {impact.length} server{impact.length === 1 ? "" : "s"}:
            </Caption1>
            <ul className={s.impact}>
              {impact.map(([server, n]) => (
                <li key={server}>
                  <Caption1>
                    {server} — {n} tool{n === 1 ? "" : "s"}
                  </Caption1>
                </li>
              ))}
            </ul>
          </div>
        </>
      )}
    </section>
  );
}

export function SimulatorPage() {
  const s = useStyles();
  const notify = useNotify();
  const [query, setQuery] = useState("");
  const [agentId, setAgentId] = useState("");
  const [maxTools, setMaxTools] = useState(5);
  const [errors, setErrors] = useState<{ query?: string; agentId?: string }>({});
  const [pending, setPending] = useState(false);
  const [result, setResult] = useState<RouteResponse | null>(null);

  const submit = async () => {
    const e: typeof errors = {};
    if (!query.trim()) e.query = "Describe the task the agent is trying to do.";
    if (!agentId.trim()) e.agentId = "Enter the agent id whose policy should apply.";
    setErrors(e);
    if (Object.keys(e).length) return;
    setPending(true);
    try {
      setResult(await simulateRoute({ query: query.trim(), agentId: agentId.trim(), maxTools }));
    } catch (err) {
      notify.error("Simulate route", err);
    } finally {
      setPending(false);
    }
  };

  return (
    <>
      <PageHeader title="Routing simulator" />
      <div className={s.layout}>
        <form
          noValidate
          className={s.form}
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <Field label="Task query" required validationMessage={errors.query}>
            <Textarea value={query} onChange={(_, d) => setQuery(d.value)} rows={4} resize="vertical" placeholder="List open pull requests on the api repo" />
          </Field>
          <Field label="Agent id" required validationMessage={errors.agentId} hint="Results are filtered by this agent's policy rules.">
            <Input value={agentId} onChange={(_, d) => setAgentId(d.value)} />
          </Field>
          <Field label="Max tools">
            <SpinButton
              value={maxTools}
              min={1}
              max={MAX_TOOLS_LIMIT}
              onChange={(_, d) => {
                const v = d.value ?? Number.parseInt(d.displayValue ?? "", 10);
                if (Number.isFinite(v)) setMaxTools(Math.min(MAX_TOOLS_LIMIT, Math.max(1, Number(v))));
              }}
            />
          </Field>
          <div>
            <Button appearance="primary" type="submit" disabled={pending}>
              {pending ? "Routing…" : "Simulate route"}
            </Button>
          </div>
        </form>
        <div>{result ? <RouteResultView result={result} /> : <Caption1>Run a query to see which tools the router would expose and why.</Caption1>}</div>
      </div>
    </>
  );
}
