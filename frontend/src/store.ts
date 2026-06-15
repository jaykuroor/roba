import { useSyncExternalStore } from "react";
import type { ApprovalRequest, Call, CallTurn, SimState, Weather } from "./types";

// A tiny Zustand-style external store: a single mutable state object, a set of
// listeners, and `useSyncExternalStore` selectors. Because it lives outside the
// React tree, the singleton WsClient (ws.ts) can push updates into it from its
// socket callbacks without any component plumbing. The store only *holds* the
// latest server state — it contains no business logic (00 §23).

export interface StoreState {
  simState: SimState | null;
  latestWeather: Weather | null;
  pendingApprovals: ApprovalRequest[];
  activeCall: Call | null;
  /** Live transcript of the active call, accumulated from call_turn events. */
  callTurns: CallTurn[];
  wsConnected: boolean;
}

const initialState: StoreState = {
  simState: null,
  latestWeather: null,
  pendingApprovals: [],
  activeCall: null,
  callTurns: [],
  wsConnected: false,
};

type Listener = () => void;

let state: StoreState = initialState;
const listeners = new Set<Listener>();

function emit(): void {
  for (const listener of listeners) listener();
}

function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function getState(): StoreState {
  return state;
}

function setState(patch: Partial<StoreState>): void {
  state = { ...state, ...patch };
  emit();
}

// -- actions (called by ws.ts and operator-action components) ---------------

export const actions = {
  setWsConnected(connected: boolean): void {
    if (state.wsConnected !== connected) setState({ wsConnected: connected });
  },

  /** Merge a (possibly partial) sim snapshot — sim_tick carries a subset. */
  setSimState(next: Partial<SimState>): void {
    const merged = { ...(state.simState ?? {}), ...next } as SimState;
    setState({ simState: merged });
  },

  setWeather(weather: Weather): void {
    setState({ latestWeather: weather });
  },

  setApprovals(approvals: ApprovalRequest[]): void {
    setState({ pendingApprovals: approvals });
  },

  upsertApproval(approval: ApprovalRequest): void {
    const rest = state.pendingApprovals.filter((a) => a.id !== approval.id);
    if (approval.status === "pending") {
      setState({ pendingApprovals: [...rest, approval] });
    } else {
      setState({ pendingApprovals: rest });
    }
  },

  removeApproval(id: number): void {
    setState({
      pendingApprovals: state.pendingApprovals.filter((a) => a.id !== id),
    });
  },

  startCall(call: Call): void {
    setState({ activeCall: call, callTurns: call.transcript ?? [] });
  },

  endCall(): void {
    setState({ activeCall: null, callTurns: [] });
  },

  appendCallTurn(turn: CallTurn): void {
    setState({ callTurns: [...state.callTurns, turn] });
  },
};

export const store = { getState, setState, subscribe };

// -- selector hooks ---------------------------------------------------------

function useSelector<T>(selector: (s: StoreState) => T): T {
  return useSyncExternalStore(
    subscribe,
    () => selector(state),
    () => selector(initialState),
  );
}

export function useSimState(): SimState | null {
  return useSelector((s) => s.simState);
}

export function useApprovals(): ApprovalRequest[] {
  return useSelector((s) => s.pendingApprovals);
}

export function useActiveCall(): Call | null {
  return useSelector((s) => s.activeCall);
}

export function useWeather(): Weather | null {
  return useSelector((s) => s.latestWeather);
}

export function useCallTurns(): CallTurn[] {
  return useSelector((s) => s.callTurns);
}

export function useWsConnected(): boolean {
  return useSelector((s) => s.wsConnected);
}
