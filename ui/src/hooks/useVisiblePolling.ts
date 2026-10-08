import { useEffect, useRef } from "react";

/**
 * Calls `fn` every `ms` while the document is visible; pauses when hidden and
 * fires once immediately on becoming visible again.
 */
export function useVisiblePolling(fn: () => void, ms = 12_000): void {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  useEffect(() => {
    let id: number | undefined;
    const start = () => {
      if (id === undefined) id = window.setInterval(() => fnRef.current(), ms);
    };
    const stop = () => {
      if (id !== undefined) window.clearInterval(id);
      id = undefined;
    };
    const onVis = () => {
      if (document.visibilityState === "visible") {
        fnRef.current();
        start();
      } else stop();
    };
    if (document.visibilityState === "visible") start();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [ms]);
}
