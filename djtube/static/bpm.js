import { formatRate } from "./rate.js";

/** Heard BPM is the track base times deck tempo. Unknown base stays null. One decimal. */
export function heardBpm(base, rate) {
  if (typeof base !== "number" || !Number.isFinite(base)) return null;
  if (typeof rate !== "number" || !Number.isFinite(rate)) return null;
  return Math.round(base * rate * 10) / 10;
}

/** One decimal, or an en dash when the base BPM is unknown. */
export function formatBpm(base, rate) {
  const heard = heardBpm(base, rate);
  if (heard == null) return "–";
  return heard.toFixed(1);
}

/** Visible readout. Uses deckState.rate, not a jog or spin rate. */
export function bpmText(deckState) {
  if (deckState?.bpmMeasuring) return "計測中";
  return `${formatBpm(deckState?.bpm, deckState?.rate)} BPM`;
}

/** Slider value text. The heard BPM is added only when a base exists. */
export function tempoValueText(deckState) {
  const rate = formatRate(deckState?.rate ?? 1);
  if (heardBpm(deckState?.bpm, deckState?.rate) == null) return rate;
  return `${rate}、${bpmText(deckState)}`;
}

const BPM_MIN = 70;
const BPM_MAX = 180;
const ANALYZE_SECONDS = 30;
const MIN_SECONDS = 3;
const SILENCE_PEAK = 0.02;
const WINDOW_SEC = 0.03;
const HOP_SEC = 0.01;
const LOW_HZ = 200;
const BLUR_RADIUS = 2;
/** Skip a podcast-length file before decodeAudioData allocates it. */
export const BPM_MAX_TRACK_SECONDS = 15 * 60;
/** Compressed body larger than a long track. The PCM is never built. */
export const BPM_MAX_BYTES = 48 * 1024 * 1024;

/** Same-origin audio URL. No Range header: a short m4a body does not decode. */
export function bpmAudioRequest(prefix, id) {
  const base = String(prefix ?? "").replace(/\/$/, "");
  return { url: `${base}/api/audio/${encodeURIComponent(id ?? "")}` };
}

/** Average channels into a new mono buffer. The result does not alias the source. */
export function mixToMono(channels) {
  const list = Array.isArray(channels) ? channels.filter((channel) => channel?.length) : [];
  const first = list[0];
  if (!first) return new Float32Array(0);
  if (list.length === 1) return new Float32Array(first);
  const mono = new Float32Array(first.length);
  for (const channel of list) {
    const length = Math.min(channel.length, mono.length);
    for (let index = 0; index < length; index += 1) mono[index] += channel[index];
  }
  const scale = 1 / list.length;
  for (let index = 0; index < mono.length; index += 1) mono[index] *= scale;
  return mono;
}

const TEMPO_CENTER = 124;
const HIGH_BAND = 0.65;
const MIN_PEAK = 0.2;
/** Floor under mean-square energy so the log does not depend on window length. */
const ENERGY_FLOOR = 1e-3;

function isLoud(sample) {
  return sample >= SILENCE_PEAK || sample <= -SILENCE_PEAK;
}

function leadingSoundIndex(samples) {
  const hop = 256;
  for (let base = 0; base < samples.length; base += hop) {
    const to = Math.min(samples.length, base + hop);
    for (let index = base; index < to; index += 1) {
      if (isLoud(samples[index])) return index;
    }
  }
  return -1;
}

function leadingAcross(channels, length) {
  const hop = 256;
  for (let base = 0; base < length; base += hop) {
    const to = Math.min(length, base + hop);
    for (let index = base; index < to; index += 1) {
      for (let channel = 0; channel < channels.length; channel += 1) {
        const sample = channels[channel][index];
        if (isLoud(sample)) return index;
      }
    }
  }
  return -1;
}

/**
 * Mono mix of the leading silence plus the next ~30s.
 * The rest of each channel is not copied. origin is seconds from the file start
 * to samples[0], so a beat time in this buffer shifts back onto the file.
 */
