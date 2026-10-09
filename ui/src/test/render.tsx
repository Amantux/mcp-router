import { expect, vi } from "vitest";
import type { ReactElement } from "react";
import { render } from "@testing-library/react";
import { FluentProvider, webLightTheme } from "@fluentui/react-components";
import { MemoryRouter } from "react-router";
import { NotificationsProvider, NotificationStack } from "../components/Notifications";

export function renderWithProviders(ui: ReactElement, opts: { route?: string } = {}) {
  const tree = (
    <FluentProvider theme={webLightTheme}>
      <NotificationsProvider>
        <NotificationStack />
        {ui}
      </NotificationsProvider>
    </FluentProvider>
  );
  return render(opts.route !== undefined ? <MemoryRouter initialEntries={[opts.route]}>{tree}</MemoryRouter> : tree);
}

export interface FetchCall {
  /** Full URL as requested, including the query string. */
  url: string;
  method: string;
  /** Path without the query string. */
  path: string;
  /** Query parameters (repeated keys keep the last value). */
  query: Record<string, string>;
  body: unknown;
  /** Top-level keys of a JSON object body (empty for no body / non-object). */
  bodyKeys: string[];
  headers: Record<string, string>;
}

type MockResponse = { status?: number; json?: unknown; text?: string };
type MockHandler = (body: unknown, call: FetchCall) => MockResponse | Promise<MockResponse>;

// Requests that hit no mocked route. setup.ts fails the test that made them: a silent
// 404 turns into a quiet toast in useLoader and hides wrong paths and missing mocks.
const unmocked: string[] = [];
export function takeUnmockedCalls(): string[] {
  return unmocked.splice(0, unmocked.length);
}

/**
 * Strict fetch mock answering by "METHOD path" (the query string is recorded on the
 * call, not matched). A request to an unmocked route still gets a 404 so the page
 * behaves as it would, but the test FAILS in afterEach. Mock a 404 explicitly when
 * the test wants one.
 */
export function mockFetch(routes: Record<string, MockHandler>) {
  const calls: FetchCall[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    const headers: Record<string, string> = {};
    new Headers(init?.headers).forEach((v, k) => (headers[k] = v));
    const parsed = new URL(url, "http://localhost");
    const query = Object.fromEntries(parsed.searchParams.entries());
    const bodyKeys = body !== null && typeof body === "object" && !Array.isArray(body) ? Object.keys(body) : [];
    const call: FetchCall = { url, method, path: url.split("?")[0], query, body, bodyKeys, headers };
    calls.push(call);
    const key = `${method} ${call.path}`;
    const handler = routes[key];
    if (!handler) {
      unmocked.push(key);
      return new Response("not mocked", { status: 404 });
    }
    const r = await handler(body, call);
    const text = r.text ?? (r.json !== undefined ? JSON.stringify(r.json) : "");
    const status = r.status ?? 200;
    // A null-body status (204 No Content) cannot carry a body, not even "".
    return new Response(status === 204 ? null : text, { status, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  return { fn, calls };
}

/** Calls to `METHOD path` (exact path, any query). */
export function callsTo(calls: FetchCall[], method: string, path: string): FetchCall[] {
  return calls.filter((c) => c.method === method && c.path === path);
}

/**
 * Asserts the LAST call to `METHOD path` carried exactly `expected` as its query
 * (no missing, extra or different keys). Returns that call.
 */
export function expectQuery(calls: FetchCall[], method: string, path: string, expected: Record<string, string>): FetchCall {
  const hits = callsTo(calls, method, path);
  if (!hits.length) throw new Error(`expectQuery: no ${method} ${path} call was made`);
  const last = hits[hits.length - 1];
  expect(last.query).toEqual(expected);
  return last;
}
