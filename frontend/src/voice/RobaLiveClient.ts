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
  | { type: "speaking" }       // first audio byte of a turn started playing
  | { type: "turn_complete" }  // server signalled the turn is done generating
  | { type: "playback_done" }  // turn done AND all audio finished playing
  | { type: "interrupted" }    // barge-in: Roba stopped, mic should stay open
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
  private _micMode: "ptt" | "conversation" = "ptt";
  private _listening = false;
  // Monotonically increasing session id — incremented on each startListening/
  // stopListening call so any in-flight startListening can detect staleness.
  private _micSession = 0;
  // True if at least one audio chunk was sent during the current mic session.
  private _audioSentThisTurn = false;

  constructor(role = "manager", mode = "confirm", micMode: "ptt" | "conversation" = "ptt") {
    this.role = role;
    this.mode = mode;
    this._micMode = micMode;
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

  setMicMode(m: "ptt" | "conversation") {
    this._micMode = m;
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

    // Capture the session id BEFORE any await so we can detect if stopListening
    // was called while we were awaiting getUserMedia or addModule.
    const session = ++this._micSession;
    this._audioSentThisTurn = false;

    let stream: MediaStream | null = null;
    let ctx: AudioContext | null = null;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, sampleRate: 48000, echoCancellation: true },
      });

      // After each await: bail if stopListening() was called in the interim.
      if (!this._listening || session !== this._micSession) {
        stream.getTracks().forEach((t) => t.stop());
        return;
      }

      ctx = new AudioContext({ sampleRate: 48000 });
      await ctx.audioWorklet.addModule("/mic-processor.js");

      if (!this._listening || session !== this._micSession) {
        ctx.close();
        stream.getTracks().forEach((t) => t.stop());
        return;
      }

      // Graph is fully wired — commit to instance fields now.
      const src = ctx.createMediaStreamSource(stream);
      const node = new AudioWorkletNode(ctx, "mic-processor");

      node.port.onmessage = (e: MessageEvent<ArrayBuffer | string>) => {
        if (typeof e.data === "string") return; // control messages
        if (this.ws?.readyState === WebSocket.OPEN) {
          this.ws.send(e.data as ArrayBuffer);
          this._audioSentThisTurn = true;
        }
      };
      src.connect(node);
      node.connect(ctx.destination);

      this.micStream = stream;
      this.audioCtx = ctx;
      this.micSource = src;
      this.workletNode = node;
    } catch (err) {
      // Only surface the error if this session is still active.
      if (session === this._micSession) {
        this._listening = false;
        ctx?.close();
        stream?.getTracks().forEach((t) => t.stop());
        this.emit({ type: "error", message: String(err) });
      } else {
        // Superseded by a stopListening() call — clean up silently.
        ctx?.close();
        stream?.getTracks().forEach((t) => t.stop());
      }
    }
  }

  stopListening(): void {
    if (!this._listening) return;
    this._listening = false;
    // Increment session id so any in-flight startListening bails on its next check.
    this._micSession++;

    // Ask the worklet to flush its partial buffer before we disconnect it.
    try { this.workletNode?.port.postMessage("flush"); } catch { /* ignore */ }

    this.micSource?.disconnect();
    this.workletNode?.disconnect();
    this.micStream?.getTracks().forEach((t) => t.stop());
    this.audioCtx?.close();
    this.micSource = null;
    this.workletNode = null;
    this.micStream = null;
    this.audioCtx = null;

    // In push-to-talk mode, send end_of_turn only when audio was actually captured.
    // In conversation mode, Gemini's auto-VAD handles turn boundaries — never send
    // end_of_turn on client stop (the conversation just ends cleanly).
    if (
      this._micMode === "ptt" &&
      this._audioSentThisTurn &&
      this.ws?.readyState === WebSocket.OPEN
    ) {
      this.ws.send(JSON.stringify({ type: "end_of_turn" }));
    }
    this._audioSentThisTurn = false;
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
    } else if (t === "turn_complete") {
      this.turnComplete = true;
      this.emit({ type: "turn_complete" });
      // No audio still playing (text-only turn or already drained) → end now.
      if (this.playbackSources === 0) this.finishTurn();
    } else if (t === "interrupted") {
      // Server-side barge-in: stop any Roba audio immediately.
      this.stopPlayback();
      this.emit({ type: "interrupted" });
    } else if (t === "error") {
      this.emit({ type: "error", message: String(msg.message ?? "") });
    }
  }

  // ---------------------------------------------------------------------------
  // PCM playback (24 kHz, mono, PCM16 LE)
  // ---------------------------------------------------------------------------

  private playbackCtx: AudioContext | null = null;
  private playbackSources = 0;   // buffers scheduled but not yet finished
  private speakingEmitted = false; // emitted "speaking" for the current turn?
  private turnComplete = false;   // server sent turn_complete for this turn?

  private stopPlayback() {
    // Clear any in-progress audio by closing and re-creating the context.
    this.playbackCtx?.close();
    this.playbackCtx = null;
    this.playbackTime = 0;
    this.playbackSources = 0;
    this.speakingEmitted = false;
    this.turnComplete = false;
  }

  // Turn finished generating AND all its audio has played out.
  private finishTurn() {
    this.speakingEmitted = false;
    this.turnComplete = false;
    this.emit({ type: "playback_done" });
  }

  private playPcm(buffer: ArrayBuffer) {
    if (!this.playbackCtx) {
      this.playbackCtx = new AudioContext({ sampleRate: 24000 });
      this.playbackTime = this.playbackCtx.currentTime;
    }
    // First audio of the turn → tell the UI Roba is speaking (clears "thinking").
    if (!this.speakingEmitted) {
      this.speakingEmitted = true;
      this.emit({ type: "speaking" });
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
    this.playbackSources += 1;
    source.onended = () => {
      this.playbackSources = Math.max(0, this.playbackSources - 1);
      // Last buffer drained and the server already closed the turn → done.
      if (this.playbackSources === 0 && this.turnComplete) this.finishTurn();
    };
    source.start(startAt);
    this.playbackTime = startAt + audioBuffer.duration;
  }
}
