import { useState } from "react";
import {
  Button,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Field,
  Input,
  makeStyles,
  Radio,
  RadioGroup,
  Textarea,
  tokens,
} from "@fluentui/react-components";
import { registerServer } from "../api/client";
import type { MCPServer, RegisterServerRequest, Transport } from "../api/types";
import { useNotify } from "../components/Notifications";

const useStyles = makeStyles({
  form: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalM, maxWidth: "480px" },
});

/** Only http(s) endpoints are accepted for network transports. */
export function validateHttpUrl(raw: string): string | null {
  const v = raw.trim();
  if (!v) return "Enter the server's URL.";
  let u: URL;
  try {
    u = new URL(v);
  } catch {
    return "Enter a full URL, e.g. https://mcp.example.com/mcp.";
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") return "Only http:// and https:// URLs are supported.";
  if (!u.hostname) return "The URL needs a host name.";
  return null;
}

interface Errors {
  name?: string;
  endpoint?: string;
  command?: string;
}

export function RegisterServerDialog({ open, onClose, onRegistered }: { open: boolean; onClose: () => void; onRegistered: (s: MCPServer) => void }) {
  const s = useStyles();
  const notify = useNotify();
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<Transport>("stdio");
  const [endpoint, setEndpoint] = useState("");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [errors, setErrors] = useState<Errors>({});
  const [pending, setPending] = useState(false);

  const reset = () => {
    setName("");
    setTransport("stdio");
    setEndpoint("");
    setCommand("");
    setArgs("");
    setErrors({});
  };

  const submit = async () => {
    const e: Errors = {};
    if (!name.trim()) e.name = "Enter a name for this server.";
    let body: RegisterServerRequest;
    if (transport === "stdio") {
      if (!command.trim()) e.command = "Enter the executable to launch, e.g. npx or uvx.";
      const argv = args
        .split("\n")
        .map((a) => a.trim())
        .filter(Boolean);
      body = { name: name.trim(), transport, stdioCommand: [command.trim(), ...argv] };
    } else {
      const err = validateHttpUrl(endpoint);
      if (err) e.endpoint = err;
      body = { name: name.trim(), transport, endpoint: endpoint.trim() };
    }
    setErrors(e);
    if (Object.keys(e).length) return;
    setPending(true);
    try {
      const created = await registerServer(body);
      notify.success(`Registered server “${created.name ?? name.trim()}”`);
      onRegistered(created);
      reset();
      onClose();
    } catch (err) {
      notify.error(`Register server “${name.trim()}”`, err);
    } finally {
      setPending(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(_, d) => !d.open && !pending && onClose()}>
      <DialogSurface>
        <form
          noValidate
          onSubmit={(ev) => {
            ev.preventDefault();
            void submit();
          }}
        >
          <DialogBody>
            <DialogTitle>Register MCP server</DialogTitle>
            <DialogContent className={s.form}>
              <Field label="Name" required validationMessage={errors.name}>
                <Input value={name} onChange={(_, d) => setName(d.value)} placeholder="github" />
              </Field>
              <Field label="Transport">
                <RadioGroup layout="horizontal" value={transport} onChange={(_, d) => setTransport(d.value as Transport)}>
                  <Radio value="stdio" label="stdio" />
                  <Radio value="streamable-http" label="Streamable HTTP" />
                  <Radio value="sse" label="SSE (legacy)" />
                </RadioGroup>
              </Field>
              {transport === "stdio" ? (
                <>
                  <Field label="Command" required validationMessage={errors.command} hint="Executable only; put arguments below.">
                    <Input value={command} onChange={(_, d) => setCommand(d.value)} />
                  </Field>
                  <Field label="Arguments" hint="One argument per line — no shell quoting is applied.">
                    <Textarea value={args} onChange={(_, d) => setArgs(d.value)} rows={4} resize="vertical" />
                  </Field>
                </>
              ) : (
                <Field label="URL" required validationMessage={errors.endpoint} hint="http:// or https:// only.">
                  <Input type="url" value={endpoint} onChange={(_, d) => setEndpoint(d.value)} />
                </Field>
              )}
            </DialogContent>
            <DialogActions>
              <Button appearance="secondary" onClick={onClose} disabled={pending}>
                Cancel
              </Button>
              <Button appearance="primary" type="submit" disabled={pending}>
                {pending ? "Registering…" : "Register server"}
              </Button>
            </DialogActions>
          </DialogBody>
        </form>
      </DialogSurface>
    </Dialog>
  );
}
