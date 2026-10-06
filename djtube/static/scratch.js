// A looping chirp. Playing it faster is the キュルキュル of a backward platter spin.

import { clampSpinRate } from "./jogspin.js";

export const SCRATCH_GRAIN_SECONDS = 0.18;

export function fillScratchBuffer(channel, sampleRate) {
  const n = channel.length;
  const rate = Number(sampleRate);
  if (!n || !Number.isFinite(rate) || rate <= 0) return;
  let phase = 0;
  for (let i = 0; i < n; i += 1) {
    const u = n <= 1 ? 0 : i / (n - 1);
    const hz = 240 * 2 ** (u * 3);
    phase += (2 * Math.PI * hz) / rate;
    const tone = Math.sin(phase);
    const grit = Math.sin(i * 0.77) * Math.sin(i * 0.131);
    const env = Math.sin(Math.PI * u);
    channel[i] = (tone * 0.72 + grit * 0.28) * env * 0.9;
  }
}

export function createScratchVoice(context, output = context.destination) {
  let source = null;
  let filter = null;
  let gain = null;

  function ensure() {
    if (source) return;
    const frames = Math.max(1, Math.floor(context.sampleRate * SCRATCH_GRAIN_SECONDS));
    const buffer = context.createBuffer(1, frames, context.sampleRate);
    fillScratchBuffer(buffer.getChannelData(0), context.sampleRate);
    source = context.createBufferSource();
    source.buffer = buffer;
    source.loop = true;
    filter = context.createBiquadFilter();
    filter.type = "bandpass";
    filter.Q.value = 6;
    gain = context.createGain();
    source.connect(filter);
    filter.connect(gain);
    gain.connect(output);
    source.start();
  }

  return {
    update(scratch, level) {
      if (!scratch || !(Number(scratch.rate) > 0)) return;
      ensure();
      const heard = clampSpinRate(scratch.rate);
      if (heard != null) source.playbackRate.value = heard;
      const hz = Number(scratch.chirpHz);
      filter.frequency.value = Math.min(12000, Math.max(80, Number.isFinite(hz) ? hz : 800));
      const loud = Number(level);
      const unit = Number.isFinite(loud) ? Math.min(1, Math.max(0, loud)) : 1;
      gain.gain.value = unit * 0.85;
    },
    stop() {
      if (!source) return;
      try {
        source.stop();
      } catch {
        /* already stopped */
      }
      source.disconnect?.();
      filter?.disconnect?.();
      gain?.disconnect?.();
      source = null;
      filter = null;
      gain = null;
    },
  };
}
