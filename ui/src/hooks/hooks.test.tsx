import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { NotificationsProvider } from "../components/Notifications";
import { ApiError } from "../api/client";
import { useLoader } from "./useLoader";
import { useDebounced } from "./useDebounced";
import { useVisiblePolling } from "./useVisiblePolling";

const wrapper = ({ children }: { children: ReactNode }) => <NotificationsProvider>{children}</NotificationsProvider>;

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("useLoader", () => {
  it("aborts a superseded request and never applies its late result", async () => {
    const pending: Record<string, ReturnType<typeof deferred<string>>> = {};
    const signals: Record<string, AbortSignal> = {};
    const fn = (key: string) => (sig: AbortSignal) => {
      signals[key] = sig;
      pending[key] = deferred<string>();
      return pending[key].promise;
    };
    const { result, rerender } = renderHook(({ k }) => useLoader("Load", fn(k), [k]), { wrapper, initialProps: { k: "a" } });
    rerender({ k: "b" });
    expect(signals.a.aborted).toBe(true);
    await act(async () => pending.b.resolve("B"));
    await act(async () => pending.a.resolve("A")); // late, superseded
    expect(result.current.data).toBe("B");
    expect(result.current.loading).toBe(false);
  });

  it("refresh() is quiet (no loading flash) and a failed refresh keeps the last good data", async () => {
    let next: () => Promise<string> = () => Promise.resolve("v1");
    const { result } = renderHook(() => useLoader("Load", () => next(), []), { wrapper });
    await waitFor(() => expect(result.current.data).toBe("v1"));
    const d = deferred<string>();
    next = () => d.promise;
    act(() => result.current.refresh());
    expect(result.current.loading).toBe(false);
    await act(async () => d.reject(new ApiError(500, "/x")));
    expect(result.current.data).toBe("v1");
    expect(result.current.failed).toBe(true);
  });

  it("reload() is visible: it shows loading until the new data lands", async () => {
    let next: () => Promise<string> = () => Promise.resolve("v1");
    const { result } = renderHook(() => useLoader("Load", () => next(), []), { wrapper });
    await waitFor(() => expect(result.current.data).toBe("v1"));
    const d = deferred<string>();
    next = () => d.promise;
    act(() => result.current.reload());
    expect(result.current.loading).toBe(true);
    await act(async () => d.resolve("v2"));
    expect([result.current.loading, result.current.data, result.current.failed]).toEqual([false, "v2", false]);
  });
});

describe("useDebounced", () => {
  afterEach(() => vi.useRealTimers());
  it("emits only the last value after the quiet period", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(({ v }) => useDebounced(v, 200), { initialProps: { v: "a" } });
    rerender({ v: "ab" });
    act(() => vi.advanceTimersByTime(150));
    rerender({ v: "abc" });
    act(() => vi.advanceTimersByTime(150));
    expect(result.current).toBe("a");
    act(() => vi.advanceTimersByTime(60));
    expect(result.current).toBe("abc");
  });
});

describe("useVisiblePolling", () => {
  const setVisibility = (v: "visible" | "hidden") => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => v });
    document.dispatchEvent(new Event("visibilitychange"));
  };
  afterEach(() => {
    vi.useRealTimers();
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
  });

  it("polls while visible, pauses while hidden, and fires once on becoming visible", () => {
    vi.useFakeTimers();
    const fn = vi.fn();
    const { unmount } = renderHook(() => useVisiblePolling(fn, 1000));
    act(() => vi.advanceTimersByTime(2500));
    expect(fn).toHaveBeenCalledTimes(2);
    act(() => setVisibility("hidden"));
    act(() => vi.advanceTimersByTime(5000));
    expect(fn).toHaveBeenCalledTimes(2);
    act(() => setVisibility("visible"));
    expect(fn).toHaveBeenCalledTimes(3);
    unmount();
    act(() => vi.advanceTimersByTime(5000));
    expect(fn).toHaveBeenCalledTimes(3);
  });
});
