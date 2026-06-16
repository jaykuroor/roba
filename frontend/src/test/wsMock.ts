// A controllable stand-in for the singleton WsClient (ws.ts). The panels only
// use `wsClient.on(event, fn)` to subscribe; here we record those handlers and
// expose `emitWs(event, payload)` so a test can feed a sample WS payload and
// assert the panel re-renders — without any real socket.
import { vi } from "vitest";
import { act } from "@testing-library/react";

type Handler = (payload: Record<string, unknown>) => void;

const handlers = new Map<string, Set<Handler>>();

export const wsClientMock = {
  on(event: string, fn: Handler): () => void {
    let set = handlers.get(event);
    if (!set) {
      set = new Set();
      handlers.set(event, set);
    }
    set.add(fn);
    return () => {
      set?.delete(fn);
    };
  },
  connect: vi.fn(),
  close: vi.fn(),
};

/** Dispatch a sample WS event to every handler the panel registered for it.
 * Wrapped in act() so the React state updates it triggers flush cleanly. */
export function emitWs(event: string, payload: Record<string, unknown>): void {
  const set = handlers.get(event);
  if (!set) return;
  act(() => {
    for (const fn of set) fn(payload);
  });
}

/** Drop all recorded handlers — call between tests so subscriptions don't leak. */
export function resetWsMock(): void {
  handlers.clear();
}