export function analysisWindow(channels, sampleRate) {
  const list = Array.isArray(channels) ? channels.filter((channel) => channel?.length) : [];
  const first = list[0];
  const rate = Number(sampleRate);
  if (!first || !(rate > 0)) return null;
  const length = first.length;
  const sound = leadingAcross(list, length);
  if (sound < 0) return null;
  const lookback = Math.max(1, Math.round(rate * WINDOW_SEC));
  const copyStart = Math.max(0, sound - lookback);
  const copyEnd = Math.min(length, sound + Math.floor(ANALYZE_SECONDS * rate));
  if (copyEnd - copyStart < Math.floor(MIN_SECONDS * rate)) return null;
  const mono = new Float32Array(copyEnd - copyStart);
  const scale = 1 / list.length;
  for (const channel of list) {
    const limit = Math.min(channel.length, copyEnd);
    for (let index = copyStart; index < limit; index += 1) mono[index - copyStart] += channel[index] * scale;
  }
  return { samples: mono, sampleRate: rate, origin: copyStart / rate };
}

function blurOnsets(values, radius) {
  const out = new Float64Array(values.length);
  for (let index = 0; index < values.length; index += 1) {
    let sum = 0;
    let weight = 0;
    for (let offset = -radius; offset <= radius; offset += 1) {
      const at = index + offset;
      if (at < 0 || at >= values.length) continue;
      const tap = radius - Math.abs(offset) + 1;
      sum += values[at] * tap;
      weight += tap;
    }
    out[index] = weight ? sum / weight : 0;
  }
  return out;
}

/** Log-compressed positive flux. Each frame is stamped on its newest sample. */
function onsetFlux(samples, sampleRate, start, stop) {
  const hop = Math.max(1, Math.round(sampleRate * HOP_SEC));
  const win = Math.max(hop + 1, Math.round(sampleRate * WINDOW_SEC));
  const regionStart = Math.max(0, start - win);
  const regionEnd = Math.min(samples.length, stop);
  if (regionEnd - start < hop * 8) return null;
  const alpha = 1 - Math.exp((-2 * Math.PI * LOW_HZ) / sampleRate);
  const low = new Float32Array(regionEnd - regionStart);
  let state = 0;
  for (let index = regionStart; index < regionEnd; index += 1) {
    state += alpha * (samples[index] - state);
    low[index - regionStart] = state;
  }

  const frameCount = Math.floor((regionEnd - 1 - start) / hop) + 1;
  const flux = new Float64Array(frameCount);
  let previousLow = 0;
  let previousHigh = 0;
  const logEnergy = (end) => {
    if (end < regionStart) return [Math.log(ENERGY_FLOOR), Math.log(ENERGY_FLOOR)];
    const from = Math.max(regionStart, end - win + 1);
    let lowEnergy = 0;
    let highEnergy = 0;
    let count = 0;
    for (let index = from; index <= end && index < regionEnd; index += 1) {
      const lowSample = low[index - regionStart];
      const highSample = samples[index] - lowSample;
      lowEnergy += lowSample * lowSample;
      highEnergy += highSample * highSample;
      count += 1;
    }
    const scale = count > 0 ? 1 / count : 0;
    return [Math.log(ENERGY_FLOOR + lowEnergy * scale), Math.log(ENERGY_FLOOR + highEnergy * scale)];
  };
  [previousLow, previousHigh] = logEnergy(start - hop);
  for (let frame = 0; frame < frameCount; frame += 1) {
    const [lowLog, highLog] = logEnergy(start + frame * hop);
    const lowDelta = lowLog - previousLow;
    const highDelta = highLog - previousHigh;
    const lowRise = lowDelta > 0 ? lowDelta : 0;
    const highRise = highDelta > 0 ? highDelta : 0;
    flux[frame] = lowRise + HIGH_BAND * highRise;
    previousLow = lowLog;
    previousHigh = highLog;
  }
  return { flux: blurOnsets(flux, BLUR_RADIUS), hop, envelopeRate: sampleRate / hop };
}

function autocorrelation(onset, minLag, maxLag) {
  const length = onset.length;
  let mean = 0;
  for (let index = 0; index < length; index += 1) mean += onset[index];
  mean /= length || 1;
  const centered = new Float64Array(length);
  for (let index = 0; index < length; index += 1) centered[index] = onset[index] - mean;
  const scores = new Float64Array(maxLag + 1);
  for (let lag = minLag; lag <= maxLag; lag += 1) {
    let dot = 0;
    let left = 0;
    let right = 0;
    const limit = length - lag;
    for (let index = 0; index < limit; index += 1) {
      const a = centered[index];
      const b = centered[index + lag];
      dot += a * b;
      left += a * a;
      right += b * b;
    }
    const denom = Math.sqrt(left * right);
    scores[lag] = denom > 0 ? dot / denom : 0;
  }
  return scores;
}

