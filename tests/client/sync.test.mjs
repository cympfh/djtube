import assert from "node:assert/strict";
import test from "node:test";

import { createActions, freshState } from "../../djtube/static/actions.js";
import { FLX4_MAP, dispatchControllerEvent, messageFromMidi } from "../../djtube/static/controller.js";
import { BINDINGS, handleKeydown, legendGroups } from "../../djtube/static/keys.js";
import { beatSyncPlan, beatSyncRate } from "../../djtube/static/sync.js";

function mod(value, period) {
  const wrapped = value % period;
  return wrapped < 0 ? wrapped + period : wrapped;
}

function wallSinceBeat(time, offset, bpm, rate) {
  return mod(time - offset, 60 / bpm) / rate;
}

/** Absolute error, in wall seconds, against the shorter beat grid. */
function alignError(time, own, other, rate) {
  const ownWall = wallSinceBeat(time, own.beatOffset, own.bpm, rate);
  const otherWall = wallSinceBeat(other.time, other.beatOffset, other.bpm, other.rate);
  const pulse = Math.min(60 / (own.bpm * rate), 60 / (other.bpm * other.rate));
  let delta = mod(ownWall - otherWall, pulse);
  if (delta > pulse / 2) delta -= pulse;
  return Math.abs(delta);
}

function expectedRate(ownBpm, otherBpm, otherRate) {
  let rate = (otherBpm * otherRate) / ownBpm;
  for (let fold = 0; fold < 8; fold += 1) {
    const half = rate * 0.5;
    const doubled = rate * 2;
    const dist = Math.abs(rate - 1);
    const halfDist = Math.abs(half - 1);
    const doubleDist = Math.abs(doubled - 1);
    if (halfDist + 1e-9 < dist && halfDist <= doubleDist + 1e-9) rate = half;
    else if (doubleDist + 1e-9 < dist) rate = doubled;
    else break;
  }
  if (rate < 0.5 - 1e-9 || rate > 2 + 1e-9) return null;
  return rate;
}

function deck(bpm, beatOffset, time, rate = 1) {
  return { bpm, beatOffset, time, rate, measuring: false };
}

test("sync rate follows the other tempo and folds half and double toward 1×", () => {
  assert.equal(beatSyncRate(null, 128, 1), null);
  assert.equal(beatSyncRate(128, null, 1), null);
  assert.equal(beatSyncRate(Number.NaN, 128, 1), null);
  assert.equal(beatSyncRate(0, 128, 1), null);
  assert.equal(beatSyncRate(-120, 128, 1), null);
  assert.equal(beatSyncRate(128, 128, 0), null);
  assert.equal(beatSyncRate(128, 128, Number.NaN), null);

  assert.equal(beatSyncRate(128, 128, 1), 1);
  assert.equal(beatSyncRate(128, 128, 1.05), 1.05);
  assert.ok(Math.abs(beatSyncRate(128, 140, 1) - 140 / 128) < 1e-12);

  // 67.9 × 2 = 135.8. Same groove at 1×, not a jump to 2× or 0.5×.
  assert.ok(Math.abs(beatSyncRate(67.9, 135.8, 1) - 1) < 1e-9);
  assert.ok(Math.abs(beatSyncRate(135.8, 67.9, 1) - 1) < 1e-9);
  assert.ok(Math.abs(beatSyncRate(67.9, 135.8, 1.05) - 1.05) < 1e-9);
  assert.ok(beatSyncRate(67.9, 135.8, 1) < 1.2);

  // 8.4× folds to 1.05. It is not clamped to 2× and locked there.
  assert.ok(Math.abs(beatSyncRate(50, 210, 2) - 1.05) < 1e-9);
  assert.ok(Math.abs(beatSyncRate(210, 50, 0.5) - 25 / 210 * 8) < 1e-9);
  assert.equal(beatSyncRate(1, 1e6, 1), null);
  assert.equal(beatSyncRate(1e6, 1, 0.5), null);
});

