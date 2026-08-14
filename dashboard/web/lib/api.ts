import { useCallback, useEffect, useRef, useState } from "react";

import type { ApiError } from "./types.ts";

export class RequestError extends Error {
  constructor(
    message: string,
    readonly issues: { path: string; message: string }[] = [],
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    const body = (await res.json().catch(() => ({ message: res.statusText }))) as ApiError;
    throw new RequestError(body.message ?? "エラーが発生しました", body.issues ?? []);
  }
  return (await res.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }),
};

/** 単発の取得＋手動再取得。エラーは画面に出す（黙って空表示にしない）。 */
export function useFetch<T>(path: string | null): {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
} {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(path !== null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (path === null) return;
    let alive = true;
    setLoading(true);
    api
      .get<T>(path)
      .then((d) => {
        if (alive) {
          setData(d);
          setError(null);
        }
      })
      .catch((e: Error) => alive && setError(e.message))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [path, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data, error, loading, reload };
}

/**
 * SSEの購読。イベント名ごとに最新の値を保持する。
 * 接続が切れたらブラウザが自動で再接続するので、こちらでは切断を明示するだけ。
 */
export function useEventStream(handlers: Record<string, (data: unknown) => void>): {
  connected: boolean;
} {
  const [connected, setConnected] = useState(false);
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  useEffect(() => {
    const es = new EventSource("/api/events");
    const names = Object.keys(handlersRef.current);
    const listeners = names.map((name) => {
      const fn = (ev: MessageEvent<string>) => {
        try {
          handlersRef.current[name]?.(JSON.parse(ev.data));
        } catch (error) {
          console.error(`[sse] ${name} の解釈に失敗:`, error);
        }
      };
      es.addEventListener(name, fn as EventListener);
      return { name, fn };
    });
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    return () => {
      for (const l of listeners) es.removeEventListener(l.name, l.fn as EventListener);
      es.close();
    };
  }, []);

  return { connected };
}
