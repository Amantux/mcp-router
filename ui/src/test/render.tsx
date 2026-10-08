import { vi } from "vitest";
import type { ReactElement } from "react";
import { render } from "@testing-library/react";
import { FluentProvider, webLightTheme } from "@fluentui/react-components";
import { NotificationsProvider, NotificationStack } from "../components/Notifications";

export function renderWithProviders(ui: ReactElement) {
  return render(
    <FluentProvider theme={webLightTheme}>
      <NotificationsProvider>
        <NotificationStack />
        {ui}
      </NotificationsProvider>
    </FluentProvider>,
  );
}

export interface FetchCall {
  url: string;
  method: string;
  body: unknown;
  headers: Record<string, string>;
}

/** Installs a fetch mock answering by "METHOD path" (query string ignored); records calls. */
export function mockFetch(routes: Record<string, (body: unknown) => { status?: number; json?: unknown; text?: string } | Promise<{ status?: number; json?: unknown; text?: string }>>) {
  const calls: FetchCall[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    const headers: Record<string, string> = {};
    new Headers(init?.headers).forEach((v, k) => (headers[k] = v));
    calls.push({ url, method, body, headers });
    const key = `${method} ${url.split("?")[0]}`;
    const handler = routes[key];
    if (!handler) return new Response("not mocked", { status: 404 });
    const r = await handler(body);
    const text = r.text ?? (r.json !== undefined ? JSON.stringify(r.json) : "");
    return new Response(text, { status: r.status ?? 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  return { fn, calls };
}
