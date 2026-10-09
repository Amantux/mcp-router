import { useEffect, useState } from "react";
import { Link } from "react-router";
import {
  Button,
  Checkbox,
  Field,
  Input,
  Radio,
  RadioGroup,
  Textarea,
  Tab,
  TabList,
  Text,
} from "@fluentui/react-components";
import {
  ApiError,
  completeSetup,
  describeError,
  createPrincipal,
  createRule,
  createSkillSource,
  getModelsHealth,
  getSetupStatus,
  importServers,
  listServers,
  listSkillSources,
  type SetupStatus,
} from "../../api/client";
import {
  CLIENTS,
  type Client,
  isHttpsGitUrl,
  snippetFor,
  suggestToken,
} from "./snippets";
import { STEP_KEY, skipSetup } from "./redirect";
import { RegisterServerDialog } from "../RegisterServerDialog";
import type { CreateRuleRequest } from "../../api/types";

type Target = { kind: "tool" | "skill"; id: string; name: string };

/** Starter policy: one read-ceiling rule per selected server (tool) or skill source (skill). */
export function starterRules(agentId: string, targets: Target[]): CreateRuleRequest[] {
  return targets.map((t) => ({
    agentId,
    resourceKind: t.kind,
    serverId: t.id,
    toolName: null,
    maxOperation: "read",
    requiresApproval: false,
  }));
}

/** A backend-valid source name (^[A-Za-z0-9][A-Za-z0-9._-]*$) from a path's last segment. */
export function sourceName(loc: string): string {
  const seg = loc.split(/[\\/]/).filter(Boolean).pop() ?? "";
  const clean = seg.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[^A-Za-z0-9]+/, "");
  return clean.slice(0, 64) || "skills";
}

/** Directory skill sources must be absolute paths on the router host. */
export function isAbsolutePath(p: string): boolean {
  return p.startsWith("/") || /^[A-Za-z]:[\\/]/.test(p);
}

export const STEPS = [
  "Admin token",
  "Decision backend",
  "MCP servers",
  "Skill sources",
  "Agent identity",
  "Connect",
  "Verify",
];

function loadStep(): number {
  try {
    const n = Number(sessionStorage.getItem(STEP_KEY));
    return Number.isInteger(n) && n >= 0 && n < STEPS.length ? n : 0;
  } catch {
    return 0;
  }
}

/** D11: the backend's 409 detail (verbatim, for matching) for a first principal in dev mode without MCPR_ADMIN_TOKEN. */
export const FIRST_PRINCIPAL_NEEDS_ADMIN_TOKEN = "Set MCPR_ADMIN_TOKEN before creating the first principal; creating one ends dev mode.";
/** What the wizard shows for it: the same advice in the UI's word, "agent" (HS-U-006). */
export const FIRST_AGENT_NEEDS_ADMIN_TOKEN = "Set MCPR_ADMIN_TOKEN before creating the first agent; creating one ends dev mode.";

/** Curated wizard message for a failed call: never the raw `HTTP 409 from /api/…`. */
export function describeFailure(what: string, e: unknown): string {
  const { status, advice } = describeError(e);
  return `${what} failed${status ? ` (${status})` : ""}. ${advice}`;
}

const AGENT_EXISTS = "agentId already exists"; // routes_policy.py create_principal's other 409

/**
 * Why Create agent failed. A 409 is one of two backend refusals: the curated detail
 * says which; without it, the setup status decides; with neither, both are named
 * rather than guessing (HS-U-019).
 */
export function createAgentFailure(agentId: string, e: unknown, status: SetupStatus | null): string {
  if (!(e instanceof ApiError) || e.status !== 409) return describeFailure("Create agent", e);
  const exists = `An agent “${agentId}” already exists. Choose another id.`;
  if (e.conflict === FIRST_PRINCIPAL_NEEDS_ADMIN_TOKEN) return FIRST_AGENT_NEEDS_ADMIN_TOKEN;
  if (e.conflict === AGENT_EXISTS) return exists;
  if (e.conflict) return describeFailure("Create agent", e);
  if (status) return status.hasAdminToken ? exists : FIRST_AGENT_NEEDS_ADMIN_TOKEN;
  return `Create agent failed (HTTP 409). Either an agent “${agentId}” already exists (choose another id), or this is dev mode: ${FIRST_AGENT_NEEDS_ADMIN_TOKEN}`;
}

