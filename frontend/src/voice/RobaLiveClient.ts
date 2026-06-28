/**
 * RobaLiveClient — manages the WebSocket connection to the Gemini Live bridge
 * (/ws/voice/live), handles binary audio I/O, and emits typed events to
 * whoever is using it (useVoiceLive).
 *
 * Audio formats:
 *   Browser → server: 16 kHz, mono, PCM16 LE  (via AudioWorklet)
 *   Server → browser: 24 kHz, mono, PCM16 LE  (decoded & queued for playback)
 */

export type LiveClientEvent =
  | { type: "connected" }
  | { type: "unavailable"; reason?: string }
  | { type: "transcript"; role: "user" | "roba"; text: string }
  | { type: "plan_preview"; plan: PlanResult }
  | { type: "tool_result"; tool: string; result: unknown }
  | { type: "applied"; plan_id: string; signal_ids: string[] }
  | { type: "error"; message: string }
  | { type: "disconnected" };

export interface PlanResult {
  plan_id?: string;
  role?: string;
  mode?: string;
  summary?: string;
  human_readable?: string;
  routes?: RouteSpec[];
  requires_approval?: boolean;
  clarification?: Clarification | null;
  status?: string;
  signal_ids?: string[];
}

export interface RouteSpec {
  signal_type: string;
  target_agents: string[];
  target_modules: string[];
  summary: string;
}

export interface Clarification {
  question: string;
  options: Array<{ value: string; label: string } | string>;
  pending_waste?: { item_name: string; qty: number };
}

type EventHandler = (event: LiveClientEvent) => void;

export class RobaLiveClient {
  private ws: WebSocket | null = null;
  private audioCtx: AudioContext | null = null;
  private micSource: MediaStreamAudioSourceNode | null = null;
  private workletNode: AudioWorkletNode | null = null;
  private micStream: MediaStream | null = null;
  private playbackTime = 0;
  private handlers: EventHandler[] = [];
  private role: string;
  private mode: string;
  private _listening = false;

  constructor(role = "manager", mode = "confirm") {
    this.role = role;
    this.mode = mode;
  }

  on(handler: EventHandler): () => void {
    this.handlers.push(handler);
    return () => {
      this.handlers = this.handlers.filter((h) => h !== handler);
    };
  }

  private emit(event: LiveClientEvent) {
    for (const h of this.handlers) h(event);
  }

  get listening() {
    return this._listening;
  }

  // ---------------------------------------------------------------------------
  // Connect / disconnect
  // ---------------------------------------------------------------------------

