import { useState } from "react";
import {
  Badge,
  Button,
  Caption1,
  DrawerBody,
  DrawerFooter,
  DrawerHeader,
  DrawerHeaderTitle,
  Field,
  Input,
  makeStyles,
  MessageBar,
  MessageBarActions,
  MessageBarBody,
  MessageBarTitle,
  OverlayDrawer,
  tokens,
} from "@fluentui/react-components";
import { DismissRegular, PlugConnectedRegular, PlugDisconnectedRegular } from "@fluentui/react-icons";
import { ApiError, getMe, probeAdmin } from "../api/client";
import { clearCredentials, setCredentials, useAuth } from "../api/auth";

const useStyles = makeStyles({
  form: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalM },
  row: { display: "flex", gap: tokens.spacingHorizontalS, alignItems: "center", flexWrap: "wrap" },
  indicator: { display: "flex", alignItems: "center", gap: tokens.spacingHorizontalXS },
});

/** Masked connection indicator. Shows only state, never any part of a credential. */
export function ConnectionIndicator() {
  const a = useAuth();
  const s = useStyles();
  if (a.status === "rejected")
    return (
      <span className={s.indicator} data-testid="connection-indicator">
        <PlugDisconnectedRegular />
        <Badge appearance="tint" color="danger" size="small">
          not connected
        </Badge>
      </span>
    );
  const label = a.hasAdminToken ? (a.status === "connected" ? "connected" : "token set") : a.status === "connected" ? "open (dev)" : "no token";
  return (
    <span className={s.indicator} data-testid="connection-indicator">
      <PlugConnectedRegular />
      <Badge appearance="tint" color={a.status === "connected" ? "success" : "informative"} size="small">
        {label}
      </Badge>
      {a.hasAgentKey && (
        <Badge appearance="outline" size="small" color={a.agentKeyRejected ? "danger" : "informative"}>
          agent key {a.agentKeyRejected ? "refused" : "set"}
        </Badge>
      )}
    </span>
  );
}

/** Global "not connected" bar: replaces per-request 401/403 error toasts. */
export function AuthBanner({ onOpen }: { onOpen: () => void }) {
  const a = useAuth();
  if (a.status !== "rejected") return null;
  return (
    <MessageBar intent="warning" layout="multiline" data-testid="auth-banner" style={{ marginBottom: 8 }}>
      <MessageBarBody>
        <MessageBarTitle>Not connected</MessageBarTitle>
        {a.hasAdminToken
          ? "The backend refused the admin token for this session. Paste the current MCPR_ADMIN_TOKEN to continue."
          : "The backend requires an admin token. Paste MCPR_ADMIN_TOKEN to load the dashboard."}
      </MessageBarBody>
      <MessageBarActions>
        <Button size="small" onClick={onOpen}>
          Open Connect
        </Button>
      </MessageBarActions>
    </MessageBar>
  );
}

type Verdict = { kind: "ok" | "error"; text: string } | null;

export function ConnectPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const s = useStyles();
  const a = useAuth();
  const [admin, setAdmin] = useState("");
  const [agent, setAgent] = useState("");
  const [pending, setPending] = useState(false);
  const [verdict, setVerdict] = useState<Verdict>(null);

  const verify = async (checkAgent: boolean) => {
    const parts: string[] = [];
    let ok = true;
    try {
      await probeAdmin();
      parts.push("Admin access confirmed.");
    } catch (e) {
      ok = false;
      parts.push(
        e instanceof ApiError && (e.status === 401 || e.status === 403)
          ? "The backend refused the admin token."
          : "Couldn't verify the admin token: the backend didn't answer. Check it is running on port 8400.",
      );
    }
    if (checkAgent) {
      try {
        const me = await getMe();
        parts.push(`Agent key authenticates as “${me.agentId}”.`);
      } catch (e) {
        ok = false;
        parts.push(e instanceof ApiError && e.status === 401 ? "The backend refused the agent key." : "Couldn't verify the agent key.");
      }
    }
    setVerdict({ kind: ok ? "ok" : "error", text: parts.join(" ") });
  };

  const save = async () => {
    setPending(true);
    setVerdict(null);
    // Blank means unchanged; clearing is the explicit "Forget" button.
    setCredentials({ adminToken: admin ? admin : undefined, agentKey: agent ? agent : undefined });
    setAdmin("");
    setAgent("");
    try {
      await verify(Boolean(agent) || a.hasAgentKey);
    } finally {
      setPending(false);
    }
  };

  return (
    <OverlayDrawer open={open} position="end" size="small" onOpenChange={(_, d) => !d.open && onClose()}>
      <DrawerHeader>
        <DrawerHeaderTitle action={<Button appearance="subtle" aria-label="Close" icon={<DismissRegular />} onClick={onClose} />}>
          Connect
        </DrawerHeaderTitle>
        <ConnectionIndicator />
      </DrawerHeader>
      <DrawerBody>
        <form
          noValidate
          className={s.form}
          aria-label="Connect"
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          <Caption1>
            Credentials stay in this browser tab only (memory and session storage). Closing the tab forgets them. They are sent as an
            Authorization header and never shown again.
          </Caption1>
          <Field label="Admin token" hint="The backend's MCPR_ADMIN_TOKEN. Not needed in dev mode.">
            <Input
              type="password"
              autoComplete="off"
              value={admin}
              onChange={(_, d) => setAdmin(d.value)}
              placeholder={a.hasAdminToken ? "•••••••• (saved — leave blank to keep)" : "Paste admin token"}
            />
          </Field>
          <Field label="Agent key (optional)" hint="Lets the playground run tools as that agent instead of as admin.">
            <Input
              type="password"
              autoComplete="off"
              value={agent}
              onChange={(_, d) => setAgent(d.value)}
              placeholder={a.hasAgentKey ? "•••••••• (saved — leave blank to keep)" : "Paste an agent API key"}
            />
          </Field>
          {a.agentId && <Caption1>Agent key runs as “{a.agentId}”.</Caption1>}
          {verdict && (
            <MessageBar intent={verdict.kind === "ok" ? "success" : "error"} data-testid="connect-verdict">
              <MessageBarBody>{verdict.text}</MessageBarBody>
            </MessageBar>
          )}
          <div className={s.row}>
            <Button appearance="primary" type="submit" disabled={pending}>
              {pending ? "Connecting…" : "Save and verify"}
            </Button>
          </div>
        </form>
      </DrawerBody>
      <DrawerFooter>
        <div className={s.row}>
          <Button size="small" disabled={!a.hasAgentKey} onClick={() => setCredentials({ agentKey: null })}>
            Forget agent key
          </Button>
          <Button
            size="small"
            disabled={!a.hasAdminToken && !a.hasAgentKey}
            onClick={() => {
              clearCredentials();
              setVerdict(null);
            }}
          >
            Disconnect
          </Button>
        </div>
      </DrawerFooter>
    </OverlayDrawer>
  );
}
