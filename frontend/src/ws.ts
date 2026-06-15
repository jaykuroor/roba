import { actions } from "./store";
import type { ApprovalRequest, Call, SimState, Weather } from "./types";

// One WebSocket connection to the backend hub (00 §21). The browser hits the
// frontend origin and Vite proxies /ws to the backend (00 §26.4), so the URL is
// always derived from window.location — never a hardcoded host/port.

type Handler = (payload: Record<string, unknown>) => void;

const RECONNECT_DELAY_MS = 2000;

export class WsClient {
  private socket: WebSocket | null = null;
  private handlers = new Map<string, Set<Handler>>();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private closedByUser = false;

  /** Register a handler for an incoming `{event, payload}`; returns an unsubscribe. */
  on(event: string, fn: Handler): () => void {
    let set = this.handlers.get(event);
    if (!set) {
      set = new Set();
      this.handlers.set(event, set);
    }
    set.add(fn);
    return () => {
      set?.delete(fn);
    };
  }

  private url(): string {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${window.location.host}/ws`;
  }

  connect(): void {
    this.closedByUser = false;
    if (
      this.socket &&
      (this.socket.readyState === WebSocket.OPEN ||
        this.socket.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }

    const socket = new WebSocket(this.url());
    this.socket = socket;

    socket.onopen = () => {
      actions.setWsConnected(true);
    };

    socket.onmessage = (ev: MessageEvent) => {
      let message: { event?: string; payload?: Record<string, unknown> };
      try {
        message = JSON.parse(ev.data as string);
      } catch {
        return;
      }
      if (!message.event) return;
      this.dispatch(message.event, message.payload ?? {});
    };

    socket.onclose = () => {
      actions.setWsConnected(false);
      this.socket = null;
      if (!this.closedByUser) this.scheduleReconnect();
    };

    socket.onerror = () => {
      // Let onclose drive the reconnect; closing here avoids a stuck socket.
      socket.close();
    };
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer !== null) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, RECONNECT_DELAY_MS);
  }

  private dispatch(event: string, payload: Record<string, unknown>): void {
    const set = this.handlers.get(event);
    if (!set) return;
    for (const fn of set) fn(payload);
  }

  close(): void {
    this.closedByUser = true;
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.socket?.close();
    this.socket = null;
  }
}

export const wsClient = new WsClient();

// Wire the core shell store to the relevant WS events (00 §21). Track panels
// register their own handlers via wsClient.on(...) for the events they need.
wsClient.on("sim_tick", (p) => actions.setSimState(p as Partial<SimState>));
wsClient.on("weather_updated", (p) =>
  actions.setWeather((p as { weather: Weather }).weather),
);
wsClient.on("approval_created", (p) =>
  actions.upsertApproval((p as { approval: ApprovalRequest }).approval),
);
wsClient.on("approval_resolved", (p) => {
  const approval = (p as { approval: ApprovalRequest }).approval;
  if (approval) actions.removeApproval(approval.id);
});
wsClient.on("call_started", (p) =>
  actions.startCall((p as { call: Call }).call),
);
wsClient.on("call_ended", () => actions.endCall());
wsClient.on("call_turn", (p) => {
  const turn = p as { role: "agent" | "counterparty"; text: string };
  actions.appendCallTurn({ role: turn.role, text: turn.text });
});
