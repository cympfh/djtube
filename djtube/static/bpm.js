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

const ANALYZE_SECONDS = 30;
const MIN_SECONDS = 3;
const SILENCE_PEAK = 0.02;
const WINDOW_SEC = 0.03;
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

/**
 * Percival & Tzanetakis 2014, "Streamlined Tempo Estimation Based on
 * Autocorrelation and Cross-correlation With Pulses", IEEE/ACM TASLP 22(12).
 * Audio frames are scaled so the onset signal stays at 44100/128 Hz.
 * That is the rate the paper's 7 Hz lowpass and 50–210 BPM lags assume.
 */
const PAPER_SAMPLE_RATE = 44100;
const PAPER_FRAME = 1024;
const PAPER_HOP = 128;
const OSS_RATE_PAPER = PAPER_SAMPLE_RATE / PAPER_HOP;
const OSS_FRAME = 2048;
const OSS_HOP = 128;
const BPM_LOW = 50;
const BPM_HIGH = 210;
const PEAK_COUNT = 10;
const GAUSSIAN_STD = 10;
const OCTAVE_TOLERANCE = 10;
/** Linear-phase delay of the 15-tap lowpass, in onset-signal samples. */
const FIR_DELAY = 7;
/** A pulse past this horizon is outside the train the paper correlates. */
const PULSE_SECONDS = 8;
/**
 * Best phase score divided by the mean phase score.
 * A flat onset signal has no phase that stands above the others, which the
 * paper treats as the absence of a rhythmic event. Periodic clicks sit far above this.
 */
const MIN_PHASE_PEAKINESS = 2;
/**
 * Share of the accumulator inside one standard deviation of the winning lag.
 * One agreed period puts about 0.7 there. Scattered onsets stay under 0.26.
 */
const MIN_AGREED_FRACTION = 0.28;

/** 14th-order Hamming FIR, cutoff 7 Hz at the paper's onset rate. */
const LOWPASS = [
  0.00933978, 0.01521148, 0.03163891, 0.05607187, 0.08390299, 0.10948195, 0.12742038, 0.13386527,
  0.12742038, 0.10948195, 0.08390299, 0.05607187, 0.03163891, 0.01521148, 0.00933978,
];

const SVM_MIN = [0.0321812, 1.68126e-83, 50.1745];
const SVM_MAX = [0.863237, 0.449184, 208.807];
const SVM_51 = [-1.9551, 0.4348, -4.6442, 3.2896];
const SVM_52 = [-3.0408, 2.7591, -6.5367, 3.081];
const SVM_12 = [-3.4624, 3.4397, -9.4897, 1.6297];

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

function analysisGrid(sampleRate) {
  const hop = Math.max(1, Math.round(sampleRate / OSS_RATE_PAPER));
  const scaled = PAPER_FRAME * (sampleRate / PAPER_SAMPLE_RATE);
  const frameSize = 2 ** Math.round(Math.log2(scaled));
  return { hop, frameSize, ossRate: sampleRate / hop };
}

function fft(re, im, invert) {
  const n = re.length;
  for (let i = 1, j = 0; i < n; i += 1) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      const swapRe = re[i];
      re[i] = re[j];
      re[j] = swapRe;
      const swapIm = im[i];
      im[i] = im[j];
      im[j] = swapIm;
    }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const ang = ((2 * Math.PI) / len) * (invert ? -1 : 1);
    const wlenRe = Math.cos(ang);
    const wlenIm = Math.sin(ang);
    const half = len >> 1;
    for (let i = 0; i < n; i += len) {
      let wRe = 1;
      let wIm = 0;
      for (let j = 0; j < half; j += 1) {
        const uRe = re[i + j];
        const uIm = im[i + j];
        const vRe = re[i + j + half] * wRe - im[i + j + half] * wIm;
        const vIm = re[i + j + half] * wIm + im[i + j + half] * wRe;
        re[i + j] = uRe + vRe;
        im[i + j] = uIm + vIm;
        re[i + j + half] = uRe - vRe;
        im[i + j + half] = uIm - vIm;
        const nextRe = wRe * wlenRe - wIm * wlenIm;
        wIm = wRe * wlenIm + wIm * wlenRe;
        wRe = nextRe;
      }
    }
  }
  if (invert) {
    for (let i = 0; i < n; i += 1) {
      re[i] /= n;
      im[i] /= n;
    }
  }
}

