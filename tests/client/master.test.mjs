import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { createActions, freshState } from "../../djtube/static/actions.js";
import { connectDeckFilter } from "../../djtube/static/filter.js";
import { deckGains } from "../../djtube/static/gains.js";
import { createAudioBus } from "../../djtube/static/master.js";
import { createDeckPlayer } from "../../djtube/static/player.js";

function node(kind) {
  return {
    kind,
    type: "",
    frequency: { value: 0 },
    Q: { value: 0 },
    gain: { value: kind === "gain" ? 1 : 0 },
    connections: [],
    connect(target) {
      this.connections.push(target);
    },
    disconnect() {},
  };
}

function installAudioWindow(overrides = {}) {
  const contexts = [];
  const previous = globalThis.window;
  globalThis.window = {
    AudioContext: function AudioContext() {
      this.destination = node("destination");
      this.state = "running";
      this.sampleRate = 44100;
      this.nodes = [];
      this.resume = function resume() {
        this.state = "running";
        this.resumed = (this.resumed || 0) + 1;
      };
      const keep = (made) => {
        this.nodes.push(made);
        return made;
      };
      this.createMediaElementSource = function createMediaElementSource(audio) {
        if (overrides.capture === false) throw new Error("captured");
        const source = node("source");
        source.media = audio;
        return keep(source);
      };
      this.createGain = () => keep(node("gain"));
      this.createBiquadFilter = () => keep(node("biquad"));
      this.createBuffer = function createBuffer(_channels, length) {
        const data = new Float32Array(length);
        return { length, getChannelData: () => data };
      };
      this.createBufferSource = () => {
        const source = node("buffer");
        source.buffer = null;
        source.loop = false;
        source.playbackRate = { value: 1 };
        source.started = false;
        source.start = function start() {
          this.started = true;
        };
        source.stop = function stop() {
          this.stopped = true;
        };
        return keep(source);
      };
      contexts.push(this);
    },
  };
  return {
    contexts,
    restore() {
      if (previous === undefined) delete globalThis.window;
      else globalThis.window = previous;
    },
  };
}

function pathFrom(start) {
  const nodes = [];
  let current = start;
  const seen = new Set();
  while (current && !seen.has(current)) {
    seen.add(current);
    nodes.push(current);
    current = current.connections?.[0];
  }
  return nodes;
}

test("decks A and B and scratch meet at one master, and faders drive the gain nodes", () => {
  const installed = installAudioWindow();
  try {
    const mix = createAudioBus();
    const audios = {
      A: createDeckPlayer("A", "player-A", mix),
      B: createDeckPlayer("B", "player-B", mix),
    };
    audios.A.audio = { volume: 1 };
    audios.B.audio = { volume: 0.2 };
    assert.equal(audios.A.setFilter(0.5), true);
    assert.equal(audios.B.setEqGain("high", 3), true);
    assert.equal(installed.contexts.length, 1);
    assert.equal(audios.A._context, audios.B._context);
    assert.equal(mix.context, audios.A._context);
    assert.equal(mix.master.gain.value, 1);
    assert.deepEqual(mix.master.connections, [mix.context.destination]);

    for (const deck of ["A", "B"]) {
      const player = audios[deck];
      const source = mix.context.nodes.find((item) => item.media === player.audio);
      const route = pathFrom(source);
      assert.deepEqual(
        route.map((item) => item.kind),
        ["source", "biquad", "biquad", "biquad", "biquad", "gain", "gain", "destination"],
      );
      assert.equal(route[1].type, "highshelf");
      assert.equal(route[2].type, "peaking");
      assert.equal(route[3].type, "lowshelf");
      assert.equal(route[4], player._color);
      assert.equal(route[5], player._level);
      assert.equal(route[6], mix.master);
      assert.equal(route[3].connections.length, 1);
      assert.equal(route[3].connections.includes(mix.context.destination), false);
    }
    assert.notEqual(audios.A._level, audios.B._level);
    assert.equal(audios.B.audio.volume, 1);

    const state = freshState();
    const actions = createActions({
      state,
      audios,
      scheduleRender() {},
      queryValue: () => "",
    });
    actions.setVolume("A", 0.4);
    actions.setVolume("B", 0.8);
    actions.setCrossfader(0.25);
    const fade = deckGains(0.25);
    assert.ok(Math.abs(audios.A._level.gain.value - fade.a * 0.4) < 1e-9);
    assert.ok(Math.abs(audios.B._level.gain.value - fade.b * 0.8) < 1e-9);
    assert.equal(audios.A.audio.volume, 1);
    assert.equal(audios.B.audio.volume, 1);
    assert.equal(mix.master.gain.value, 1);

    actions.setCrossfader(0);
    assert.ok(Math.abs(audios.A._level.gain.value - 0.4) < 1e-9);
    assert.ok(audios.B._level.gain.value < 1e-9);
    actions.setVolume("A", 0);
    assert.equal(audios.A._level.gain.value, 0);
    actions.setVolume("A", 0.5);
    actions.setCrossfader(1);
    assert.ok(audios.A._level.gain.value < 1e-9);
    assert.ok(Math.abs(audios.B._level.gain.value - 0.8) < 1e-9);

    actions.setCrossfader(0);
    audios.A.playScratch({ rate: 2, chirpHz: 800 });
    audios.B.playScratch({ rate: 4, chirpHz: 1200 });
    const scratchGains = mix.context.nodes.filter(
      (item) => item.kind === "gain" && item !== mix.master && item !== audios.A._level && item !== audios.B._level,
    );
    assert.equal(scratchGains.length, 2);
    assert.equal(installed.contexts.length, 1);
    for (const gain of scratchGains) assert.deepEqual(gain.connections, [mix.master]);
    const scratchLevel = (player) => player.volume * 0.85;
    assert.ok(scratchGains.some((gain) => Math.abs(gain.gain.value - scratchLevel(audios.A)) < 1e-9));
    assert.ok(scratchGains.some((gain) => Math.abs(gain.gain.value - scratchLevel(audios.B)) < 1e-9));
    assert.ok(Math.abs(scratchLevel(audios.A) - 0.5 * 0.85) < 1e-9);
    assert.ok(scratchLevel(audios.B) < 1e-9);
    audios.A.playScratch({ rate: 3, chirpHz: 900 });
    assert.equal(scratchGains.length, 2);
    assert.ok(scratchGains.some((gain) => Math.abs(gain.gain.value - 0.5 * 0.85) < 1e-9));

    const appJs = readFileSync(new URL("../../djtube/static/app.js", import.meta.url), "utf8");
    assert.match(appJs, /const mix = createAudioBus\(\)/);
    assert.match(appJs, /createDeckPlayer\("A", "player-A", mix\)/);
    assert.match(appJs, /createDeckPlayer\("B", "player-B", mix\)/);
  } finally {
    installed.restore();
  }
});

