import { useCallback, useEffect, useRef, useState } from "react";
import { isAbort } from "../api/client";
import { useNotify } from "../components/Notifications";

export interface Loader<T> {
  data: T | undefined;
  loading: boolean;
  failed: boolean;
  /** Visible reload: shows the loading state. */
  reload: () => void;
  /** Quiet refresh: keeps last-good data on screen, no loading flash. */
  refresh: () => void;
}

/**
 * Shared fetch-on-deps loader. Aborts superseded requests; on failure posts a
 * persistent error notification naming `what` and keeps any last-good data.
 */
export function useLoader<T>(what: string, fn: (signal: AbortSignal) => Promise<T>, deps: unknown[]): Loader<T> {
  const notify = useNotify();
  const [data, setData] = useState<T>();
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [tick, setTick] = useState(0);
  const quiet = useRef(false);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    const ctrl = new AbortController();
    if (!quiet.current) setLoading(true);
    fnRef
      .current(ctrl.signal)
      .then((d) => {
        setData(d);
        setFailed(false);
      })
      .catch((e: unknown) => {
        if (isAbort(e)) return;
        setFailed(true);
        notify.error(what, e);
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setLoading(false);
        quiet.current = false;
      });
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- caller supplies deps explicitly
  }, [...deps, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  const refresh = useCallback(() => {
    quiet.current = true;
    setTick((t) => t + 1);
  }, []);
  return { data, loading, failed, reload, refresh };
}