function lowpass(oss) {
  const out = new Float64Array(oss.length);
  const taps = LOWPASS.length;
  for (let index = 0; index < oss.length; index += 1) {
    let sum = 0;
    for (let tap = 0; tap < taps; tap += 1) {
      const at = index - tap;
      if (at >= 0) sum += LOWPASS[tap] * oss[at];
    }
    out[index] = sum;
  }
  return out;
}

/**
 * Positive change in log mean-square energy, at the paper's frame and hop.
 * Mean energy is what makes 22050 and 44100 describe the same rise.
 * A broadband spectral flux, which the paper uses on music, treats a kick
 * under eighth-note hats as another hat, so the tactus collapses to the hat rate.
 */
const ENERGY_FLOOR = 1e-3;

function onsetStrength(samples, sampleRate, start, stop) {
  const { hop, frameSize, ossRate } = analysisGrid(sampleRate);
  const flux = [];
  let previous = 0;
  let ready = false;
  for (let pos = start; pos + frameSize <= stop; pos += hop) {
    let energy = 0;
    for (let index = 0; index < frameSize; index += 1) {
      const sample = samples[pos + index];
      energy += sample * sample;
    }
    const logEnergy = Math.log(ENERGY_FLOOR + energy / frameSize);
    const delta = ready ? logEnergy - previous : 0;
    previous = logEnergy;
    ready = true;
    flux.push(delta > 0 ? delta : 0);
  }
  return { oss: lowpass(flux), hop, frameSize, ossRate };
}

function enhanceHarmonics(input) {
  const output = Float64Array.from(input);
  const limit = Math.floor(input.length / 4);
  for (let index = 0; index < limit; index += 1) {
    output[index] += output[index * 2] + output[index * 4];
  }
  return output;
}

function generalizedAutocorrelation(frame) {
  const size = frame.length;
  const fftSize = 2 ** Math.ceil(Math.log2(size * 2));
  const re = new Float64Array(fftSize);
  const im = new Float64Array(fftSize);
  for (let index = 0; index < size; index += 1) re[index] = frame[index];
  fft(re, im, false);
  for (let index = 0; index < fftSize; index += 1) {
    re[index] = Math.sqrt(Math.hypot(re[index], im[index]));
    im[index] = 0;
  }
  fft(re, im, true);
  return re.subarray(0, size);
}

function parabolicPeak(values, index) {
  if (index <= 0 || index >= values.length - 1) return { position: index, value: values[index] };
  const left = values[index - 1];
  const mid = values[index];
  const right = values[index + 1];
  const denom = left - 2 * mid + right;
  if (!(denom < 0)) return { position: index, value: mid };
  const delta = 0.5 * ((left - right) / denom);
  if (!(delta > -1 && delta < 1)) return { position: index, value: mid };
  return { position: index + delta, value: mid - 0.25 * (left - right) * delta };
}

function tempoPeaks(enhanced, minLag, maxLag) {
  const peaks = [];
  const last = Math.min(maxLag, enhanced.length - 2);
  for (let lag = Math.max(1, minLag); lag <= last; lag += 1) {
    if (!(enhanced[lag] > enhanced[lag - 1] && enhanced[lag] >= enhanced[lag + 1])) continue;
    const fitted = parabolicPeak(enhanced, lag);
    if (fitted.value > 0) peaks.push(fitted);
  }
  peaks.sort((a, b) => b.value - a.value);
  return peaks.slice(0, PEAK_COUNT);
}

function evaluatePulses(oss, lag, ossRate, maxBeats = Infinity) {
  const period = Math.max(1, Math.round(lag));
  const limit = Math.min(oss.length, Math.round(PULSE_SECONDS * ossRate));
  const scores = new Float64Array(period);
  const primaryMeans = new Float64Array(period);
  let total = 0;
  for (let phase = 0; phase < period; phase += 1) {
    let mag = 0;
    for (let beat = 0; beat < maxBeats; beat += 1) {
      const atBeat = phase + beat * period;
      const atHalf = phase + beat * period * 2;
      const atTriplet = phase + Math.floor((beat * period * 3) / 2);
      if (atBeat >= limit && atHalf >= limit && atTriplet >= limit) break;
      if (atBeat < limit) mag += oss[atBeat];
      if (atHalf < limit) mag += 0.5 * oss[atHalf];
      if (atTriplet < limit) mag += 0.5 * oss[atTriplet];
    }
    let primary = 0;
    let primaryCount = 0;
    for (let beat = 0; ; beat += 1) {
      const atBeat = Math.round(phase + beat * lag);
      if (atBeat >= limit) break;
      primary += oss[atBeat];
      primaryCount += 1;
    }
    scores[phase] = mag;
    primaryMeans[phase] = primaryCount ? primary / primaryCount : 0;
    total += scores[phase];
  }
  let magScore = scores[0];
  let phase = 0;
  for (let index = 1; index < period; index += 1) {
    if (scores[index] > magScore) {
      magScore = scores[index];
      phase = index;
    }
  }
  const mean = total / period;
  let variance = 0;
  for (let index = 0; index < period; index += 1) {
    const delta = scores[index] - mean;
    variance += delta * delta;
  }
  variance /= period;
  const peakiness = mean > 0 ? magScore / mean : 0;
  return { magScore, variance, phase, peakiness, primaryMean: primaryMeans[phase] };
}

