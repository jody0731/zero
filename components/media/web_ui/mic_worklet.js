/* AudioWorklet processor capturing mono 16-bit LE PCM chunks for live dictation.
 * Served from /ui/components/media/mic_worklet.js under script-src 'self'.
 */

class ZeroMicPcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._chunkSamples = Math.max(1600, Math.round(sampleRate * 0.2));
    this._buffer = new Int16Array(this._chunkSamples);
    this._offset = 0;
  }

  process(inputs) {
    const input = inputs && inputs[0];
    if (!input || !input.length) return true;
    const channel = input[0];
    if (!channel || !channel.length) return true;
    for (let i = 0; i < channel.length; i += 1) {
      const clamped = Math.max(-1, Math.min(1, channel[i]));
      this._buffer[this._offset] = clamped < 0
        ? Math.round(clamped * 32768)
        : Math.round(clamped * 32767);
      this._offset += 1;
      if (this._offset >= this._chunkSamples) {
        const out = this._buffer;
        this._buffer = new Int16Array(this._chunkSamples);
        this._offset = 0;
        this.port.postMessage(
          { pcm: out.buffer, sampleRate },
          [out.buffer],
        );
      }
    }
    return true;
  }
}

registerProcessor("zero-mic-pcm", ZeroMicPcmProcessor);
