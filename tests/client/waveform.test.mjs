import assert from "node:assert/strict";
import test from "node:test";

import { createActions, freshState } from "../../djtube/static/actions.js";
import { BPM_MAX_TRACK_SECONDS } from "../../djtube/static/bpm.js";
import {
  WAVE_HALF_SECONDS,
  beatLines,
  fileTimeToX,
  overviewPlayheadX,
  peaksFromChannels,
  reducePeaks,
  xToFileTime,
} from "../../djtube/static/waveform.js";

function tone({ sampleRate = 44100, seconds = 2, bursts = [] } = {}) {
  const samples = new Float32Array(Math.floor(sampleRate * seconds));
  for (const burst of bursts) {
    const origin = Math.round((burst.at ?? 0) * sampleRate);
    const length = Math.max(1, Math.round((burst.length ?? 0.02) * sampleRate));
    const amplitude = burst.amplitude ?? 0.8;
    const frequency = burst.frequency ?? 440;
    for (let index = 0; index < length && origin + index < samples.length; index += 1) {
      const time = index / sampleRate;
      samples[origin + index] += amplitude * Math.sin(2 * Math.PI * frequency * time);
    }
  }
  return samples;
}

function clicks({ bpm = 120, sampleRate = 44100, seconds = 8, lead = 0.2, amplitude = 1 } = {}) {
  const total = Math.floor(sampleRate * seconds);
  const samples = new Float32Array(total);
  const period = (sampleRate * 60) / bpm;
  for (let at = Math.round(sampleRate * lead); at < total; at += period) {
    const index = Math.round(at);
    if (index >= 0 && index < total) samples[index] = amplitude;
  }
  return samples;
}

function harness(overrides = {}) {
  const state = freshState();
  const audios = {
    A: {
      paused: true,
      currentTime: 0,
      duration: 120,
      playbackRate: 1,
      play() {
        this.paused = false;
        return Promise.resolve();
      },
      pause() {
        this.paused = true;
      },
      loadVideo() {
        return true;
      },
      cancelPendingSeek() {},
    },
    B: {
      paused: true,
      currentTime: 0,
      duration: 120,
      playbackRate: 1,
      play() {
        return Promise.resolve();
      },
      pause() {},
      loadVideo() {
        return true;
      },
      cancelPendingSeek() {},
    },
  };
  const actions = createActions({
    state,
    audios,
    prefix: "/djtube",
    scheduleRender() {},
    queryValue: () => "",
    async fetchSearch() {
      return { tracks: [] };
    },
    ...overrides,
  });
  return { state, audios, actions };
}

test("peak reduction keeps a generated burst and ignores the silent bins around it", () => {
  const sampleRate = 44100;
  const samples = tone({
    sampleRate,
    seconds: 2,
    bursts: [{ at: 1, length: 0.02, amplitude: 0.8, frequency: 440 }],
  });
  const before = samples[Math.round(sampleRate * 1)];
  const peaks = reducePeaks([samples], sampleRate);
  assert.equal(samples[Math.round(sampleRate * 1)], before);
  assert.equal(peaks.sampleRate, sampleRate);
  assert.ok(Math.abs(peaks.binSeconds - 0.01) < 1e-6);
  const bin = Math.floor(1 / peaks.binSeconds);
  assert.ok(peaks.maxs[bin] > 0.7, `burst max ${peaks.maxs[bin]}`);
  assert.ok(peaks.mins[bin] < -0.7, `burst min ${peaks.mins[bin]}`);
  assert.equal(peaks.maxs[bin - 2], 0);
  assert.equal(peaks.mins[bin - 2], 0);
  assert.equal(peaks.maxs[bin + 3], 0);
  assert.ok(peaks.overview.columns > 0);
  const overviewBin = Math.floor((1 / peaks.duration) * peaks.overview.columns);
  assert.ok(peaks.overview.maxs[overviewBin] > 0.7);
});