test("a level chosen before the graph is copied onto the gain", () => {
  const installed = installAudioWindow();
  try {
    const mix = createAudioBus();
    const player = createDeckPlayer("A", "player-A", mix);
    player.audio = { volume: 1 };
    player.volume = 0.25;
    assert.equal(player.audio.volume, 0.25);
    assert.equal(player._level, null);
    assert.equal(player.setFilter(1), true);
    assert.equal(player.audio.volume, 1);
    assert.equal(player._level.gain.value, 0.25);
    assert.equal(player._level.connections[0], mix.master);
    player.volume = 0.1;
    assert.equal(player._level.gain.value, 0.1);
    assert.equal(player.audio.volume, 1);
  } finally {
    installed.restore();
  }
});

test("without a graph the element volume still carries the fader", () => {
  const previous = globalThis.window;
  delete globalThis.window;
  try {
    const player = createDeckPlayer("A", "player-A");
    player.audio = { volume: 1 };
    player.volume = 0.2;
    assert.equal(player.setFilter(0), false);
    assert.equal(player.audio.volume, 0.2);
    assert.equal(player._level, null);
    assert.equal(player._context, null);
  } finally {
    if (previous === undefined) delete globalThis.window;
    else globalThis.window = previous;
  }
});

test("scratch still reaches the master when the deck element cannot join the graph", () => {
  const installed = installAudioWindow({ capture: false });
  try {
    const mix = createAudioBus();
    const player = createDeckPlayer("A", "player-A", mix);
    player.audio = { volume: 1 };
    player.volume = 0.4;
    assert.equal(player.setEqGain("low", -3), false);
    assert.equal(player.audio.volume, 0.4);
    assert.equal(player._level, null);
    assert.equal(player.offMaster(), true);
    player.playScratch({ rate: 2, chirpHz: 640 });
    const scratchGain = mix.context.nodes.find((item) => item.kind === "gain" && item !== mix.master);
    assert.ok(scratchGain);
    assert.deepEqual(scratchGain.connections, [mix.master]);
    assert.ok(Math.abs(scratchGain.gain.value - 0.4 * 0.85) < 1e-9);
    assert.equal(installed.contexts.length, 1);
    assert.deepEqual(mix.master.connections, [mix.context.destination]);
  } finally {
    installed.restore();
  }
});

test("a source captured before the graph throws still reaches the master through a gain", () => {
  const previous = globalThis.window;
  const made = [];
  let sources = 0;
  globalThis.window = {
    AudioContext: function AudioContext() {
      this.destination = { kind: "destination" };
      this.state = "running";
      this.resume = function resume() {};
      this.createMediaElementSource = function createMediaElementSource(audio) {
        sources += 1;
        const source = {
          kind: "source",
          media: audio,
          connections: [],
          connect(target) {
            this.connections.push(target);
          },
        };
        made.push(source);
        return source;
      };
      this.createGain = function createGain() {
        const gain = {
          kind: "gain",
          gain: { value: 1 },
          connections: [],
          connect(target) {
            this.connections.push(target);
          },
        };
        made.push(gain);
        return gain;
      };
      this.createBiquadFilter = function createBiquadFilter() {
        throw new Error("biquad");
      };
    },
  };
  try {
    const player = createDeckPlayer("A", "player-A");
    player.audio = { volume: 1 };
    player.volume = 0.35;
    assert.equal(player.setEqGain("high", 3), false);
    assert.equal(sources, 1);
    assert.equal(player.audio.volume, 1);
    assert.equal(player.offMaster(), false);
    assert.equal(player._graphFailed, false);
    const master = player._bus.master;
    const source = made.find((item) => item.kind === "source");
    assert.equal(player._level.gain.value, 0.35);
    assert.deepEqual(source.connections, [player._level]);
    assert.deepEqual(player._level.connections, [master]);
    assert.deepEqual(master.connections, [player._context.destination]);
    assert.equal(player.setFilter(0), false);
    assert.equal(sources, 1);
    assert.equal(player._level.gain.value, 0.35);
  } finally {
    if (previous === undefined) delete globalThis.window;
    else globalThis.window = previous;
  }
});

test("the deck filter can feed a gain instead of the speakers", () => {
  const tail = node("biquad");
  const level = node("gain");
  const context = {
    destination: node("destination"),
    createBiquadFilter: () => node("biquad"),
  };
  const color = connectDeckFilter(tail, context, level);
  assert.deepEqual(tail.connections, [color]);
  assert.deepEqual(color.connections, [level]);
  assert.equal(color.connections.includes(context.destination), false);
});
