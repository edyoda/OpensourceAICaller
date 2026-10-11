// Runs in the audio thread. Buffers the 128-sample render quanta into 40ms
// frames of PCM16 so we send ~25 websocket messages/sec instead of ~375.
const FRAME = 640; // 40ms @ 16kHz

class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = new Int16Array(FRAME);
    this.n = 0;
  }

  process(inputs) {
    const channel = inputs[0][0];
    if (!channel) return true;

    for (let i = 0; i < channel.length; i++) {
      const s = Math.max(-1, Math.min(1, channel[i]));
      this.buf[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      if (this.n === FRAME) {
        const frame = this.buf.slice();
        this.port.postMessage(frame.buffer, [frame.buffer]);
        this.n = 0;
      }
    }
    return true;
  }
}

registerProcessor("capture", CaptureProcessor);
