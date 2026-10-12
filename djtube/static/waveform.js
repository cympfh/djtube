/**
 * Scrolling waveforms for the two decks.
 * The horizontal axis is heard time: file seconds divided by the deck tempo.
 * Both decks use the same window, so matched beats line up.
 * Peaks are min/max over short file-time bins. The PCM is not kept.
 */

/** Heard seconds on each side of the playhead. Both decks share this window. */
export const WAVE_HALF_SECONDS = 4;
/** File-time width of one peak bin. */
export const WAVE_BIN_SECONDS = 0.01;
/** Main-thread slices. About this many seconds, then a timer turn. */
export const WAVE_SLICE_SECONDS = 4;
/** Columns in the full-track overview. */
export const WAVE_OVERVIEW_COLUMNS = 480;
/** beatOffset is beat 1. Every fourth beat is a bar downbeat. */
export const BEATS_PER_BAR = 4;

function positive(value) {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

/** Deck tempo. A missing or unusable rate stays at 1 so the window still has a width. */
export function heardSpanRate(rate) {
  return positive(rate) ? rate : 1;
}

function channelList(channels) {
  const list = (Array.isArray(channels) ? channels : []).filter(
    (channel) => channel && typeof channel.length === "number" && channel.length > 0,
  );
  if (!list.length) return null;
  const length = list.reduce((min, channel) => Math.min(min, channel.length), list[0].length);
  if (!(length > 0)) return null;
  return { list, length };
}

export function createPeaks(sampleCount, sampleRate, binSeconds = WAVE_BIN_SECONDS) {
  const count = Math.floor(Number(sampleCount) || 0);
  const rate = Number(sampleRate);
  const width = Number(binSeconds);
  if (!(count > 0) || !(rate > 0) || !(width > 0)) return null;
  const samplesPerBin = Math.max(1, Math.round(rate * width));
  const columns = Math.ceil(count / samplesPerBin);
  return {
    mins: new Float32Array(columns),
    maxs: new Float32Array(columns),
    samplesPerBin,
    sampleRate: rate,
    columns,
    samples: count,
    duration: count / rate,
    binSeconds: samplesPerBin / rate,
  };
}

/**
 * Min and max of the mono mix for samples [fromSample, toSample).
 * Callers pass bin-aligned ranges so each bin is written once.
 * Channel buffers are not modified.
 */
export function writePeakSlice(channels, peaks, fromSample, toSample) {
  const packed = channelList(channels);
  if (!packed || !peaks?.mins || !(peaks.samplesPerBin > 0)) return;
  const { list } = packed;
  const length = Math.min(peaks.samples, packed.length);
  const from = Math.max(0, Math.floor(fromSample));
  const to = Math.min(length, Math.floor(toSample));
  if (!(to > from)) return;
  const span = peaks.samplesPerBin;
  const scale = 1 / list.length;
  const firstBin = Math.floor(from / span);
  const lastBin = Math.floor((to - 1) / span);
  for (let bin = firstBin; bin <= lastBin && bin < peaks.columns; bin += 1) {
    const start = Math.max(from, bin * span);
    const end = Math.min(to, (bin + 1) * span, length);
    let min = Infinity;
    let max = -Infinity;
    for (let index = start; index < end; index += 1) {
      let sample = 0;
      for (let channel = 0; channel < list.length; channel += 1) {
        const value = list[channel][index];
        if (typeof value === "number" && Number.isFinite(value)) sample += value;
      }
      sample *= scale;
      if (sample < min) min = sample;
      if (sample > max) max = sample;
    }
    if (min === Infinity) continue;
    peaks.mins[bin] = min;
    peaks.maxs[bin] = max;
  }
}

export function peakSliceEnd(peaks, fromSample, budget) {
  const span = peaks.samplesPerBin;
  const from = Math.max(0, Math.floor(Math.max(0, fromSample) / span) * span);
  if (from >= peaks.samples) return { from: peaks.samples, to: peaks.samples };
  const steps = Math.max(1, Math.floor(Math.max(0, budget) / span));
  const to = Math.min(peaks.samples, from + steps * span);
  return { from, to };
}

export function overviewFromPeaks(peaks, columns = WAVE_OVERVIEW_COLUMNS) {
  if (!peaks?.columns || !peaks.mins || !peaks.maxs) return null;
  const count = Math.max(1, Math.floor(columns) || 0);
  const mins = new Float32Array(count);
  const maxs = new Float32Array(count);
  for (let index = 0; index < count; index += 1) {
    const start = Math.floor((index * peaks.columns) / count);
    const end = Math.max(start + 1, Math.min(peaks.columns, Math.floor(((index + 1) * peaks.columns) / count)));
    let min = Infinity;
    let max = -Infinity;
    for (let bin = start; bin < end; bin += 1) {
      if (peaks.mins[bin] < min) min = peaks.mins[bin];
      if (peaks.maxs[bin] > max) max = peaks.maxs[bin];
    }
    mins[index] = min === Infinity ? 0 : min;
    maxs[index] = max === -Infinity ? 0 : max;
  }
  return {
    mins,
    maxs,
    columns: count,
    duration: peaks.duration,
    binSeconds: peaks.duration / count,
  };
}

function finishPeaks(peaks) {
  peaks.overview = overviewFromPeaks(peaks);
  return peaks;
}

/** One pass over the whole buffer. Same bins as the sliced builder. */
export function reducePeaks(channels, sampleRate, binSeconds = WAVE_BIN_SECONDS) {
  const packed = channelList(channels);
  if (!packed) return null;
  const peaks = createPeaks(packed.length, sampleRate, binSeconds);
  if (!peaks) return null;
  writePeakSlice(packed.list, peaks, 0, peaks.samples);
  return finishPeaks(peaks);
}

function sliceBudget(sampleRate, sliceSamples) {
  if (positive(sliceSamples)) return Math.floor(sliceSamples);
  const rate = positive(sampleRate) ? sampleRate : 44100;
  return Math.max(1, Math.round(rate * WAVE_SLICE_SECONDS));
}

function yieldToHost() {
  return new Promise((resolve) => {
    if (typeof setTimeout === "function") setTimeout(resolve, 0);
    else resolve();
  });
}

/**
 * Min/max peaks in slices. Yields between slices so playback and the stream
 * keep getting turns. Resolves null when the signal aborts.
 */
export async function peaksFromChannels(channels, sampleRate, options = {}) {
  const packed = channelList(channels);
  if (!packed) return null;
  const peaks = createPeaks(packed.length, sampleRate, options.binSeconds);
  if (!peaks) return null;
  const signal = options.signal;
  const give = typeof options.yield === "function" ? options.yield : yieldToHost;
  const budget = sliceBudget(peaks.sampleRate, options.sliceSamples);
  let cursor = 0;
  while (cursor < peaks.samples) {
    if (signal?.aborted) return null;
    const slice = peakSliceEnd(peaks, cursor, budget);
    if (!(slice.to > cursor)) break;
    writePeakSlice(packed.list, peaks, slice.from, slice.to);
    cursor = slice.to;
    if (cursor < peaks.samples) await give();
  }
  if (signal?.aborted) return null;
  return finishPeaks(peaks);
}

export function usablePeaks(peaks) {
  if (!peaks || !(peaks.columns > 0) || !peaks.mins || !peaks.maxs) return null;
  if (peaks.mins.length !== peaks.columns || peaks.maxs.length !== peaks.columns) return null;
  if (!(peaks.samplesPerBin > 0) || !(peaks.binSeconds > 0)) return null;
  return peaks;
}

/**
 * Pixel of a file time. The playhead (position) is the center.
 * rate is file-seconds per heard second, so a faster tempo packs more file
 * time into the same window and pulls a fixed file time toward the center.
 */
export function fileTimeToX(fileTime, position, rate, width, halfSeconds = WAVE_HALF_SECONDS) {
  const span = halfSeconds * 2;
  const pixels = Number(width);
  if (!(span > 0) || !(pixels > 0) || !Number.isFinite(fileTime) || !Number.isFinite(position)) return NaN;
  const wall = (fileTime - position) / heardSpanRate(rate);
  return pixels / 2 + (wall / span) * pixels;
}

/** File time under a pixel. Inverse of fileTimeToX for the same rate and window. */
export function xToFileTime(x, position, rate, width, halfSeconds = WAVE_HALF_SECONDS) {
  const span = halfSeconds * 2;
  const pixels = Number(width);
  if (!(span > 0) || !(pixels > 0) || !Number.isFinite(x) || !Number.isFinite(position)) return NaN;
  const wall = ((x - pixels / 2) / pixels) * span;
  return position + wall * heardSpanRate(rate);
}

function knownBeat(bpm, beatOffset) {
  return (
    typeof bpm === "number" &&
    Number.isFinite(bpm) &&
    bpm > 0 &&
    typeof beatOffset === "number" &&
    Number.isFinite(beatOffset) &&
    beatOffset >= 0
  );
}

function mod(value, period) {
  const wrapped = value % period;
  return wrapped < 0 ? wrapped + period : wrapped;
}

/**
 * Beat markers inside the heard window.
 * Index 0 is the beat at beatOffset. Every BEATS_PER_BAR-th beat is strong.
 * Beats before the file start are omitted. Without bpm and beatOffset, none.
 */
export function beatLines({
  position,
  rate,
  width,
  bpm,
  beatOffset,
  duration = Infinity,
  halfSeconds = WAVE_HALF_SECONDS,
  beatsPerBar = BEATS_PER_BAR,
} = {}) {
  if (!knownBeat(bpm, beatOffset)) return [];
  const pixels = Number(width);
  if (!(pixels > 0) || !Number.isFinite(position)) return [];
  const speed = heardSpanRate(rate);
  const period = 60 / bpm;
  if (!(period > 0) || !Number.isFinite(period)) return [];
  const start = position - halfSeconds * speed;
  const end = position + halfSeconds * speed;
  let index = Math.ceil((start - beatOffset) / period - 1e-9);
  const last = Math.floor((end - beatOffset) / period + 1e-9);
  const lines = [];
  const limit = Math.min(last, index + 4000);
  for (; index <= limit; index += 1) {
    const fileTime = beatOffset + index * period;
    if (fileTime < -1e-6) continue;
    if (Number.isFinite(duration) && fileTime > duration + 1e-6) break;
    lines.push({
      index,
      fileTime,
      x: fileTimeToX(fileTime, position, speed, pixels, halfSeconds),
      strong: beatsPerBar > 0 && mod(index, beatsPerBar) === 0,
    });
  }
  return lines;
}

/** Playhead pixel on the full-track overview. 0 is the start, width is the end. */
export function overviewPlayheadX(position, duration, width) {
  const pixels = Number(width);
  const length = Number(duration);
  if (!(pixels > 0) || !(length > 0) || !Number.isFinite(position)) return 0;
  const ratio = Math.min(1, Math.max(0, position / length));
  return ratio * pixels;
}

function bitmapSize(canvas) {
  const rect = typeof canvas.getBoundingClientRect === "function" ? canvas.getBoundingClientRect() : null;
  const cssW = rect?.width || canvas.clientWidth || 0;
  const cssH = rect?.height || canvas.clientHeight || 0;
  if (!(cssW > 0) || !(cssH > 0)) return null;
  const ratio = typeof globalThis.devicePixelRatio === "number" ? globalThis.devicePixelRatio : 1;
  const dpr = Math.min(2, ratio > 0 ? ratio : 1);
  return {
    width: Math.max(1, Math.round(cssW * dpr)),
    height: Math.max(1, Math.round(cssH * dpr)),
  };
}

function prepare(canvas) {
  if (!canvas || typeof canvas.getContext !== "function") return null;
  const size = bitmapSize(canvas);
  if (!size) return null;
  if (canvas.width !== size.width) canvas.width = size.width;
  if (canvas.height !== size.height) canvas.height = size.height;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = "#0d0c0a";
  ctx.fillRect(0, 0, size.width, size.height);
  return { ctx, ...size };
}

function binSpan(peaks, left, right) {
  const binSeconds = peaks.binSeconds;
  if (!(binSeconds > 0)) return null;
  let from = Math.floor(Math.min(left, right) / binSeconds);
  let to = Math.ceil(Math.max(left, right) / binSeconds);
  if (to <= from) to = from + 1;
  if (from < 0) from = 0;
  if (to > peaks.columns) to = peaks.columns;
  if (from >= to) return null;
  return { from, to };
}

function drawPeaks(ctx, peaks, width, height, color, timeAt) {
  ctx.fillStyle = color;
  const mid = height / 2;
  const amp = height * 0.46;
  for (let x = 0; x < width; x += 1) {
    const span = binSpan(peaks, timeAt(x), timeAt(x + 1));
    if (!span) continue;
    let min = Infinity;
    let max = -Infinity;
    for (let bin = span.from; bin < span.to; bin += 1) {
      if (peaks.mins[bin] < min) min = peaks.mins[bin];
      if (peaks.maxs[bin] > max) max = peaks.maxs[bin];
    }
    if (min === Infinity) continue;
    const yMax = mid - Math.max(-1, Math.min(1, max)) * amp;
    const yMin = mid - Math.max(-1, Math.min(1, min)) * amp;
    const top = Math.min(yMax, yMin);
    ctx.fillRect(x, top, 1, Math.max(1, Math.abs(yMin - yMax)));
  }
}

export function paintDetailWave(canvas, view = {}) {
  const surface = prepare(canvas);
  if (!surface) return;
  const peaks = usablePeaks(view.peaks);
  if (!peaks) return;
  const { ctx, width, height } = surface;
  const position = Number.isFinite(view.position) ? view.position : 0;
  const rate = heardSpanRate(view.rate);
  const half = positive(view.halfSeconds) ? view.halfSeconds : WAVE_HALF_SECONDS;
  drawPeaks(ctx, peaks, width, height, view.color || "#f3efe6", (x) => xToFileTime(x, position, rate, width, half));
  const lines = beatLines({
    position,
    rate,
    width,
    bpm: view.bpm,
    beatOffset: view.beatOffset,
    duration: peaks.duration,
    halfSeconds: half,
  });
  for (const line of lines) {
    ctx.fillStyle = line.strong ? "rgba(243, 239, 230, 0.92)" : "rgba(243, 239, 230, 0.32)";
    const thickness = line.strong ? 2 : 1;
    const x = Math.round(line.x) - (line.strong ? 1 : 0);
    ctx.fillRect(x, 0, thickness, height);
  }
}

/** Shown on the lane while peaks are missing. Long tracks stay without a wave. */
export function waveStatusText(deck) {
  if (deck?.waveLong) return "長いので波形なし";
  if (deck?.wavePreparing) return "波形を準備中";
  return "";
}

/**
 * Paints one deck pair. `position` is each deck's playback position and
 * `decks[deck].rate` is its tempo. The overview length is the peak duration.
 */
export function paintDeckWaves(surfaces, input = {}) {
  const decks = input.decks || {};
  for (const deck of ["A", "B"]) {
    const source = decks[deck] || {};
    const position = Number(input.position?.[deck]);
    const view = {
      peaks: source.wave,
      position: Number.isFinite(position) ? position : 0,
      rate: source.rate,
      bpm: source.bpm,
      beatOffset: source.beatOffset,
      color: input.color?.[deck],
    };
    paintDetailWave(surfaces?.detail?.[deck], view);
    paintOverviewWave(surfaces?.overview?.[deck], view);
  }
}

export function paintOverviewWave(canvas, view = {}) {
  const surface = prepare(canvas);
  if (!surface) return;
  const detail = usablePeaks(view.peaks);
  const peaks = detail?.overview || detail;
  if (!peaks?.columns || !(detail.duration > 0)) return;
  const { ctx, width, height } = surface;
  const duration = detail.duration;
  drawPeaks(ctx, peaks, width, height, view.color || "#f3efe6", (x) => (x / width) * duration);
  const position = Number.isFinite(view.position) ? view.position : 0;
  ctx.fillStyle = "#f3efe6";
  ctx.fillRect(Math.round(overviewPlayheadX(position, duration, width)), 0, 1, height);
}
