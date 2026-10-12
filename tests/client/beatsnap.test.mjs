import assert from "node:assert/strict";
import test from "node:test";

import { createActions, freshState } from "../../djtube/static/actions.js";
import {
  BEAT_SNAP_MAX_FRACTION,
  BEAT_SNAP_MAX_SECONDS,
  beatSnapPlan,
} from "../../djtube/static/sync.js";

function deck(bpm, beatOffset, time, rate = 1, measuring = false) {
  return { bpm, beatOffset, time, rate, measuring };
}

/** Smallest wall-clock gap between a beat of each deck after `own` is moved. */
function nearestBeatGap(time, own, other) {
  const ownHeard = 60 / (own.bpm * own.rate);
  const otherHeard = 60 / (other.bpm * other.rate);
  const ownWall = mod(time - own.beatOffset, 60 / own.bpm) / own.rate;
  const otherWall = mod(other.time - other.beatOffset, 60 / other.bpm) / other.rate;
  let best = Infinity;
  for (let k = -4; k <= 6; k += 1) {
    const ownAt = k * ownHeard - ownWall;
    for (let n = -4; n <= 6; n += 1) {
      const gap = Math.abs(ownAt - (n * otherHeard - otherWall));
      if (gap < best) best = gap;
    }
  }
  return best;
}

function mod(value, period) {
  const wrapped = value % period;
  return wrapped < 0 ? wrapped + period : wrapped;
}

test("beat snap uses an 80 ms cap and 20% of the shorter beat", () => {
  assert.equal(BEAT_SNAP_MAX_SECONDS, 0.08);
  assert.equal(BEAT_SNAP_MAX_FRACTION, 0.2);

  const onBeat = deck(120, 0, 10);
  const within = beatSnapPlan(deck(120, 0, 10.04), onBeat);
  assert.ok(within);
  assert.ok(Math.abs(within.time - 10) < 1e-9);
  assert.ok(Math.abs(within.deltaWall + 0.04) < 1e-9);
  assert.ok(nearestBeatGap(within.time, deck(120, 0, 10.04), onBeat) < 1e-9);

  // 80 ms is the window at 120 BPM (20% of 500 ms is 100 ms).
  const atCap = beatSnapPlan(deck(120, 0, BEAT_SNAP_MAX_SECONDS), deck(120, 0, 0));
  assert.ok(atCap);
  assert.ok(Math.abs(atCap.time) < 1e-9);
  assert.equal(beatSnapPlan(deck(120, 0, BEAT_SNAP_MAX_SECONDS + 0.001), deck(120, 0, 0)), null);

  // Above ~150 BPM the fraction binds, so a shift under 80 ms can still be too far.
  const fast = 60 / 180;
  const fraction = BEAT_SNAP_MAX_FRACTION * fast;
  assert.ok(fraction < BEAT_SNAP_MAX_SECONDS);
  const atFraction = beatSnapPlan(deck(180, 0, fraction), deck(180, 0, 0));
  assert.ok(atFraction);
  assert.ok(Math.abs(atFraction.time) < 1e-9);
  assert.equal(beatSnapPlan(deck(180, 0, fraction + 0.004), deck(180, 0, 0)), null);
  assert.ok(fraction + 0.004 < BEAT_SNAP_MAX_SECONDS);
});

test("beat snap follows beatOffset and rate, and lines up unequal tempos", () => {
  const own = deck(120, 0.1, 0.14);
  const other = deck(120, 0.25, 0.25);
  const shifted = beatSnapPlan(own, other);
  assert.ok(Math.abs(shifted.time - 0.1) < 1e-9);
  assert.ok(nearestBeatGap(shifted.time, own, other) < 1e-9);

  // 40 ms of wall time is 50 ms of file time at 1.25×.
  const rated = deck(120, 0, 10.05, 1.25);
  const ratedPlan = beatSnapPlan(rated, deck(120, 0, 10, 1));
  assert.ok(Math.abs(ratedPlan.time - 10) < 1e-9);
  assert.ok(Math.abs(ratedPlan.deltaWall + 0.04) < 1e-9);
  assert.equal(ratedPlan.rate, undefined);

  // 120 against 128. The kicks are 40 ms apart in wall time, not a phase fraction.
  const slow = deck(120, 0, 8.04, 1);
  const fast = deck(128, 0, 8, 1);
  const mixed = beatSnapPlan(slow, fast);
  assert.ok(Math.abs(mixed.time - 8) < 1e-9);
  assert.ok(nearestBeatGap(mixed.time, slow, fast) < 1e-6);

  // Double-time grid. 220 ms past the slow beat is only 30 ms from the fast beat
  // on the other side of the halfway point, so the snap goes forward.
  const halfTime = deck(120, 0, 4.22);
  const doubleTime = deck(240, 0, 4);
  const doubled = beatSnapPlan(halfTime, doubleTime);
  assert.ok(Math.abs(doubled.deltaWall - 0.03) < 1e-9);
  assert.ok(Math.abs(doubled.time - 4.25) < 1e-9);
  assert.ok(nearestBeatGap(doubled.time, halfTime, doubleTime) < 1e-9);

  // 70 ms is inside 80 ms but more than 20% of the 250 ms double-time beat.
  assert.equal(beatSnapPlan(deck(120, 0, 4.2), deck(240, 0, 4.02)), null);
});

