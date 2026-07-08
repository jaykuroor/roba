import { useSyncExternalStore } from "react";
import { apiGet } from "./api";
import type { ApprovalRequest, Call, CallTurn, ManagerChange, SimState, Weather } from "./types";

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
  /** The most recently completed call, retained for the desk summary card.
   *  Cleared when a new call starts or when the summary is dismissed. */
  lastCompletedCall: Call | null;
  wsConnected: boolean;
  /** Incremented each time a manager_change WS event arrives; components use
   * this to trigger a refetch of the changes list. */
  managerChangeVersion: number;
  /** Latest manager changes loaded by components; set on refetch. */
  managerChanges: ManagerChange[];
  /** Incremented each time a procurement_plan_updated WS event arrives; components
   * use this to trigger a refetch of the procurement plan. */
  procurementPlanVersion: number;
}

const initialState: StoreState = {
  simState: null,
  latestWeather: null,
  pendingApprovals: [],
  activeCall: null,
  callTurns: [],
  lastCompletedCall: null,
  wsConnected: false,
  managerChangeVersion: 0,
  managerChanges: [],
  procurementPlanVersion: 0,
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

  endCall(completedCall?: Call): void {
    setState({
      activeCall: null,
      callTurns: [],
      lastCompletedCall: completedCall ?? state.activeCall,
    });
  },

  dismissCompletedCall(): void {
    setState({ lastCompletedCall: null });
  },

  /** Fetch the currently-active call from the server and sync the store.
   *  Called on socket open/reconnect so late-joining surfaces show the
   *  indicator even when they missed the `call_started` WS event. */
  hydrateActiveCall(): void {
    apiGet<Call | null>("/api/calls/active")
      .then((call) => {
        if (call) {
          actions.startCall(call);
        } else {
          if (state.activeCall !== null) {
            // Clear the active indicator — don't overwrite a completed-call summary.
            setState({ activeCall: null, callTurns: [] });
          }
          // Hydrate the most-recently-completed call so the desk card survives a
          // page refresh (the WS call_ended event is ephemeral).
          actions.hydrateLastCompletedCall();
        }
      })
      .catch(() => undefined);
  },

  /** Fetch the most recent completed call and surface it as the desk summary card. */
  hydrateLastCompletedCall(): void {
    if (state.lastCompletedCall !== null) return; // already populated from WS
    apiGet<Call | null>("/api/calls/recent")
      .then((call) => {
        if (call) setState({ lastCompletedCall: call });
      })
      .catch(() => undefined);
  },

  appendCallTurn(turn: CallTurn): void {
    setState({ callTurns: [...state.callTurns, turn] });
  },

  bumpManagerChangeVersion(): void {
    setState({ managerChangeVersion: state.managerChangeVersion + 1 });
  },

  setManagerChanges(changes: ManagerChange[]): void {
    setState({ managerChanges: changes });
  },

  bumpProcurementPlanVersion(): void {
    setState({ procurementPlanVersion: state.procurementPlanVersion + 1 });
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

export function useLastCompletedCall(): Call | null {
  return useSelector((s) => s.lastCompletedCall);
}

export function useWsConnected(): boolean {
  return useSelector((s) => s.wsConnected);
}

export function useManagerChangeVersion(): number {
  return useSelector((s) => s.managerChangeVersion);
}

export function useManagerChanges(): ManagerChange[] {
  return useSelector((s) => s.managerChanges);
}

export function useProcurementPlanVersion(): number {
  return useSelector((s) => s.procurementPlanVersion);
}