export function SetupPage() {
  const [step, setStepRaw] = useState(loadStep);
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [msg, setMsg] = useState("");
  const [token] = useState(suggestToken);
  const [serversJson, setServersJson] = useState("");
  const [gitUrl, setGitUrl] = useState("");
  const [agentId, setAgentId] = useState("my-agent");
  const [apiKey, setApiKey] = useState<string | null>(null); // memory only: shown once
  const [client, setClient] = useState<Client>("Claude Code");
  const [health, setHealth] = useState("");
  const [addOpen, setAddOpen] = useState(false);
  const [dirPath, setDirPath] = useState("");
  const [policy, setPolicy] = useState<"readonly" | "later">("readonly");
  const [targets, setTargets] = useState<Target[] | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [ruleCount, setRuleCount] = useState<number | null>(null);
  const [rulesBusy, setRulesBusy] = useState(false);
  const [createdAgent, setCreatedAgent] = useState<string | null>(null); // server-confirmed id
  const url = `${window.location.origin}/mcp`;
  // A message belongs to the step that produced it.
  const setStep = (n: number) => {
    setStepRaw(n);
    setMsg("");
  };
  const copy = (text: string) =>
    navigator.clipboard
      .writeText(text)
      .then(() => setMsg("Copied."))
      .catch(() => setMsg("Couldn't copy to the clipboard here. Select the text and copy it manually."));

  const recheck = () =>
    getSetupStatus().then(setStatus, () =>
      setMsg("Status needs the admin token. Paste it in Connect (bottom of the left menu)."),
    );
  useEffect(() => {
    void recheck();
  }, []);
  useEffect(() => {
    try {
      sessionStorage.setItem(STEP_KEY, String(step));
    } catch {
      /* storage unavailable: progress just isn't remembered */
    }
  }, [step]);

  useEffect(() => {
    if (apiKey === null) return;
    void Promise.all([listServers(), listSkillSources()]).then(
      ([servers, sources]) =>
        setTargets([
          ...servers.map((x) => ({ kind: "tool" as const, id: x.id, name: x.name })),
          ...sources.map((x) => ({ kind: "skill" as const, id: x.id, name: x.name })),
        ]),
      () => setMsg("Could not load servers and skill sources."),
    );
  }, [apiKey]);

  const run = (what: string, p: Promise<unknown>, ok: string) =>
    p.then(
      () => setMsg(ok),
      (e: unknown) => setMsg(describeFailure(what, e)),
    );

  const body = [
    <div key="a">
      <Text block>
        MCPR_ADMIN_TOKEN must be set in the server environment; it is never
        stored in the database.
      </Text>
      <pre>{`MCPR_ADMIN_TOKEN=${token}`}</pre>
      <Button onClick={() => void copy(token)}>
        Copy suggestion
      </Button>{" "}
      <Button onClick={() => void recheck()}>I've set it, re-check</Button>
      <Text block>
        Admin token:{" "}
        {status ? (status.hasAdminToken ? "configured" : "not configured") : "unknown (status not loaded)"}
      </Text>
    </div>,
    <div key="b">
      <Text block>
        Current backend: {status?.backend ?? "unknown"}. Change it in env, then
        restart:
      </Text>
      <pre>
        {
          "MCPR_DECISION_BACKEND=laya|deterministic|remote|aoai\n# remote: MCPR_DECISION_ENDPOINT, MCPR_DECISION_API_KEY_FILE\n# aoai: MCPR_AOAI_* (see docs/backends.md)"
        }
      </pre>
    </div>,
    <div key="c">
      <Text block>
        Paste a Claude Desktop config (the object with "mcpServers"):
      </Text>
      <Field label="mcpServers JSON">
        <Textarea
          value={serversJson}
          onChange={(_, d) => setServersJson(d.value)}
        />
      </Field>
      <Button
        onClick={() => {
          try {
            void run(
              "Import servers",
              importServers(JSON.parse(serversJson)),
              "Servers imported.",
            );
          } catch {
            setMsg("That is not valid JSON.");
          }
        }}
      >
        Import servers
      </Button>{" "}
      <Button onClick={() => setAddOpen(true)}>Add one server</Button>
      <RegisterServerDialog
        open={addOpen}
        httpsOnly
        onClose={() => setAddOpen(false)}
        onRegistered={(srv) => setMsg(`Server ${srv.name} registered.`)}
      />
    </div>,
    <div key="d">
      <Field label="Git URL" hint="https:// only">
        <Input
          placeholder="https://github.com/org/skills.git"
          value={gitUrl}
          onChange={(_, d) => setGitUrl(d.value)}
        />
      </Field>
      <Button
        onClick={() => {
          if (!isHttpsGitUrl(gitUrl))
            return setMsg("Only https:// git URLs are accepted.");
          void run(
            "Add skill source",
            createSkillSource({
              name: new URL(gitUrl).pathname.split("/").pop() || "skills",
              kind: "git",
              location: gitUrl,
            }),
            "Skill source added.",
          );
        }}
      >
        Add skill source
      </Button>
      <Field label="Directory path" hint="An absolute path on the router host">
        <Input
          placeholder="/srv/skills"
          value={dirPath}
          onChange={(_, d) => setDirPath(d.value)}
        />
      </Field>
      <Button
        onClick={() => {
          const loc = dirPath.trim();
          if (!isAbsolutePath(loc))
            return setMsg("Enter an absolute directory path, e.g. /srv/skills.");
          void run(
            "Add directory source",
            createSkillSource({
              name: sourceName(loc),
              kind: "directory",
              location: loc,
            }),
            "Directory source added.",
          );
        }}
      >
        Add directory source
      </Button>
    </div>,
    <div key="e">
      <Field label="Agent id">
        <Input
          disabled={apiKey !== null}
          value={agentId}
          onChange={(_, d) => setAgentId(d.value)}
        />
      </Field>
      <Button
        disabled={apiKey !== null}
        onClick={() =>
          void createPrincipal({ agentId, maxTools: 8 }).then(
            (p) => {
              setCreatedAgent(p.agentId);
              setApiKey(p.apiKey);
            },
            (e: unknown) => setMsg(createAgentFailure(agentId, e, status)),
          )
        }
      >
        Create agent
      </Button>
      {apiKey && (
        <div role="alert">
          <Text block weight="semibold">
            Copy this key now. It is shown once and cannot be retrieved later.
          </Text>
          <pre data-testid="api-key">{apiKey}</pre>
          <Button onClick={() => void copy(apiKey)}>
            Copy key
          </Button>
        </div>
      )}
      {apiKey !== null && ruleCount === null && (
        <div aria-label="Starter policy">
          <RadioGroup
            value={policy}
            onChange={(_, d) => setPolicy(d.value as "readonly" | "later")}
          >
            <Radio value="readonly" label="Read-only on selected servers/sources" />
            <Radio value="later" label="I'll configure rules later" />
          </RadioGroup>
          {policy === "readonly" &&
            (targets ?? []).map((t) => {
              const k = `${t.kind}:${t.id}`;
              return (
                <Checkbox
                  key={k}
                  label={`${t.kind === "tool" ? "Server" : "Skill source"}: ${t.name}`}
                  checked={picked.has(k)}
                  onChange={(_, d) => {
                    const next = new Set(picked);
                    if (d.checked) next.add(k);
                    else next.delete(k);
                    setPicked(next);
                  }}
                />
              );
            })}
          {policy === "readonly" && (
            <Button
              disabled={picked.size === 0 || rulesBusy || createdAgent === null}
              onClick={() => {
                const sel = (targets ?? []).filter((t) => picked.has(`${t.kind}:${t.id}`));
                if (createdAgent === null) return;
                let made = 0; // sequential, so a partial failure can say how many landed
                const landed = new Set<string>();
                setRulesBusy(true);
                void starterRules(createdAgent, sel)
                  .reduce<Promise<void>>(
                    (acc, r, i) =>
                      acc.then(() =>
                        createRule(r).then(() => {
                          made++;
                          landed.add(`${sel[i].kind}:${sel[i].id}`);
                        }),
                      ),
                    Promise.resolve(),
                  )
                  .then(
                    () => setRuleCount(made),
                    () => {
                      // Unpick what landed so a retry cannot duplicate it.
                      setPicked(new Set([...picked].filter((k) => !landed.has(k))));
                      setMsg(`Created ${made} of ${sel.length} rules; retry or add the rest on the Policy page.`);
                    },
                  )
                  .finally(() => setRulesBusy(false));
              }}
            >
              Create rules
            </Button>
          )}
        </div>
      )}
      {ruleCount !== null && <Text block>Created {ruleCount} rule(s).</Text>}
    </div>,
    <div key="f">
      <TabList
        selectedValue={client}
        onTabSelect={(_, d) => setClient(d.value as Client)}
      >
        {CLIENTS.map((c) => (
          <Tab key={c} value={c}>
            {c}
          </Tab>
        ))}
      </TabList>
      <pre data-testid="snippet">
        {snippetFor(client, url, apiKey ?? "<agent-api-key>")}
      </pre>
    </div>,
    <div key="g">
      <Button
        onClick={() =>
          void Promise.all([getModelsHealth(), getSetupStatus()]).then(
            ([h, s]) => {
              setStatus(s);
              const notLoaded = h.models.filter((m) => !m.loaded).map((m) => m.kind);
              setHealth(
                `${notLoaded.length ? `models not loaded: ${notLoaded.join(", ")}` : "models: loaded"}; servers ${s.counts.servers}, tools ${s.counts.tools}`,
              );
            },
            () => setHealth("Health check failed."),
          )
        }
      >
        Run checks
      </Button>
      <Text block>{health}</Text>
      <Link to="/lens">Open Lens</Link>{" "}
      <Button
        appearance="primary"
        onClick={() => void run("Finish setup", completeSetup(), "Setup complete.")}
      >
        Finish setup
      </Button>
    </div>,
  ];

  return (
    <section aria-label="Setup wizard">
      <Link to="/" onClick={skipSetup}>
        Skip setup
      </Link>{" "}
      <Text size={200}>(reopen it any time from “Setup wizard” in the left menu)</Text>
      <TabList
        selectedValue={step}
        onTabSelect={(_, d) => setStep(d.value as number)}
      >
        {STEPS.map((s, i) => (
          <Tab key={s} value={i}>{`${i + 1}. ${s}`}</Tab>
        ))}
      </TabList>
      {body[step]}
      <div>
        <Button disabled={step === 0} onClick={() => setStep(step - 1)}>
          Back
        </Button>{" "}
        <Button
          disabled={step === STEPS.length - 1}
          onClick={() => setStep(step + 1)}
        >
          Next
        </Button>
      </div>
      {msg && (
        <Text block role="status">
          {msg}
        </Text>
      )}
    </section>
  );
}