  async connect(): Promise<void> {
    const base = window.location.host;
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${base}/ws/voice/live?role=${this.role}&mode=${this.mode}`;
    this.ws = new WebSocket(url);
    this.ws.binaryType = "arraybuffer";

    this.ws.onopen = () => {
      // Connected — wait for the server's "connected" or "unavailable" frame.
    };

    this.ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        try {
          const msg = JSON.parse(ev.data) as Record<string, unknown>;
          this.handleJson(msg);
        } catch {
          // ignore
        }
      } else if (ev.data instanceof ArrayBuffer) {
        // Binary PCM16 at 24 kHz from Roba's voice.
        this.playPcm(ev.data);
      }
    };

    this.ws.onclose = () => {
      this.emit({ type: "disconnected" });
    };

    this.ws.onerror = () => {
      this.emit({ type: "error", message: "WebSocket error" });
    };
  }

  disconnect() {
    this.stopListening();
    this.ws?.close();
    this.ws = null;
  }

  // ---------------------------------------------------------------------------
  // Microphone capture
  // ---------------------------------------------------------------------------

  async startListening(): Promise<void> {
    if (this._listening) return;
    this._listening = true;
    this.stopPlayback(); // barge-in: stop Roba audio when user starts speaking

    try {
      this.micStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, sampleRate: 48000, echoCancellation: true },
      });
      this.audioCtx = new AudioContext({ sampleRate: 48000 });
      await this.audioCtx.audioWorklet.addModule("/mic-processor.js");
      this.micSource = this.audioCtx.createMediaStreamSource(this.micStream);
      this.workletNode = new AudioWorkletNode(this.audioCtx, "mic-processor");
      this.workletNode.port.onmessage = (e: MessageEvent<ArrayBuffer>) => {
        if (this.ws?.readyState === WebSocket.OPEN) {
          this.ws.send(e.data);
        }
      };
      this.micSource.connect(this.workletNode);
      this.workletNode.connect(this.audioCtx.destination);
    } catch (err) {
      this._listening = false;
      this.emit({ type: "error", message: String(err) });
    }
  }

  stopListening(): void {
    if (!this._listening) return;
    this._listening = false;
    this.micSource?.disconnect();
    this.workletNode?.disconnect();
    this.micStream?.getTracks().forEach((t) => t.stop());
    this.audioCtx?.close();
    this.micSource = null;
    this.workletNode = null;
    this.micStream = null;
    this.audioCtx = null;
    // Signal end-of-turn to Gemini.
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "end_of_turn" }));
    }
  }

  // ---------------------------------------------------------------------------
  // Text input fallback
  // ---------------------------------------------------------------------------

  sendText(text: string) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "text_input", text }));
    }
  }

  // ---------------------------------------------------------------------------
  // Plan confirm / cancel
  // ---------------------------------------------------------------------------

  confirmPlan(planId: string) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "confirm_plan", plan_id: planId }));
    }
  }

  cancelPlan(planId: string) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "cancel_plan", plan_id: planId }));
    }
  }

  // ---------------------------------------------------------------------------
  // JSON frame handling
  // ---------------------------------------------------------------------------

  private handleJson(msg: Record<string, unknown>) {
    const t = msg.type as string;
    if (t === "connected") {
      this.emit({ type: "connected" });
    } else if (t === "unavailable") {
      this.emit({ type: "unavailable", reason: msg.reason as string | undefined });
    } else if (t === "transcript") {
      this.emit({
        type: "transcript",
        role: (msg.role as "user" | "roba") ?? "roba",
        text: String(msg.text ?? ""),
      });
    } else if (t === "tool_result") {
      const tool = String(msg.tool ?? "");
      const result = msg.result as Record<string, unknown> | undefined;
      this.emit({ type: "tool_result", tool, result });
      // If it's a plan result, surface as plan_preview.
      if (tool === "process_note" && result) {
        this.emit({ type: "plan_preview", plan: result as PlanResult });
      }
      if (tool === "confirm_plan" && result?.status === "applied") {
        this.emit({
          type: "applied",
          plan_id: String(result.plan_id ?? ""),
          signal_ids: (result.signal_ids as string[]) ?? [],
        });
      }
    } else if (t === "error") {
      this.emit({ type: "error", message: String(msg.message ?? "") });
    }
  }

  // ---------------------------------------------------------------------------
  // PCM playback (24 kHz, mono, PCM16 LE)
  // ---------------------------------------------------------------------------

  private playbackCtx: AudioContext | null = null;

  private stopPlayback() {
    // Clear any in-progress audio by closing and re-creating the context.
    this.playbackCtx?.close();
    this.playbackCtx = null;
    this.playbackTime = 0;
  }

  private playPcm(buffer: ArrayBuffer) {
    if (!this.playbackCtx) {
      this.playbackCtx = new AudioContext({ sampleRate: 24000 });
      this.playbackTime = this.playbackCtx.currentTime;
    }
    const samples = new Int16Array(buffer);
    const float32 = new Float32Array(samples.length);
    for (let i = 0; i < samples.length; i++) {
      float32[i] = samples[i] / (samples[i] < 0 ? 0x8000 : 0x7fff);
    }
    const audioBuffer = this.playbackCtx.createBuffer(1, float32.length, 24000);
    audioBuffer.copyToChannel(float32, 0);
    const source = this.playbackCtx.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(this.playbackCtx.destination);
    const startAt = Math.max(this.playbackTime, this.playbackCtx.currentTime);
    source.start(startAt);
    this.playbackTime = startAt + audioBuffer.duration;
  }
}
