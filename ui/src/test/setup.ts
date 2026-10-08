import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

// jsdom lacks ResizeObserver, which Fluent's MessageBar reflow logic uses.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
