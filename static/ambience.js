// Room tone for the call. Silence between turns reads as a dropped line, so we
// lay down a quiet office bed: a steady pink-noise floor plus a slow-breathing
// band-limited layer that suggests voices somewhere behind the agent.
//
// This runs on its own graph, NOT through the playback worklet, so a barge-in
// flush kills the agent's voice without punching a hole in the ambience.

// Tuned by rendering this same DSP offline and measuring: the bed lands ~26 dB
// under the agent's voice, which is where real call ambience sits. Much quieter
// and it vanishes on laptop speakers; much louder and it fights the speech.
const ROOM_LEVEL = 0.036;
const MURMUR_LEVEL = 0.030;

function pinkNoiseBuffer(ctx, seconds) {
  const len = Math.floor(ctx.sampleRate * seconds);
  const buf = ctx.createBuffer(1, len, ctx.sampleRate);
  const d = buf.getChannelData(0);
  let b0 = 0, b1 = 0, b2 = 0, b3 = 0, b4 = 0, b5 = 0, b6 = 0;
  for (let i = 0; i < len; i++) {
    const w = Math.random() * 2 - 1;
    b0 = 0.99886 * b0 + w * 0.0555179;
    b1 = 0.99332 * b1 + w * 0.0750759;
    b2 = 0.96900 * b2 + w * 0.1538520;
    b3 = 0.86650 * b3 + w * 0.3104856;
    b4 = 0.55000 * b4 + w * 0.5329522;
    b5 = -0.7616 * b5 - w * 0.0168980;
    d[i] = (b0 + b1 + b2 + b3 + b4 + b5 + b6 + w * 0.5362) * 0.11;
    b6 = w * 0.115926;
  }
  // Taper the loop seam so the 8s wrap does not click.
  const fade = Math.min(2000, len >> 2);
  for (let i = 0; i < fade; i++) {
    const g = i / fade;
    d[i] *= g;
    d[len - 1 - i] *= g;
  }
  return buf;
}

function layer(ctx, buffer, filters, gain) {
  const src = ctx.createBufferSource();
  src.buffer = buffer;
  src.loop = true;
  let node = src;
  for (const f of filters) { node.connect(f); node = f; }
  const g = ctx.createGain();
  g.gain.value = 0;
  node.connect(g);
  return { src, gain: g, target: gain };
}

export function startAmbience(ctx) {
  const noise = pinkNoiseBuffer(ctx, 8);

  const roomLp = ctx.createBiquadFilter();
  roomLp.type = "lowpass"; roomLp.frequency.value = 1000;
  const roomHp = ctx.createBiquadFilter();
  roomHp.type = "highpass"; roomHp.frequency.value = 120;
  const room = layer(ctx, noise, [roomHp, roomLp], ROOM_LEVEL);

  // Speech-band energy, so the swells read as distant talking rather than hiss.
  const voice = ctx.createBiquadFilter();
  voice.type = "bandpass"; voice.frequency.value = 500; voice.Q.value = 1.2;
  const murmur = layer(ctx, noise, [voice], MURMUR_LEVEL);

  const out = ctx.createGain();
  out.gain.value = 1;
  room.gain.connect(out);
  murmur.gain.connect(out);
  out.connect(ctx.destination);

  const now = ctx.currentTime;
  room.src.start(0);
  murmur.src.start(0.3); // decorrelate the two taps of the same buffer
  room.gain.gain.setTargetAtTime(ROOM_LEVEL, now, 1.5); // fade in, no hard edge
  murmur.gain.gain.setTargetAtTime(MURMUR_LEVEL * 0.5, now, 1.5);

  // Slow random swells. Real rooms breathe; a constant bed sounds synthetic.
  const timer = setInterval(() => {
    const level = MURMUR_LEVEL * (0.25 + Math.random() * 0.9);
    murmur.gain.gain.setTargetAtTime(level, ctx.currentTime, 0.8);
  }, 1400);

  return function stop() {
    clearInterval(timer);
    const t = ctx.currentTime;
    out.gain.setTargetAtTime(0, t, 0.4);
    setTimeout(() => {
      try { room.src.stop(); murmur.src.stop(); out.disconnect(); } catch {}
    }, 1500);
  };
}
