// Jitter buffer for the agent's voice. Holds decoded PCM and drains it at the
// device clock; `flush` empties it instantly, which is what makes barge-in
// sound like an interruption rather than a delayed stop.
class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.offset = 0;
    this.port.onmessage = (e) => {
      if (e.data === "flush") {
        this.queue = [];
        this.offset = 0;
      } else {
        this.queue.push(new Float32Array(e.data));
      }
    };
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    let written = 0;

    while (written < out.length && this.queue.length) {
      const head = this.queue[0];
      const take = Math.min(out.length - written, head.length - this.offset);
      out.set(head.subarray(this.offset, this.offset + take), written);
      written += take;
      this.offset += take;
      if (this.offset >= head.length) {
        this.queue.shift();
        this.offset = 0;
      }
    }
    out.fill(0, written); // underrun -> silence, never a click
    return true;
  }
}

registerProcessor("playback", PlaybackProcessor);
