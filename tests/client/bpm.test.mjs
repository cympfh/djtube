import assert from "node:assert/strict";
import test from "node:test";

import { createActions, freshState } from "../../djtube/static/actions.js";
import {
  BPM_MAX_BYTES,
  BPM_MAX_TRACK_SECONDS,
  analysisWindow,
  analyzeBpm,
  bpmAudioRequest,
  bpmText,
  heardBpm,
  mixToMono,
} from "../../djtube/static/bpm.js";

function clickTrack({ bpm = 120, sampleRate = 44100, lead = 0, seconds = 8, amplitude = 1 } = {}) {
  const total = Math.floor(sampleRate * (lead + seconds));
  const samples = new Float32Array(total);
  const period = (sampleRate * 60) / bpm;
  const first = Math.round(sampleRate * lead);
  for (let at = first; at < total; at += period) {
    const index = Math.round(at);
    if (index >= 0 && index < total) samples[index] = amplitude;
  }
  return samples;
}

function beatError(offset, first, bpm) {
  const period = 60 / bpm;
  let delta = (offset - first) % period;
  if (delta < 0) delta += period;
  if (delta > period / 2) delta -= period;
  return Math.abs(delta);
}

function assertClick(result, bpm, lead) {
  assert.ok(result, `expected a BPM near ${bpm}`);
  assert.ok(Math.abs(result.bpm - bpm) <= 0.5, `bpm ${result.bpm} vs ${bpm}`);
  assert.equal(typeof result.beatOffset, "number");
  assert.ok(result.beatOffset >= 0);
  const error = beatError(result.beatOffset, lead, bpm);
  assert.ok(error <= 0.02, `beat ${result.beatOffset} vs ${lead} (err ${error})`);
}

function drumTrack({
  bpm,
  seconds = 40,
  kick = [1, 0, 0, 0],
  hat = 0,
  hatAmp = 0.3,
  snare = false,
  swing = 0,
  seed = 7,
  sampleRate = 44100,
} = {}) {
  const total = Math.floor(sampleRate * seconds);
  const samples = new Float32Array(total);
  const beat = 60 / bpm;
  let state = seed;
  const noise = () => ((state = (state * 1664525 + 1013904223) >>> 0) / 4294967296) * 2 - 1;
  const add = (time, frequency, amplitude, decay, noisy) => {
    const origin = Math.round(time * sampleRate);
    const limit = Math.min(total - origin, Math.floor(sampleRate * 0.25));
    for (let index = 0; index < limit; index += 1) {
      const secondsAt = index / sampleRate;
      const wave = noisy ? noise() : Math.sin(2 * Math.PI * frequency * secondsAt);
      samples[origin + index] += amplitude * Math.exp(-secondsAt / decay) * wave;
    }
  };
  for (let step = 0; step * beat < seconds - 1; step += 1) {
    const time = 0.5 + step * beat;
    if (kick[step % kick.length]) add(time, 55, 0.8, 0.06, false);
    if (snare && step % 2 === 1) add(time, 0, 0.5, 0.05, true);
    if (hat) {
      for (let hit = 0; hit < hat; hit += 1) {
        const swingAt = hit ? beat * (hit / hat + (hit % 2 ? swing : 0)) : 0;
        add(time + swingAt, 0, hatAmp, 0.01, true);
      }
    }
  }
  return samples;
}

