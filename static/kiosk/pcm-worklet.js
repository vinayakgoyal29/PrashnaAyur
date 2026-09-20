/**
 * pcm-worklet.js
 * ==============
 * High-performance AudioWorkletProcessors for PrashnaAyur Voice Triage:
 * 1. PcmCaptureProcessor: captures microphone audio at native rate, downsamples to 16kHz PCM16.
 * 2. PcmPlaybackProcessor: ring-buffer audio playback for 24kHz/native Float32 PCM playback.
 *
 * Runs inside the browser's dedicated audio rendering thread.
 */

// ── 1. MIC CAPTURE PROCESSOR ──────────────────────────────────────────────────
const TARGET_SAMPLE_RATE = 16000; // Gemini Live API input requirement
const CHUNK_FRAMES = 1600;        // ~100ms at 16kHz (1600 samples × 2 bytes = 3200 bytes/chunk)

class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super(options);
    this._ratio = sampleRate / TARGET_SAMPLE_RATE;
    this._buffer = new Int16Array(CHUNK_FRAMES);
    this._bufferPos = 0;
    this._accumulator = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;

    const channelData = input[0];

    for (let i = 0; i < channelData.length; i++) {
      this._accumulator += 1;

      while (this._accumulator >= this._ratio) {
        this._accumulator -= this._ratio;

        const f = Math.max(-1.0, Math.min(1.0, channelData[i]));
        this._buffer[this._bufferPos++] = f < 0 ? f * 32768 : f * 32767;

        if (this._bufferPos >= CHUNK_FRAMES) {
          const transfer = this._buffer.buffer;
          this.port.postMessage(transfer, [transfer]);
          this._buffer = new Int16Array(CHUNK_FRAMES);
          this._bufferPos = 0;
        }
      }
    }

    return true;
  }
}

registerProcessor("pcm-capture-processor", PcmCaptureProcessor);


// ── 2. WORKLET-BASED PLAYBACK PROCESSOR (Ring Buffer) ────────────────────────
class PcmPlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    // 8-second buffer capacity at context sample rate (e.g. 48000 * 8 = 384,000 samples)
    this._capacity = Math.max(192000, Math.floor(sampleRate * 8));
    this._ringBuffer = new Float32Array(this._capacity);
    this._readIndex = 0;
    this._writeIndex = 0;
    this._availableSamples = 0;
    this._chunksReceived = 0;
    this._chunksPlayed = 0;
    this._isPlaying = false;

    this.port.onmessage = (event) => {
      const data = event.data;
      if (!data) return;

      if (data.command === "flush") {
        this._readIndex = 0;
        this._writeIndex = 0;
        this._availableSamples = 0;
        this._isPlaying = false;
        this.port.postMessage({ type: "flushed" });
        return;
      }

      if (data.command === "write" && data.samples) {
        const samples = data.samples;
        this._chunksReceived++;

        for (let i = 0; i < samples.length; i++) {
          if (this._availableSamples < this._capacity) {
            this._ringBuffer[this._writeIndex] = samples[i];
            this._writeIndex = (this._writeIndex + 1) % this._capacity;
            this._availableSamples++;
          }
        }

        if (!this._isPlaying && this._availableSamples > 0) {
          this._isPlaying = true;
          this.port.postMessage({ type: "playback_started" });
        }

        this.port.postMessage({
          type: "diagnostic",
          queueLength: this._availableSamples,
          chunksReceived: this._chunksReceived,
        });
      }
    };
  }

  process(inputs, outputs) {
    const output = outputs[0];
    if (!output || !output[0]) return true;

    const channel = output[0];
    const framesToRender = channel.length;

    let framesRendered = 0;
    while (framesRendered < framesToRender && this._availableSamples > 0) {
      channel[framesRendered] = this._ringBuffer[this._readIndex];
      this._readIndex = (this._readIndex + 1) % this._capacity;
      this._availableSamples--;
      framesRendered++;
    }

    // Fill remaining render quantum with silence if queue is starved
    while (framesRendered < framesToRender) {
      channel[framesRendered] = 0.0;
      framesRendered++;
    }

    if (this._isPlaying && this._availableSamples === 0) {
      this._isPlaying = false;
      this._chunksPlayed++;
      this.port.postMessage({
        type: "drained",
        chunksPlayed: this._chunksPlayed,
      });
    }

    return true;
  }
}

registerProcessor("pcm-playback-processor", PcmPlaybackProcessor);