function isLocalMax(scores, lag, minLag, maxLag) {
  const left = lag > minLag ? scores[lag - 1] : -Infinity;
  const right = lag < maxLag ? scores[lag + 1] : -Infinity;
  return scores[lag] >= left && scores[lag] >= right;
}

function scoreAt(scores, lag) {
  const index = Math.round(lag);
  if (index < 1 || index >= scores.length) return 0;
  return scores[index] > 0 ? scores[index] : 0;
}

function harmonicScore(scores, lag) {
  return scoreAt(scores, lag) + 0.5 * scoreAt(scores, lag / 2) + 0.25 * scoreAt(scores, lag / 3) + 0.35 * scoreAt(scores, lag * 2);
}

function interp(values, at) {
  const index = Math.floor(at);
  const frac = at - index;
  if (index < 0 || index >= values.length) return 0;
  const left = values[index];
  const right = index + 1 < values.length ? values[index + 1] : left;
  return left + (right - left) * frac;
}

/**
 * On-beat energy must beat the halfway off-beat.
 * Mean height alone scores a half-time grid the same as the pulses themselves.
 */
function combScore(flux, envelopeRate, bpm) {
  const period = (60 / bpm) * envelopeRate;
  if (!(period > 2)) return { score: 0, phase: 0 };
  let mean = 0;
  for (let index = 0; index < flux.length; index += 1) mean += flux[index];
  mean /= flux.length || 1;
  if (!(mean > 0)) return { score: 0, phase: 0 };
  const steps = Math.max(8, Math.round(period));
  let bestOn = -Infinity;
  let bestOff = 0;
  let bestPhase = 0;
  for (let step = 0; step < steps; step += 1) {
    const phase = (step * period) / steps;
    let on = 0;
    let off = 0;
    for (let at = phase; at < flux.length; at += period) on += interp(flux, at);
    const offPhase = phase + period / 2;
    for (let at = offPhase; at < flux.length; at += period) off += interp(flux, at);
    if (on > bestOn) {
      bestOn = on;
      bestOff = off;
      bestPhase = phase;
    }
  }
  if (!(bestOn > 0)) return { score: 0, phase: bestPhase };
  const beats = Math.max(1, (flux.length - bestPhase) / period);
  const accent = bestOn / (bestOn + Math.max(0, bestOff));
  return { score: ((bestOn / beats) * accent) / mean, phase: bestPhase };
}

function refineTempo(flux, envelopeRate, centerBpm) {
  const lag = (60 / centerBpm) * envelopeRate;
  const low = Math.max(BPM_MIN, (60 * envelopeRate) / (lag + 1.25));
  const high = Math.min(BPM_MAX, (60 * envelopeRate) / Math.max(1, lag - 1.25));
  let best = -Infinity;
  let bestBpm = Math.round(centerBpm * 20) / 20;
  let bestPhase = 0;
  const first = Math.ceil(low * 20 - 1e-6);
  const last = Math.floor(high * 20 + 1e-6);
  for (let step = first; step <= last; step += 1) {
    const next = step / 20;
    const found = combScore(flux, envelopeRate, next);
    if (found.score > best) {
      best = found.score;
      bestBpm = next;
      bestPhase = found.phase;
    }
  }
  return { bpm: bestBpm, score: best, phase: bestPhase };
}

/** True when bpm is within a few percent of fundamental × a power of two. */
function relatedByOctave(bpm, fundamental) {
  if (!(bpm > 0) || !(fundamental > 0)) return false;
  const ratio = Math.log(bpm / fundamental) / Math.log(2);
  const nearest = Math.round(ratio);
  if (nearest < -2 || nearest > 3) return false;
  const aligned = fundamental * 2 ** nearest;
  return Math.abs(bpm - aligned) / bpm < 0.04;
}

function acfNear(scores, envelopeRate, bpm) {
  if (!(bpm > 0)) return 0;
  const lag = (60 / bpm) * envelopeRate;
  let best = 0;
  for (let offset = -2; offset <= 2; offset += 1) best = Math.max(best, scoreAt(scores, lag + offset));
  return best;
}