test("stereo peaks are the mono mix, and sliced reduction matches one pass", async () => {
  const sampleRate = 8000;
  const left = new Float32Array(sampleRate);
  const right = new Float32Array(sampleRate);
  left.fill(0.4);
  right[100] = -0.2;
  const once = reducePeaks([left, right], sampleRate);
  let yields = 0;
  const sliced = await peaksFromChannels([left, right], sampleRate, {
    sliceSamples: 1000,
    yield() {
      yields += 1;
      return Promise.resolve();
    },
  });
  assert.ok(yields >= 1);
  assert.equal(sliced.columns, once.columns);
  assert.equal(sliced.samplesPerBin, once.samplesPerBin);
  for (let index = 0; index < once.columns; index += 1) {
    assert.ok(Math.abs(sliced.mins[index] - once.mins[index]) < 1e-6);
    assert.ok(Math.abs(sliced.maxs[index] - once.maxs[index]) < 1e-6);
  }
  assert.ok(Math.abs(once.maxs[0] - 0.2) < 1e-5);
  const clickBin = Math.floor(100 / once.samplesPerBin);
  assert.ok(once.mins[clickBin] < 0.16);
  assert.ok(Math.abs(left[100] - 0.4) < 1e-6);
  assert.ok(Math.abs(right[100] + 0.2) < 1e-6);
});

test("an impulse maps to the playhead, and a tempo change keeps the playhead centered", () => {
  const sampleRate = 44100;
  const at = 1.25;
  const samples = new Float32Array(Math.floor(sampleRate * 4));
  const index = Math.round(at * sampleRate);
  samples[index] = 0.9;
  const peaks = reducePeaks([samples], sampleRate);
  const bin = Math.floor(index / peaks.samplesPerBin);
  assert.ok(peaks.maxs[bin] > 0.89);
  assert.equal(peaks.maxs[Math.max(0, bin - 1)], 0);
  const width = 800;
  const fileTime = (bin + 0.5) * peaks.binSeconds;
  const center = fileTimeToX(fileTime, at, 1, width);
  assert.ok(Math.abs(center - width / 2) < 1, `center ${center}`);
  assert.equal(fileTimeToX(at, at, 2, width), width / 2);
  const ahead = at + 1;
  assert.equal(fileTimeToX(ahead, at, 1, width), width / 2 + width / 8);
  assert.equal(fileTimeToX(ahead, at, 2, width), width / 2 + width / 16);
  const heard = fileTimeToX(ahead, at, 1.5, width);
  const later = fileTimeToX(ahead, at + 1.5, 1.5, width);
  assert.ok(Math.abs(heard - later - width / (WAVE_HALF_SECONDS * 2)) < 1e-9);
  assert.equal(fileTimeToX(ahead, ahead, 1.5, width), width / 2);
  const x = fileTimeToX(12.25, 10, 1.25, 640);
  assert.ok(Math.abs(xToFileTime(x, 10, 1.25, 640) - 12.25) < 1e-9);
});

test("beat lines use beatOffset, thicken every fourth beat, and line up across tempos", () => {
  const width = 800;
  assert.deepEqual(beatLines({ position: 10, rate: 1, width, bpm: null, beatOffset: 0 }), []);
  assert.deepEqual(beatLines({ position: 10, rate: 1, width, bpm: 120, beatOffset: null }), []);
  const lines = beatLines({ position: 0.2, rate: 1, width, bpm: 120, beatOffset: 0.2 });
  const downbeat = lines.find((line) => Math.abs(line.fileTime - 0.2) < 1e-9);
  const next = lines.find((line) => Math.abs(line.fileTime - 0.7) < 1e-9);
  const bar = lines.find((line) => Math.abs(line.fileTime - 2.2) < 1e-9);
  assert.equal(downbeat.strong, true);
  assert.equal(downbeat.x, width / 2);
  assert.equal(next.strong, false);
  assert.equal(bar.strong, true);
  assert.ok(bar.x > downbeat.x);
  const start = beatLines({ position: 0, rate: 1, width, bpm: 120, beatOffset: 0 });
  assert.ok(start.every((line) => line.fileTime >= -1e-9));
  assert.equal(start[0].x, width / 2);

  const aPosition = 8 * (60 / 126);
  const bPosition = 8 * (60 / 120);
  const alignedA = beatLines({ position: aPosition, rate: 1, width, bpm: 126, beatOffset: 0 });
  const alignedB = beatLines({ position: bPosition, rate: 1.05, width, bpm: 120, beatOffset: 0 });
  assert.ok(alignedA.length > 4);
  assert.equal(alignedA.length, alignedB.length);
  for (let index = 0; index < alignedA.length; index += 1) {
    assert.ok(Math.abs(alignedA[index].x - alignedB[index].x) < 0.02);
    assert.equal(alignedA[index].strong, alignedB[index].strong);
  }
  const slipped = beatLines({
    position: bPosition + 0.25,
    rate: 1.05,
    width,
    bpm: 120,
    beatOffset: 0,
  });
  const wallHalf = 0.25 / 1.05;
  const expected = (wallHalf / (WAVE_HALF_SECONDS * 2)) * width;
  assert.ok(Math.abs(Math.abs(slipped[0].x - alignedA[0].x) - expected) < 0.05);
  assert.ok(Math.abs(slipped.find((line) => line.index === 8).x - width / 2) > 20);
});

