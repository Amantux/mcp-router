import { useEffect, useState } from "react";
import { Link } from "react-router";
import {
  Button,
  Input,
  Textarea,
  Tab,
  TabList,
  Text,
} from "@fluentui/react-components";
import {
  completeSetup,
  createPrincipal,
  createSkillSource,
  getModelsHealth,
  getSetupStatus,
  importServers,
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

export function SetupPage() {
  const [step, setStep] = useState(loadStep);
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [msg, setMsg] = useState("");
  const [token] = useState(suggestToken);
  const [serversJson, setServersJson] = useState("");
  const [gitUrl, setGitUrl] = useState("");
  const [agentId, setAgentId] = useState("my-agent");
  const [apiKey, setApiKey] = useState<string | null>(null); // memory only: shown once
  const [client, setClient] = useState<Client>("Claude Code");
  const [health, setHealth] = useState("");
  const url = `${window.location.origin}/mcp`;

  const recheck = () =>
    getSetupStatus().then(setStatus, () =>
      setMsg("Status needs the admin token (set it in Settings)."),
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

  const run = (p: Promise<unknown>, ok: string) =>
    p.then(
      () => setMsg(ok),
      (e: unknown) => setMsg(e instanceof Error ? e.message : "Request failed"),
    );

  const body = [
    <div key="a">
      <Text block>
        MCPR_ADMIN_TOKEN must be set in the server environment; it is never
        stored in the database.
      </Text>
      <pre>{`MCPR_ADMIN_TOKEN=${token}`}</pre>
      <Button onClick={() => void navigator.clipboard.writeText(token)}>
        Copy suggestion
      </Button>{" "}
      <Button onClick={() => void recheck()}>I've set it, re-check</Button>
      <Text block>
        Admin token configured:{" "}
        {status ? String(status.hasAdminToken) : "unknown"}
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
      <Textarea
        aria-label="mcpServers JSON"
        value={serversJson}
        onChange={(_, d) => setServersJson(d.value)}
      />
      <Button
        onClick={() => {
          try {
            void run(
              importServers(JSON.parse(serversJson)),
              "Servers imported.",
            );
          } catch {
            setMsg("That is not valid JSON.");
          }
        }}
      >
        Import servers
      </Button>
    </div>,
    <div key="d">
      <Input
        aria-label="Git URL"
        placeholder="https://github.com/org/skills.git"
        value={gitUrl}
        onChange={(_, d) => setGitUrl(d.value)}
      />
      <Button
        onClick={() => {
          if (!isHttpsGitUrl(gitUrl))
            return setMsg("Only https:// git URLs are accepted.");
          void run(
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
    </div>,
    <div key="e">
      <Input
        aria-label="Agent id"
        value={agentId}
        onChange={(_, d) => setAgentId(d.value)}
      />
      <Button
        disabled={apiKey !== null}
        onClick={() =>
          void createPrincipal({ agentId, maxTools: 8 }).then(
            (p) => setApiKey(p.apiKey),
            () => setMsg("Could not create the agent."),
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
          <Button onClick={() => void navigator.clipboard.writeText(apiKey)}>
            Copy key
          </Button>
        </div>
      )}
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
        {snippetFor(client, url, apiKey ?? "<your-agent-key>")}
      </pre>
    </div>,
    <div key="g">
      <Button
        onClick={() =>
          void Promise.all([getModelsHealth(), getSetupStatus()]).then(
            ([h, s]) => {
              setStatus(s);
              setHealth(
                `${JSON.stringify(h).length > 0 ? "models: ok" : ""}; servers ${s.counts.servers}, tools ${s.counts.tools}`,
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
        onClick={() => void run(completeSetup(), "Setup complete.")}
      >
        Finish setup
      </Button>
    </div>,
  ];

  return (
    <section aria-label="Setup wizard">
      <Link to="/" onClick={skipSetup}>
        Skip setup
      </Link>
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
          Next (or skip)
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
