import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { createActions, freshState } from "../../djtube/static/actions.js";
import { createDeckPlayer } from "../../djtube/static/player.js";

test("a late platter pause stays on that deck and does not spin the other", async () => {
  const listeners = { A: {}, B: {} };
  function element(deck) {
    const node = {
      paused: true,
      currentTime: 12,
      duration: 120,
      volume: 1,
      rate: 1,
      set playbackRate(value) {
        this.rate = value;
      },
      get playbackRate() {
        return this.rate;
      },
      addEventListener(name, fn) {
        listeners[deck][name] = fn;
      },
      play() {
        this.paused = false;
        listeners[deck].play?.();
        return Promise.resolve();
      },
      pause() {
        this.paused = true;
        listeners[deck].pause?.();
      },
    };
    return node;
  }

  const state = freshState();
  const audios = { A: createDeckPlayer("A", "player-A"), B: createDeckPlayer("B", "player-B") };
  audios.A.audio = element("A");
  audios.B.audio = element("B");
  const actions = createActions({
    state,
    audios,
    prefix: "/djtube",
    scheduleRender() {},
    queryValue: () => "",
  });
  function mirror(deck) {
    audios[deck].attach({
      onPlaying(name) {
        state.decks[name].playing = true;
      },
      onPaused(name) {
        state.decks[name].playing = false;
      },
    });
  }
  mirror("A");
  mirror("B");

  for (const deck of ["A", "B"]) {
    state.decks[deck].id = deck === "A" ? "abcdefghijk" : "zzzzzzzzzzz";
    state.decks[deck].status = "ready";
    state.decks[deck].bpm = 120;
    state.decks[deck].beatOffset = 0;
    state.decks[deck].playing = true;
    audios[deck].audio.paused = false;
    audios[deck].paused = false;
  }
  state.decks.A.rate = 1;
  state.decks.B.rate = 1;
  audios.A.playbackRate = 1;
  audios.B.playbackRate = 1;
  actions.syncBeat("A");
  const coupled = state.decks.A.rate;
  assert.equal(state.decks.A.syncing, true);
  assert.equal(audios.A.audio.rate, coupled);

  actions.pressDisc("A", true);
  assert.equal(audios.A.audio.paused, true);
  assert.equal(state.decks.A.playing, false);
  assert.equal(state.decks.B.playing, true);
  assert.equal(audios.B.audio.paused, false);
  assert.equal(audios.B.audio.rate, 1);

  actions.jog("B", 0.2);
  assert.ok(audios.B.audio.rate > 2);
  assert.equal(audios.A.audio.rate, coupled);
  assert.equal(state.decks.A.rate, coupled);
  assert.equal(state.decks.B.rate, 1);
  assert.equal(state.decks.A.playing, false);
  assert.notEqual(audios.A.audio.rate, audios.B.audio.rate);

  actions.pressDisc("A", false);
  assert.equal(audios.A.audio.paused, false);
  assert.equal(state.decks.A.playing, true);
  assert.equal(audios.A.paused, false);
  assert.equal(state.decks.B.playing, true);

  audios.A.audio.paused = false;
  listeners.A.pause();
  assert.equal(audios.A.paused, false);
  assert.equal(state.decks.A.playing, true);
  assert.equal(audios.A.audio.rate, coupled);
  assert.equal(state.decks.B.playing, true);
  assert.equal(state.decks.B.rate, 1);

  actions.jog("B", 0.2);
  assert.ok(audios.B.audio.rate > 2);
  assert.equal(audios.A.audio.rate, coupled);
  assert.equal(state.decks.A.rate, coupled);
  assert.equal(state.decks.A.playing, true);
  assert.equal(audios.A.paused, false);
  assert.equal(audios.A.audio.paused, false);
  assert.notEqual(audios.A.audio.rate, audios.B.audio.rate);
  assert.notEqual(audios.A.playbackRate, audios.B.audio.rate);
  assert.equal(state.decks.B.playing, true);
  assert.equal(state.decks.B.rate, 1);

  audios.A.audio.paused = true;
  listeners.A.play();
  assert.equal(audios.A.paused, false);
  assert.equal(state.decks.A.playing, true);
  audios.A.pause();
  assert.equal(audios.A.paused, true);
  assert.equal(state.decks.A.playing, false);
  assert.equal(state.decks.B.playing, true);
  assert.equal(audios.B.audio.paused, false);
  assert.equal(audios.A.audio.rate, coupled);

  const appJs = readFileSync(new URL("../../djtube/static/app.js", import.meta.url), "utf8");
  const pausedHook = appJs.slice(appJs.indexOf("onPaused(deck)"), appJs.indexOf("onEnded(deck)"));
  const playingHook = appJs.slice(appJs.indexOf("onPlaying(deck)"), appJs.indexOf("onPaused(deck)"));
  assert.match(pausedHook, /audios\[deck\]\?\.audio\?\.paused === false\) return/);
  assert.match(playingHook, /audios\[deck\]\?\.audio\?\.paused === true\) return/);
});
