import assert from "node:assert/strict";
import test from "node:test";

import { createActions, freshState } from "../../djtube/static/actions.js";
import { FLX4_MAP, dispatchControllerEvent, messageFromMidi } from "../../djtube/static/controller.js";
import { formatTime } from "../../djtube/static/format.js";
import { deckGains } from "../../djtube/static/gains.js";
import { BINDINGS, handleKeydown, legendGroups } from "../../djtube/static/keys.js";
import { RATE_STEP, clampRate, formatRate, rateFromMidi } from "../../djtube/static/rate.js";
import { createDeckPlayer } from "../../djtube/static/youtube.js";

function fakeAudio() {
  return {
    paused: true,
    currentTime: 0,
    duration: 120,
    volume: 1,
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
  };
}

function bodyTarget() {
  return { tagName: "BODY", closest() { return null; } };
}

function searchTarget() {
  return { id: "search-input", tagName: "INPUT", type: "search", closest() { return null; } };
}

function keyEvent(key, target, extra = {}) {
  return {
    key,
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

function harness(overrides = {}) {
  const state = freshState();
  const audios = { A: fakeAudio(), B: fakeAudio() };
  let query = overrides.query ?? "city pop";
  let focused = overrides.focused ?? false;
  const calls = { search: 0 };
  const actions = createActions({
    state,
    audios,
    prefix: "/djtube",
    scheduleRender() {},
    queryValue: () => query,
    isSearchFocused: () => focused,
    focusSearchElement() {
      focused = true;
    },
    blurSearchElement() {
      focused = false;
    },
    async fetchSearch() {
      calls.search += 1;
      return {
        source: "ytdlp",
        tracks: [
          { id: "abcdefghijk", title: "夜", channel: "A店", duration: 90 },
          { id: "zzzzzzzzzzz", title: "昼", channel: "B店", duration: 80 },
        ],
      };
    },
    async prepareTrack() {},
    ...overrides.deps,
  });
  return { state, audios, actions, calls, setQuery: (value) => { query = value; }, setFocused: (value) => { focused = value; } };
}

test("equal power crossfader and clock", () => {
  assert.equal(deckGains(0).a, 1);
  assert.ok(deckGains(0).b < 0.0001);
  assert.equal(deckGains(1).b, 1);
  assert.ok(Math.abs(deckGains(0.5).a - Math.SQRT1_2) < 0.0001);
  assert.ok(Math.abs(deckGains(0.5).b - Math.SQRT1_2) < 0.0001);
  assert.equal(formatTime(65), "1:05");
  assert.equal(formatTime(3723), "1:02:03");
  assert.equal(formatTime(null), "–:––");
});

test("FLX4 map is empty and dispatch uses the shared actions", () => {
  assert.deepEqual(FLX4_MAP, {});
  assert.equal(messageFromMidi(new Uint8Array([0x80, 11, 0])), null);
  assert.deepEqual(messageFromMidi(new Uint8Array([0x90, 11, 0])), null);
  const note = messageFromMidi(new Uint8Array([0x91, 12, 40]));
  assert.deepEqual(note, { type: "note", channel: 1, number: 12, value: 40 });
  const cc = messageFromMidi(new Uint8Array([0xb0, 23, 127]));
  assert.equal(cc.type, "cc");

  const { actions, state } = harness();
  assert.equal(dispatchControllerEvent(note, actions), false);
  assert.equal(
    dispatchControllerEvent(note, actions, { "note:1:12": { action: "toggleLoadTarget" } }),
    true,
  );
  assert.equal(state.loadTarget, "B");
  assert.equal(
    dispatchControllerEvent(cc, actions, {
      "cc:0:23": { action: "setCrossfaderFromController", passValue: true },
    }),
    true,
  );
  assert.equal(state.crossfader, 1);
  assert.equal(dispatchControllerEvent(cc, actions, { "cc:0:23": { action: "missing" } }), false);
});

test("keyboard map covers deck operations and skips typed search", async () => {
  const labels = legendGroups().flatMap((group) => group.items.map((item) => item.label));
  for (const label of ["検索にフォーカス", "デッキ A へロード", "デッキ B へロード", "デッキ A 再生/停止", "デッキ B キュー", "デッキ A を戻す", "デッキ B を進める", "フェーダーを A へ", "ロード先を切り替え"]) {
    assert.ok(labels.includes(label), label);
  }
  const actionsInMap = new Set(BINDINGS.map((binding) => binding.action));
  for (const name of ["focusSearch", "onEnter", "moveSelection", "loadSelected", "togglePlay", "cue", "jog", "nudgeCrossfader", "toggleLoadTarget"]) {
    assert.ok(actionsInMap.has(name), name);
  }

  const { state, audios, actions } = harness();
  const search = searchTarget();
  assert.equal(handleKeydown(keyEvent("a", search), actions), false);
  assert.equal(handleKeydown(keyEvent("/", search), actions), false);

  assert.equal(handleKeydown(keyEvent("/", bodyTarget()), actions), true);
  assert.equal(handleKeydown(keyEvent("Enter", search), actions), true);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(state.results.length, 2);

  assert.equal(handleKeydown(keyEvent("ArrowDown", search), actions), true);
  assert.equal(state.selected, 1);
  assert.equal(handleKeydown(keyEvent("k", bodyTarget()), actions), true);
  assert.equal(state.selected, 0);

  assert.equal(handleKeydown(keyEvent("b", bodyTarget()), actions), true);
  await Promise.resolve();
  assert.equal(state.decks.B.id, "abcdefghijk");
  assert.equal(audios.B.videoId, "abcdefghijk");
  assert.equal(state.decks.B.status, "ready");

  assert.equal(handleKeydown(keyEvent("w", bodyTarget()), actions), true);
  assert.equal(audios.B.paused, false);
  assert.equal(handleKeydown(keyEvent("w", bodyTarget()), actions), true);
  assert.equal(audios.B.paused, true);

  audios.B.currentTime = 12;
  assert.equal(handleKeydown(keyEvent("x", bodyTarget()), actions), true);
  assert.equal(audios.B.currentTime, 0);
  assert.equal(audios.B.paused, true);
  assert.equal(handleKeydown(keyEvent("x", bodyTarget()), actions), true);
  assert.equal(audios.B.paused, false);

  audios.B.pause();
  audios.B.currentTime = 8;
  assert.equal(handleKeydown(keyEvent("x", bodyTarget(), { shiftKey: true }), actions), true);
  assert.equal(state.decks.B.cue, 8);

  const before = state.crossfader;
  assert.equal(handleKeydown(keyEvent("ArrowLeft", bodyTarget()), actions), true);
  assert.ok(state.crossfader < before);
  assert.equal(handleKeydown(keyEvent("Home", bodyTarget()), actions), true);
  assert.equal(state.crossfader, 0);
  assert.ok(Math.abs(audios.A.volume - 1) < 0.0001);
  assert.ok(audios.B.volume < 0.0001);

  assert.equal(handleKeydown(keyEvent("t", bodyTarget()), actions), true);
  assert.equal(state.loadTarget, "B");
});

test("M toggles the music limit outside the search field", async () => {
  const seen = [];
  const { state, actions } = harness({
    deps: {
      async fetchSearch(query, musicOnly) {
        seen.push({ query, musicOnly });
        return { source: "youtube", tracks: [{ id: "abcdefghijk", title: "夜", channel: "A店", duration: 90 }] };
      },
    },
  });
  assert.equal(state.musicOnly, true);
  assert.equal(handleKeydown(keyEvent("m", searchTarget()), actions), false);
  assert.equal(state.musicOnly, true);
  state.lastQuery = "city pop";
  assert.equal(handleKeydown(keyEvent("m", bodyTarget()), actions), true);
  assert.equal(state.musicOnly, false);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(seen.at(-1), { query: "city pop", musicOnly: false });
  const labels = legendGroups().flatMap((group) => group.items.map((item) => item.label));
  assert.ok(labels.includes("音楽に限る"));
});

test("second enter loads the targeted deck", async () => {
  const { state, audios, actions, calls, setFocused } = harness({ focused: true });
  await actions.onEnter();
  assert.equal(calls.search, 1);
  assert.equal(state.decks.A.id, "");
  setFocused(true);
  await actions.onEnter();
  assert.equal(calls.search, 1);
  assert.equal(audios.A.videoId, "abcdefghijk");
  assert.equal(state.loadTarget, "A");
});

test("jog seeks the deck and does not move cue or the crossfader", () => {
  const { state, audios, actions } = harness();
  state.results = [{ id: "abcdefghijk", title: "曲", channel: "", duration: 120 }];
  return actions.loadSelected("A").then(() => {
    state.decks.A.cue = 3;
    state.decks.B.status = "ready";
    state.decks.B.id = "zzzzzzzzzzz";
    audios.A.currentTime = 20;
    audios.A.duration = 120;
    audios.B.currentTime = 8;
    audios.B.duration = 30;
    const fader = state.crossfader;

    assert.equal(handleKeydown(keyEvent("[", bodyTarget()), actions), true);
    assert.equal(audios.A.currentTime, 19);
    assert.equal(handleKeydown(keyEvent("]", bodyTarget()), actions), true);
    assert.equal(audios.A.currentTime, 20);
    assert.equal(handleKeydown(keyEvent("]", bodyTarget(), { shiftKey: true }), actions), true);
    assert.equal(audios.A.currentTime, 30);
    assert.equal(handleKeydown(keyEvent("}", bodyTarget(), { shiftKey: true }), actions), true);
    assert.equal(audios.A.currentTime, 40);

    assert.equal(handleKeydown(keyEvent(";", bodyTarget()), actions), true);
    assert.equal(audios.B.currentTime, 7);
    assert.equal(handleKeydown(keyEvent("'", bodyTarget()), actions), true);
    assert.equal(audios.B.currentTime, 8);
    assert.equal(handleKeydown(keyEvent('"', bodyTarget(), { shiftKey: true }), actions), true);
    assert.equal(audios.B.currentTime, 18);

    audios.A.currentTime = 115;
    actions.jog("A", 10);
    assert.equal(audios.A.currentTime, 120);
    actions.jog("A", -1000);
    assert.equal(audios.A.currentTime, 0);
    audios.A.duration = Number.NaN;
    audios.A.currentTime = 50;
    actions.jog("A", 10);
    assert.equal(audios.A.currentTime, 60);

    const before = audios.A.currentTime;
    state.decks.A.status = "preparing";
    actions.jog("A", 1);
    assert.equal(audios.A.currentTime, before);
    assert.equal(handleKeydown(keyEvent("[", searchTarget()), actions), false);
    assert.equal(audios.A.currentTime, before);

    assert.equal(state.decks.A.cue, 3);
    assert.equal(state.decks.B.cue, 0);
    assert.equal(state.crossfader, fader);
    assert.deepEqual(FLX4_MAP, {});
    assert.equal(
      dispatchControllerEvent({ type: "note", channel: 0, number: 1, value: 1 }, actions, {
        "note:0:1": { action: "jog", args: ["B", -1] },
      }),
      true,
    );
    assert.equal(audios.B.currentTime, 17);
    assert.equal(state.crossfader, fader);
    assert.deepEqual(FLX4_MAP, {});
  });
});

test("jog keeps adding while the player still reports the old time", () => {
  const { state, audios, actions } = harness();
  state.results = [{ id: "abcdefghijk", title: "曲", channel: "", duration: 120 }];
  return actions.loadSelected("A").then(() => {
    let reported = 10;
    let commanded = 10;
    Object.defineProperty(audios.A, "currentTime", {
      configurable: true,
      get() {
        return reported;
      },
      set(value) {
        commanded = value;
      },
    });
    audios.A.duration = 80;
    actions.jog("A", 1);
    assert.equal(commanded, 11);
    actions.jog("A", 1);
    assert.equal(commanded, 12);
    actions.jog("A", 10);
    assert.equal(commanded, 22);
    reported = 22;
    actions.syncJog("A");
    assert.equal(state.decks.A.jogCommand, null);
    actions.jog("A", 1);
    assert.equal(commanded, 23);
    assert.equal(state.decks.A.cue, 0);
  });
});

test("cue back near the pre-jog time does not jump to the old jog target", () => {
  const { state, audios, actions } = harness();
  state.results = [{ id: "abcdefghijk", title: "曲", channel: "", duration: 120 }];
  return actions.loadSelected("A").then(() => {
    let reported = 10;
    let commanded = 10;
    let seekPending = false;
    Object.defineProperty(audios.A, "currentTime", {
      configurable: true,
      get() {
        if (seekPending && Math.abs(reported - commanded) > 0.35) return commanded;
        seekPending = false;
        return reported;
      },
      set(value) {
        commanded = value;
        seekPending = true;
      },
    });
    state.decks.A.cue = 10;
    actions.jog("A", 10);
    assert.equal(commanded, 20);
    assert.equal(state.decks.A.jogCommand.at, 20);
    actions.cue("A");
    assert.equal(commanded, 10);
    assert.equal(state.decks.A.jogCommand, null);
    actions.jog("A", 1);
    assert.equal(commanded, 11);
  });
});

test("a position bar seek drops the stored jog target", () => {
  const { state, audios, actions } = harness();
  state.results = [{ id: "abcdefghijk", title: "曲", channel: "", duration: 120 }];
  return actions.loadSelected("A").then(() => {
    audios.A.currentTime = 10;
    audios.A.duration = 80;
    actions.jog("A", 10);
    assert.equal(audios.A.currentTime, 20);
    actions.seek("A", 10);
    assert.equal(audios.A.currentTime, 10);
    assert.equal(state.decks.A.jogCommand, null);
    actions.jog("A", 1);
    assert.equal(audios.A.currentTime, 11);
    assert.equal(state.decks.A.cue, 0);
    assert.equal(state.crossfader, 0.5);
  });
});

test("tempo clamps, nudges, resets, and stays callable from the action table", async () => {
  assert.equal(clampRate(Number.NaN), 1);
  assert.equal(clampRate(4), 2);
  assert.equal(clampRate(0.1), 0.5);
  assert.equal(rateFromMidi(0), 0.5);
  assert.equal(rateFromMidi(64), 1);
  assert.equal(rateFromMidi(127), 2);
  assert.equal(formatRate(1), "1.00×");
  assert.equal(RATE_STEP, 0.25);

  const labels = legendGroups().find((group) => group.name === "テンポ").items.map((item) => item.label);
  assert.ok(labels.includes("デッキ A のテンポを上げる"));
  assert.ok(labels.includes("デッキ B のテンポを 1.0 に戻す"));
  const tempoKeys = BINDINGS.filter((binding) => binding.group === "テンポ").flatMap((binding) => binding.keys);
  assert.deepEqual(tempoKeys, ["1", "2", "3", "8", "9", "0"]);

  const deck = createDeckPlayer("A", "yt-A");
  const seen = [];
  deck.player = {
    setPlaybackRate(value) {
      seen.push(value);
    },
    cueVideoById() {},
  };
  deck.playbackRate = 1.25;
  deck.playbackRate = 9;
  deck.playbackRate = 0.1;
  assert.equal(deck.playbackRate, 0.5);
  assert.deepEqual(seen, [1.25, 2, 0.5]);
  deck.apiReady = true;
  deck.playbackRate = 1.5;
  seen.length = 0;
  assert.equal(deck.loadVideo("abcdefghijk"), true);
  assert.equal(seen[seen.length - 1], 1.5);

  const { state, audios, actions } = harness();
  assert.equal(state.decks.A.rate, 1);
  actions.setRate("A", 1.5);
  assert.equal(state.decks.A.rate, 1.5);
  assert.equal(audios.A.playbackRate, 1.5);
  actions.nudgeRate("A", RATE_STEP);
  assert.equal(state.decks.A.rate, 1.75);
  actions.nudgeRate("A", 1);
  assert.equal(state.decks.A.rate, 2);
  actions.nudgeRate("B", -1);
  assert.equal(state.decks.B.rate, 0.5);
  actions.resetRate("B");
  assert.equal(state.decks.B.rate, 1);
  actions.setRate("C", 2);
  assert.equal(state.decks.A.rate, 2);

  state.results = [{ id: "abcdefghijk", title: "曲", channel: "", duration: 10 }];
  await actions.loadSelected("A");
  assert.equal(audios.A.playbackRate, 2);
  assert.equal(state.decks.A.rate, 2);

  assert.equal(handleKeydown(keyEvent("2", searchTarget()), actions), false);
  assert.equal(state.decks.A.rate, 2);
  actions.resetRate("A");
  assert.equal(handleKeydown(keyEvent("2", bodyTarget()), actions), true);
  assert.equal(state.decks.A.rate, 1.25);
  assert.equal(audios.A.playbackRate, 1.25);
  assert.equal(handleKeydown(keyEvent("1", bodyTarget()), actions), true);
  assert.equal(state.decks.A.rate, 1);
  assert.equal(handleKeydown(keyEvent("3", bodyTarget()), actions), true);
  assert.equal(state.decks.A.rate, 1);
  actions.setRate("B", 1.5);
  assert.equal(handleKeydown(keyEvent("0", bodyTarget()), actions), true);
  assert.equal(state.decks.B.rate, 1);
  const beforeJog = state.decks.A.rate;
  assert.equal(handleKeydown(keyEvent("[", bodyTarget()), actions), true);
  assert.equal(state.decks.A.rate, beforeJog);

  const cc = messageFromMidi(new Uint8Array([0xb0, 40, 127]));
  assert.equal(
    dispatchControllerEvent(cc, actions, {
      "cc:0:40": { action: "setRateFromController", args: ["A"], passValue: true },
    }),
    true,
  );
  assert.equal(state.decks.A.rate, 2);
  assert.equal(audios.A.playbackRate, 2);
  assert.deepEqual(FLX4_MAP, {});
  const center = messageFromMidi(new Uint8Array([0xb0, 40, 64]));
  assert.equal(
    dispatchControllerEvent(center, actions, {
      "cc:0:40": { action: "setRateFromController", args: ["B"], passValue: true },
    }),
    true,
  );
  assert.equal(state.decks.B.rate, 1);
});

test("cue while playing returns and pauses", () => {
  const { state, audios, actions } = harness();
  state.results = [{ id: "abcdefghijk", title: "曲", channel: "", duration: 10 }];
  return actions.loadSelected("A").then(() => {
    audios.A.currentTime = 4;
    actions.togglePlay("A");
    actions.cue("A");
    assert.equal(audios.A.paused, true);
    assert.equal(audios.A.currentTime, 0);
  });
});