function frameLag(oss, ossRate) {
  const enhanced = enhanceHarmonics(generalizedAutocorrelation(oss));
  const minLag = Math.floor((ossRate * 60) / BPM_HIGH);
  const maxLag = Math.floor((ossRate * 60) / BPM_LOW);
  const peaks = tempoPeaks(enhanced, minLag, maxLag);
  if (!peaks.length) return null;
  const scored = [];
  let magSum = 0;
  let varSum = 0;
  for (const peak of peaks) {
    const period = Math.round(peak.position);
    if (!(period > 1 && period < oss.length)) continue;
    const pulse = evaluatePulses(oss, peak.position, ossRate, 4);
    scored.push({ position: peak.position, period, acf: peak.value, ...pulse });
    magSum += pulse.magScore;
    varSum += pulse.variance;
  }
  if (!scored.length || !(magSum > 0) || !(varSum > 0)) return null;
  let best = scored[0];
  let bestScore = -Infinity;
  for (const candidate of scored) {
    const score = candidate.magScore / magSum + candidate.variance / varSum;
    if (score > bestScore) {
      bestScore = score;
      best = candidate;
    }
  }
  // Four beats of the 3/2-period train give a half-tempo candidate one extra
  // hit on a filled grid. The weight-1 pulses, followed at the fractional lag
  // for the whole frame, say whether that faster grid is actually as tall.
  const faster = scored.find((candidate) => {
    const ratio = best.period / candidate.period;
    return ratio > 1.85 && ratio < 2.15;
  });
  const slower = scored.find((candidate) => {
    const ratio = candidate.period / best.period;
    return ratio > 1.85 && ratio < 2.15;
  });
  best.slowerMean = (slower || best).primaryMean;
  best.fasterMean = (faster || best).primaryMean;
  if (faster && best.primaryMean > 0 && faster.primaryMean >= best.primaryMean * 0.93) {
    const chosen = faster;
    chosen.slowerMean = best.slowerMean;
    chosen.fasterMean = best.fasterMean;
    chosen.peakiness = best.peakiness > chosen.peakiness ? best.peakiness : chosen.peakiness;
    return chosen;
  }
  return best;
}

function accumulateLags(lags, length) {
  const accum = new Float64Array(length);
  const scale = 1 / (GAUSSIAN_STD * Math.sqrt(2 * Math.PI));
  for (const lag of lags) {
    for (let index = 0; index < length; index += 1) {
      const delta = (index - lag) / GAUSSIAN_STD;
      accum[index] += scale * Math.exp(-0.5 * delta * delta);
    }
  }
  return accum;
}

function argmax(values) {
  let best = 0;
  for (let index = 1; index < values.length; index += 1) {
    if (values[index] > values[best]) best = index;
  }
  return best;
}

function sumRange(values, low, high) {
  let start = Math.round(low);
  let end = Math.round(high);
  if (start < 0) start = 0;
  if (end > values.length - 1) end = values.length - 1;
  if (end < start) return 0;
  let sum = 0;
  for (let index = start; index <= end; index += 1) sum += values[index];
  return sum;
}