/** Keep the harmonic winner unless the double-time grid is actually stronger. */
function chooseTempo(flux, scores, envelopeRate, baseBpm) {
  const base = refineTempo(flux, envelopeRate, baseBpm);
  const doubled = baseBpm * 2;
  if (doubled > BPM_MAX + 0.4) return base;
  const fast = refineTempo(flux, envelopeRate, Math.min(BPM_MAX, doubled));
  const acfBase = acfNear(scores, envelopeRate, base.bpm);
  const acfFast = acfNear(scores, envelopeRate, fast.bpm);
  if (acfFast < 0.2 && acfBase >= 0.3) return base;
  const fasterPulse = acfFast > acfBase * 1.15;
  const fasterComb = acfFast > acfBase * 0.75 && fast.score > base.score * 1.35;
  return fasterPulse || fasterComb ? fast : base;
}

function openingPhase(flux, envelopeRate, bpm) {
  const period = (60 / bpm) * envelopeRate;
  const steps = Math.max(12, Math.round(period * 2));
  let best = -Infinity;
  let bestPhase = 0;
  const limit = Math.min(flux.length, period * 8 + 1);
  for (let step = 0; step < steps; step += 1) {
    const phase = (step * period) / steps;
    let score = 0;
    let weight = 1;
    for (let at = phase; at < limit; at += period) {
      score += interp(flux, at) * weight;
      weight *= 0.85;
    }
    if (score > best) {
      best = score;
      bestPhase = phase;
    }
  }
  return bestPhase;
}

function nearestPeak(samples, center, radius) {
  const from = Math.max(0, center - radius);
  const to = Math.min(samples.length - 1, center + radius);
  let best = Math.max(0, Math.min(samples.length - 1, center));
  let bestValue = -1;
  for (let index = from; index <= to; index += 1) {
    const value = Math.abs(samples[index]);
    if (value > bestValue) {
      bestValue = value;
      best = index;
    }
  }
  return best;
}

