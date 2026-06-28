/**
 * AudioWorklet processor: captures microphone input, downsamples from the
 * browser's default sample rate to 16 kHz, converts to 16-bit LE PCM, and
 * posts ArrayBuffer chunks (~20 ms) for the main thread to send over WebSocket.
 *
 * The live API expects: 16 kHz, mono, PCM16 (little-endian).
 *
 * The processor also accepts a "flush" message via port.onmessage to drain any
 * partial buffer remaining at the end of a recording session, ensuring the last
 * ~20 ms of speech isn't lost when the mic is stopped.
 */
class MicProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super(options);
    // Target: 16000 Hz; sampleRate is the AudioContext rate (usually 44100 or 48000).
    this._targetRate = 16000;
    this._ratio = sampleRate / this._targetRate;
    this._buffer = [];
    // ~20 ms at 16kHz = 320 samples per chunk.
    this._chunkSamples = Math.round(0.02 * this._targetRate);

    // Listen for a "flush" request from the main thread so we can drain any
    // partial buffer accumulated at the end of a PTT session.
    this.port.onmessage = (e) => {
      if (e.data === "flush") {
        this._flush();
      }
    };
  }

  _flush() {
    if (this._buffer.length === 0) return;
    const chunk = this._buffer.splice(0);
    const pcm = new Int16Array(chunk.length);
    for (let i = 0; i < chunk.length; i++) {
      const clamped = Math.max(-1, Math.min(1, chunk[i]));
      pcm[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    }
    this.port.postMessage(pcm.buffer, [pcm.buffer]);
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const raw = input[0]; // mono

    // Downsample by skipping samples (nearest-neighbour — good enough for speech).
    for (let i = 0; i < raw.length; i++) {
      const srcIdx = Math.floor(i * this._ratio);
      if (srcIdx < raw.length) {
        this._buffer.push(raw[srcIdx]);
      }
    }

    // Flush complete chunks.
    while (this._buffer.length >= this._chunkSamples) {
      const chunk = this._buffer.splice(0, this._chunkSamples);
      // Convert float32 → int16 LE.
      const pcm = new Int16Array(chunk.length);
      for (let i = 0; i < chunk.length; i++) {
        const clamped = Math.max(-1, Math.min(1, chunk[i]));
        pcm[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
      }
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }
    return true; // keep processor alive
  }
}

registerProcessor("mic-processor", MicProcessor);
