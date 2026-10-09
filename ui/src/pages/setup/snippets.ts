// Per-client connect snippets (mirrors docs/INSTALL.md §3). Rendered locally;
// the key is the one just shown once in the identity step.
export const CLIENTS = [
  "Claude Code",
  "Copilot CLI",
  "Codex",
  "Cursor",
  "VS Code",
  "Gemini",
] as const;
export type Client = (typeof CLIENTS)[number];

const json = (v: unknown) => JSON.stringify(v, null, 2);

export function snippetFor(client: Client, url: string, key: string): string {
  const auth = { Authorization: `Bearer ${key}` };
  switch (client) {
    case "Claude Code":
      return `claude mcp add --transport http mcp-router ${url} --header "Authorization: Bearer ${key}"`;
    case "Copilot CLI":
      return json({
        mcpServers: {
          "mcp-router": { type: "http", url, headers: auth, tools: ["*"] },
        },
      });
    case "Codex":
      return `[mcp_servers.mcp-router]\nurl = "${url}"\nhttp_headers = { Authorization = "Bearer ${key}" }`;
    case "Cursor":
      return json({ mcpServers: { "mcp-router": { url, headers: auth } } });
    case "VS Code":
      return json({
        servers: { "mcp-router": { type: "http", url, headers: auth } },
      });
    case "Gemini":
      return json({
        mcpServers: { "mcp-router": { httpUrl: url, headers: auth } },
      });
  }
}

export function isHttpsGitUrl(raw: string): boolean {
  try {
    return new URL(raw).protocol === "https:";
  } catch {
    return false;
  }
}

export function suggestToken(): string {
  const b = new Uint8Array(24);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}