function tempoAt(samples, rate, start, stop) {
  if (stop - start < Math.floor(MIN_SECONDS * rate)) return null;
  const onset = onsetFlux(samples, rate, start, stop);
  if (!onset || onset.flux.length < onset.envelopeRate * MIN_SECONDS) return null;
  const { flux, hop, envelopeRate } = onset;

  const slowBpm = 28;
  const fastBpm = 400;
  const minLag = Math.max(1, Math.floor((60 / fastBpm) * envelopeRate));
  const maxLag = Math.min(flux.length - 2, Math.ceil((60 / slowBpm) * envelopeRate));
  if (!(maxLag > minLag + 1)) return null;
  const scores = autocorrelation(flux, minLag, maxLag);

  const peaks = [];
  for (let lag = minLag; lag <= maxLag; lag += 1) {
    if (!isLocalMax(scores, lag, minLag, maxLag)) continue;
    const bpm = (60 * envelopeRate) / lag;
    const acf = scoreAt(scores, lag);
    peaks.push({ lag, bpm, acf, score: harmonicScore(scores, lag) });
  }
  if (!peaks.length) return null;

  const musical = peaks
    .filter((peak) => peak.bpm >= BPM_MIN - 0.8 && peak.bpm <= BPM_MAX + 0.8)
    .sort((a, b) => b.score - a.score);
  let primary = musical[0];
  if (primary) {
    let octave = null;
    for (const peak of musical) {
      const ratio = peak.bpm / primary.bpm;
      const related = (ratio > 1.85 && ratio < 2.15) || (ratio > 0.46 && ratio < 0.54);
      if (!related) continue;
      if (!octave || peak.acf > octave.acf) octave = peak;
    }
    if (octave && octave.acf > primary.acf * 0.8 && octave.acf < primary.acf * 1.25) {
      const primaryDist = Math.abs(Math.log(primary.bpm / TEMPO_CENTER));
      const octaveDist = Math.abs(Math.log(octave.bpm / TEMPO_CENTER));
      if (octaveDist + 0.02 < primaryDist) primary = octave;
    }
  }
  let baseBpm = 0;
  let support = 0;
  if (primary && primary.acf >= MIN_PEAK) {
    baseBpm = Math.min(BPM_MAX, Math.max(BPM_MIN, primary.bpm));
    support = primary.acf;
  } else {
    const slow = peaks
      .filter((peak) => peak.bpm >= slowBpm && peak.bpm < BPM_MIN && peak.acf >= 0.28)
      .sort((a, b) => b.acf - a.acf)[0];
    if (!slow) return null;
    const quadrupled = slow.bpm * 4;
    const doubled = slow.bpm * 2;
    if (quadrupled >= BPM_MIN && quadrupled <= BPM_MAX) baseBpm = quadrupled;
    else if (doubled >= BPM_MIN && doubled <= BPM_MAX) baseBpm = doubled;
    else return null;
    support = slow.acf;
  }

  let chosen = chooseTempo(flux, scores, envelopeRate, baseBpm);
  const dominant = peaks.reduce((best, peak) => (peak.acf > best.acf ? peak : best));
  // 4:3 and 3:2 are not octaves of the strongest period. Use that period, or nothing.
  const supportOf = Math.max(support, acfNear(scores, envelopeRate, chosen.bpm), acfNear(scores, envelopeRate, chosen.bpm / 2));
  if (!relatedByOctave(chosen.bpm, dominant.bpm) && dominant.acf > supportOf * 1.5) {
    const octaves = [2, 4]
      .map((factor) => dominant.bpm * factor)
      .filter((bpm) => bpm >= BPM_MIN && bpm <= BPM_MAX)
      .map((bpm) => refineTempo(flux, envelopeRate, bpm));
    const rescued = octaves.sort((a, b) => b.score - a.score)[0];
    if (!rescued) return null;
    const rivals = [3 / 4, 2 / 3, 3 / 2, 4 / 3].map((factor) => {
      const bpm = rescued.bpm * factor;
      if (bpm < BPM_MIN || bpm > BPM_MAX) return 0;
      return combScore(flux, envelopeRate, bpm).score;
    });
    if (!(rescued.score > Math.max(...rivals, 0) * 1.25)) return null;
    chosen = rescued;
  }
  const peak = Math.max(
    acfNear(scores, envelopeRate, chosen.bpm),
    acfNear(scores, envelopeRate, chosen.bpm / 2),
    acfNear(scores, envelopeRate, chosen.bpm / 4),
  );
  const decoyScores = [0.9, 1.12, 1.18].map((factor) => {
    const bpm = chosen.bpm * factor;
    if (bpm < BPM_MIN || bpm > BPM_MAX || Math.abs(bpm - chosen.bpm) < 4) return 0;
    return combScore(flux, envelopeRate, bpm).score;
  });
  const decoy = Math.max(...decoyScores, 0);
  const clearPulse = chosen.score > decoy * 1.35 && chosen.score > 1;
  if (!(peak >= MIN_PEAK) || !clearPulse) return null;
  if (!(chosen.bpm >= BPM_MIN && chosen.bpm <= BPM_MAX)) return null;

  const phase = openingPhase(flux, envelopeRate, chosen.bpm);
  const coarse = start + Math.round(phase * hop);
  const peakSample = nearestPeak(samples, coarse, Math.round(rate * 0.02));
  const beatOffset = Math.round((peakSample / rate) * 1e5) / 1e5;
  return { bpm: chosen.bpm, beatOffset };
}

/**
 * Track BPM and the media time of one beat.
 * Each frame is the log of mean energy, so 22050 and 44100 describe the same rise.
 * Autocorrelation picks a coarse lag, then a 0.05 BPM comb refines it.
 * A tempo that is not an octave of a stronger period is dropped.
 * Leading silence is skipped. Only the following ~30s is read.
 * A quiet opening is skipped inside that window when it has no beat.
 * Returns null when the peak is not a beat.
 */
export function analyzeBpm(samples, sampleRate) {
  const rate = Number(sampleRate);
  const length = samples?.length || 0;
  if (!(rate > 0) || length < 2) return null;

  const sound = leadingSoundIndex(samples);
  if (sound < 0) return null;
  const limit = Math.min(length, sound + Math.floor(ANALYZE_SECONDS * rate));
  for (const skipSeconds of [0, 3, 6, 9]) {
    const start = sound + Math.floor(skipSeconds * rate);
    const retryFloor = skipSeconds === 0 ? MIN_SECONDS : 15;
    if (limit - start < Math.floor(retryFloor * rate)) continue;
    const found = tempoAt(samples, rate, start, limit);
    if (found) return found;
  }
  return null;
}