test("sync phase uses beatOffset, currentTime, and rate, including half and double", () => {
  const same = beatSyncPlan(deck(120, 0, 1.2), deck(120, 0, 1));
  assert.equal(same.rate, 1);
  assert.ok(Math.abs(same.time - 1) < 1e-9);

  const offsets = beatSyncPlan(deck(120, 0.2, 5), deck(120, 0, 5));
  assert.equal(offsets.rate, 1);
  assert.ok(Math.abs(offsets.time - 5.2) < 1e-9);
  assert.ok(alignError(offsets.time, deck(120, 0.2, 5), deck(120, 0, 5), offsets.rate) < 1e-9);

  // Follower is the half-tempo record. Stay at 1× and land on the leader's beats.
  const half = beatSyncPlan(deck(60, 0, 1), deck(120, 0, 0.1));
  assert.equal(half.rate, 1);
  assert.ok(Math.abs(half.time - 1.1) < 1e-9);

  // Follower is already the double-tempo record. Do not slow it to 0.5×.
  const doubled = beatSyncPlan(deck(120, 0, 0.3), deck(60, 0, 0.3));
  assert.equal(doubled.rate, 1);
  assert.ok(Math.abs(doubled.time - 0.3) < 1e-9);

  const leaderTempo = beatSyncPlan(deck(128, 0, 4, 1.9), deck(128, 0, 4, 1.05));
  assert.equal(leaderTempo.rate, 1.05);
  assert.equal(beatSyncPlan(deck(128, 0, 4, 0.6), deck(128, 0, 4, 1.05)).rate, 1.05);

  const samples = [
    { ownBpm: 128, otherBpm: 128, otherRate: 1, ownTime: 10, otherTime: 10.2, ownOffset: 0.1, otherOffset: 0.4 },
    { ownBpm: 67.9, otherBpm: 135.8, otherRate: 1, ownTime: 12.3, otherTime: 4.4, ownOffset: 0.2, otherOffset: 0.05 },
    { ownBpm: 135.8, otherBpm: 67.9, otherRate: 1.08, ownTime: 3, otherTime: 8.25, ownOffset: 0, otherOffset: 0.33 },
    { ownBpm: 100, otherBpm: 140, otherRate: 0.97, ownTime: 20, otherTime: 1.5, ownOffset: 1.2, otherOffset: 0.7 },
    { ownBpm: 90, otherBpm: 180, otherRate: 0.92, ownTime: 2.2, otherTime: 6.6, ownOffset: 0.4, otherOffset: 0.15 },
  ];
  for (const sample of samples) {
    const own = deck(sample.ownBpm, sample.ownOffset, sample.ownTime, 1.7);
    const other = deck(sample.otherBpm, sample.otherOffset, sample.otherTime, sample.otherRate);
    const plan = beatSyncPlan(own, other);
    const want = expectedRate(sample.ownBpm, sample.otherBpm, sample.otherRate);
    assert.ok(Math.abs(plan.rate - want) < 1e-9, `${sample.ownBpm} vs ${sample.otherBpm}`);
    assert.ok(plan.rate >= 0.5 && plan.rate <= 2);
    const pulse = Math.min(60 / (sample.ownBpm * plan.rate), 60 / (sample.otherBpm * sample.otherRate));
    assert.ok(Math.abs(plan.time - sample.ownTime) <= (pulse * plan.rate) / 2 + 1e-6);
    assert.ok(alignError(plan.time, own, other, plan.rate) < 1e-6, alignError(plan.time, own, other, plan.rate));
  }
});

test("sync plan does nothing when either deck has no beat grid", () => {
  const ready = deck(128, 0.2, 3, 1);
  assert.equal(beatSyncPlan({ ...ready, bpm: null }, ready), null);
  assert.equal(beatSyncPlan(ready, { ...ready, bpm: null }), null);
  assert.equal(beatSyncPlan({ ...ready, beatOffset: null }, ready), null);
  assert.equal(beatSyncPlan(ready, { ...ready, beatOffset: -0.01 }), null);
  assert.equal(beatSyncPlan({ ...ready, measuring: true }, ready), null);
  assert.equal(beatSyncPlan(ready, { ...ready, measuring: true, bpm: 128 }), null);
  assert.equal(beatSyncPlan({ ...ready, time: Number.NaN }, ready), null);
  // A beat on the first sample is a real offset, not "missing".
  const onGrid = beatSyncPlan(deck(100, 0, 1), deck(100, 0, 1.2));
  assert.equal(onGrid.rate, 1);
  assert.ok(Math.abs(onGrid.time - 1.2) < 1e-9);
});

