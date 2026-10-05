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
const ENVELOPE_HZ = 400;
const SILENCE_PEAK = 0.02;
const CLEAR_PEAK = 0.28;

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

function leadingSoundIndex(samples) {
  for (let index = 0; index < samples.length; index += 1) {
    const sample = samples[index];
    if (sample >= SILENCE_PEAK || sample <= -SILENCE_PEAK) return index;
  }
  return 0;
}

function onsetEnvelope(samples, start, end, hop) {
  const frames = Math.floor((end - start) / hop);
  const energy = new Float64Array(frames);
  for (let frame = 0; frame < frames; frame += 1) {
    const at = start + frame * hop;
    let sum = 0;
    for (let index = 0; index < hop; index += 1) {
      const sample = samples[at + index];
      sum += sample * sample;
    }
    energy[frame] = sum / hop;
  }
  const onset = new Float64Array(frames);
  let previous = 0;
  for (let frame = 0; frame < frames; frame += 1) {
    const delta = energy[frame] - previous;
    onset[frame] = delta > 0 ? delta : 0;
    previous = energy[frame];
  }
  return onset;
}

function autocorrelation(onset, minLag, maxLag) {
  const length = onset.length;
  let mean = 0;
  for (let index = 0; index < length; index += 1) mean += onset[index];
  mean /= length;
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

function tempoWeight(bpm) {
  const octaves = Math.log(bpm / 124) / Math.log(2);
  return Math.exp(-0.5 * (octaves / 0.55) ** 2);
}

function isLocalMax(scores, lag, minLag, maxLag) {
  const left = lag > minLag ? scores[lag - 1] : -Infinity;
  const right = lag < maxLag ? scores[lag + 1] : -Infinity;
  return scores[lag] >= left && scores[lag] >= right;
}

function nearestLocalMax(scores, lag, minLag, maxLag) {
  let current = lag;
  for (let guard = 0; guard < 6; guard += 1) {
    const left = current > minLag ? scores[current - 1] : -Infinity;
    const right = current < maxLag ? scores[current + 1] : -Infinity;
    if (left > scores[current] && left >= right) current -= 1;
    else if (right > scores[current]) current += 1;
    else break;
  }
  return current;
}

function neighborhoodPeak(scores, lag, radius, minLag, maxLag) {
  let bestLag = -1;
  let best = -Infinity;
  const from = Math.max(minLag, lag - radius);
  const to = Math.min(maxLag, lag + radius);
  for (let index = from; index <= to; index += 1) {
    if (scores[index] > best) {
      best = scores[index];
      bestLag = index;
    }
  }
  return { lag: bestLag, score: best };
}

/** A strong peak at half the period is the beat; the longer lag is the half-time. */
function preferBeatLag(scores, lag, minLag, maxLag) {
  let chosen = lag;
  for (let step = 0; step < 2; step += 1) {
    let switched = false;
    for (const divisor of [2, 3]) {
      const faster = Math.round(chosen / divisor);
      if (faster < minLag) continue;
      const alt = neighborhoodPeak(scores, faster, 2, minLag, maxLag);
      if (alt.lag < 0 || !isLocalMax(scores, alt.lag, minLag, maxLag)) continue;
      if (alt.score > scores[chosen] * 0.55) {
        chosen = alt.lag;
        switched = true;
      }
    }
    if (!switched) break;
  }
  return chosen;
}

function refineLag(scores, lag, minLag, maxLag) {
  if (lag <= minLag || lag >= maxLag) return lag;
  const y0 = scores[lag - 1];
  const y1 = scores[lag];
  const y2 = scores[lag + 1];
  const denom = y0 - 2 * y1 + y2;
  if (!(denom < 0)) return lag;
  const delta = (0.5 * (y0 - y2)) / denom;
  if (delta < -0.5 || delta > 0.5) return lag;
  return lag + delta;
}

function beatPhase(onset, lag) {
  const beats = Math.max(1, Math.min(8, Math.floor((onset.length - 1) / lag)));
  const limit = Math.min(onset.length, beats * lag + 1);
  let bestPhase = 0;
  let bestScore = -Infinity;
  for (let phase = 0; phase < lag; phase += 1) {
    let score = 0;
    let weight = 1;
    for (let index = phase; index < limit; index += lag) {
      score += onset[index] * weight;
      weight *= 0.85;
    }
    if (score > bestScore) {
      bestScore = score;
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

/**
 * Track BPM and the media time of one beat.
 * Uses the energy envelope and autocorrelation from 70 to 180.
 * Leading silence is skipped. Only the following ~30s is read.
 * Returns null when the peak is not a beat.
 */
export function analyzeBpm(samples, sampleRate) {
  const rate = Number(sampleRate);
  const length = samples?.length || 0;
  if (!(rate > 0) || length < 2) return null;

  const start = leadingSoundIndex(samples);
  const take = Math.min(length - start, Math.floor(ANALYZE_SECONDS * rate));
  if (take < Math.floor(MIN_SECONDS * rate)) return null;

  const hop = Math.max(1, Math.round(rate / ENVELOPE_HZ));
  const onset = onsetEnvelope(samples, start, start + take, hop);
  const envelopeRate = rate / hop;
  if (onset.length < envelopeRate * MIN_SECONDS) return null;

  const minLag = Math.max(1, Math.floor((60 / BPM_MAX) * envelopeRate));
  const maxLag = Math.min(onset.length - 2, Math.ceil((60 / BPM_MIN) * envelopeRate));
  if (!(maxLag > minLag + 1)) return null;

  const scores = autocorrelation(onset, minLag, maxLag);
  let bestLag = -1;
  let best = -Infinity;
  for (let lag = minLag; lag <= maxLag; lag += 1) {
    if (!isLocalMax(scores, lag, minLag, maxLag)) continue;
    const bpm = (60 * envelopeRate) / lag;
    const weighted = scores[lag] * tempoWeight(bpm);
    if (weighted > best) {
      best = weighted;
      bestLag = lag;
    }
  }
  if (bestLag < 0) return null;
  bestLag = preferBeatLag(scores, nearestLocalMax(scores, bestLag, minLag, maxLag), minLag, maxLag);
  const peak = scores[bestLag];
  if (!(peak >= CLEAR_PEAK)) return null;

  let mean = 0;
  let count = 0;
  for (let lag = minLag; lag <= maxLag; lag += 1) {
    mean += scores[lag];
    count += 1;
  }
  mean /= count;
  if (!(peak > mean * 2)) return null;

  const refined = refineLag(scores, bestLag, minLag, maxLag);
  const raw = (60 * envelopeRate) / refined;
  if (!(raw >= BPM_MIN - 1 && raw <= BPM_MAX + 1)) return null;
  const bpm = Math.min(BPM_MAX, Math.max(BPM_MIN, Math.round(raw * 10) / 10));

  const phase = beatPhase(onset, bestLag);
  const coarse = start + phase * hop;
  const peakSample = nearestPeak(samples, coarse, hop);
  const beatOffset = Math.round((peakSample / rate) * 1e5) / 1e5;
  return { bpm, beatOffset };
}