test("overview playhead is the file position across the track", () => {
  assert.equal(overviewPlayheadX(0, 200, 400), 0);
  assert.equal(overviewPlayheadX(100, 200, 400), 200);
  assert.equal(overviewPlayheadX(200, 200, 400), 400);
  assert.equal(overviewPlayheadX(-5, 200, 400), 0);
  assert.equal(overviewPlayheadX(999, 200, 400), 400);
  assert.equal(overviewPlayheadX(10, 0, 400), 0);
});

test("loading a track rebuilds peaks from the same decode and drops a stale result", async () => {
  const samples = clicks({ bpm: 120, seconds: 8, lead: 0.2 });
  const channel = new Float32Array(samples);
  let closed = 0;
  const fetches = [];
  const { state, actions } = harness({
    async fetch(url) {
      fetches.push(url);
      return { ok: true, arrayBuffer: async () => new ArrayBuffer(8) };
    },
    openAudioContext() {
      return {
        decodeAudioData() {
          return Promise.resolve({
            numberOfChannels: 1,
            sampleRate: 44100,
            getChannelData() {
              return channel;
            },
          });
        },
        close() {
          channel.fill(0);
          closed += 1;
          return Promise.resolve();
        },
      };
    },
  });

  await actions.loadTrack("A", { id: "abcdefghijk", title: "生成音" });
  assert.equal(state.decks.A.wave, null);
  assert.equal(state.decks.A.wavePreparing, true);
  assert.equal(state.decks.B.wave, null);
  assert.equal(state.decks.B.wavePreparing, false);

  await actions.onDeckReady("A");
  assert.equal(fetches.length, 1);
  assert.equal(fetches[0], "/djtube/api/audio/abcdefghijk");
  assert.equal(closed, 1);
  assert.equal(state.decks.A.wavePreparing, false);
  assert.equal(state.decks.A.bpmMeasuring, false);
  assert.ok(Math.abs(state.decks.A.bpm - 120) <= 0.5);
  const peaks = state.decks.A.wave;
  assert.ok(peaks.columns > 10);
  const click = Math.round(0.2 * 44100);
  const bin = Math.floor(click / peaks.samplesPerBin);
  assert.ok(peaks.maxs[bin] >= 0.9);
  assert.equal(channel[click], 0);

  let releaseFirst = null;
  const held = new Promise((resolve) => {
    releaseFirst = resolve;
  });
  const replacement = harness({
    async fetchAudio() {
      return new ArrayBuffer(4);
    },
    decodeAudio() {
      return held.then(() => ({
        samples: clicks({ bpm: 100, seconds: 8, lead: 0.1 }),
        sampleRate: 44100,
        peaks: reducePeaks([clicks({ bpm: 100, seconds: 8, lead: 0.1 })], 44100),
      }));
    },
  });
  await replacement.actions.loadTrack("A", { id: "abcdefghijk", title: "先" });
  const first = replacement.actions.onDeckReady("A");
  await replacement.actions.loadTrack("A", { id: "zzzzzzzzzzz", title: "次" });
  assert.equal(replacement.state.decks.A.wave, null);
  assert.equal(replacement.state.decks.A.wavePreparing, true);
  releaseFirst();
  await first;
  assert.equal(replacement.state.decks.A.id, "zzzzzzzzzzz");
  assert.equal(replacement.state.decks.A.wave, null);
  assert.equal(replacement.state.decks.A.bpm, null);
});

test("a track past the decode limit never stays on the preparing waveform", async () => {
  const { state, audios, actions } = harness({
    async fetch() {
      throw new Error("should not fetch");
    },
  });
  audios.A.duration = BPM_MAX_TRACK_SECONDS + 1;
  await actions.loadTrack("A", { id: "abcdefghijk", title: "長い" });
  assert.equal(state.decks.A.wavePreparing, true);
  state.decks.A.status = "preparing";
  await actions.onDeckReady("A");
  assert.equal(state.decks.A.wave, null);
  assert.equal(state.decks.A.wavePreparing, false);
  assert.equal(state.decks.A.bpm, null);
});