function bodyTarget() {
  return { tagName: "BODY", closest() { return null; } };
}

function searchTarget() {
  return { id: "search-input", tagName: "INPUT", type: "search", closest() { return null; } };
}

function keyEvent(key, target, extra = {}) {
  return {
    key,
    code: /^[0-9]$/.test(key) ? `Digit${key}` : "",
    shiftKey: false,
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    target,
    defaultPrevented: false,
    preventDefault() {
      this.defaultPrevented = true;
    },
    ...extra,
  };
}

function fakeAudio() {
  return {
    paused: true,
    currentTime: 0,
    duration: 300,
    volume: 1,
    playbackRate: 1,
    playCalls: 0,
    play() {
      this.playCalls += 1;
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
  };
}

function step(env) {
  const batch = [...env.timers.entries()];
  assert.equal(batch.length, 1);
  for (const [id] of batch) env.timers.delete(id);
  for (const [, fn] of batch) fn();
}

function harness(extra = {}) {
  const state = freshState();
  const audios = { A: fakeAudio(), B: fakeAudio() };
  const timers = new Map();
  const clock = { t: 1000 };
  let nextTimer = 1;
  const actions = createActions({
    state,
    audios,
    prefix: "/djtube",
    scheduleRender() {},
    queryValue: () => "",
    now: () => clock.t,
    later(fn) {
      const id = nextTimer;
      nextTimer += 1;
      timers.set(id, fn);
      return id;
    },
    cancelLater(id) {
      timers.delete(id);
    },
    async fetchSearch() {
      return { tracks: [] };
    },
    ...extra,
  });
  return {
    state,
    audios,
    actions,
    timers,
    clock,
    queued() {
      if (timers.size !== 1) return null;
      return timers.values().next().value;
    },
  };
}

function arm(env, name, { bpm = 128, beatOffset = 0, time = 0, rate = 1, measuring = false } = {}) {
  const deckState = env.state.decks[name];
  deckState.status = "ready";
  deckState.id = "abcdefghijk";
  deckState.bpm = bpm;
  deckState.beatOffset = beatOffset;
  deckState.bpmMeasuring = measuring;
  deckState.rate = rate;
  deckState.playing = false;
  env.audios[name].currentTime = time;
  env.audios[name].playbackRate = rate;
  env.audios[name].paused = true;
  env.audios[name].playCalls = 0;
}

test("syncBeat changes only the pressed deck's rate and position", () => {
  const env = harness();
  arm(env, "A", { bpm: 120, beatOffset: 0.2, time: 5, rate: 1.4 });
  arm(env, "B", { bpm: 120, beatOffset: 0, time: 5, rate: 1 });
  env.state.decks.A.eq.high = 0.2;
  env.state.decks.A.volume = 0.4;
  env.state.decks.A.cue = 1.5;
  env.state.decks.A.filter = 0.25;
  env.state.crossfader = 0.2;
  const leaderTime = env.audios.B.currentTime;
  const leaderRate = env.state.decks.B.rate;

  env.actions.syncBeat("A");

  assert.equal(env.state.decks.A.rate, 1);
  assert.equal(env.audios.A.playbackRate, 1);
  assert.ok(Math.abs(env.audios.A.currentTime - 5.2) < 1e-9);
  assert.equal(env.audios.A.paused, true);
  assert.equal(env.audios.A.playCalls, 0);
  assert.equal(env.state.decks.A.playing, false);
  assert.equal(env.audios.B.currentTime, leaderTime);
  assert.equal(env.state.decks.B.rate, leaderRate);
  assert.equal(env.audios.B.playbackRate, leaderRate);
  assert.equal(env.audios.B.playCalls, 0);
  assert.equal(env.state.decks.A.eq.high, 0.2);
  assert.equal(env.state.decks.A.volume, 0.4);
  assert.equal(env.state.decks.A.cue, 1.5);
  assert.equal(env.state.decks.A.filter, 0.25);
  assert.equal(env.state.crossfader, 0.2);
  assert.equal(env.state.decks.A.bpm, 120);
  assert.equal(env.state.decks.B.bpm, 120);
  assert.equal(env.state.decks.A.syncing, true);
  assert.equal(env.state.decks.B.syncing, false);

  env.actions.syncBeat("A");
  assert.equal(env.state.decks.A.syncing, false);
  assert.equal(env.state.decks.A.rate, 1);
  assert.ok(Math.abs(env.audios.A.currentTime - 5.2) < 1e-9);
  assert.equal(env.audios.A.playCalls, 0);
  assert.equal(env.audios.B.playCalls, 0);
});

test("syncBeat is a no-op without a BPM, while measuring, or before the deck is ready", () => {
  const env = harness();
  arm(env, "A", { bpm: 128, time: 4, rate: 1.2 });
  arm(env, "B", { bpm: null, beatOffset: null, time: 2, rate: 1 });
  env.actions.syncBeat("A");
  assert.equal(env.state.decks.A.rate, 1.2);
  assert.equal(env.audios.A.currentTime, 4);
  assert.equal(env.audios.A.playCalls, 0);

  env.state.decks.B.bpm = 128;
  env.state.decks.B.beatOffset = 0;
  env.state.decks.A.bpmMeasuring = true;
  env.actions.syncBeat("A");
  assert.equal(env.state.decks.A.rate, 1.2);
  assert.equal(env.audios.A.currentTime, 4);

  env.state.decks.A.bpmMeasuring = false;
  env.state.decks.B.bpmMeasuring = true;
  env.actions.syncBeat("B");
  assert.equal(env.state.decks.B.rate, 1);
  assert.equal(env.audios.B.currentTime, 2);

  env.state.decks.B.bpmMeasuring = false;
  env.state.decks.A.status = "preparing";
  env.actions.syncBeat("A");
  assert.equal(env.audios.A.currentTime, 4);
  assert.equal(env.audios.A.playCalls, 0);
});

test("syncBeat clamps the seek to the file and does not play", () => {
  const env = harness();
  arm(env, "A", { bpm: 120, time: 0.05 });
  arm(env, "B", { bpm: 120, time: 0.4 });
  env.actions.syncBeat("A");
  assert.equal(env.audios.A.currentTime, 0);
  assert.equal(env.audios.A.playCalls, 0);
  assert.equal(env.audios.A.paused, true);

  env.actions.syncBeat("A");
  arm(env, "A", { bpm: 120, time: 10 });
  arm(env, "B", { bpm: 120, time: 10.2 });
  env.audios.A.duration = 10.05;
  env.actions.syncBeat("A");
  assert.equal(env.audios.A.currentTime, 10.05);
  assert.equal(env.audios.B.currentTime, 10.2);
  assert.equal(env.audios.A.playCalls, 0);
});

test("sync during a backspin does not resume playback", () => {
  const env = harness();
  arm(env, "A", { bpm: 120, beatOffset: 0, time: 10, rate: 1 });
  arm(env, "B", { bpm: 120, beatOffset: 0, time: 10.2, rate: 1 });
  env.state.decks.A.playing = true;
  env.audios.A.paused = false;
  env.actions.jog("A", -1);
  assert.equal(env.audios.A.paused, true);
  assert.equal(env.audios.A.playCalls, 0);
  assert.equal(typeof env.queued(), "function");

  env.actions.syncBeat("A");

  assert.equal(env.state.decks.A.syncing, true);
  assert.equal(env.audios.A.playCalls, 0);
  assert.equal(env.audios.A.paused, true);
  assert.equal(env.state.decks.A.rate, 1);
  assert.equal(env.audios.A.playbackRate, 1);
  assert.ok(Math.abs(env.audios.A.currentTime - 9.2) < 1e-9);
  step(env);
  assert.equal(env.audios.A.playCalls, 0);
  assert.equal(env.audios.A.paused, true);
  assert.ok(Math.abs(env.audios.A.currentTime - 9.2) < 1e-9);
  assert.equal(env.audios.B.playCalls, 0);
});

test("a sync with no BPM leaves the jog release alone", () => {
  const env = harness();
  arm(env, "A", { bpm: null, beatOffset: null, time: 10 });
  arm(env, "B", { bpm: 120, time: 10 });
  env.state.decks.A.bpm = null;
  env.actions.jog("A", -1);
  const release = env.queued();
  assert.equal(typeof release, "function");
  env.actions.syncBeat("A");
  assert.equal(env.queued(), release);
  assert.equal(env.audios.A.playCalls, 0);
  assert.equal(env.audios.A.currentTime, 9);
});

test("Shift+Digit3 and Shift+Digit8 toggle sync, without character aliases", () => {
  const labels = legendGroups().find((group) => group.name === "テンポ").items.map((item) => item.label);
  assert.ok(labels.includes("デッキ A の同期を入／切"));
  assert.ok(labels.includes("デッキ B の同期を入／切"));
  const syncA = BINDINGS.find((binding) => binding.action === "syncBeat" && binding.args[0] === "A");
  const syncB = BINDINGS.find((binding) => binding.action === "syncBeat" && binding.args[0] === "B");
  assert.deepEqual(syncA.keys, ["3"]);
  assert.deepEqual(syncA.codes, ["Digit3"]);
  assert.equal(syncA.shift, true);
  assert.deepEqual(syncB.keys, ["8"]);
  assert.deepEqual(syncB.codes, ["Digit8"]);
  assert.equal(syncB.shift, true);

  const env = harness();
  arm(env, "A", { bpm: 128, time: 4, rate: 1.2 });
  arm(env, "B", { bpm: 128, time: 4, rate: 1.05 });

  assert.equal(handleKeydown(keyEvent("3", searchTarget(), { shiftKey: true }), env.actions), false);
  assert.equal(env.state.decks.A.rate, 1.2);
  assert.equal(handleKeydown(keyEvent("#", searchTarget(), { shiftKey: true, code: "Digit3" }), env.actions), false);

  assert.equal(handleKeydown(keyEvent("3", bodyTarget()), env.actions), true);
  assert.equal(env.state.decks.A.rate, 1);

  env.actions.setRate("A", 1.2);
  assert.equal(handleKeydown(keyEvent("#", bodyTarget(), { shiftKey: true, code: "Digit3" }), env.actions), true);
  assert.equal(env.state.decks.A.syncing, true);
  assert.equal(env.state.decks.A.rate, 1.05);
  assert.equal(env.state.decks.B.rate, 1.05);
  assert.equal(env.audios.A.playCalls, 0);
  assert.equal(handleKeydown(keyEvent("#", bodyTarget(), { shiftKey: true, code: "Digit3" }), env.actions), true);
  assert.equal(env.state.decks.A.syncing, false);
  assert.equal(env.state.decks.A.rate, 1.05);

  // US Shift+9 is "(". JIS Shift+: is "*". Neither is deck sync.
  env.actions.setRate("A", 1.2);
  assert.equal(handleKeydown(keyEvent("(", bodyTarget(), { shiftKey: true, code: "Digit9" }), env.actions), false);
  assert.equal(handleKeydown(keyEvent("*", bodyTarget(), { shiftKey: true, code: "Quote" }), env.actions), false);
  assert.equal(env.state.decks.A.syncing, false);
  assert.equal(env.state.decks.A.rate, 1.2);

  env.actions.setRate("B", 1.4);
  assert.equal(handleKeydown(keyEvent("8", bodyTarget()), env.actions), true);
  assert.equal(env.state.decks.B.rate, 1.39);

  assert.equal(handleKeydown(keyEvent("*", bodyTarget(), { shiftKey: true, code: "Digit8" }), env.actions), true);
  assert.equal(env.state.decks.B.syncing, true);
  assert.equal(env.state.decks.B.rate, 1.2);
  assert.equal(env.audios.B.playCalls, 0);
  assert.equal(handleKeydown(keyEvent("(", bodyTarget(), { shiftKey: true, code: "Digit8" }), env.actions), true);
  assert.equal(env.state.decks.B.syncing, false);
  assert.equal(env.state.decks.B.rate, 1.2);
});

test("FLX4 BEAT SYNC note 0x58 syncs the pressed deck to the other", () => {
  assert.equal(FLX4_MAP["note:0:88"].action, "syncBeat");
  assert.deepEqual(FLX4_MAP["note:0:88"].args, ["A"]);
  assert.equal(FLX4_MAP["note:0:88"].hold, undefined);
  assert.equal(FLX4_MAP["note:1:88"].action, "syncBeat");
  assert.deepEqual(FLX4_MAP["note:1:88"].args, ["B"]);

  assert.equal(messageFromMidi(new Uint8Array([0x90, 0x58, 0])), null);
  assert.equal(messageFromMidi(new Uint8Array([0x80, 0x58, 0])), null);

  const env = harness();
  arm(env, "A", { bpm: 67.9, beatOffset: 0, time: 1, rate: 1 });
  arm(env, "B", { bpm: 135.8, beatOffset: 0, time: 0.1, rate: 1 });
  const noteA = messageFromMidi(new Uint8Array([0x90, 0x58, 0x7f]));
  assert.equal(dispatchControllerEvent(noteA, env.actions), true);
  assert.equal(env.state.decks.A.syncing, true);
  assert.ok(Math.abs(env.state.decks.A.rate - 1) < 1e-9);
  assert.notEqual(env.state.decks.A.rate, 2);
  assert.equal(env.state.decks.B.rate, 1);
  assert.equal(env.audios.A.playCalls, 0);
  assert.equal(env.audios.B.playCalls, 0);
  assert.equal(dispatchControllerEvent(noteA, env.actions), true);
  assert.equal(env.state.decks.A.syncing, false);
  assert.ok(Math.abs(env.state.decks.A.rate - 1) < 1e-9);
  assert.equal(dispatchControllerEvent(noteA, env.actions), true);
  assert.equal(env.state.decks.A.syncing, true);
  assert.ok(alignError(env.audios.A.currentTime, deck(67.9, 0, 1), deck(135.8, 0, 0.1), env.state.decks.A.rate) < 1e-6);

  env.audios.B.currentTime = 2;
  const noteB = messageFromMidi(new Uint8Array([0x91, 0x58, 0x7f]));
  assert.equal(dispatchControllerEvent(noteB, env.actions), true);
  assert.equal(env.state.decks.A.syncing, false);
  assert.equal(env.state.decks.B.syncing, true);
  assert.ok(Math.abs(env.state.decks.B.rate - 1) < 1e-9);
  assert.notEqual(env.state.decks.B.rate, 0.5);
  assert.equal(env.audios.B.playCalls, 0);
  assert.equal(env.audios.A.paused, true);
  assert.ok(
    alignError(
      env.audios.B.currentTime,
      deck(135.8, 0, 2, 1),
      { bpm: 67.9, beatOffset: 0, time: env.audios.A.currentTime, rate: env.state.decks.A.rate },
      env.state.decks.B.rate,
    ) < 1e-6,
  );
});

test("a locked deck keeps the other deck's tempo, including half and double", () => {
  const env = harness();
  arm(env, "A", { bpm: 67.9, beatOffset: 0, time: 1, rate: 1 });
  arm(env, "B", { bpm: 135.8, beatOffset: 0, time: 0.1, rate: 1 });
  env.actions.syncBeat("A");
  assert.equal(env.state.decks.A.syncing, true);
  assert.ok(Math.abs(env.state.decks.A.rate - 1) < 1e-9);

  env.actions.setRate("B", 1.1);
  assert.ok(Math.abs(env.state.decks.A.rate - 1.1) < 1e-9);
  step(env);
  assert.ok(Math.abs(env.state.decks.A.rate - 1.1) < 1e-9);
  assert.ok(Math.abs(env.state.decks.A.rate - 2.2) > 0.5);
  assert.equal(env.audios.A.playCalls, 0);
  assert.equal(env.audios.A.paused, true);
  assert.equal(env.state.decks.B.rate, 1.1);
  const held = env.audios.A.currentTime;

  env.actions.syncBeat("A");
  assert.equal(env.state.decks.A.syncing, false);
  env.actions.setRate("B", 1.25);
  assert.equal(env.queued(), null);
  assert.ok(Math.abs(env.state.decks.A.rate - 1.1) < 1e-9);
  assert.equal(env.audios.A.currentTime, held);
  assert.equal(env.audios.A.playCalls, 0);
});

test("while locked, phase seeks on a large master jump and holds a follower nudge", () => {
  const env = harness();
  arm(env, "A", { bpm: 120, beatOffset: 0.2, time: 5, rate: 1 });
  arm(env, "B", { bpm: 120, beatOffset: 0, time: 5, rate: 1 });
  env.actions.syncBeat("A");
  assert.ok(Math.abs(env.audios.A.currentTime - 5.2) < 1e-9);

  const parked = env.audios.A.currentTime;
  env.audios.B.currentTime = 5.02;
  step(env);
  assert.equal(env.audios.A.currentTime, parked);
  assert.equal(env.audios.A.playCalls, 0);

  env.audios.A.currentTime = parked + 0.2;
  step(env);
  assert.ok(Math.abs(env.audios.A.currentTime - (parked + 0.2)) < 1e-9);
  const heldError = alignError(
    env.audios.A.currentTime,
    deck(120, 0.2, 0),
    deck(120, 0, env.audios.B.currentTime),
    1,
  );
  assert.ok(heldError > 0.05);

  env.audios.B.currentTime = 8.3;
  step(env);
  assert.ok(Math.abs(env.audios.A.currentTime - (parked + 0.2)) > 0.05);
  const afterJump = alignError(
    env.audios.A.currentTime,
    deck(120, 0.2, 0),
    deck(120, 0, env.audios.B.currentTime),
    1,
  );
  assert.ok(Math.abs(afterJump - heldError) < 1e-6);
  assert.equal(env.audios.A.playCalls, 0);
  assert.equal(env.audios.B.playCalls, 0);
  assert.equal(env.audios.B.currentTime, 8.3);
});

test("a master tempo change updates the follower rate and does not seek", () => {
  const env = harness();
  arm(env, "A", { bpm: 128, time: 4, rate: 1 });
  arm(env, "B", { bpm: 128, time: 4.2, rate: 1 });
  env.actions.syncBeat("A");
  const parked = env.audios.A.currentTime;
  for (const rate of [1.04, 1.11, 1.2, 0.9]) {
    env.actions.setRate("B", rate);
    assert.ok(Math.abs(env.state.decks.A.rate - rate) < 1e-9);
    assert.equal(env.audios.A.currentTime, parked);
    step(env);
    assert.ok(Math.abs(env.state.decks.A.rate - rate) < 1e-9);
    assert.equal(env.audios.A.currentTime, parked);
    assert.equal(env.audios.A.playCalls, 0);
  }
});

test("changing the follower tempo leaves sync", () => {
  const env = harness();
  arm(env, "A", { bpm: 120, time: 2, rate: 1 });
  arm(env, "B", { bpm: 120, time: 2, rate: 1.05 });
  env.actions.syncBeat("A");
  assert.equal(env.state.decks.A.syncing, true);
  env.actions.setRate("A", 1.25);
  assert.equal(env.state.decks.A.syncing, false);
  assert.equal(env.state.decks.A.rate, 1.25);
  env.actions.setRate("B", 1.4);
  assert.equal(env.queued(), null);
  assert.equal(env.state.decks.A.rate, 1.25);
  assert.equal(env.audios.A.playCalls, 0);

  env.actions.syncBeat("A");
  assert.equal(env.state.decks.A.syncing, true);
  env.actions.resetRate("A");
  assert.equal(env.state.decks.A.syncing, false);
  assert.equal(env.state.decks.A.rate, 1);

  env.actions.syncBeat("A");
  assert.equal(env.state.decks.A.syncing, true);
  env.actions.setRateFromController("A", 127);
  assert.equal(env.state.decks.A.syncing, false);
  assert.ok(env.state.decks.A.rate > 1.9);
  assert.equal(env.audios.A.playCalls, 0);
});

test("starting playback realigns phase and drops a paused slip", () => {
  const env = harness();
  arm(env, "A", { bpm: 120, beatOffset: 0, time: 1, rate: 1 });
  arm(env, "B", { bpm: 120, beatOffset: 0, time: 1, rate: 1 });
  env.audios.B.paused = false;
  env.state.decks.B.playing = true;
  env.actions.syncBeat("A");
  assert.equal(env.audios.A.paused, true);
  assert.ok(Math.abs(env.audios.A.currentTime - 1) < 1e-9);

  env.audios.A.currentTime = 1.2;
  step(env);
  assert.ok(Math.abs(env.audios.A.currentTime - 1.2) < 1e-9);

  env.clock.t += 1000;
  env.audios.B.currentTime = 2;
  env.actions.togglePlay("A");
  assert.equal(env.audios.A.playCalls, 1);
  assert.equal(env.audios.A.paused, false);
  assert.equal(env.audios.B.playCalls, 0);
  assert.ok(
    alignError(env.audios.A.currentTime, deck(120, 0, 0), deck(120, 0, env.audios.B.currentTime), 1) < 1e-6,
  );
  assert.ok(Math.abs(env.audios.A.currentTime - 1.2) > 0.05);

  env.actions.syncBeat("A");
  arm(env, "A", { bpm: 120, time: 4, rate: 1 });
  arm(env, "B", { bpm: 120, time: 4, rate: 1 });
  env.actions.syncBeat("A");
  env.audios.A.currentTime += 0.2;
  step(env);
  const slipped = env.audios.A.currentTime;
  env.clock.t += 400;
  env.actions.togglePlay("B");
  assert.equal(env.audios.B.playCalls, 1);
  assert.equal(env.audios.A.playCalls, 0);
  assert.equal(env.audios.A.paused, true);
  assert.ok(Math.abs(env.audios.A.currentTime - slipped) > 0.05);
  assert.ok(
    alignError(env.audios.A.currentTime, deck(120, 0, 0), deck(120, 0, env.audios.B.currentTime), 1) < 1e-6,
  );
});

test("a jog timer does not replace the sync follow timer", () => {
  const env = harness();
  arm(env, "A", { bpm: 120, time: 3, rate: 1 });
  arm(env, "B", { bpm: 120, time: 3, rate: 1 });
  env.actions.syncBeat("A");
  assert.equal(env.timers.size, 1);
  const syncId = env.timers.keys().next().value;
  env.audios.A.paused = false;
  env.state.decks.A.playing = true;
  env.actions.jog("A", 1);
  assert.equal(env.timers.size, 2);
  assert.equal(env.timers.has(syncId), true);
  const jogId = [...env.timers.keys()].find((id) => id !== syncId);
  const release = env.timers.get(jogId);
  env.timers.delete(jogId);
  release();
  assert.equal(env.timers.has(syncId), true);
  assert.equal(env.audios.A.playCalls, 0);
});

test("an unfoldable tempo does not enter sync", () => {
  const env = harness();
  arm(env, "A", { bpm: 1, time: 3, rate: 1 });
  arm(env, "B", { bpm: 1e6, time: 3, rate: 1 });
  const time = env.audios.A.currentTime;
  env.actions.syncBeat("A");
  assert.equal(env.state.decks.A.syncing, false);
  assert.equal(env.state.decks.A.rate, 1);
  assert.equal(env.audios.A.currentTime, time);
  assert.equal(env.audios.A.playCalls, 0);
  assert.equal(env.queued(), null);
});

test("loading a deck or clearing its BPM leaves sync", async () => {
  const env = harness();
  arm(env, "A", { bpm: 128, time: 3, rate: 1 });
  arm(env, "B", { bpm: 128, time: 3, rate: 1.05 });
  env.actions.syncBeat("A");
  assert.equal(env.state.decks.A.syncing, true);
  await env.actions.loadTrack("B", { id: "zzzzzzzzzzz", title: "別" });
  assert.equal(env.state.decks.A.syncing, false);
  assert.equal(env.state.decks.B.syncing, false);
  assert.equal(env.audios.A.playCalls, 0);

  env.state.decks.B.status = "ready";
  env.state.decks.B.bpm = 100;
  env.state.decks.B.beatOffset = 0;
  env.state.decks.B.bpmMeasuring = false;
  env.actions.syncBeat("A");
  assert.equal(env.state.decks.A.syncing, true);
  await env.actions.loadTrack("A", { id: "abcdefghijk", title: "曲" });
  assert.equal(env.state.decks.A.syncing, false);
  assert.equal(env.audios.A.playCalls, 0);
});
