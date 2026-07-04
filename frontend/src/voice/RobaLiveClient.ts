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
  | { type: "connected"; model?: string }
  | { type: "unavailable"; reason?: string }
  | { type: "reconnecting" }   // socket dropped unexpectedly; auto-reconnect in progress
  | { type: "transcript"; role: "user" | "roba"; text: string; turn_id: string; final: boolean }
  | { type: "plan_preview"; plan: PlanResult }
  | { type: "tool_result"; tool: string; result: unknown }
  | { type: "applied"; plan_id: string; signal_ids: string[]; summary?: string; tool?: string }
  | { type: "speaking" }       // first audio byte of a turn started playing
  | { type: "turn_complete" }  // server signalled the turn is done generating
  | { type: "playback_done" }  // turn done AND all audio finished playing
  | { type: "interrupted" }    // barge-in: Roba stopped, mic should stay open
  | { type: "error"; message: string }
  | { type: "disconnected" };

// Backoff delays (ms) for browser-socket auto-reconnect.
// Capped at the last value for any subsequent attempts.
const RECONNECT_DELAYS_MS = [1_000, 2_000, 5_000];

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
  // Callback set by stopListening(); invoked by the worklet "flushed" sentinel
  // (or the 250 ms fallback timer) to guarantee activity_end is sent AFTER all
  // audio has been written to the socket.
  private _finalizeStop: (() => void) | null = null;

  private _model: string | undefined;
  private _lastUserTurnId = "";
  private _lastRobaTurnId = "";
  /** Extra query params appended to the WS URL (e.g. call_id for call sessions). */
  private _extraParams: Record<string, string>;

  // Auto-reconnect for unexpected browser-socket drops.
  private _closedByUser = false;
  private _reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private _reconnectAttempt = 0;

  constructor(
    role = "manager",
    mode = "confirm",
    micMode: "ptt" | "conversation" = "ptt",
    model?: string,
    extraParams?: Record<string, string>,
  ) {
    this.role = role;
    this.mode = mode;
    this._micMode = micMode;
    this._model = model;
    this._extraParams = extraParams ?? {};
  }

  setModel(model: string | undefined) {
    this._model = model;
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
    this._closedByUser = false;
    this._reconnectAttempt = 0;
    this._openSocket();
  }

  /** Internal: open a fresh WebSocket, wiring up all handlers. */
  private _openSocket(): void {
    // Cancel any pending reconnect before opening a new socket.
    if (this._reconnectTimer !== null) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }

    const base = window.location.host;
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const params = new URLSearchParams({
      role: this.role,
      mode: this.mode,
      mic_mode: this._micMode,
    });
    if (this._model) params.set("model", this._model);
    for (const [k, v] of Object.entries(this._extraParams)) {
      params.set(k, v);
    }
    const url = `${proto}://${base}/ws/voice/live?${params.toString()}`;
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    this.ws = ws;

    ws.onopen = () => {
      // Connected — wait for the server's "connected" or "unavailable" frame.
    };

    ws.onmessage = (ev) => {
      if (this.ws !== ws) return; // stale socket
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

    ws.onclose = () => {
      if (this.ws !== ws) return; // stale socket
      this.ws = null;
      if (this._closedByUser) {
        // Intentional teardown (mode change / unmount) — inform the hook.
        this.emit({ type: "disconnected" });
      } else {
        // Unexpected drop — auto-reconnect with backoff.
        this._scheduleReconnect();
      }
    };

    ws.onerror = () => {
      if (this.ws !== ws) return; // stale socket
      // Let onclose drive reconnect; just flag the error for onerror-only events.
      // Closing a CONNECTING socket produces a browser warning, skip it.
      if (ws.readyState === WebSocket.OPEN) ws.close();
    };
  }

  private _scheduleReconnect(): void {
    if (this._reconnectTimer !== null) return; // already scheduled
    const delay =
      RECONNECT_DELAYS_MS[Math.min(this._reconnectAttempt, RECONNECT_DELAYS_MS.length - 1)];
    this._reconnectAttempt++;
    this.emit({ type: "reconnecting" });
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      if (!this._closedByUser) this._openSocket();
    }, delay);
  }

  disconnect() {
    this._closedByUser = true;
    if (this._reconnectTimer !== null) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    this.stopListening();
    this.ws?.close();
    this.ws = null;
  }

  // ---------------------------------------------------------------------------
  // Microphone capture
  // ---------------------------------------------------------------------------

  async startListening(): Promise<void> {
    if (this._listening) return;

    // Browsers only expose mediaDevices on HTTPS or localhost (secure context).
    if (!navigator.mediaDevices?.getUserMedia) {
      this.emit({
        type: "error",
        message:
          "Microphone requires HTTPS or localhost. Please open this page via https://… (accept the self-signed certificate warning).",
      });
      this._listening = false;
      return;
    }

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

      const AC: typeof AudioContext = window.AudioContext ?? (window as any).webkitAudioContext;
      ctx = new AC({ sampleRate: 48000 });
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
        if (typeof e.data === "string") {
          // "flushed" sentinel: the worklet has drained its buffer — safe to
          // send activity_end and tear down the audio graph in order.
          if (e.data === "flushed") this._finalizeStop?.();
          return;
        }
        if (this.ws?.readyState === WebSocket.OPEN) {
          // In PTT mode, signal speech start to Gemini with the very first chunk
          // so the server knows to begin buffering this turn (VAD is disabled).
          if (this._micMode === "ptt" && !this._audioSentThisTurn) {
            this.ws.send(JSON.stringify({ type: "activity_start" }));
          }
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

  /**
   * Stop listening and tear down the mic graph.
   *
   * @param opts.abort  When true, skip sending `activity_end` even if audio was
   *   captured — used for empty/accidental presses so the server never starts a
   *   generation turn and the hook can return straight to "ready".
   * @returns Whether at least one audio chunk was sent this press (useful for
   *   the hook's empty-utterance guardrail).
   */
  stopListening(opts?: { abort?: boolean }): boolean {
    if (!this._listening) return false;
    this._listening = false;
    // Increment session id so any in-flight startListening bails on its next check.
    this._micSession++;

    // Capture graph refs locally so finalize() can't race with a new startListening.
    const micSource = this.micSource;
    const workletNode = this.workletNode;
    const micStream = this.micStream;
    const audioCtx = this.audioCtx;
    const audioSentThisTurn = this._audioSentThisTurn;
    const abort = opts?.abort ?? false;

    // Clear instance fields immediately so the next startListening starts clean.
    this.micSource = null;
    this.workletNode = null;
    this.micStream = null;
    this.audioCtx = null;
    this._audioSentThisTurn = false;

    // finalize() tears down the audio graph and — crucially — sends activity_end
    // AFTER all buffered audio has been flushed, so Gemini receives audio before
    // the turn-end marker (not the reverse).  A `done` flag prevents double-run.
    let done = false;
    const finalize = () => {
      if (done) return;
      done = true;
      this._finalizeStop = null;

      micSource?.disconnect();
      workletNode?.disconnect();
      micStream?.getTracks().forEach((t) => t.stop());
      audioCtx?.close();

      // In PTT mode, activity_end is the hard turn commit (VAD is disabled).
      // Only send it if audio was actually captured this press AND we're not
      // aborting (empty/accidental press — server must not start a generation).
      // In conversation mode, Gemini's auto-VAD handles turn ends — we just
      // close the mic silently.
      if (
        !abort &&
        this._micMode === "ptt" &&
        audioSentThisTurn &&
        this.ws?.readyState === WebSocket.OPEN
      ) {
        this.ws.send(JSON.stringify({ type: "activity_end" }));
      }
    };

    if (workletNode) {
      // Ask the worklet to drain its partial buffer first.  It will post a
      // "flushed" string sentinel when done; the onmessage handler above calls
      // finalize() then.  The 250 ms fallback covers the rare case where the
      // sentinel is lost (e.g. the worklet was already torn down).
      this._finalizeStop = finalize;
      try { workletNode.port.postMessage("flush"); } catch { /* already stopped */ }
      setTimeout(finalize, 250);
    } else {
      // No worklet — nothing to flush, finalize immediately.
      finalize();
    }

    // Return whether audio was sent this turn — lets the hook skip "thinking"
    // for an accidental tap-and-immediate-release (sub-20ms, no chunks buffered).
    return audioSentThisTurn;
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
      this.emit({ type: "connected", model: msg.model as string | undefined });
    } else if (t === "unavailable") {
      this.emit({ type: "unavailable", reason: msg.reason as string | undefined });
    } else if (t === "transcript") {
      const txRole = (msg.role as "user" | "roba") ?? "roba";
      let turn_id: string;
      if (msg.turn_id != null) {
        turn_id = String(msg.turn_id);
        // Store so we can fall back to it when subsequent chunks omit turn_id.
        if (txRole === "user") this._lastUserTurnId = turn_id;
        else this._lastRobaTurnId = turn_id;
      } else {
        // Reuse the last known turn_id for this role to keep the bubble stable.
        const last = txRole === "user" ? this._lastUserTurnId : this._lastRobaTurnId;
        turn_id = last || `fallback-${Date.now()}`;
      }
      this.emit({
        type: "transcript",
        role: txRole,
        text: String(msg.text ?? ""),
        turn_id,
        final: Boolean(msg.final),
      });
    } else if (t === "plan_preview") {
      // Backend staged a write action in confirm mode — show the card.
      const plan = msg.plan as PlanResult | undefined;
      if (plan) {
        this.emit({ type: "plan_preview", plan });
      }
    } else if (t === "applied") {
      // Backend applied an action (auto mode or after spoken confirm).
      this.emit({
        type: "applied",
        plan_id: String(msg.plan_id ?? ""),
        signal_ids: (msg.signal_ids as string[]) ?? [],
        summary: String(msg.summary ?? ""),
        tool: String(msg.tool ?? ""),
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
          summary: String(result.human_readable ?? result.summary ?? "Done."),
          tool: "confirm_plan",
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
      const AC: typeof AudioContext = window.AudioContext ?? (window as any).webkitAudioContext;
      this.playbackCtx = new AC({ sampleRate: 24000 });
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