function octaveMultiplier(accum, selectedLag, bpm) {
  const total = sumRange(accum, 0, accum.length - 1);
  if (!(total > 0)) return 1;
  const under = sumRange(accum, 0, selectedLag - OCTAVE_TOLERANCE) / total;
  const half = selectedLag * 0.5;
  const around = sumRange(accum, half - OCTAVE_TOLERANCE, half + OCTAVE_TOLERANCE) / total;
  const raw = [under, around, bpm];
  const features = raw.map((value, index) => (value - SVM_MIN[index]) / (SVM_MAX[index] - SVM_MIN[index]));
  const decide = (weights) => weights[3] + features[0] * weights[0] + features[1] * weights[1] + features[2] * weights[2];
  const svm51 = decide(SVM_51);
  const svm52 = decide(SVM_52);
  const svm12 = decide(SVM_12);
  let multiplier = 1;
  if (svm52 > 0 && svm12 > 0) multiplier = 2;
  if (svm51 <= 0 && svm52 <= 0) multiplier = 0.5;
  return multiplier;
}

function beatSample(regionStart, frameIndex, phase, hop, frameSize) {
  const ossIndex = frameIndex * OSS_HOP + phase - FIR_DELAY;
  return regionStart + ossIndex * hop + frameSize / 2;
}

/**
 * Track BPM and the media time of one beat.
 * Lag picking follows Percival & Tzanetakis 2014: generalized autocorrelation,
 * pulse trains, a Gaussian accumulator, and their linear SVM.
 * The onset is log mean energy at that paper's frame rate, so 22050 and 44100 agree.
 * A half-tempo SVM decision is kept only when the slower pulses are actually taller.
 * Only the sounded region and the following ~30s are read.
 * Returns null when that region has no rhythmic pulse.
 */
export function analyzeBpm(samples, sampleRate) {
  const rate = Number(sampleRate);
  const length = samples?.length || 0;
  if (!(rate > 0) || length < 2) return null;
  const sound = leadingSoundIndex(samples);
  if (sound < 0) return null;
  const limit = Math.min(length, sound + Math.floor(ANALYZE_SECONDS * rate));
  const onset = onsetStrength(samples, rate, sound, limit);
  const { oss, hop, frameSize, ossRate } = onset;
  if (oss.length < OSS_FRAME) return null;

  const votes = [];
  for (let pos = 0; pos + OSS_FRAME <= oss.length; pos += OSS_HOP) {
    const frame = oss.subarray(pos, pos + OSS_FRAME);
    const found = frameLag(frame, ossRate);
    if (!found) continue;
    votes.push({ ...found, frameIndex: Math.round(pos / OSS_HOP) });
  }
  if (!votes.length) return null;
  const clearest = votes.reduce((best, vote) => (vote.peakiness > best.peakiness ? vote : best));
  if (!(clearest.peakiness >= MIN_PHASE_PEAKINESS)) return null;

  const maxLag = Math.floor((ossRate * 60) / BPM_LOW);
  const accum = accumulateLags(
    votes.map((vote) => vote.position),
    maxLag + 1,
  );
  const selectedLag = argmax(accum);
  if (!(accum[selectedLag] > 0) || !(selectedLag > 0)) return null;
  const refined = parabolicPeak(accum, selectedLag).position;
  const coarseBpm = (ossRate * 60) / selectedLag;
  let multiplier = octaveMultiplier(accum, selectedLag, coarseBpm);
  if (multiplier === 0.5) {
    const slowTaller = votes.filter((vote) => vote.slowerMean > vote.fasterMean * 1.1).length;
    if (slowTaller * 2 < votes.length) multiplier = 1;
  }
  const bpm = multiplier * ((ossRate * 60) / refined);
  if (!(bpm > 0) || !Number.isFinite(bpm)) return null;
  const agreed = sumRange(accum, selectedLag - GAUSSIAN_STD, selectedLag + GAUSSIAN_STD);
  const accumTotal = sumRange(accum, 0, accum.length - 1);
  if (!(accumTotal > 0) || agreed / accumTotal < MIN_AGREED_FRACTION) return null;

  const period = Math.max(1, Math.round((ossRate * 60) / bpm));
  let bestPulse = null;
  for (const vote of votes) {
    const frame = oss.subarray(vote.frameIndex * OSS_HOP, vote.frameIndex * OSS_HOP + OSS_FRAME);
    const pulse = evaluatePulses(frame, period, ossRate, 4);
    if (!bestPulse || pulse.magScore > bestPulse.magScore) bestPulse = { ...pulse, frameIndex: vote.frameIndex };
  }
  if (!bestPulse) return null;
  const at = beatSample(sound, bestPulse.frameIndex, bestPulse.phase, hop, frameSize);
  const beatOffset = Math.max(0, Math.round((at / rate) * 1e5) / 1e5);
  return { bpm, beatOffset };
}
