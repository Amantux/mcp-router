import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import { hydrateAuth } from "../api/auth";

// jsdom lacks ResizeObserver, which Fluent's MessageBar reflow logic uses.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver;

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  window.localStorage.clear();
  hydrateAuth(); // reset in-memory credentials between tests
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