test("beat snap does nothing without both beat grids, or when the seek would leave the file", () => {
  const ready = deck(120, 0, 1);
  assert.equal(beatSnapPlan(deck(null, null, 1), deck(null, null, 1)), null);
  assert.equal(beatSnapPlan(deck(null, null, 1), ready), null);
  assert.equal(beatSnapPlan(ready, deck(120, null, 1)), null);
  assert.equal(beatSnapPlan({ ...ready, measuring: true }, ready), null);
  assert.equal(beatSnapPlan(ready, { ...ready, measuring: true }), null);
  assert.equal(beatSnapPlan(ready, ready), null);

  // 50 ms back from 20 ms would cross 0. Do not step forward a whole beat.
  assert.equal(beatSnapPlan(deck(120, 0, 0.02), deck(120, 0, 0.47)), null);
  assert.equal(beatSnapPlan(deck(120, 0, 9.98), deck(120, 0, 0), 9.99), null);
});

test("a half-beat error does not snap, and the near side of that boundary does", () => {
  const leader = deck(120, 0, 10);
  assert.equal(beatSnapPlan(deck(120, 0, 10.25), leader), null);
  // Just past halfway the long way (260 ms) is 240 ms from the previous beat.
  assert.equal(beatSnapPlan(deck(120, 0, 10.26), leader), null);

  // 30 ms before the beat is 470 ms after the previous one. Snap forward.
  const before = beatSnapPlan(deck(120, 0, 10.47), leader);
  assert.ok(before);
  assert.ok(Math.abs(before.deltaWall - 0.03) < 1e-9);
  assert.ok(Math.abs(before.time - 10.5) < 1e-9);
  assert.ok(before.time > 10.47);
  assert.ok(nearestBeatGap(before.time, deck(120, 0, 10.47), leader) < 1e-9);
});

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
    loadVideo() {
      return true;
    },
  };
}

function harness() {
  const state = freshState();
  const audios = { A: fakeAudio(), B: fakeAudio() };
  const actions = createActions({
    state,
    audios,
    prefix: "/djtube",
    scheduleRender() {},
    queryValue: () => "",
    now: () => 1000,
    later() {
      return 1;
    },
    cancelLater() {},
    async fetchSearch() {
      return { tracks: [] };
    },
  });
  return { state, audios, actions };
}

function arm(env, name, { bpm = 120, beatOffset = 0, time = 0, rate = 1 } = {}) {
  const deckState = env.state.decks[name];
  deckState.status = "ready";
  deckState.id = name === "A" ? "abcdefghijk" : "zzzzzzzzzzz";
  deckState.bpm = bpm;
  deckState.beatOffset = beatOffset;
  deckState.bpmMeasuring = false;
  deckState.rate = rate;
  deckState.playing = false;
  deckState.syncing = false;
  env.audios[name].currentTime = time;
  env.audios[name].playbackRate = rate;
  env.audios[name].paused = true;
  env.audios[name].playCalls = 0;
}

function play(env, name) {
  env.audios[name].paused = false;
  env.state.decks[name].playing = true;
}

test("starting either deck snaps only that deck, and only while the other is playing", () => {
  const env = harness();
  arm(env, "A", { time: 10.04 });
  arm(env, "B", { time: 10 });
  env.actions.togglePlay("A");
  assert.equal(env.audios.A.currentTime, 10.04);
  assert.equal(env.audios.A.paused, false);
  env.actions.togglePlay("A");

  play(env, "B");
  env.actions.togglePlay("A");
  assert.ok(Math.abs(env.audios.A.currentTime - 10) < 1e-9);
  assert.equal(env.audios.B.currentTime, 10);
  assert.equal(env.state.decks.A.rate, 1);
  assert.equal(env.audios.A.playbackRate, 1);
  assert.equal(env.audios.B.playCalls, 0);

  env.audios.A.currentTime = 20;
  play(env, "A");
  env.audios.B.currentTime = 20.04;
  env.audios.B.paused = true;
  env.state.decks.B.playing = false;
  env.actions.togglePlay("B");
  assert.ok(Math.abs(env.audios.B.currentTime - 20) < 1e-9);
  assert.equal(env.audios.A.currentTime, 20);
  assert.equal(env.state.decks.B.rate, 1);
  assert.equal(env.audios.B.playbackRate, 1);

  env.actions.togglePlay("B");
  env.audios.B.currentTime = 30.2;
  env.audios.A.currentTime = 30;
  play(env, "A");
  env.actions.togglePlay("B");
  assert.equal(env.audios.B.currentTime, 30.2);
  assert.equal(env.audios.A.currentTime, 30);
});

test("sync play-start alignment still runs when the error is outside the snap window", () => {
  const env = harness();
  arm(env, "A", { time: 4 });
  arm(env, "B", { time: 4 });
  play(env, "B");
  env.actions.syncBeat("A");
  assert.equal(env.state.decks.A.syncing, true);
  env.audios.A.currentTime = 4.2;
  env.actions.togglePlay("A");
  assert.equal(env.state.decks.A.syncing, true);
  assert.ok(Math.abs(env.audios.A.currentTime - 4) < 1e-6);
  assert.equal(env.audios.B.currentTime, 4);
  assert.equal(env.state.decks.A.rate, 1);

  env.actions.syncBeat("A");
  assert.equal(env.state.decks.A.syncing, false);
  arm(env, "A", { time: 10.04 });
  arm(env, "B", { time: 10 });
  env.actions.syncBeat("B");
  env.audios.A.currentTime = 10.04;
  env.audios.B.currentTime = 10;
  play(env, "B");
  env.actions.togglePlay("A");
  assert.equal(env.state.decks.A.syncing, false);
  assert.equal(env.state.decks.B.syncing, true);
  assert.ok(Math.abs(env.audios.A.currentTime - 10.04) < 1e-9);
  assert.ok(Math.abs(env.audios.B.currentTime - 10.04) < 1e-6);
  assert.equal(env.state.decks.A.rate, 1);
  assert.equal(env.state.decks.B.rate, 1);
});