test("drum patterns read within 0.2 BPM", () => {
  const cases = [
    ["decaying kicks", { bpm: 120, kick: [1] }],
    ["kick every bar", { bpm: 120 }],
    ["kick and snare", { bpm: 120, snare: true }],
    ["eighth hats", { bpm: 90, hat: 2 }],
    ["backbeat and eighth hats", { bpm: 90, kick: [1, 0, 1, 0], snare: true, hat: 2 }],
    ["sixteenth hats", { bpm: 85, kick: [1, 0, 1, 0], snare: true, hat: 4, hatAmp: 0.2 }],
    ["offbeat hats", { bpm: 128, hat: 2, hatAmp: 0.4 }],
    ["quiet hats", { bpm: 128, hat: 2, hatAmp: 0.05 }],
    ["fast kick and snare", { bpm: 174, kick: [1, 0, 0, 0], snare: true, hat: 2 }],
    ["swung hats", { bpm: 100, kick: [1, 0, 1, 0], snare: true, hat: 2, swing: 0.08 }],
    ["slow backbeat", { bpm: 75, kick: [1, 0, 0, 0], snare: true }],
    ["half-time hats", { bpm: 140, kick: [1, 0, 0, 0], hat: 2 }],
    ["slow kick and snare", { bpm: 70, snare: true }],
  ];
  for (const [name, options] of cases) {
    const high = analyzeBpm(drumTrack({ ...options, sampleRate: 44100 }), 44100);
    const low = analyzeBpm(drumTrack({ ...options, sampleRate: 22050 }), 22050);
    assert.ok(high, name);
    assert.ok(Math.abs(high.bpm - options.bpm) <= 0.2, `${name} ${high.bpm}`);
    assert.ok(low, `${name} at 22050`);
    assert.ok(Math.abs(high.bpm - low.bpm) <= 0.1, `${name} ${high.bpm} vs ${low.bpm}`);
    assert.ok(beatError(high.beatOffset, low.beatOffset, options.bpm) <= 0.05, `${name} beat`);
  }
});

test("the same clicks agree within 0.1 BPM at 22050 and 44100", () => {
  for (const bpm of [70, 120, 174]) {
    const lead = 0.2;
    const high = analyzeBpm(clickTrack({ bpm, sampleRate: 44100, lead, seconds: 10 }), 44100);
    const low = analyzeBpm(clickTrack({ bpm, sampleRate: 22050, lead, seconds: 10 }), 22050);
    assert.ok(high && low, String(bpm));
    assert.ok(Math.abs(high.bpm - bpm) <= 0.5, `${bpm} ${high.bpm}`);
    assert.ok(Math.abs(high.bpm - low.bpm) <= 0.1, `${high.bpm} vs ${low.bpm}`);
    assert.ok(beatError(high.beatOffset, low.beatOffset, bpm) <= 0.05);
    // Percival & Tzanetakis: BPM = onsetRate * 60 / lag, onsetRate = 44100/128.
    const onsetRate = 44100 / 128;
    const lag = (onsetRate * 60) / high.bpm;
    assert.ok(Math.abs(lag - (onsetRate * 60) / bpm) < 0.5, `${bpm} lag ${lag}`);
  }
});

test("irregular onsets are not reported as a tempo", () => {
  const sampleRate = 44100;
  const samples = new Float32Array(sampleRate * 12);
  let state = 3;
  const next = () => ((state = (state * 1664525 + 1013904223) >>> 0) / 4294967296);
  let at = sampleRate * 0.3;
  while (at < samples.length - 10) {
    samples[Math.floor(at)] = 1;
    at += sampleRate * (0.15 + next() * 0.7);
  }
  assert.equal(analyzeBpm(samples, sampleRate), null);
});

