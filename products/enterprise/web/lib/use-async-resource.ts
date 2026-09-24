"use client";

import { useCallback, useEffect, useRef, useState } from "react";

type AsyncResourceState<T> = {
  /** Most recent data, or null before the first successful load. */
  data: T | null;
  /** True during the initial load (before any data has arrived). */
  loading: boolean;
  /** True during a refresh that is not the initial load. */
  refreshing: boolean;
  /** Human-readable error string from the last fetch attempt, or null. */
  error: string | null;
  /** Re-run the fetcher. Swallows errors (they land in `error`); returns the
   * new data on success or null on failure. */
  refresh: () => Promise<T | null>;
};

/**
 * Run an async fetcher on mount and expose its loading/error state plus a
 * `refresh` callback. Guards against setState after unmount.
 *
 * Designed for the common "load a list on mount, refresh after mutations"
 * pattern that was copy-pasted across workspace components. Components with
 * more complex state (multiple independent fetches, optimistic updates,
 * polling) should keep their own logic.
 *
 * `deps` re-runs the fetch when its values change (e.g. a filter or role flag).
 * Pass the same values you'd put in a useEffect dep array.
 */
export function useAsyncResource<T>(
  fetcher: () => Promise<T>,
  deps: ReadonlyArray<unknown> = [],
): AsyncResourceState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Latest fetcher + whether we've ever produced data, kept in refs so the
  // stable `refresh` callback and the `deps`-driven effect can both read them
  // without re-creating `refresh` on every render.
  const fetcherRef = useRef(fetcher);
  const mountedRef = useRef(true);
  const hasDataRef = useRef(false);

  useEffect(() => {
    fetcherRef.current = fetcher;
  });

  const run = useCallback(async (): Promise<T | null> => {
    if (!hasDataRef.current) {
      setLoading(true);
    } else {
      setRefreshing(true);
    }
    setError(null);
    try {
      const next = await fetcherRef.current();
      if (mountedRef.current) {
        setData(next);
        hasDataRef.current = true;
      }
      return next;
    } catch (err) {
      if (mountedRef.current) {
        setError(err instanceof Error ? err.message : "Failed to load.");
      }
      return null;
    } finally {
      if (mountedRef.current) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, []);

  const refresh = useCallback(() => run(), [run]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Re-fetch on mount and whenever any value in `deps` changes.
  useEffect(() => {
    void run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, loading, refreshing, error, refresh };
}
