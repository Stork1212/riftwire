import { useCallback, useEffect, useRef, useState } from "react";

export interface Polled<T> {
  data: T | undefined;
  error: Error | undefined;
  loading: boolean;
  refresh: () => void;
}

/** Fetch now and then every `intervalMs`; pauses while the tab is hidden.
 *  Responses from a superseded request (e.g. after deps change) are ignored. */
export function usePolling<T>(fetcher: () => Promise<T>, intervalMs: number, deps: unknown[] = []): Polled<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<Error>();
  const fetcherRef = useRef(fetcher);
  const generation = useRef(0);

  useEffect(() => {
    fetcherRef.current = fetcher;
  });

  const run = useCallback(async (gen: number) => {
    try {
      const result = await fetcherRef.current();
      if (gen === generation.current) {
        setData(result);
        setError(undefined);
      }
    } catch (e) {
      if (gen === generation.current) setError(e instanceof Error ? e : new Error(String(e)));
    }
  }, []);

  useEffect(() => {
    const gen = ++generation.current;
    void run(gen);
    const id = window.setInterval(() => {
      if (document.visibilityState !== "hidden") void run(gen);
    }, intervalMs);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run, intervalMs, ...deps]);

  return {
    data,
    error,
    loading: data === undefined && error === undefined,
    refresh: () => void run(generation.current),
  };
}