test("energy autocorrelation reads a click track and ignores the tail and silence", () => {
  assert.deepEqual(Object.keys(bpmAudioRequest("/djtube/", "abcdefghijk")), ["url"]);
  assert.equal(bpmAudioRequest("/djtube/", "abcdefghijk").url, "/djtube/api/audio/abcdefghijk");
  assert.equal(bpmAudioRequest("/djtube", "a/b").url, "/djtube/api/audio/a%2Fb");

  const left = clickTrack({ bpm: 100, lead: 0.2, seconds: 6 });
  const right = new Float32Array(left.length);
  const mixed = mixToMono([left, right]);
  assert.equal(mixed.length, left.length);
  assert.ok(Math.abs(mixed[Math.round(0.2 * 44100)] - 0.5) < 1e-6);
  assertClick(analyzeBpm(mixed, 44100), 100, 0.2);

  for (const bpm of [70, 87, 96, 120, 128, 140, 174, 180]) {
    const lead = bpm % 2 === 0 ? 0.35 : 0;
    assertClick(analyzeBpm(clickTrack({ bpm, lead, seconds: 8 }), 44100), bpm, lead);
  }
  assertClick(analyzeBpm(clickTrack({ bpm: 180, sampleRate: 48000, lead: 0.35, seconds: 8 }), 48000), 180, 0.35);

  const sampleRate = 44100;
  const lead = 0.2;
  const head = clickTrack({ bpm: 120, sampleRate, lead, seconds: 30, amplitude: 0.5 });
  const tail = clickTrack({ bpm: 70, sampleRate, lead: 0, seconds: 20, amplitude: 1 });
  const joined = new Float32Array(head.length + tail.length);
  joined.set(head, 0);
  joined.set(tail, head.length);
  assertClick(analyzeBpm(joined, sampleRate), 120, lead);

  assert.equal(analyzeBpm(new Float32Array(sampleRate * 6), sampleRate), null);
  assert.equal(analyzeBpm(clickTrack({ seconds: 1 }), sampleRate), null);
  const noise = new Float32Array(sampleRate * 6);
  let seed = 1;
  for (let index = 0; index < noise.length; index += 1) {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    noise[index] = (seed / 4294967296) * 2 - 1;
  }
  assert.equal(analyzeBpm(noise, sampleRate), null);

  const rate = 44100;
  const long = new Float32Array(rate * 90);
  long.set(clickTrack({ bpm: 120, sampleRate: rate, lead: 2, seconds: 36 }), 0);
  for (let index = rate * 40; index < long.length; index += rate) long[index] = 1;
  const windowed = analysisWindow([long, new Float32Array(long.length)], rate);
  assert.ok(windowed.samples.length < rate * 31);
  assert.ok(windowed.samples.length > rate * 29);
  assert.ok(windowed.origin > 1.9 && windowed.origin <= 2);
  const windowedBpm = analyzeBpm(windowed.samples, rate);
  assert.ok(Math.abs(windowedBpm.bpm - 120) <= 0.5);
  assert.ok(beatError(windowedBpm.beatOffset + windowed.origin, 2, 120) <= 0.02);
  assert.ok(Math.abs(windowed.samples[Math.round((2 - windowed.origin) * rate)] - 0.5) < 1e-6);
});

function harness(overrides = {}) {
  const state = freshState();
  const audios = { A: fakeAudio(), B: fakeAudio() };
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

function fakeAudio() {
  return {
    paused: true,
    currentTime: 0,
    duration: 120,
    volume: 1,
    playbackRate: 1,
    videoId: "",
    play() {
      this.paused = false;
      return Promise.resolve();
    },
    pause() {
      this.paused = true;
    },
    loadVideo(id) {
      this.videoId = id;
      return true;
    },
    cancelPendingSeek() {},
  };
}

function spyDeck(audio) {
  const spy = { plays: 0, seeks: 0, graph: 0, eq: 0, liveClosed: 0 };
  const play = audio.play.bind(audio);
  audio.play = () => {
    spy.plays += 1;
    return play();
  };
  let time = 0;
  Object.defineProperty(audio, "currentTime", {
    configurable: true,
    get() {
      return time;
    },
    set(value) {
      spy.seeks += 1;
      time = value;
    },
  });
  audio._ensureGraph = () => {
    spy.graph += 1;
    return false;
  };
  audio.setEqGain = () => {
    spy.eq += 1;
    return true;
  };
  audio._context = {
    close() {
      spy.liveClosed += 1;
    },
  };
  return spy;
}

function deferredJobs() {
  const jobs = [];
  return {
    jobs,
    fetchAudio(id, { signal } = {}) {
      return new Promise((resolve, reject) => {
        const job = { id, settled: false };
        const abort = () => {
          if (job.settled) return;
          job.settled = true;
          const error = new Error("aborted");
          error.name = "AbortError";
          reject(error);
        };
        if (signal?.aborted) {
          abort();
          return;
        }
        signal?.addEventListener("abort", abort, { once: true });
        job.finish = (bytes) => {
          if (job.settled) return;
          job.settled = true;
          resolve(bytes);
        };
        jobs.push(job);
      });
    },
  };
}

test("ready deck measures BPM without playing, seeking, or the live graph", async () => {
  const samples = clickTrack({ bpm: 120, lead: 0.35, seconds: 8 });
  const fetchCalls = [];
  const { state, audios, actions } = harness({
    async fetch(url, options) {
      fetchCalls.push({ url, options });
      return {
        ok: true,
        arrayBuffer: async () => new Uint8Array([9, 8, 7]).buffer,
      };
    },
    async decodeAudio(bytes) {
      assert.equal(new Uint8Array(bytes)[0], 9);
      assert.equal(bytes.byteLength, 3);
      return { samples, sampleRate: 44100 };
    },
  });
  const spy = spyDeck(audios.A);
  audios.B._ensureGraph = () => {
    throw new Error("other deck");
  };
  state.decks.B.bpm = 99;
  state.decks.B.beatOffset = 0.2;

  await actions.loadTrack("A", { id: "abcdefghijk", title: "曲" });
  const eqAfterLoad = spy.eq;
  state.decks.A.status = "preparing";
  actions.togglePlay("A");
  assert.equal(state.decks.A.playing, false);
  actions.setRate("A", 1.5);

  const pending = actions.onDeckReady("A");
  assert.equal(state.decks.A.status, "ready");
  assert.equal(state.decks.A.bpmMeasuring, true);
  assert.equal(bpmText(state.decks.A), "計測中");
  assert.equal(spy.plays, 0);
  actions.togglePlay("A");
  assert.equal(spy.plays, 1);
  assert.equal(state.decks.A.playing, true);
  assert.equal(bpmText(state.decks.A), "計測中");
  await pending;

  assert.equal(fetchCalls.length, 1);
  assert.equal(fetchCalls[0].url, "/djtube/api/audio/abcdefghijk");
  assert.deepEqual(Object.keys(fetchCalls[0].options), ["signal"]);
  assert.equal(fetchCalls[0].options.headers, undefined);
  assert.ok(Math.abs(state.decks.A.bpm - 120) <= 0.5);
  assert.ok(beatError(state.decks.A.beatOffset, 0.35, 120) <= 0.02);
  assert.equal(state.decks.A.bpmMeasuring, false);
  assert.equal(state.decks.A.rate, 1.5);
  assert.equal(bpmText(state.decks.A), `${heardBpm(state.decks.A.bpm, 1.5).toFixed(1)} BPM`);
  assert.equal(state.decks.A.playing, true);
  assert.equal(spy.plays, 1);
  assert.equal(spy.seeks, 0);
  assert.equal(spy.graph, 0);
  assert.equal(spy.eq, eqAfterLoad);
  assert.equal(spy.liveClosed, 0);
  assert.equal(state.decks.B.bpm, 99);
  assert.equal(state.decks.B.beatOffset, 0.2);

  await actions.onDeckReady("A");
  assert.equal(fetchCalls.length, 1);
});

test("a late or failed measurement does not stick, and the temporary context closes", async () => {
  const fast = clickTrack({ bpm: 128, lead: 0.1, seconds: 8 });
  const slow = clickTrack({ bpm: 90, lead: 0.4, seconds: 8 });
  let decoded = null;
  const { jobs, fetchAudio } = deferredJobs();
  const { state, audios, actions } = harness({
    fetchAudio,
    async decodeAudio(bytes) {
      const marker = new Uint8Array(bytes)[0];
      if (marker === 1) {
        decoded = { samples: slow, sampleRate: 44100 };
        return new Promise((resolve) => {
          decoded.resolve = () => resolve(decoded);
        });
      }
      return { samples: fast, sampleRate: 44100 };
    },
  });
  const spy = spyDeck(audios.A);

  await actions.loadTrack("A", { id: "abcdefghijk", title: "先" });
  const first = actions.onDeckReady("A");
  await Promise.resolve();
  assert.equal(jobs.length, 1);
  jobs[0].finish(new Uint8Array([1]).buffer);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(typeof decoded.resolve, "function");
  assert.equal(bpmText(state.decks.A), "計測中");

  await actions.loadTrack("A", { id: "zzzzzzzzzzz", title: "次" });
  assert.equal(state.decks.A.bpm, null);
  assert.equal(state.decks.A.bpmMeasuring, false);
  assert.equal(bpmText(state.decks.A), "– BPM");
  decoded.resolve();
  await first;
  assert.equal(state.decks.A.id, "zzzzzzzzzzz");
  assert.equal(state.decks.A.bpm, null);
  assert.equal(state.decks.A.beatOffset, null);
  assert.equal(spy.plays, 0);
  assert.equal(spy.seeks, 0);

  state.decks.A.status = "preparing";
  const second = actions.onDeckReady("A");
  assert.equal(state.decks.A.status, "ready");
  await Promise.resolve();
  assert.equal(jobs.length, 2);
  assert.equal(jobs[1].id, "zzzzzzzzzzz");
  jobs[1].finish(new Uint8Array([2]).buffer);
  await second;
  assert.ok(Math.abs(state.decks.A.bpm - 128) <= 0.5);
  assert.equal(spy.plays, 0);

  const again = actions.onDeckReady("A");
  await again;
  assert.equal(jobs.length, 2);

  actions.onDeckError("A");
  assert.equal(state.decks.A.bpm, null);
  assert.equal(state.decks.A.beatOffset, null);
  assert.equal(state.decks.A.bpmMeasuring, false);
  assert.equal(bpmText(state.decks.A), "– BPM");
  await actions.onDeckReady("A");
  await Promise.resolve();
  assert.equal(jobs.length, 2);
  assert.equal(state.decks.A.bpm, null);
});

test("a successful decode copies PCM before the temporary AudioContext closes", async () => {
  const samples = clickTrack({ bpm: 120, lead: 0.25, seconds: 8 });
  const channel = new Float32Array(samples);
  const log = { closed: 0 };
  const { state, actions } = harness({
    async fetch(_url, options) {
      assert.equal(options.headers, undefined);
      return { ok: true, arrayBuffer: async () => new ArrayBuffer(8) };
    },
    openAudioContext() {
      return {
        decodeAudioData(bytes) {
          assert.ok(bytes.byteLength > 0);
          assert.ok(bytes !== channel.buffer);
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
          log.closed += 1;
          return Promise.resolve();
        },
      };
    },
  });
  await actions.loadTrack("A", { id: "abcdefghijk", title: "曲" });
  await actions.onDeckReady("A");
  assert.equal(log.closed, 1);
  assert.equal(channel[Math.round(0.25 * 44100)], 0);
  assert.ok(Math.abs(state.decks.A.bpm - 120) <= 0.5);
  assert.ok(beatError(state.decks.A.beatOffset, 0.25, 120) <= 0.02);
  assert.equal(state.decks.A.bpmMeasuring, false);
});

test("BPM failure still closes the temporary AudioContext and leaves playback ready", async () => {
  const log = { opened: 0, closed: 0 };
  const { state, audios, actions } = harness({
    async fetch() {
      return { ok: true, arrayBuffer: async () => new ArrayBuffer(4) };
    },
    openAudioContext() {
      log.opened += 1;
      return {
        decodeAudioData() {
          return Promise.reject(new Error("bad m4a"));
        },
        close() {
          log.closed += 1;
          return Promise.reject(new Error("close"));
        },
      };
    },
  });
  const spy = spyDeck(audios.A);
  await actions.loadTrack("A", { id: "abcdefghijk", title: "曲" });
  state.decks.A.status = "preparing";
  await actions.onDeckReady("A");
  assert.equal(log.opened, 1);
  assert.equal(log.closed, 1);
  assert.equal(state.decks.A.status, "ready");
  assert.equal(state.decks.A.bpm, null);
  assert.equal(state.decks.A.beatOffset, null);
  assert.equal(state.decks.A.bpmMeasuring, false);
  assert.equal(bpmText(state.decks.A), "– BPM");
  actions.togglePlay("A");
  assert.equal(state.decks.A.playing, true);
  assert.equal(spy.seeks, 0);
  assert.equal(spy.graph, 0);
  assert.equal(spy.liveClosed, 0);

  const quiet = harness({
    async fetch() {
      return { ok: false };
    },
    openAudioContext() {
      log.opened += 1;
      return { decodeAudioData() {}, close() {} };
    },
  });
  await quiet.actions.loadTrack("A", { id: "abcdefghijk", title: "曲" });
  await quiet.actions.onDeckReady("A");
  assert.equal(log.opened, 1);
  assert.equal(bpmText(quiet.state.decks.A), "– BPM");
});

test("a long track is not fetched and a huge body is not decoded", async () => {
  let fetches = 0;
  const { state, audios, actions } = harness({
    async fetch() {
      fetches += 1;
      throw new Error("fetched");
    },
  });
  await actions.loadTrack("A", { id: "abcdefghijk", title: "長い" });
  state.decks.A.status = "preparing";
  audios.A.duration = BPM_MAX_TRACK_SECONDS + 1;
  await actions.onDeckReady("A");
  assert.equal(fetches, 0);
  assert.equal(state.decks.A.status, "ready");
  assert.equal(state.decks.A.bpm, null);
  assert.equal(state.decks.A.bpmMeasuring, false);
  assert.equal(bpmText(state.decks.A), "– BPM");
  await actions.onDeckReady("A");
  assert.equal(fetches, 0);

  audios.A.duration = Infinity;
  state.decks.A.status = "preparing";
  state.decks.A.gen += 1;
  await actions.onDeckReady("A");
  assert.equal(fetches, 0);
  assert.equal(bpmText(state.decks.A), "– BPM");

  let buffered = 0;
  let cancelled = 0;
  const huge = harness({
    async fetch() {
      return {
        ok: true,
        headers: {
          get(name) {
            return name.toLowerCase() === "content-length" ? String(BPM_MAX_BYTES + 1) : null;
          },
        },
        body: {
          cancel() {
            cancelled += 1;
            return Promise.resolve();
          },
        },
        arrayBuffer() {
          buffered += 1;
          return Promise.resolve(new ArrayBuffer(8));
        },
      };
    },
  });
  await huge.actions.loadTrack("A", { id: "abcdefghijk", title: "大きい" });
  huge.state.decks.A.status = "preparing";
  await huge.actions.onDeckReady("A");
  assert.equal(buffered, 0);
  assert.equal(cancelled, 1);
  assert.equal(huge.state.decks.A.bpm, null);
  assert.equal(huge.state.decks.A.bpmMeasuring, false);
  assert.equal(bpmText(huge.state.decks.A), "– BPM");
  assert.equal(huge.state.decks.A.status, "ready");
});

test("decode prefers a one-channel offline context at 22050", async () => {
  const calls = [];
  const previousOffline = globalThis.OfflineAudioContext;
  const previousLive = globalThis.AudioContext;
  globalThis.OfflineAudioContext = function OfflineAudioContext(channels, length, rate) {
    calls.push([channels, length, rate]);
    return {
      decodeAudioData() {
        return Promise.resolve({
          numberOfChannels: 1,
          sampleRate: 22050,
          getChannelData() {
            return clickTrack({ bpm: 100, sampleRate: 22050, lead: 0.2, seconds: 8 });
          },
        });
      },
    };
  };
  delete globalThis.AudioContext;
  try {
    const { state, actions } = harness({
      async fetch() {
        return { ok: true, arrayBuffer: async () => new ArrayBuffer(4) };
      },
    });
    await actions.loadTrack("A", { id: "abcdefghijk", title: "曲" });
    await actions.onDeckReady("A");
    assert.deepEqual(calls, [[1, 1, 22050]]);
    assert.ok(Math.abs(state.decks.A.bpm - 100) <= 0.5);
    assert.ok(beatError(state.decks.A.beatOffset, 0.2, 100) <= 0.02);
  } finally {
    if (previousOffline === undefined) delete globalThis.OfflineAudioContext;
    else globalThis.OfflineAudioContext = previousOffline;
    if (previousLive === undefined) delete globalThis.AudioContext;
    else globalThis.AudioContext = previousLive;
  }
});
