import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { SOURCE_UNAVAILABLE, createActions, freshState, sourcePlaybackBlocked } from "../../djtube/static/actions.js";
import {
  FLX4_MAP,
  JOG_SEARCH_STEP_SECONDS,
  JOG_STEP_SECONDS,
  controllerStatusText,
  dispatchControllerEvent,
  midiButtonState,
  messageFromMidi,
  relativeMidiTicks,
} from "../../djtube/static/controller.js";
import { formatTime } from "../../djtube/static/format.js";
import { EQ_BOOST_DB, EQ_CUT_DB, EQ_STEP, connectEqGraph, eqGainDb, eqUnitFromMidi, formatEqDb } from "../../djtube/static/eq.js";
import {
  FILTER_HPF_MAX_HZ,
  FILTER_HPF_OPEN_HZ,
  FILTER_LPF_MIN_HZ,
  FILTER_LPF_OPEN_HZ,
  FILTER_STEP,
  applyFilter,
  connectDeckFilter,
  filterSpec,
  filterUnitFromMidi,
  formatFilter,
} from "../../djtube/static/filter.js";
import { deckGains } from "../../djtube/static/gains.js";
import { JOG_FIRST_TICK_SECONDS, JOG_RELEASE_MS, SPIN_RATE_MAX, jogReleaseHear, jogSpinPlan, scratchFromSpeed } from "../../djtube/static/jogspin.js";
import { BINDINGS, JOG_STEP, JOG_STEP_LARGE, VOLUME_STEP, handleKeydown, legendGroups } from "../../djtube/static/keys.js";
import { createScratchVoice } from "../../djtube/static/scratch.js";
import { RATE_STEP, clampRate, formatRate, rateFromMidi } from "../../djtube/static/rate.js";
import { cookiePanelOpen, cookiePanelShown, nextChosenOpen } from "../../djtube/static/cookies.js";
import { createDeckPlayer } from "../../djtube/static/player.js";
import { DISC_DEGREES_PER_SECOND, discRotationDegrees } from "../../djtube/static/disc.js";

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

test("dispatch uses the shared actions", () => {
  assert.equal(messageFromMidi(new Uint8Array([0x80, 11, 0])), null);
  assert.deepEqual(messageFromMidi(new Uint8Array([0x90, 11, 0])), null);
  const note = messageFromMidi(new Uint8Array([0x91, 12, 40]));
  assert.deepEqual(note, { type: "note", channel: 1, number: 12, value: 40 });
  const cc = messageFromMidi(new Uint8Array([0xb0, 23, 127]));
  assert.equal(cc.type, "cc");

  const { actions, state } = harness();
  state.results = [
    { id: "abcdefghijk", title: "夜" },
    { id: "zzzzzzzzzzz", title: "昼" },
  ];
  assert.equal(dispatchControllerEvent({ type: "note", channel: 0, number: 14, value: 127 }, actions), false);
  assert.equal(
    dispatchControllerEvent(note, actions, { "note:1:12": { action: "moveSelection", args: [1] } }),
    true,
  );
  assert.equal(state.selected, 1);
  assert.equal(
    dispatchControllerEvent(cc, actions, {
      "cc:0:23": { action: "setCrossfaderFromController", passValue: true },
    }),
    true,
  );
  assert.equal(state.crossfader, 1);
  assert.equal(dispatchControllerEvent(cc, actions, { "cc:0:23": { action: "missing" } }), false);
});

test("FLX4 map sends notes and CCs to deck actions", async () => {
  const expected = {
    "note:0:11": ["togglePlay", ["A"]],
    "note:1:11": ["togglePlay", ["B"]],
    "note:0:12": ["cue", ["A"]],
    "note:1:12": ["cue", ["B"]],
    "note:6:70": ["loadOpenSelection", ["A"]],
    "note:6:71": ["loadOpenSelection", ["B"]],
    "cc:6:31": ["setCrossfaderFromController", undefined],
    "note:0:54": ["pressDisc", ["A"]],
    "note:1:54": ["pressDisc", ["B"]],
    "cc:0:33": ["jog", ["A"]],
    "cc:0:34": ["jog", ["A"]],
    "cc:0:35": ["jog", ["A"]],
    "cc:1:33": ["jog", ["B"]],
    "cc:1:34": ["jog", ["B"]],
    "cc:1:35": ["jog", ["B"]],
    "cc:0:41": ["jog", ["A"]],
    "cc:1:41": ["jog", ["B"]],
    "cc:6:64": ["moveSelection", undefined],
    "cc:0:0": ["setRateFromController", ["A"]],
    "cc:1:0": ["setRateFromController", ["B"]],
    "cc:0:7": ["setEqFromController", ["A", "high"]],
    "cc:0:11": ["setEqFromController", ["A", "mid"]],
    "cc:0:15": ["setEqFromController", ["A", "low"]],
    "cc:1:7": ["setEqFromController", ["B", "high"]],
    "cc:1:11": ["setEqFromController", ["B", "mid"]],
    "cc:1:15": ["setEqFromController", ["B", "low"]],
    "cc:0:19": ["setVolumeFromController", ["A"]],
    "cc:1:19": ["setVolumeFromController", ["B"]],
    "cc:6:23": ["setFilterFromController", ["A"]],
    "cc:6:24": ["setFilterFromController", ["B"]],
  };
  for (const [key, [action, args]] of Object.entries(expected)) {
    assert.equal(FLX4_MAP[key]?.action, action, key);
    assert.deepEqual(FLX4_MAP[key]?.args, args, key);
  }
  assert.equal(FLX4_MAP["cc:6:31"].passValue, true);
  assert.equal(FLX4_MAP["cc:0:34"].relative, "center64");
  assert.equal(FLX4_MAP["cc:0:34"].scale, JOG_STEP_SECONDS);
  assert.equal(FLX4_MAP["cc:1:41"].scale, JOG_SEARCH_STEP_SECONDS);
  assert.ok(JOG_SEARCH_STEP_SECONDS > JOG_STEP_SECONDS);
  assert.equal(FLX4_MAP["cc:6:64"].relative, "signed7");
  assert.equal(FLX4_MAP["cc:0:0"].passValue, true);
  assert.equal(FLX4_MAP["cc:1:15"].passValue, true);
  assert.equal(FLX4_MAP["cc:0:19"].passValue, true);
  assert.equal(FLX4_MAP["cc:1:19"].passValue, true);
  assert.equal(FLX4_MAP["cc:6:23"].passValue, true);
  assert.equal(FLX4_MAP["cc:6:24"].passValue, true);
  for (const key of [
    "note:0:14",
    "note:0:72",
    "note:0:103",
    "note:0:102",
    "note:0:82",
    "note:1:102",
    "note:1:82",
    "cc:0:51",
    "cc:1:51",
    "cc:6:63",
    "cc:0:4",
    "cc:0:32",
    "cc:0:39",
    "cc:0:43",
    "cc:0:47",
    "cc:1:32",
    "cc:6:55",
    "cc:6:56",
  ]) {
    assert.equal(FLX4_MAP[key], undefined, key);
  }

  assert.equal(relativeMidiTicks(65, "center64"), 1);
  assert.equal(relativeMidiTicks(63, "center64"), -1);
  assert.equal(relativeMidiTicks(64, "center64"), 0);
  assert.equal(relativeMidiTicks(1, "signed7"), 1);
  assert.equal(relativeMidiTicks(127, "signed7"), -1);

  const { state, audios, actions } = harness();
  state.results = [
    { id: "abcdefghijk", title: "夜", channel: "A店", duration: 90 },
    { id: "zzzzzzzzzzz", title: "昼", channel: "B店", duration: 80 },
  ];
  await actions.loadSelected("A");
  audios.A.currentTime = 10;
  const cue = state.decks.A.cue;

  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0x90, 0x0b, 0x7f])), actions), true);
  assert.equal(audios.A.paused, false);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb0, 0x22, 0x41])), actions), true);
  assert.ok(Math.abs(audios.A.currentTime - (10 + JOG_STEP_SECONDS)) < 0.0001);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb0, 0x22, 0x3f])), actions), true);
  assert.ok(Math.abs(audios.A.currentTime - 10) < 0.0001);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb0, 0x29, 0x42])), actions), true);
  assert.ok(Math.abs(audios.A.currentTime - (10 + 2 * JOG_SEARCH_STEP_SECONDS)) < 0.0001);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb0, 0x22, 0x40])), actions), true);
  assert.equal(state.decks.A.cue, cue);

  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb6, 0x1f, 0x00])), actions), true);
  assert.equal(state.crossfader, 0);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb6, 0x40, 0x01])), actions), true);
  assert.equal(state.selected, 1);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb6, 0x40, 0x7f])), actions), true);
  assert.equal(state.selected, 0);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0x96, 0x47, 0x7f])), actions), true);
  await Promise.resolve();
  assert.equal(state.decks.B.id, "abcdefghijk");
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0x90, 0x0e, 0x7f])), actions), false);

  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb0, 0x00, 64])), actions), true);
  assert.equal(state.decks.A.rate, rateFromMidi(64));
  assert.equal(audios.A.playbackRate, rateFromMidi(64));
  const tempoA = 1 + (96 - 64) / 63;
  assert.notEqual(tempoA, 1.5);
  assert.notEqual(tempoA, 1.25);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb0, 0x00, 96])), actions), true);
  assert.equal(state.decks.A.rate, tempoA);
  assert.equal(audios.A.playbackRate, tempoA);
  assert.equal(state.decks.A.rate, rateFromMidi(96));
  const tempoB = 1 + (80 - 64) / 63;
  assert.notEqual(tempoB, 1.25);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb1, 0x00, 80])), actions), true);
  assert.equal(state.decks.B.rate, tempoB);
  assert.equal(audios.B.playbackRate, tempoB);
  assert.equal(state.decks.A.rate, tempoA);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb0, 0x20, 40])), actions), false);
  assert.equal(state.decks.A.rate, tempoA);
  assert.equal(state.decks.B.rate, tempoB);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb0, 0x00, 0])), actions), true);
  assert.equal(state.decks.A.rate, 0.5);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb1, 0x00, 127])), actions), true);
  assert.equal(state.decks.B.rate, 2);
  assert.equal(state.decks.A.rate, 0.5);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb0, 0x20, 0])), actions), false);

  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb0, 0x07, 64])), actions), true);
  assert.equal(state.decks.A.eq.high, eqUnitFromMidi(64));
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb0, 0x0b, 0])), actions), true);
  assert.equal(state.decks.A.eq.mid, 0);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb0, 0x0f, 127])), actions), true);
  assert.equal(state.decks.A.eq.low, 1);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb1, 0x07, 0])), actions), true);
  assert.equal(state.decks.B.eq.high, 0);
  assert.equal(state.decks.A.eq.high, eqUnitFromMidi(64));
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb0, 0x27, 0])), actions), false);
  assert.equal(state.crossfader, 0);
  assert.equal(state.decks.A.cue, cue);

  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb0, 0x13, 0x00])), actions), true);
  assert.equal(state.decks.A.volume, 0);
  assert.equal(audios.A.volume, 0);
  assert.equal(state.decks.B.volume, 1);
  assert.equal(state.crossfader, 0);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb0, 0x33, 0x7f])), actions), false);
  assert.equal(state.decks.A.volume, 0);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb1, 0x13, 0x7f])), actions), true);
  assert.equal(state.decks.B.volume, 1);
  assert.equal(state.decks.A.volume, 0);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb1, 0x33, 0x00])), actions), false);
  assert.equal(state.decks.B.volume, 1);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0x90, 0x66, 0x7f])), actions), false);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0x90, 0x52, 0x7f])), actions), false);
  assert.equal(audios.A.paused, false);
  assert.equal(state.decks.A.volume, 0);

  assert.equal(controllerStatusText(), "MIDI未接続");
  assert.equal(controllerStatusText({ state: "idle" }), "MIDI未接続");
  assert.match(controllerStatusText({ state: "insecure" }), /HTTPS/);
  assert.equal(controllerStatusText({ state: "open", names: ["DDJ-FLX4"], connected: true }), "接続: DDJ-FLX4");
  assert.equal(controllerStatusText({ state: "open", names: [] }), "未接続");
  assert.equal(controllerStatusText({ state: "unsupported" }), "Web MIDI 非対応");
  assert.equal(controllerStatusText({ state: "denied" }), "MIDI が拒否されました");
  assert.equal(controllerStatusText({ state: "opening" }), "MIDI を開いています…");
});

test("MIDI button is on only after a device connects", () => {
  assert.equal(midiButtonState(), "off");
  assert.equal(midiButtonState({ state: "idle" }), "off");
  assert.equal(midiButtonState({ state: "opening", names: [], connected: false }), "off");
  assert.equal(midiButtonState({ state: "unsupported", names: [], connected: false }), "off");
  assert.equal(midiButtonState({ state: "insecure", names: [], connected: false }), "off");
  assert.equal(midiButtonState({ state: "denied", names: [], connected: false }), "off");
  assert.equal(midiButtonState({ state: "open", names: [], connected: false }), "off");
  assert.equal(midiButtonState({ state: "open", names: ["DDJ-FLX4"], connected: true }), "on");
  assert.equal(midiButtonState({ state: "open", names: ["DDJ-FLX4"] }), "on");

  const app = readFileSync(new URL("../../djtube/static/app.js", import.meta.url), "utf8");
  const click = app.slice(app.indexOf('midiButton.addEventListener("click"'));
  assert.equal(click.includes('dataset.midi = "wait"'), false);
  assert.equal(click.includes('dataset.midi = "on"'), false);
  assert.match(app, /midiButton\.dataset\.midi = midiButtonState\(status\)/);
  const css = readFileSync(new URL("../../djtube/static/app.css", import.meta.url), "utf8");
  assert.equal(css.includes('[data-midi="wait"]'), false);
  assert.match(css, /\.midi-button\[data-midi="on"\][\s\S]*background:\s*var\(--a\)/);
});

test("keyboard map covers deck operations and skips typed search", async () => {
  const labels = legendGroups().flatMap((group) => group.items.map((item) => item.label));
  for (const label of ["検索にフォーカス", "検索", "デッキ A へロード", "デッキ B へロード", "デッキ A 再生/停止", "デッキ B キュー", "デッキ A を戻す", "デッキ B を進める", "フェーダーを A へ", "プレイリストをデッキ A へ", "プレイリストをデッキ B へ"]) {
    assert.ok(labels.includes(label), label);
  }
  for (const label of ["検索 / ロード", "ロード先を切り替え"]) {
    assert.equal(labels.includes(label), false, label);
  }
  const actionsInMap = new Set(BINDINGS.map((binding) => binding.action));
  for (const name of ["focusSearch", "onEnter", "moveSelection", "loadSelected", "togglePlay", "cue", "jog", "nudgeCrossfader"]) {
    assert.ok(actionsInMap.has(name), name);
  }
  assert.equal(actionsInMap.has("toggleLoadTarget"), false);
  assert.equal(BINDINGS.some((binding) => binding.keys.includes("t") && binding.action === "nudgeFilter"), true);

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

  assert.equal(state.decks.A.id, "");
  assert.equal(handleKeydown(keyEvent("Enter", bodyTarget()), actions), true);
  assert.equal(state.decks.A.id, "");
  assert.equal(state.decks.B.id, "abcdefghijk");
  assert.equal(handleKeydown(keyEvent("t", bodyTarget()), actions), true);
  assert.equal(state.decks.A.id, "");
  assert.equal(state.decks.A.filter, 0.45);
  assert.equal(handleKeydown(keyEvent("a", bodyTarget()), actions), true);
  await Promise.resolve();
  assert.equal(state.decks.A.id, "abcdefghijk");
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

test("a loaded deck shows the search thumbnail and does not invent one", async () => {
  const { state, actions } = harness();
  state.results = [
    {
      id: "abcdefghijk",
      title: "夜",
      channel: "A店",
      thumbnail: "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg",
    },
  ];
  await actions.loadSelected("A");
  assert.equal(state.decks.A.thumbnail, "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg");

  state.selected = 0;
  state.results = [{ id: "zzzzzzzzzzz", title: "昼", channel: "B店" }];
  await actions.loadSelected("B");
  assert.equal(state.decks.B.id, "zzzzzzzzzzz");
  assert.equal(state.decks.B.thumbnail, "");

  state.results = [{ id: "abcdefghijk", title: "夜", thumbnail: "" }];
  await actions.loadSelected("A");
  assert.equal(state.decks.A.id, "abcdefghijk");
  assert.equal(state.decks.A.thumbnail, "");
});

test("enter in the search field searches and does not load a deck", async () => {
  const { state, audios, actions, calls, setFocused } = harness({ focused: true });
  await actions.onEnter();
  assert.equal(calls.search, 1);
  assert.equal(state.decks.A.id, "");
  assert.equal(state.decks.B.id, "");
  setFocused(true);
  await actions.onEnter();
  assert.equal(calls.search, 1);
  assert.equal(state.decks.A.id, "");
  assert.equal(state.decks.B.id, "");
  assert.equal(audios.A.videoId, "");
  assert.equal(audios.B.videoId, "");
  const search = searchTarget();
  assert.equal(handleKeydown(keyEvent("Enter", search), actions), true);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(calls.search, 1);
  assert.equal(state.decks.A.id, "");
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

test("FLX4 jog keeps adding while the playhead is stale", () => {
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
    const tick = (value) => dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb0, 0x22, value])), actions);
    assert.equal(tick(0x41), true);
    assert.ok(Math.abs(commanded - (10 + JOG_STEP_SECONDS)) < 0.0001);
    assert.equal(tick(0x41), true);
    assert.ok(Math.abs(commanded - (10 + 2 * JOG_STEP_SECONDS)) < 0.0001);
    reported = commanded;
    actions.syncJog("A");
    assert.equal(state.decks.A.jogCommand, null);
  });
});

function holdingDeck(state, deck) {
  const audio = createDeckPlayer(deck, `player-${deck}`);
  let reported = 10;
  const element = {
    duration: 120,
    paused: true,
    volume: 1,
    playbackRate: 1,
    src: "",
    load() {},
    play() {
      this.paused = false;
      return Promise.resolve();
    },
    pause() {
      this.paused = true;
    },
    addEventListener() {},
  };
  Object.defineProperty(element, "currentTime", {
    configurable: true,
    get() {
      return reported;
    },
    set() {},
  });
  audio.audio = element;
  audio.onSeekLanded = () => {
    state.decks[deck].jogCommand = null;
  };
  return {
    audio,
    at(seconds) {
      reported = seconds;
    },
  };
}

test("jog lands when playback has left the pre-jog time toward the command", () => {
  const { state, audios, actions } = harness();
  const held = holdingDeck(state, "A");
  audios.A = held.audio;
  state.results = [{ id: "abcdefghijk", title: "曲", channel: "", duration: 120 }];
  return actions.loadSelected("A").then(() => {
    state.decks.A.status = "ready";
    held.at(10);
    actions.jog("A", 1);
    actions.jog("A", 1);
    assert.equal(state.decks.A.jogCommand.from, 10);
    assert.equal(state.decks.A.jogCommand.at, 12);
    assert.equal(held.audio.currentTime, 12);

    held.at(10);
    actions.syncJog("A");
    assert.equal(state.decks.A.jogCommand.at, 12);
    assert.equal(held.audio.currentTime, 12);

    held.at(9);
    actions.syncJog("A");
    assert.equal(state.decks.A.jogCommand.at, 12);
    assert.equal(held.audio.currentTime, 12);

    held.at(10.04);
    actions.syncJog("A");
    assert.equal(state.decks.A.jogCommand, null);
    assert.equal(held.audio.currentTime, 10.04);
    actions.jog("A", 1);
    assert.equal(state.decks.A.jogCommand.at, 13);
    assert.equal(state.decks.A.jogCommand.from, 12);
  });
});

test("one FLX4 jog tick lands when the report reaches it", () => {
  const { state, audios, actions } = harness();
  const held = holdingDeck(state, "A");
  audios.A = held.audio;
  state.results = [{ id: "abcdefghijk", title: "曲", channel: "", duration: 120 }];
  return actions.loadSelected("A").then(() => {
    state.decks.A.status = "ready";
    held.at(10);
    const tick = (value) => dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb0, 0x22, value])), actions);
    assert.equal(tick(0x41), true);
    assert.ok(Math.abs(state.decks.A.jogCommand.at - 10.05) < 0.0001);
    assert.equal(state.decks.A.jogCommand.from, 10);
    actions.syncJog("A");
    assert.equal(state.decks.A.jogCommand.from, 10);
    assert.ok(Math.abs(held.audio.currentTime - 10.05) < 0.0001);

    held.at(state.decks.A.jogCommand.at);
    actions.syncJog("A");
    assert.equal(state.decks.A.jogCommand, null);
    held.at(10.2);
    assert.ok(Math.abs(held.audio.currentTime - 10.2) < 0.0001);
    assert.equal(tick(0x41), true);
    assert.ok(Math.abs(state.decks.A.jogCommand.at - 10.1) < 0.0001);
    assert.equal(state.decks.A.jogCommand.from, 10.05);
  });
});

test("a backward FLX4 tick lands on arrival and the next tick adds to that seek", () => {
  const { state, audios, actions } = harness();
  const held = holdingDeck(state, "A");
  audios.A = held.audio;
  state.results = [{ id: "abcdefghijk", title: "曲", channel: "", duration: 120 }];
  return actions.loadSelected("A").then(() => {
    state.decks.A.status = "ready";
    held.at(10);
    const tick = (value) => dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb0, 0x22, value])), actions);
    assert.equal(tick(0x3f), true);
    assert.ok(Math.abs(state.decks.A.jogCommand.at - 9.95) < 0.0001);
    assert.equal(state.decks.A.jogCommand.from, 10);
    assert.ok(Math.abs(held.audio.currentTime - 9.95) < 0.0001);

    held.at(9.97);
    actions.syncJog("A");
    assert.equal(state.decks.A.jogCommand, null);
    assert.ok(Math.abs(held.audio.currentTime - 9.97) < 0.0001);

    held.at(10.2);
    assert.ok(Math.abs(held.audio.currentTime - 10.2) < 0.0001);
    assert.equal(tick(0x41), true);
    assert.ok(Math.abs(state.decks.A.jogCommand.at - 10) < 0.0001);
    assert.equal(state.decks.A.jogCommand.from, 9.95);
  });
});

test("cue, the position bar, and load clear jog memory before reading or seeking", () => {
  const { state, audios, actions } = harness();
  const held = holdingDeck(state, "A");
  audios.A = held.audio;
  let cancels = 0;
  const cancelPendingSeek = held.audio.cancelPendingSeek.bind(held.audio);
  held.audio.cancelPendingSeek = () => {
    cancels += 1;
    cancelPendingSeek();
  };
  state.results = [{ id: "abcdefghijk", title: "曲", channel: "", duration: 120 }];
  return actions.loadSelected("A").then(() => {
    state.decks.A.status = "ready";
    held.at(10);
    state.decks.A.cue = 10;
    actions.jog("A", 5);
    assert.equal(held.audio.currentTime, 15);
    const beforeCue = cancels;
    actions.cue("A");
    assert.ok(cancels > beforeCue);
    assert.equal(state.decks.A.jogCommand, null);
    assert.equal(held.audio.paused, false);
    assert.equal(held.audio.currentTime, 10);

    held.audio.pause();
    actions.jog("A", 5);
    const beforeSeek = cancels;
    actions.seek("A", 4);
    assert.ok(cancels > beforeSeek);
    assert.equal(state.decks.A.jogCommand, null);
    assert.equal(held.audio.currentTime, 4);
    actions.jog("A", 1);
    assert.equal(state.decks.A.jogCommand.at, 5);

    const beforeLoad = cancels;
    return actions.loadTrack("A", { id: "abcdefghijk", title: "曲" }).then(() => {
      assert.ok(cancels > beforeLoad);
      assert.equal(state.decks.A.jogCommand, null);
      held.at(3);
      assert.equal(held.audio.currentTime, 3);
    });
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

    actions.seek("A", 115);
    actions.jog("A", 10);
    assert.equal(audios.A.currentTime, 120);
    actions.jog("A", -1000);
    assert.equal(audios.A.currentTime, 0);
    audios.A.duration = Number.NaN;
    actions.seek("A", 50);
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
    const mapBefore = { ...FLX4_MAP };
    assert.equal(
      dispatchControllerEvent({ type: "note", channel: 0, number: 1, value: 1 }, actions, {
        "note:0:1": { action: "jog", args: ["B", -1] },
      }),
      true,
    );
    assert.equal(audios.B.currentTime, 17);
    assert.equal(state.crossfader, fader);
    assert.deepEqual(FLX4_MAP, mapBefore);
  });
});

async function readyDecks() {
  const harnessed = harness();
  const { state, actions } = harnessed;
  state.results = [{ id: "abcdefghijk", title: "夜", channel: "A店", duration: 90 }];
  await actions.loadSelected("A");
  await actions.loadTrack("B", { id: "zzzzzzzzzzz", title: "昼", channel: "B店", duration: 80 });
  return harnessed;
}

test("holding the disc pauses playback", async () => {
  const { state, audios, actions } = await readyDecks();
  audios.A.currentTime = 12;
  state.decks.A.cue = 2;
  actions.togglePlay("A");
  actions.togglePlay("B");
  assert.equal(FLX4_MAP["note:0:11"].action, "togglePlay");
  assert.equal(FLX4_MAP["note:0:11"].hold, undefined);
  assert.equal(FLX4_MAP["note:0:54"].action, "pressDisc");
  assert.deepEqual(FLX4_MAP["note:0:54"].args, ["A"]);
  assert.equal(FLX4_MAP["note:0:54"].hold, true);
  assert.equal(FLX4_MAP["cc:0:34"].action, "jog");

  actions.pressDisc("A", true);
  assert.equal(audios.A.paused, true);
  assert.equal(state.decks.A.playing, false);
  assert.equal(audios.B.paused, false);
  assert.equal(audios.A.currentTime, 12);
  assert.equal(state.decks.A.cue, 2);

  actions.pressDisc("A", false);
  assert.equal(audios.A.paused, false);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0x90, 0x36, 0x7f])), actions), true);
  assert.equal(audios.A.paused, true);
  assert.equal(state.decks.A.playing, false);
  assert.equal(audios.B.paused, false);
  assert.equal(state.decks.B.playing, true);
  assert.equal(audios.A.currentTime, 12);
  assert.equal(state.crossfader, 0.5);
});

test("releasing the disc resumes a deck that was playing", async () => {
  const { state, audios, actions } = await readyDecks();
  audios.A.currentTime = 12;
  actions.togglePlay("A");
  actions.togglePlay("B");

  actions.pressDisc("A", true);
  assert.equal(audios.A.paused, true);
  actions.pressDisc("A", false);
  assert.equal(audios.A.paused, false);
  assert.equal(state.decks.A.playing, true);
  assert.equal(audios.A.currentTime, 12);
  assert.equal(audios.B.paused, false);

  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0x90, 0x36, 0x7f])), actions), true);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb0, 0x22, 0x41])), actions), true);
  assert.ok(Math.abs(audios.A.currentTime - (12 + JOG_STEP_SECONDS)) < 0.0001);
  assert.equal(audios.A.paused, true);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0x90, 0x36, 0x00])), actions), true);
  assert.equal(audios.A.paused, false);
  assert.equal(state.decks.A.playing, true);
  assert.equal(audios.B.paused, false);
  assert.ok(Math.abs(audios.A.currentTime - (12 + JOG_STEP_SECONDS)) < 0.0001);

  const touchOff = messageFromMidi(new Uint8Array([0x80, 0x36, 0x00]));
  assert.deepEqual(touchOff, { type: "note", channel: 0, number: 0x36, value: 0 });
  actions.pressDisc("A", true);
  assert.equal(dispatchControllerEvent(touchOff, actions), true);
  assert.equal(audios.A.paused, false);
  assert.equal(state.decks.A.playing, true);

  actions.togglePlay("A");
  assert.equal(audios.A.paused, true);
  actions.togglePlay("A");
  assert.equal(audios.A.paused, false);
  assert.equal(messageFromMidi(new Uint8Array([0x90, 0x0b, 0x00])), null);
  assert.equal(messageFromMidi(new Uint8Array([0x80, 0x0b, 0x00])), null);
});

test("releasing the disc does not start a deck that was already paused", async () => {
  const { state, audios, actions } = await readyDecks();
  audios.A.currentTime = 12;
  audios.B.currentTime = 4;
  assert.equal(audios.A.paused, true);
  assert.equal(state.decks.A.playing, false);

  actions.pressDisc("A", true);
  actions.pressDisc("A", false);
  assert.equal(audios.A.paused, true);
  assert.equal(state.decks.A.playing, false);
  assert.equal(audios.A.currentTime, 12);

  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0x91, 0x36, 0x7f])), actions), true);
  assert.equal(audios.B.paused, true);
  assert.equal(state.decks.B.playing, false);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0x91, 0x36, 0x00])), actions), true);
  assert.equal(audios.B.paused, true);
  assert.equal(state.decks.B.playing, false);
  assert.equal(audios.B.currentTime, 4);
  assert.equal(FLX4_MAP["note:1:54"].action, "pressDisc");
  assert.equal(FLX4_MAP["note:1:54"].hold, true);

  actions.togglePlay("A");
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0x90, 0x36, 0x00])), actions), true);
  assert.equal(audios.A.paused, false);
  assert.equal(state.decks.A.playing, true);
});

test("+ seeks deck B backward about 10 seconds from the commanded position", () => {
  const { state, audios, actions } = harness();
  state.results = [{ id: "abcdefghijk", title: "曲", channel: "", duration: 120 }];
  return actions.loadSelected("B").then(() => {
    let reported = 40;
    let commanded = 40;
    Object.defineProperty(audios.B, "currentTime", {
      configurable: true,
      get() {
        return reported;
      },
      set(value) {
        commanded = value;
      },
    });
    audios.B.duration = 120;
    const cue = state.decks.B.cue;
    const fader = state.crossfader;
    const jogKeys = Object.fromEntries(
      legendGroups()
        .find((group) => group.name === "ジョグ")
        .items.map((item) => [item.label, item.keys]),
    );
    assert.equal(jogKeys["デッキ A を戻す"], "[");
    assert.equal(jogKeys["デッキ A を進める"], "]");
    assert.equal(jogKeys["デッキ A を大きく戻す"], "Shift+[");
    assert.equal(jogKeys["デッキ A を大きく進める"], "Shift+]");
    assert.equal(jogKeys["デッキ B を戻す"], ";");
    assert.equal(jogKeys["デッキ B を進める"], "'");
    assert.equal(jogKeys["デッキ B を大きく戻す"], "Shift+; +");
    assert.equal(jogKeys["デッキ B を大きく進める"], "Shift+'");

    assert.equal(handleKeydown(keyEvent("+", bodyTarget()), actions), false);
    assert.equal(commanded, 40);
    assert.equal(handleKeydown(keyEvent("+", searchTarget(), { shiftKey: true }), actions), false);
    assert.equal(commanded, 40);

    assert.equal(handleKeydown(keyEvent("+", bodyTarget(), { shiftKey: true }), actions), true);
    assert.equal(commanded, 30);
    assert.equal(state.decks.B.jogCommand.at, 30);
    assert.equal(reported, 40);
    assert.equal(handleKeydown(keyEvent("+", bodyTarget(), { shiftKey: true }), actions), true);
    assert.equal(commanded, 20);
    assert.equal(state.decks.B.jogCommand.at, 20);
    assert.equal(reported, 40);
    assert.equal(handleKeydown(keyEvent(":", bodyTarget(), { shiftKey: true }), actions), true);
    assert.equal(commanded, 10);
    assert.equal(state.decks.B.cue, cue);
    assert.equal(state.crossfader, fader);
    assert.equal(audios.A.currentTime, 0);
  });
});

test("deck volume is independent of the crossfader", async () => {
  assert.equal(VOLUME_STEP, 0.05);
  const labels = legendGroups().find((group) => group.name === "音量").items.map((item) => item.label);
  assert.ok(labels.includes("デッキ A の音量を下げる"));
  assert.ok(labels.includes("デッキ A の音量を上げる"));
  assert.ok(labels.includes("デッキ B の音量を下げる"));
  assert.ok(labels.includes("デッキ B の音量を上げる"));

  const { state, audios, actions } = harness();
  assert.equal(state.decks.A.volume, 1);
  assert.equal(state.decks.B.volume, 1);
  actions.setCrossfader(state.crossfader);
  const center = deckGains(0.5);
  assert.ok(Math.abs(audios.A.volume - center.a) < 0.0001);
  assert.ok(Math.abs(audios.B.volume - center.b) < 0.0001);

  actions.setVolume("A", 1);
  actions.setVolume("B", 1);
  actions.setCrossfader(0.5);
  assert.ok(audios.A.volume > 0.7);
  assert.ok(audios.B.volume > 0.7);
  assert.equal(state.crossfader, 0.5);

  actions.setVolume("A", 0);
  assert.equal(state.decks.A.volume, 0);
  assert.equal(audios.A.volume, 0);
  assert.ok(audios.B.volume > 0.7);
  assert.equal(state.decks.B.volume, 1);
  assert.equal(state.crossfader, 0.5);
  actions.setCrossfader(0);
  assert.equal(audios.A.volume, 0);
  assert.equal(state.decks.A.volume, 0);
  assert.ok(audios.B.volume < 0.0001);
  actions.setCrossfader(1);
  assert.equal(audios.A.volume, 0);
  assert.ok(Math.abs(audios.B.volume - 1) < 0.0001);
  assert.equal(state.decks.B.volume, 1);

  actions.setVolume("B", 0);
  actions.setCrossfader(0);
  assert.equal(audios.B.volume, 0);
  assert.equal(state.decks.A.volume, 0);
  actions.setVolume("A", 1);
  assert.ok(Math.abs(audios.A.volume - 1) < 0.0001);
  assert.equal(audios.B.volume, 0);
  assert.equal(state.crossfader, 0);

  actions.setVolume("A", 1.4);
  assert.equal(state.decks.A.volume, 1);
  actions.setVolume("A", -0.2);
  assert.equal(state.decks.A.volume, 0);
  actions.setVolume("C", 0.5);
  actions.setVolume("A", Number.NaN);
  assert.equal(state.decks.A.volume, 0);
  actions.resetVolume("A");
  assert.equal(state.decks.A.volume, 1);
  actions.nudgeVolume("A", -VOLUME_STEP);
  assert.equal(state.decks.A.volume, 0.95);
  actions.nudgeVolume("B", VOLUME_STEP);
  assert.equal(state.decks.B.volume, 0.05);
  actions.nudgeVolume("A", 0);
  assert.equal(state.decks.A.volume, 0.95);
  assert.equal(state.decks.A.rate, 1);
  assert.equal(state.crossfader, 0);

  actions.setVolumeFromController("A", 127);
  assert.equal(state.decks.A.volume, 1);
  actions.setVolumeFromController("B", 0);
  assert.equal(state.decks.B.volume, 0);
  actions.setVolumeFromController("A", 64);
  assert.equal(state.decks.A.volume, 64 / 127);
  actions.setVolumeFromController("A", Number.NaN);
  assert.equal(state.decks.A.volume, 64 / 127);
  assert.equal(state.decks.B.volume, 0);

  state.results = [{ id: "abcdefghijk", title: "曲", channel: "", duration: 10 }];
  actions.setVolume("A", 0.25);
  actions.setVolume("B", 0.5);
  actions.setRate("A", 1.4);
  actions.setRate("B", 0.8);
  const fader = state.crossfader;
  const fade = deckGains(fader);
  await actions.loadSelected("A");
  assert.equal(state.decks.A.volume, 0.25);
  assert.ok(Math.abs(audios.A.volume - fade.a * 0.25) < 0.0001);
  assert.equal(state.decks.A.rate, 1);
  assert.equal(audios.A.playbackRate, 1);
  assert.equal(state.decks.B.volume, 0.5);
  assert.ok(Math.abs(audios.B.volume - fade.b * 0.5) < 0.0001);
  assert.equal(state.decks.B.rate, 0.8);
  assert.equal(audios.B.playbackRate, 0.8);
  assert.equal(state.crossfader, fader);
  actions.setVolumeFromController("A", 0);
  actions.setCrossfader(0);
  await actions.loadSelected("A");
  assert.equal(state.decks.A.volume, 0);
  assert.equal(audios.A.volume, 0);
  assert.equal(state.decks.A.rate, 1);
  assert.equal(state.crossfader, 0);
  actions.setCrossfader(1);
  assert.equal(state.decks.A.volume, 0);
  assert.equal(audios.A.volume, 0);
  assert.equal(state.decks.B.volume, 0.5);

  actions.setVolume("A", 0.4);
  assert.equal(handleKeydown(keyEvent("-", searchTarget()), actions), false);
  assert.equal(state.decks.A.volume, 0.4);
  assert.equal(handleKeydown(keyEvent("-", bodyTarget()), actions), true);
  assert.equal(state.decks.A.volume, 0.35);
  assert.equal(handleKeydown(keyEvent("=", bodyTarget()), actions), true);
  assert.equal(state.decks.A.volume, 0.4);
  assert.equal(handleKeydown(keyEvent("=", bodyTarget(), { shiftKey: true }), actions), true);
  assert.equal(state.decks.A.volume, 0.45);
  assert.equal(state.decks.B.volume, 0.5);
  actions.setCrossfader(0.5);
  const beforeFader = state.crossfader;
  assert.equal(handleKeydown(keyEvent(",", bodyTarget()), actions), true);
  assert.ok(state.crossfader < beforeFader);
  assert.equal(state.decks.A.volume, 0.45);
  assert.equal(state.decks.B.volume, 0.5);
  assert.equal(handleKeydown(keyEvent("<", bodyTarget(), { shiftKey: true }), actions), true);
  assert.equal(state.decks.B.volume, 0.45);
  assert.equal(state.decks.A.volume, 0.45);
  assert.equal(handleKeydown(keyEvent(">", bodyTarget(), { shiftKey: true }), actions), true);
  assert.equal(state.decks.B.volume, 0.5);
  assert.equal(handleKeydown(keyEvent("+", bodyTarget(), { shiftKey: true }), actions), true);
  assert.equal(state.decks.A.volume, 0.45);
  assert.equal(state.decks.B.volume, 0.5);
  actions.setRate("B", 1.2);
  assert.equal(handleKeydown(keyEvent("9", bodyTarget()), actions), true);
  assert.equal(state.decks.B.rate, 1.21);
  assert.equal(state.decks.B.volume, 0.5);
});

test("tempo clamps, nudges, resets, and stays callable from the action table", async () => {
  assert.equal(clampRate(Number.NaN), 1);
  assert.equal(clampRate(4), 2);
  assert.equal(clampRate(0.1), 0.5);
  assert.equal(rateFromMidi(0), 0.5);
  assert.equal(rateFromMidi(64), 1);
  assert.equal(rateFromMidi(127), 2);
  assert.equal(rateFromMidi(80), 1 + 16 / 63);
  assert.notEqual(rateFromMidi(80), 1.25);
  assert.equal(rateFromMidi(96), 1 + 32 / 63);
  assert.equal(formatRate(1), "1.00×");
  assert.equal(formatRate(1.13), "1.13×");
  assert.equal(RATE_STEP, 0.01);
  assert.ok(RATE_STEP < 0.25);

  const labels = legendGroups().find((group) => group.name === "テンポ").items.map((item) => item.label);
  assert.ok(labels.includes("デッキ A のテンポを上げる"));
  assert.ok(labels.includes("デッキ B のテンポを 1.0 に戻す"));
  const tempoKeys = BINDINGS.filter((binding) => binding.group === "テンポ").flatMap((binding) => binding.keys);
  assert.deepEqual(tempoKeys, ["1", "2", "3", "8", "9", "0"]);

  const deck = createDeckPlayer("A", "player-A");
  const seen = [];
  const listeners = {};
  deck.audio = {
    src: "",
    load() {},
    addEventListener(name, fn) {
      listeners[name] = fn;
    },
    set playbackRate(value) {
      seen.push(value);
    },
  };
  deck.playbackRate = 1.25;
  deck.playbackRate = 9;
  deck.playbackRate = 0.1;
  assert.equal(deck.playbackRate, 0.5);
  assert.deepEqual(seen, [1.25, 2, 0.5]);
  const midiRate = 1 + 16 / 63;
  deck.playbackRate = midiRate;
  assert.equal(deck.playbackRate, midiRate);
  assert.notEqual(deck.playbackRate, 1.25);
  deck.attach({});
  deck.playbackRate = 1.5;
  seen.length = 0;
  assert.equal(deck.loadVideo("abcdefghijk"), false);
  assert.equal(deck.audio.src.endsWith("/api/audio/abcdefghijk"), true);
  listeners.canplay();
  assert.deepEqual(seen, [1.5, 1.5]);

  const { state, audios, actions } = harness();
  assert.equal(state.decks.A.rate, 1);
  actions.setRate("A", 1.13);
  assert.equal(state.decks.A.rate, 1.13);
  assert.equal(audios.A.playbackRate, 1.13);
  actions.setRate("A", 1.5);
  assert.equal(state.decks.A.rate, 1.5);
  assert.equal(audios.A.playbackRate, 1.5);
  actions.nudgeRate("A", RATE_STEP);
  assert.equal(state.decks.A.rate, 1.51);
  actions.nudgeRate("A", 1);
  assert.equal(state.decks.A.rate, 2);
  actions.nudgeRate("B", -1);
  assert.equal(state.decks.B.rate, 0.5);
  actions.resetRate("B");
  assert.equal(state.decks.B.rate, 1);
  actions.setRate("C", 2);
  assert.equal(state.decks.A.rate, 2);

  state.results = [{ id: "abcdefghijk", title: "曲", channel: "", duration: 10 }];
  actions.setRate("B", 0.75);
  await actions.loadSelected("A");
  assert.equal(audios.A.playbackRate, 1);
  assert.equal(state.decks.A.rate, 1);
  assert.equal(state.decks.B.rate, 0.75);
  assert.equal(audios.B.playbackRate, 0.75);

  assert.equal(handleKeydown(keyEvent("2", searchTarget()), actions), false);
  assert.equal(state.decks.A.rate, 1);
  actions.resetRate("A");
  assert.equal(handleKeydown(keyEvent("2", bodyTarget()), actions), true);
  assert.equal(state.decks.A.rate, 1.01);
  assert.equal(audios.A.playbackRate, 1.01);
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

  const mapBefore = { ...FLX4_MAP };
  const cc = messageFromMidi(new Uint8Array([0xb0, 40, 127]));
  assert.equal(
    dispatchControllerEvent(cc, actions, {
      "cc:0:40": { action: "setRateFromController", args: ["A"], passValue: true },
    }),
    true,
  );
  assert.equal(state.decks.A.rate, 2);
  assert.equal(audios.A.playbackRate, 2);
  const center = messageFromMidi(new Uint8Array([0xb0, 40, 64]));
  assert.equal(
    dispatchControllerEvent(center, actions, {
      "cc:0:40": { action: "setRateFromController", args: ["B"], passValue: true },
    }),
    true,
  );
  assert.equal(state.decks.B.rate, 1);
  assert.deepEqual(FLX4_MAP, mapBefore);
});

test("loading a track resets only that deck tempo to 1.0 and centers its EQ", async () => {
  const { state, audios, actions } = harness();
  for (const deck of ["A", "B"]) {
    audios[deck].eqDb = {};
    audios[deck].setEqGain = (band, db) => {
      audios[deck].eqDb[band] = db;
      return true;
    };
  }
  state.results = [
    { id: "abcdefghijk", title: "曲", channel: "", duration: 10 },
    { id: "zzzzzzzzzzz", title: "次", channel: "", duration: 12 },
  ];
  actions.setEq("A", "high", 1);
  actions.setEq("A", "mid", 0);
  actions.setEq("A", "low", 0.8);
  actions.setEq("B", "high", 0.2);
  actions.setEq("B", "mid", 0.9);
  actions.setEq("B", "low", 0);
  actions.setRate("A", 1.5);
  actions.setRate("B", 0.75);
  actions.setVolume("A", 0.2);
  actions.setVolume("B", 0.4);

  await actions.loadSelected("A");
  assert.equal(state.decks.A.id, "abcdefghijk");
  assert.equal(state.decks.A.rate, 1);
  assert.equal(audios.A.playbackRate, 1);
  assert.equal(state.decks.A.volume, 0.2);
  assert.equal(state.decks.B.volume, 0.4);
  const loadedFade = deckGains(state.crossfader);
  assert.ok(Math.abs(audios.A.volume - loadedFade.a * 0.2) < 0.0001);
  assert.ok(Math.abs(audios.B.volume - loadedFade.b * 0.4) < 0.0001);
  assert.equal(state.decks.B.rate, 0.75);
  assert.equal(state.decks.A.eq.high, 0.5);
  assert.equal(state.decks.A.eq.mid, 0.5);
  assert.equal(state.decks.A.eq.low, 0.5);
  assert.equal(audios.A.eqDb.high, 0);
  assert.equal(audios.A.eqDb.mid, 0);
  assert.equal(audios.A.eqDb.low, 0);
  assert.equal(state.decks.B.rate, 0.75);
  assert.equal(audios.B.playbackRate, 0.75);
  assert.equal(state.decks.B.eq.high, 0.2);
  assert.equal(state.decks.B.eq.mid, 0.9);
  assert.equal(state.decks.B.eq.low, 0);
  assert.equal(audios.B.eqDb.high, eqGainDb(0.2));
  assert.equal(audios.B.eqDb.mid, eqGainDb(0.9));
  assert.equal(audios.B.eqDb.low, EQ_CUT_DB);

  state.selected = 1;
  actions.setRate("A", 1.25);
  actions.setVolume("A", 0.3);
  actions.setVolume("B", 0.15);
  actions.setEq("A", "high", 0.7);
  actions.setEq("A", "mid", 0.3);
  actions.setEq("A", "low", 1);
  actions.setRate("B", 2);
  actions.setEq("B", "high", 1);
  actions.setEq("B", "mid", 0);
  actions.setEq("B", "low", 0.8);
  await actions.loadSelected("B");
  assert.equal(state.decks.B.id, "zzzzzzzzzzz");
  assert.equal(state.decks.B.rate, 1);
  assert.equal(audios.B.playbackRate, 1);
  assert.equal(state.decks.B.volume, 0.15);
  assert.equal(state.decks.A.volume, 0.3);
  const loadedFadeB = deckGains(state.crossfader);
  assert.ok(Math.abs(audios.B.volume - loadedFadeB.b * 0.15) < 0.0001);
  assert.ok(Math.abs(audios.A.volume - loadedFadeB.a * 0.3) < 0.0001);
  assert.equal(state.decks.A.rate, 1.25);
  assert.equal(state.decks.B.eq.high, 0.5);
  assert.equal(state.decks.B.eq.mid, 0.5);
  assert.equal(state.decks.B.eq.low, 0.5);
  assert.equal(audios.B.eqDb.high, 0);
  assert.equal(audios.B.eqDb.mid, 0);
  assert.equal(audios.B.eqDb.low, 0);
  assert.equal(state.decks.A.rate, 1.25);
  assert.equal(audios.A.playbackRate, 1.25);
  assert.equal(state.decks.A.eq.high, 0.7);
  assert.equal(state.decks.A.eq.mid, 0.3);
  assert.equal(state.decks.A.eq.low, 1);
  assert.equal(audios.A.eqDb.high, eqGainDb(0.7));
  assert.equal(audios.A.eqDb.mid, eqGainDb(0.3));
  assert.equal(audios.A.eqDb.low, EQ_BOOST_DB);

  actions.setRate("A", 2);
  actions.setEq("A", "high", 1);
  actions.setEq("A", "mid", 0.1);
  actions.setEq("A", "low", 0.9);
  actions.setRate("B", 0.5);
  actions.setEq("B", "high", 0.2);
  actions.setEq("B", "mid", 0.4);
  actions.setEq("B", "low", 0.6);
  const mapBefore = { ...FLX4_MAP };
  const note = messageFromMidi(new Uint8Array([0x90, 20, 40]));
  assert.equal(
    dispatchControllerEvent(note, actions, {
      "note:0:20": { action: "loadTrack", args: ["A", { id: "yyy", title: "別" }] },
    }),
    true,
  );
  assert.equal(state.decks.A.id, "yyy");
  assert.equal(state.decks.A.rate, 1);
  assert.equal(audios.A.playbackRate, 1);
  assert.equal(state.decks.A.volume, 0.3);
  assert.equal(state.decks.B.volume, 0.15);
  assert.equal(state.decks.A.eq.high, 0.5);
  assert.equal(state.decks.A.eq.mid, 0.5);
  assert.equal(state.decks.A.eq.low, 0.5);
  assert.equal(audios.A.eqDb.high, 0);
  assert.equal(audios.A.eqDb.mid, 0);
  assert.equal(audios.A.eqDb.low, 0);
  assert.equal(state.decks.B.rate, 0.5);
  assert.equal(audios.B.playbackRate, 0.5);
  assert.equal(state.decks.B.eq.high, 0.2);
  assert.equal(state.decks.B.eq.mid, 0.4);
  assert.equal(state.decks.B.eq.low, 0.6);
  assert.equal(audios.B.eqDb.low, eqGainDb(0.6));
  assert.deepEqual(FLX4_MAP, mapBefore);

  assert.equal(handleKeydown(keyEvent("1", bodyTarget()), actions), true);
  assert.equal(state.decks.A.rate, 0.99);
  assert.equal(audios.A.playbackRate, 0.99);
  assert.equal(handleKeydown(keyEvent("2", bodyTarget()), actions), true);
  assert.equal(state.decks.A.rate, 1);
  assert.equal(handleKeydown(keyEvent("3", bodyTarget()), actions), true);
  assert.equal(state.decks.A.rate, 1);
  assert.equal(audios.A.playbackRate, 1);

  assert.equal(handleKeydown(keyEvent("9", bodyTarget()), actions), true);
  assert.equal(state.decks.B.rate, 0.51);
  assert.equal(audios.B.playbackRate, 0.51);
  assert.equal(handleKeydown(keyEvent("8", bodyTarget()), actions), true);
  assert.equal(state.decks.B.rate, 0.5);
  assert.equal(handleKeydown(keyEvent("0", bodyTarget()), actions), true);
  assert.equal(state.decks.B.rate, 1);
  assert.equal(audios.B.playbackRate, 1);
  assert.equal(state.decks.A.rate, 1);

  assert.equal(handleKeydown(keyEvent("r", bodyTarget()), actions), true);
  assert.equal(state.decks.A.eq.high, 0.6);
  assert.equal(audios.A.eqDb.high, eqGainDb(0.6));
  assert.equal(handleKeydown(keyEvent("e", bodyTarget()), actions), true);
  assert.equal(state.decks.A.eq.high, 0.5);
  assert.equal(handleKeydown(keyEvent("f", bodyTarget()), actions), true);
  assert.equal(state.decks.A.eq.mid, 0.6);
  assert.equal(handleKeydown(keyEvent("d", bodyTarget()), actions), true);
  assert.equal(state.decks.A.eq.mid, 0.5);
  assert.equal(handleKeydown(keyEvent("v", bodyTarget()), actions), true);
  assert.equal(state.decks.A.eq.low, 0.6);
  assert.equal(handleKeydown(keyEvent("c", bodyTarget()), actions), true);
  assert.equal(state.decks.A.eq.low, 0.5);
  assert.equal(handleKeydown(keyEvent("4", bodyTarget()), actions), true);
  assert.equal(state.decks.A.eq.high, 0.5);
  assert.equal(state.decks.A.eq.mid, 0.5);
  assert.equal(state.decks.A.eq.low, 0.5);
  assert.equal(handleKeydown(keyEvent("o", bodyTarget()), actions), true);
  assert.equal(state.decks.B.eq.high, 0.3);
  assert.equal(handleKeydown(keyEvent("i", bodyTarget()), actions), true);
  assert.equal(state.decks.B.eq.high, 0.2);
  assert.equal(handleKeydown(keyEvent("7", bodyTarget()), actions), true);
  assert.equal(state.decks.B.eq.high, 0.5);
  assert.equal(state.decks.B.eq.mid, 0.5);
  assert.equal(state.decks.B.eq.low, 0.5);
  assert.equal(state.decks.A.eq.low, 0.5);

  actions.nudgeRate("A", -4);
  assert.equal(state.decks.A.rate, 0.5);
  assert.equal(audios.A.playbackRate, 0.5);
  actions.nudgeRate("B", 8);
  assert.equal(state.decks.B.rate, 2);
  assert.equal(audios.B.playbackRate, 2);

  const elementRates = [];
  const deck = createDeckPlayer("A", "player-A");
  deck.audio = {
    src: "",
    load() {},
    pause() {},
    set playbackRate(value) {
      elementRates.push(value);
    },
  };
  deck.playbackRate = 1.75;
  const eqGains = [];
  deck.setEqGain = (band, db) => {
    eqGains.push([band, db]);
    return true;
  };
  elementRates.length = 0;
  const played = freshState();
  played.decks.A.eq = { high: 1, mid: 0, low: 0.2 };
  played.decks.B.rate = 0.5;
  played.decks.B.eq = { high: 0.1, mid: 0.2, low: 0.3 };
  const other = fakeAudio();
  other.playbackRate = 0.5;
  other.eqDb = { high: eqGainDb(0.1), mid: eqGainDb(0.2), low: eqGainDb(0.3) };
  other.setEqGain = (band, db) => {
    other.eqDb[band] = db;
    return true;
  };
  const deckActions = createActions({
    state: played,
    audios: { A: deck, B: other },
    scheduleRender() {},
    queryValue: () => "",
  });
  await deckActions.loadTrack("A", { id: "abcdefghijk", title: "曲" });
  assert.equal(deck.playbackRate, 1);
  assert.equal(elementRates.at(-1), 1);
  assert.ok(elementRates.every((value) => value === 1));
  assert.equal(played.decks.A.rate, 1);
  assert.equal(played.decks.A.eq.high, 0.5);
  assert.equal(played.decks.A.eq.mid, 0.5);
  assert.equal(played.decks.A.eq.low, 0.5);
  assert.deepEqual(eqGains.slice(-3), [
    ["high", 0],
    ["mid", 0],
    ["low", 0],
  ]);
  assert.equal(played.decks.B.rate, 0.5);
  assert.equal(played.decks.B.eq.high, 0.1);
  assert.equal(played.decks.B.eq.mid, 0.2);
  assert.equal(played.decks.B.eq.low, 0.3);
  assert.equal(other.playbackRate, 0.5);
  assert.equal(other.eqDb.high, eqGainDb(0.1));
});

test("eq gain mapping drives the filter and the shared actions", () => {
  assert.equal(eqGainDb(0.5), 0);
  assert.equal(eqGainDb(1), EQ_BOOST_DB);
  assert.equal(eqGainDb(0), EQ_CUT_DB);
  assert.equal(eqGainDb(2), EQ_BOOST_DB);
  assert.equal(eqGainDb(-1), EQ_CUT_DB);
  assert.ok(eqGainDb(0.2) < eqGainDb(0.4));
  assert.ok(eqGainDb(0.4) < eqGainDb(0.5));
  assert.ok(eqGainDb(0.5) < eqGainDb(0.7));
  assert.ok(eqGainDb(0.7) < eqGainDb(1));
  assert.equal(eqUnitFromMidi(0), 0);
  assert.equal(eqUnitFromMidi(64), 0.5);
  assert.equal(eqUnitFromMidi(127), 1);
  assert.equal(formatEqDb(0), "0.0 dB");
  assert.equal(formatEqDb(EQ_BOOST_DB), "+12.0 dB");
  assert.equal(formatEqDb(EQ_CUT_DB), "-36.0 dB");

  const source = { next: null, connect(target) { this.next = target; } };
  const context = {
    destination: { name: "out" },
    createBiquadFilter() {
      return {
        type: "",
        frequency: { value: 0 },
        Q: { value: 0 },
        gain: { value: 0 },
        connect(target) { this.next = target; },
      };
    },
  };
  const nodes = connectEqGraph(source, context);
  assert.equal(source.next, nodes.high);
  assert.equal(nodes.high.next, nodes.mid);
  assert.equal(nodes.mid.next, nodes.low);
  assert.equal(nodes.low.next, context.destination);
  assert.equal(nodes.high.type, "highshelf");
  assert.equal(nodes.high.frequency.value, 10000);
  assert.equal(nodes.mid.type, "peaking");
  assert.equal(nodes.mid.frequency.value, 1000);
  assert.equal(nodes.low.type, "lowshelf");
  assert.equal(nodes.low.frequency.value, 100);
  assert.equal(nodes.low.gain.value, 0);

  const labels = legendGroups().find((group) => group.name === "イコライザー").items.map((item) => item.label);
  assert.ok(labels.includes("デッキ A の LOW を下げる"));
  assert.ok(labels.includes("デッキ B の HIGH を上げる"));
  const eqKeys = new Set(BINDINGS.filter((binding) => binding.group === "イコライザー").flatMap((binding) => binding.keys));
  for (const key of ["a", "q", "z", "m", "[", "]", ";", "'", "1", "2", "3", "8", "9", "0"]) {
    assert.equal(eqKeys.has(key), false, key);
  }

  const { state, audios, actions } = harness();
  for (const deck of ["A", "B"]) {
    audios[deck].eqDb = {};
    audios[deck].setEqGain = (band, db) => {
      audios[deck].eqDb[band] = db;
      return audios[deck].eqLive !== false;
    };
  }
  actions.setEq("A", "low", 0);
  assert.equal(state.decks.A.eq.low, 0);
  assert.equal(audios.A.eqDb.low, EQ_CUT_DB);
  actions.nudgeEq("A", "low", EQ_STEP);
  assert.equal(state.decks.A.eq.low, 0.1);
  assert.equal(audios.A.eqDb.low, eqGainDb(0.1));
  actions.nudgeEq("A", "low", -1);
  assert.equal(state.decks.A.eq.low, 0);
  actions.setEq("A", "nope", 1);
  assert.equal(state.decks.A.eq.high, 0.5);
  actions.resetEq("A", "low");
  assert.equal(state.decks.A.eq.low, 0.5);
  assert.equal(audios.A.eqDb.low, 0);

  assert.equal(handleKeydown(keyEvent("r", searchTarget()), actions), false);
  assert.equal(handleKeydown(keyEvent("r", bodyTarget()), actions), true);
  assert.equal(state.decks.A.eq.high, 0.6);
  assert.equal(audios.A.eqDb.high, eqGainDb(0.6));
  assert.equal(handleKeydown(keyEvent("4", bodyTarget()), actions), true);
  assert.equal(state.decks.A.eq.high, 0.5);
  assert.equal(state.decks.A.eq.mid, 0.5);
  assert.equal(state.decks.A.eq.low, 0.5);
  assert.equal(audios.A.eqDb.high, 0);
  const before = state.decks.A.eq.low;
  assert.equal(handleKeydown(keyEvent("[", bodyTarget()), actions), true);
  assert.equal(state.decks.A.eq.low, before);

  const mapBefore = { ...FLX4_MAP };
  const cc = messageFromMidi(new Uint8Array([0xb0, 7, 0]));
  assert.equal(
    dispatchControllerEvent(cc, actions, {
      "cc:0:7": { action: "setEqFromController", args: ["B", "low"], passValue: true },
    }),
    true,
  );
  assert.equal(state.decks.B.eq.low, 0);
  assert.equal(audios.B.eqDb.low, EQ_CUT_DB);
  assert.equal(
    dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb0, 7, 64])), actions, {
      "cc:0:7": { action: "setEqFromController", args: ["B", "low"], passValue: true },
    }),
    true,
  );
  assert.equal(state.decks.B.eq.low, 0.5);
  assert.equal(audios.B.eqDb.low, 0);
  assert.equal(FLX4_MAP["cc:0:7"].action, "setEqFromController");
  assert.deepEqual(FLX4_MAP["cc:0:7"].args, ["A", "high"]);
  assert.deepEqual(FLX4_MAP, mapBefore);

  audios.A.eqLive = false;
  actions.setEq("A", "mid", 1);
  assert.equal(state.decks.A.eq.mid, 1);
  assert.equal(state.decks.A.eqError, "イコライザーを音声に接続できませんでした");
  audios.A.eqLive = true;
  actions.resetEq("A", "mid");
  assert.equal(state.decks.A.eqError, "");
  assert.equal(audios.A.eqDb.mid, 0);

  state.results = [{ id: "abcdefghijk", title: "曲", channel: "", duration: 10 }];
  actions.setEq("A", "high", 1);
  actions.setEq("A", "mid", 0);
  actions.setEq("A", "low", 0.2);
  actions.setEq("B", "high", 0.7);
  actions.setEq("B", "mid", 0.1);
  actions.setEq("B", "low", 0.9);
  return actions.loadSelected("A").then(() => {
    assert.equal(state.decks.A.eq.high, 0.5);
    assert.equal(state.decks.A.eq.mid, 0.5);
    assert.equal(state.decks.A.eq.low, 0.5);
    assert.equal(audios.A.eqDb.high, 0);
    assert.equal(audios.A.eqDb.mid, 0);
    assert.equal(audios.A.eqDb.low, 0);
    assert.equal(state.decks.B.eq.high, 0.7);
    assert.equal(state.decks.B.eq.mid, 0.1);
    assert.equal(state.decks.B.eq.low, 0.9);
    assert.equal(audios.B.eqDb.high, eqGainDb(0.7));
    assert.equal(state.decks.A.rate, 1);
    assert.equal(audios.A.playbackRate, 1);
    assert.equal(handleKeydown(keyEvent("r", bodyTarget()), actions), true);
    assert.equal(state.decks.A.eq.high, 0.6);
    assert.equal(audios.A.eqDb.high, eqGainDb(0.6));
    assert.equal(handleKeydown(keyEvent("u", bodyTarget()), actions), true);
    assert.equal(state.decks.B.eq.mid, 0.2);
    assert.equal(handleKeydown(keyEvent("4", bodyTarget()), actions), true);
    assert.equal(state.decks.A.eq.high, 0.5);
    assert.equal(state.decks.B.eq.mid, 0.2);
  });
});

test("playlists stay off reserved keys and load through the search path", async () => {
  function bound(key, shift = false) {
    return BINDINGS.find((binding) => !!binding.shift === shift && binding.keys.includes(key))?.action;
  }
  assert.equal(bound("a"), "loadSelected");
  assert.equal(bound("b"), "loadSelected");
  assert.equal(bound("a", true), "loadPlaylistTrack");
  assert.equal(bound("b", true), "loadPlaylistTrack");
  assert.equal(bound("p"), "focusPlaylistName");
  assert.equal(bound("l"), "addSearchHit");
  assert.equal(bound("s"), "addDeckTrack");
  assert.equal(bound("s", true), "addDeckTrack");
  assert.equal(bound("j"), "moveSelection");
  assert.equal(bound("k"), "moveSelection");
  assert.equal(bound("g"), "movePlaylistSelection");
  assert.equal(bound("5"), "movePlaylistTrack");
  assert.equal(bound("6"), "movePlaylistTrack");
  assert.equal(bound("e"), "nudgeEq");
  assert.equal(bound("r"), "nudgeEq");
  assert.equal(bound("4"), "resetEq");
  assert.equal(bound("h"), "nudgeEq");
  assert.equal(bound("7"), "resetEq");
  assert.equal(bound("1"), "nudgeRate");
  assert.equal(bound("/"), "focusSearch");
  const labels = legendGroups().find((group) => group.name === "プレイリスト").items.map((item) => item.label);
  assert.ok(labels.includes("検索の曲を追加"));
  assert.ok(labels.includes("プレイリストをデッキ A へ"));
  assert.ok(labels.includes("上の曲"));
  assert.ok(labels.includes("下の曲"));

  const db = { playlists: [] };
  let seq = 0;
  let nameValue = "";
  let nameFocused = false;
  const { state, audios, actions } = harness({
    deps: {
      playlistNameValue: () => nameValue,
      setPlaylistNameValue(value) {
        nameValue = value;
      },
      focusPlaylistNameElement() {
        nameFocused = true;
      },
      blurPlaylistNameElement() {
        nameFocused = false;
      },
      confirmDelete: () => true,
      async createPlaylist(name) {
        const playlist = { id: `abc${String(++seq).padStart(8, "0")}`, name, tracks: [] };
        db.playlists.push(playlist);
        return JSON.parse(JSON.stringify(playlist));
      },
      async renamePlaylist(id, name) {
        const playlist = db.playlists.find((item) => item.id === id);
        playlist.name = name;
        return JSON.parse(JSON.stringify(playlist));
      },
      async deletePlaylist(id) {
        db.playlists = db.playlists.filter((item) => item.id !== id);
      },
      async addPlaylistTrack(id, track) {
        const playlist = db.playlists.find((item) => item.id === id);
        const index = track.index;
        const fields = { ...track };
        delete fields.index;
        playlist.tracks.splice(index, 0, fields);
        return JSON.parse(JSON.stringify(playlist));
      },
      async removePlaylistTrack(id, index) {
        const playlist = db.playlists.find((item) => item.id === id);
        playlist.tracks.splice(index, 1);
        return JSON.parse(JSON.stringify(playlist));
      },
      async movePlaylistTrack(id, from, to) {
        const playlist = db.playlists.find((item) => item.id === id);
        const [item] = playlist.tracks.splice(from, 1);
        playlist.tracks.splice(to, 0, item);
        return JSON.parse(JSON.stringify(playlist));
      },
    },
  });

  const search = searchTarget();
  const nameField = { id: "playlist-name", tagName: "INPUT", type: "text", closest() { return null; } };
  state.results = [
    {
      id: "abcdefghijk",
      title: "検索曲",
      channel: "人",
      duration: 90,
      thumbnail: "https://i.ytimg.com/vi/abcdefghijk/mqdefault.jpg",
    },
  ];
  assert.equal(handleKeydown(keyEvent("l", search), actions), false);
  assert.equal(handleKeydown(keyEvent("p", search), actions), false);
  assert.equal(handleKeydown(keyEvent("5", search), actions), false);
  assert.equal(handleKeydown(keyEvent("s", search), actions), false);
  assert.equal(handleKeydown(keyEvent("a", search, { shiftKey: true }), actions), false);
  assert.equal(handleKeydown(keyEvent("PageDown", search), actions), false);
  assert.equal(handleKeydown(keyEvent("Backspace", search), actions), false);
  assert.equal(state.playlists.length, 0);
  assert.equal(handleKeydown(keyEvent("l", nameField), actions), false);
  assert.equal(handleKeydown(keyEvent("5", nameField), actions), false);
  assert.equal(handleKeydown(keyEvent("a", nameField, { shiftKey: true }), actions), false);

  assert.equal(handleKeydown(keyEvent("p", bodyTarget()), actions), true);
  assert.equal(nameFocused, true);
  assert.equal(nameValue, "");
  nameValue = "夜";
  assert.equal(handleKeydown(keyEvent("Enter", nameField), actions), true);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(state.playlists.length, 1);
  assert.equal(state.playlists[0].name, "夜");
  assert.equal(state.playlistId, state.playlists[0].id);

  assert.equal(handleKeydown(keyEvent("l", bodyTarget()), actions), true);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(state.playlists[0].tracks[0], {
    id: "abcdefghijk",
    title: "検索曲",
    channel: "人",
    duration: 90,
    thumbnail: "https://i.ytimg.com/vi/abcdefghijk/mqdefault.jpg",
  });

  state.decks.B.track = { id: "zzzzzzzzzzz", title: "デッキ", channel: "店", duration: 12, thumbnail: null };
  state.decks.B.id = "zzzzzzzzzzz";
  assert.equal(handleKeydown(keyEvent("s", bodyTarget(), { shiftKey: true }), actions), true);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(state.playlists[0].tracks[1].id, "zzzzzzzzzzz");
  assert.equal(state.playlistIndex, 1);

  assert.equal(handleKeydown(keyEvent("5", bodyTarget()), actions), true);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(state.playlists[0].tracks[0].id, "zzzzzzzzzzz");
  assert.equal(state.playlistIndex, 0);

  await actions.placePlaylistTrack(0, 1);
  assert.equal(state.playlists[0].tracks[0].id, "abcdefghijk");
  assert.equal(state.playlists[0].tracks[1].id, "zzzzzzzzzzz");
  assert.equal(state.playlistIndex, 1);
  await actions.placePlaylistTrack(1, 1);
  assert.equal(state.playlists[0].tracks[1].id, "zzzzzzzzzzz");
  await actions.placePlaylistTrack(1, 0);
  assert.equal(state.playlists[0].tracks[0].id, "zzzzzzzzzzz");
  assert.equal(state.playlistIndex, 0);
  await actions.placePlaylistTrack(-1, 0);
  assert.equal(state.playlists[0].tracks[0].id, "zzzzzzzzzzz");

  assert.equal(handleKeydown(keyEvent("a", bodyTarget()), actions), true);
  await Promise.resolve();
  assert.equal(audios.A.videoId, "abcdefghijk");
  assert.equal(handleKeydown(keyEvent("a", bodyTarget(), { shiftKey: true }), actions), true);
  await Promise.resolve();
  assert.equal(audios.A.videoId, "zzzzzzzzzzz");
  assert.equal(state.decks.A.title, "デッキ");
  assert.equal(state.decks.A.track.id, "zzzzzzzzzzz");
  await actions.loadSelected("B");
  assert.equal(audios.B.videoId, "abcdefghijk");
  await actions.loadPlaylistTrack("B");
  assert.equal(audios.B.videoId, "zzzzzzzzzzz");

  assert.equal(handleKeydown(keyEvent("Delete", bodyTarget()), actions), true);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(state.playlists[0].tracks.length, 1);
  assert.equal(state.playlists[0].tracks[0].id, "abcdefghijk");

  assert.equal(handleKeydown(keyEvent("p", bodyTarget(), { shiftKey: true }), actions), true);
  assert.equal(nameValue, "夜");
  assert.equal(state.playlistNaming, "rename");
  nameValue = "朝";
  assert.equal(handleKeydown(keyEvent("Enter", nameField), actions), true);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(state.playlists[0].name, "朝");

  nameValue = "残す";
  await actions.renamePlaylist("昼");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(state.playlists[0].name, "昼");
  assert.equal(nameValue, "残す");
  await actions.renamePlaylist("   ");
  assert.equal(state.playlistError, "名前を入れてください");
  assert.equal(state.playlists[0].name, "昼");

  assert.equal(handleKeydown(keyEvent("Backspace", bodyTarget(), { shiftKey: true }), actions), true);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(state.playlists.length, 0);

  assert.equal(handleKeydown(keyEvent("p", bodyTarget()), actions), true);
  nameValue = "昼";
  assert.equal(handleKeydown(keyEvent("Enter", nameField), actions), true);
  await new Promise((resolve) => setTimeout(resolve, 0));
  const morning = { id: "abcdefghijk", title: "検索曲", channel: "人", duration: 90, thumbnail: null };
  await actions.addTrackToPlaylist(state.playlists[0].id, morning);
  assert.equal(state.playlists[0].tracks[0].id, "abcdefghijk");
  await actions.addTrackToPlaylist("missing-id", morning);
  assert.equal(state.playlistError, "プレイリストを作ってください");
});

test("a failed drag puts the playlist order back", async () => {
  const { state, actions } = harness({
    deps: {
      async movePlaylistTrack() {
        throw new Error("保存できません");
      },
    },
  });
  state.playlists = [
    {
      id: "abc00000001",
      name: "夜",
      tracks: [
        { id: "aaaaaaaaaaa", title: "朝", channel: "", duration: 1, thumbnail: null },
        { id: "bbbbbbbbbbb", title: "昼", channel: "", duration: 1, thumbnail: null },
      ],
    },
  ];
  state.playlistId = "abc00000001";
  state.playlistIndex = 0;
  await actions.placePlaylistTrack(0, 1);
  assert.equal(state.playlists[0].tracks[0].id, "aaaaaaaaaaa");
  assert.equal(state.playlists[0].tracks[1].id, "bbbbbbbbbbb");
  assert.equal(state.playlistIndex, 0);
  assert.equal(state.playlistError, "保存できません");
});

test("a drag release moves the grabbed song after 6 changed the indexes", async () => {
  const moves = [];
  const song = (id, title) => ({ id, title, channel: "", duration: 1, thumbnail: null });
  const afterKey = [song("bbbbbbbbbbb", "B"), song("aaaaaaaaaaa", "A"), song("ccccccccccc", "C")];
  let saved = afterKey.map((track) => ({ ...track }));
  const { state, actions } = harness({
    deps: {
      async movePlaylistTrack(id, from, to) {
        moves.push({ id, from, to });
        const next = saved.map((track) => ({ ...track }));
        const [item] = next.splice(from, 1);
        next.splice(to, 0, item);
        saved = next;
        return { id, name: "夜", tracks: next.map((track) => ({ ...track })) };
      },
    },
  });
  state.playlists = [{ id: "abc00000001", name: "夜", tracks: afterKey.map((track) => ({ ...track })) }];
  state.playlistId = "abc00000001";
  state.playlistIndex = 1;

  await actions.placePlaylistTrack(0, 2, "missing-song");
  assert.deepEqual(moves, []);
  assert.deepEqual(
    state.playlists[0].tracks.map((track) => track.id),
    ["bbbbbbbbbbb", "aaaaaaaaaaa", "ccccccccccc"],
  );

  // A was grabbed at index 0. 6 then moved A down, so the list is [B, A, C].
  // Dropping A at the bottom must not apply index 0 to this later list.
  await actions.placePlaylistTrack(0, 2, "aaaaaaaaaaa");
  assert.deepEqual(moves, [{ id: "abc00000001", from: 1, to: 2 }]);
  assert.deepEqual(
    state.playlists[0].tracks.map((track) => track.id),
    ["bbbbbbbbbbb", "ccccccccccc", "aaaaaaaaaaa"],
  );
  assert.equal(state.playlistIndex, 2);
});

test("playlist tab j/k, browse, and LOAD use that playlist", async () => {
  const { state, audios, actions } = harness();
  state.results = [
    { id: "abcdefghijk", title: "夜" },
    { id: "zzzzzzzzzzz", title: "昼" },
    { id: "yyyyyyyyyyy", title: "朝" },
  ];
  state.selected = 1;
  state.playlists = [
    {
      id: "abc00000001",
      name: "夜",
      tracks: [
        { id: "aaaaaaaaaaa", title: "1" },
        { id: "bbbbbbbbbbb", title: "2" },
        { id: "ccccccccccc", title: "3" },
      ],
    },
  ];
  state.playlistId = "abc00000001";
  state.playlistIndex = 1;
  state.library = "playlist";

  assert.equal(handleKeydown(keyEvent("j", bodyTarget()), actions, "playlist"), true);
  assert.equal(state.playlistIndex, 2);
  assert.equal(state.selected, 1);
  assert.equal(handleKeydown(keyEvent("k", bodyTarget()), actions, "playlist"), true);
  assert.equal(state.playlistIndex, 1);
  assert.equal(state.selected, 1);
  assert.equal(handleKeydown(keyEvent("ArrowDown", bodyTarget()), actions, "playlist"), true);
  assert.equal(state.playlistIndex, 2);
  assert.equal(handleKeydown(keyEvent("ArrowUp", bodyTarget()), actions, "playlist"), true);
  assert.equal(state.playlistIndex, 1);

  const nameField = { id: "playlist-name", tagName: "INPUT", type: "text", closest() { return null; } };
  assert.equal(handleKeydown(keyEvent("j", nameField), actions, "playlist"), false);
  assert.equal(state.playlistIndex, 1);

  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb6, 0x40, 0x01])), actions), true);
  assert.equal(state.playlistIndex, 2);
  assert.equal(state.selected, 1);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb6, 0x40, 0x7f])), actions), true);
  assert.equal(state.playlistIndex, 1);
  assert.equal(state.selected, 1);

  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0x96, 0x46, 0x7f])), actions), true);
  await Promise.resolve();
  assert.equal(state.decks.A.id, "bbbbbbbbbbb");
  assert.equal(audios.A.videoId, "bbbbbbbbbbb");
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0x96, 0x47, 0x7f])), actions), true);
  await Promise.resolve();
  assert.equal(state.decks.B.id, "bbbbbbbbbbb");
  assert.equal(audios.B.videoId, "bbbbbbbbbbb");

  assert.equal(handleKeydown(keyEvent("a", bodyTarget()), actions, "playlist"), true);
  await Promise.resolve();
  assert.equal(state.decks.A.id, "zzzzzzzzzzz");

  state.library = "search";
  assert.equal(handleKeydown(keyEvent("j", bodyTarget()), actions, "search"), true);
  assert.equal(state.selected, 2);
  assert.equal(state.playlistIndex, 1);
  assert.equal(handleKeydown(keyEvent("k", bodyTarget()), actions, "search"), true);
  assert.equal(state.selected, 1);
  assert.equal(state.playlistIndex, 1);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb6, 0x40, 0x01])), actions), true);
  assert.equal(state.selected, 2);
  assert.equal(state.playlistIndex, 1);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb6, 0x40, 0x7f])), actions), true);
  assert.equal(state.selected, 1);
  assert.equal(state.playlistIndex, 1);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0x96, 0x46, 0x7f])), actions), true);
  await Promise.resolve();
  assert.equal(state.decks.A.id, "zzzzzzzzzzz");
  assert.equal(state.playlistIndex, 1);
});

test("a deck with no audio source does not start, and the other deck is left alone", async () => {
  const { state, audios, actions } = harness();
  state.results = [
    { id: "abcdefghijk", title: "夜", channel: "A店", duration: 90 },
    { id: "zzzzzzzzzzz", title: "昼", channel: "B店", duration: 80 },
  ];
  await actions.loadSelected("A");
  state.selected = 1;
  await actions.loadSelected("B");
  state.decks.B.rate = 1.2;
  state.decks.B.eq = { high: 0.8, mid: 0.4, low: 0.2 };
  audios.B.playbackRate = 1.2;
  actions.togglePlay("B");
  assert.equal(audios.B.paused, false);

  state.decks.A.status = "error";
  state.decks.A.playing = false;
  state.decks.A.error = SOURCE_UNAVAILABLE;
  audios.A.pause();
  const deckB = {
    id: state.decks.B.id,
    status: state.decks.B.status,
    error: state.decks.B.error,
    playError: state.decks.B.playError,
    playing: state.decks.B.playing,
    rate: state.decks.B.rate,
    eq: { ...state.decks.B.eq },
    videoId: audios.B.videoId,
  };

  assert.equal(sourcePlaybackBlocked(state.decks.A), true);
  assert.equal(sourcePlaybackBlocked(state.decks.B), false);
  assert.equal(FLX4_MAP["note:0:11"].action, "togglePlay");
  assert.deepEqual(FLX4_MAP["note:0:11"].args, ["A"]);
  assert.equal(FLX4_MAP["note:1:11"].action, "togglePlay");
  assert.deepEqual(FLX4_MAP["note:1:11"].args, ["B"]);

  assert.equal(handleKeydown(keyEvent("q", bodyTarget()), actions), true);
  assert.equal(audios.A.paused, true);
  assert.equal(state.decks.A.playing, false);
  assert.equal(state.decks.A.status, "error");
  assert.equal(state.decks.A.error, SOURCE_UNAVAILABLE);
  actions.togglePlay("A");
  assert.equal(audios.A.paused, true);
  assert.equal(
    dispatchControllerEvent(messageFromMidi(new Uint8Array([0x90, 0x0b, 127])), actions),
    true,
  );
  assert.equal(audios.A.paused, true);
  assert.equal(state.decks.A.error, SOURCE_UNAVAILABLE);

  assert.equal(audios.B.paused, false);
  assert.equal(state.decks.B.playing, true);
  assert.equal(state.decks.B.rate, deckB.rate);
  assert.deepEqual(state.decks.B.eq, deckB.eq);
  assert.equal(state.decks.B.error, "");
  assert.equal(handleKeydown(keyEvent("w", bodyTarget()), actions), true);
  assert.equal(audios.B.paused, true);
  assert.equal(state.decks.A.error, SOURCE_UNAVAILABLE);
  assert.equal(audios.A.videoId, "abcdefghijk");
  assert.equal(audios.B.videoId, deckB.videoId);

  state.decks.A.playError = "再生がブロックされました";
  state.decks.A.status = "ready";
  state.decks.A.error = "";
  assert.equal(sourcePlaybackBlocked(state.decks.A), false);
  actions.togglePlay("A");
  assert.equal(audios.A.paused, false);
  audios.A.pause();
  state.decks.A.playing = false;
  state.decks.A.playError = "";
  state.decks.A.status = "error";
  state.decks.A.error = SOURCE_UNAVAILABLE;

  state.selected = 1;
  await actions.loadSelected("A");
  assert.equal(state.decks.A.error, "");
  assert.equal(state.decks.A.playError, "");
  assert.equal(state.decks.A.status, "ready");
  assert.equal(state.decks.A.id, "zzzzzzzzzzz");
  assert.equal(sourcePlaybackBlocked(state.decks.A), false);
  assert.equal(state.decks.B.id, deckB.id);
  assert.equal(state.decks.B.rate, deckB.rate);
  assert.deepEqual(state.decks.B.eq, deckB.eq);
  assert.equal(state.decks.B.status, deckB.status);
  assert.equal(audios.B.videoId, deckB.videoId);
  assert.equal(audios.B.paused, true);
  actions.togglePlay("A");
  assert.equal(audios.A.paused, false);
  assert.equal(state.decks.A.playing, true);
});

test("cookie panel follows the server flag, not the yt-dlp sentence", async () => {
  const { state, actions } = harness();
  state.decks.A.status = "error";
  state.decks.A.error = SOURCE_UNAVAILABLE;
  state.decks.B.error = "Sign in to confirm you're not a bot";
  assert.equal(cookiePanelOpen(state.decks), false);
  assert.equal(sourcePlaybackBlocked(state.decks.A), true);
  state.decks.A.cookies = true;
  assert.equal(cookiePanelOpen(state.decks), true);
  state.results = [{ id: "abcdefghijk", title: "曲", channel: "", duration: 10 }];
  await actions.loadSelected("A");
  assert.equal(state.decks.A.cookies, false);
  assert.equal(state.decks.A.error, "");
  assert.equal(cookiePanelOpen(state.decks), false);
  state.decks.B.cookies = true;
  assert.equal(cookiePanelOpen(state.decks), true);
});

test("cookie replace stays reachable without a playback failure", () => {
  const decks = { A: { cookies: false }, B: { cookies: false } };
  assert.equal(cookiePanelOpen(decks), false);
  assert.equal(cookiePanelShown(false, false), false);
  assert.equal(nextChosenOpen(false, false), true);
  assert.equal(cookiePanelShown(false, true), true);
  assert.equal(nextChosenOpen(false, true), false);
  assert.equal(cookiePanelShown(true, false), true);
  assert.equal(nextChosenOpen(true, false), false);
  assert.equal(cookiePanelShown(true, true), true);
  assert.equal(nextChosenOpen(true, true), true);
});

test("the thumbnail ring turns with playback time and rests at zero", () => {
  assert.equal(DISC_DEGREES_PER_SECOND, 180);
  assert.equal(discRotationDegrees(0), 0);
  assert.equal(discRotationDegrees(-3), 0);
  assert.equal(discRotationDegrees(Number.NaN), 0);
  assert.equal(discRotationDegrees(0.5), 90);
  assert.equal(discRotationDegrees(1), 180);
  assert.equal(discRotationDegrees(2), 0);
  assert.equal(discRotationDegrees(2.5), 90);
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

test("each deck filter is a bypass at center and stacks with the rest of the deck", async () => {
  assert.equal(FILTER_STEP, 0.05);
  assert.equal(filterUnitFromMidi(64), 0.5);
  assert.equal(filterUnitFromMidi(0), 0);
  assert.equal(filterUnitFromMidi(127), 1);
  assert.ok(filterUnitFromMidi(63) < 0.5);
  assert.ok(filterUnitFromMidi(65) > 0.5);
  assert.equal(formatFilter(0.5), "なし");
  assert.equal(formatFilter(filterUnitFromMidi(64)), "なし");
  assert.match(formatFilter(0), /^ローパス /);
  assert.match(formatFilter(1), /^ハイパス /);
  assert.equal(formatFilter(0).includes("なし"), false);
  assert.equal(formatFilter(1).includes("なし"), false);

  const fullLow = filterSpec(0);
  const nearCenterLow = filterSpec(0.49);
  const nearCenterHigh = filterSpec(0.51);
  const fullHigh = filterSpec(1);
  assert.equal(filterSpec(0.5).bypass, true);
  assert.equal(fullLow.bypass, false);
  assert.equal(fullLow.type, "lowpass");
  assert.equal(fullLow.frequency, FILTER_LPF_MIN_HZ);
  assert.equal(nearCenterLow.type, "lowpass");
  assert.ok(nearCenterLow.frequency > fullLow.frequency);
  assert.ok(nearCenterLow.frequency < FILTER_LPF_OPEN_HZ);
  assert.ok(nearCenterLow.frequency > 8000);
  assert.equal(fullHigh.type, "highpass");
  assert.equal(fullHigh.frequency, FILTER_HPF_MAX_HZ);
  assert.equal(nearCenterHigh.type, "highpass");
  assert.ok(nearCenterHigh.frequency > FILTER_HPF_OPEN_HZ);
  assert.ok(nearCenterHigh.frequency < fullHigh.frequency);
  assert.ok(nearCenterHigh.frequency < 200);
  let previous = filterSpec(0).frequency;
  for (const unit of [0.1, 0.2, 0.3, 0.4]) {
    const spec = filterSpec(unit);
    assert.equal(spec.type, "lowpass");
    assert.ok(spec.frequency > previous);
    previous = spec.frequency;
  }
  previous = filterSpec(0.6).frequency;
  for (const unit of [0.7, 0.8, 0.9, 1]) {
    const spec = filterSpec(unit);
    assert.equal(spec.type, "highpass");
    assert.ok(spec.frequency > previous);
    previous = spec.frequency;
  }

  function biquad() {
    return {
      type: "",
      frequency: { value: 0 },
      Q: { value: 0 },
      gain: { value: 1 },
      connections: [],
      next: null,
      connect(target) {
        this.connections.push(target);
        this.next = target;
      },
      disconnect() {
        throw new Error("the filter is part of the EQ chain and is not spliced in later");
      },
    };
  }
  const source = { connections: [], next: null, connect(target) { this.connections.push(target); this.next = target; } };
  const context = {
    destination: { name: "out" },
    createBiquadFilter: biquad,
  };
  const eq = connectEqGraph(source, context, null);
  assert.equal(eq.low.next, null);
  assert.deepEqual(eq.low.connections, []);
  const color = connectDeckFilter(eq.low, context);
  assert.deepEqual(eq.low.connections, [color]);
  assert.equal(eq.low.next, color);
  assert.equal(eq.low.connections.includes(context.destination), false);
  assert.equal(color.next, context.destination);
  assert.deepEqual(color.connections, [context.destination]);
  assert.equal(source.next, eq.high);
  assert.equal(eq.high.next, eq.mid);
  assert.equal(eq.mid.next, eq.low);
  assert.equal(color.type, "peaking");
  assert.equal(color.gain.value, 0);
  assert.notEqual(color.type, "lowpass");
  assert.notEqual(color.type, "highpass");

  applyFilter(color, 0);
  assert.equal(color.type, "lowpass");
  assert.equal(color.frequency.value, FILTER_LPF_MIN_HZ);
  applyFilter(color, 0.5);
  assert.equal(color.type, "peaking");
  assert.equal(color.gain.value, 0);
  assert.notEqual(color.type, "lowpass");
  assert.notEqual(color.type, "highpass");
  applyFilter(color, 1);
  assert.equal(color.type, "highpass");
  assert.equal(color.frequency.value, FILTER_HPF_MAX_HZ);

  const labels = legendGroups().find((group) => group.name === "フィルター").items.map((item) => item.label);
  assert.ok(labels.includes("デッキ A のフィルターをローパス側へ"));
  assert.ok(labels.includes("デッキ A のフィルターをハイパス側へ"));
  assert.ok(labels.includes("デッキ B のフィルターをローパス側へ"));
  assert.ok(labels.includes("デッキ B のフィルターをハイパス側へ"));

  const { state, audios, actions } = harness();
  for (const deck of ["A", "B"]) {
    audios[deck].color = {
      type: "",
      frequency: { value: 0 },
      Q: { value: 0 },
      gain: { value: 1 },
    };
    audios[deck].setFilter = (unit) => {
      audios[deck].filterUnit = unit;
      if (audios[deck].filterLive === false) return false;
      applyFilter(audios[deck].color, unit);
      return true;
    };
    audios[deck].eqDb = { high: 0, mid: 0, low: 0 };
    audios[deck].setEqGain = (band, db) => {
      audios[deck].eqDb[band] = db;
      return true;
    };
  }

  assert.equal(state.decks.A.filter, 0.5);
  assert.equal(state.decks.B.filter, 0.5);
  actions.setFilter("A", 0.5);
  assert.equal(audios.A.color.type, "peaking");
  assert.equal(audios.A.color.gain.value, 0);
  actions.setVolume("A", 0.4);
  actions.setVolume("B", 0.8);
  actions.setRate("A", 1.4);
  actions.setRate("B", 0.7);
  actions.setEq("A", "high", 0.8);
  actions.setCrossfader(0.25);
  const stacked = {
    volumeA: state.decks.A.volume,
    volumeB: state.decks.B.volume,
    rateA: state.decks.A.rate,
    rateB: state.decks.B.rate,
    eqHigh: state.decks.A.eq.high,
    crossfader: state.crossfader,
    audioA: audios.A.volume,
    audioB: audios.B.volume,
  };

  actions.setFilter("A", 0);
  assert.equal(state.decks.A.filter, 0);
  assert.equal(audios.A.filterUnit, 0);
  assert.equal(audios.A.color.type, "lowpass");
  assert.equal(audios.A.color.frequency.value, FILTER_LPF_MIN_HZ);
  assert.equal(state.decks.B.filter, 0.5);
  assert.equal(state.decks.A.volume, stacked.volumeA);
  assert.equal(state.decks.B.volume, stacked.volumeB);
  assert.equal(state.decks.A.rate, stacked.rateA);
  assert.equal(state.decks.B.rate, stacked.rateB);
  assert.equal(state.decks.A.eq.high, stacked.eqHigh);
  assert.equal(state.crossfader, stacked.crossfader);
  assert.equal(audios.A.volume, stacked.audioA);
  assert.equal(audios.B.volume, stacked.audioB);
  assert.equal(audios.A.playbackRate, stacked.rateA);

  actions.setVolume("A", 0.2);
  actions.setCrossfader(0.8);
  actions.nudgeRate("A", 0.01);
  actions.nudgeEq("A", "low", EQ_STEP);
  assert.equal(state.decks.A.filter, 0);
  assert.equal(audios.A.color.type, "lowpass");
  assert.equal(audios.A.color.frequency.value, FILTER_LPF_MIN_HZ);

  actions.resetFilter("A");
  assert.equal(state.decks.A.filter, 0.5);
  assert.equal(audios.A.color.type, "peaking");
  assert.equal(audios.A.color.gain.value, 0);
  actions.setFilter("A", 1);
  assert.equal(audios.A.color.type, "highpass");
  assert.equal(audios.A.color.frequency.value, FILTER_HPF_MAX_HZ);
  actions.setFilter("C", 0);
  assert.equal(state.decks.A.filter, 1);

  assert.equal(handleKeydown(keyEvent("t", searchTarget()), actions), false);
  assert.equal(state.decks.A.filter, 1);
  assert.equal(handleKeydown(keyEvent("t", bodyTarget()), actions), true);
  assert.equal(state.decks.A.filter, 0.95);
  assert.equal(audios.A.color.type, "highpass");
  assert.ok(audios.A.color.frequency.value < FILTER_HPF_MAX_HZ);
  assert.equal(handleKeydown(keyEvent("T", bodyTarget(), { shiftKey: true }), actions), true);
  assert.equal(state.decks.A.filter, 1);
  assert.equal(audios.A.paused, true);
  assert.equal(handleKeydown(keyEvent("q", bodyTarget()), actions), true);
  assert.equal(audios.A.paused, true);
  assert.equal(state.decks.A.filter, 1);
  assert.equal(state.decks.B.filter, 0.5);
  assert.equal(handleKeydown(keyEvent("Q", bodyTarget(), { shiftKey: true }), actions), true);
  assert.equal(state.decks.B.filter, 0.45);
  assert.equal(audios.B.color.type, "lowpass");
  assert.equal(audios.A.paused, true);
  assert.equal(state.decks.A.filter, 1);
  assert.equal(handleKeydown(keyEvent("w", bodyTarget()), actions), true);
  assert.equal(audios.B.paused, true);
  assert.equal(state.decks.B.filter, 0.45);
  assert.equal(handleKeydown(keyEvent("W", bodyTarget(), { shiftKey: true }), actions), true);
  assert.equal(state.decks.B.filter, 0.5);
  assert.equal(audios.B.color.type, "peaking");
  assert.equal(audios.B.color.gain.value, 0);
  assert.equal(audios.B.paused, true);

  const rateA = state.decks.A.rate;
  const volumeA = state.decks.A.volume;
  const eqLow = state.decks.A.eq.low;
  const fader = state.crossfader;
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb6, 0x17, 0])), actions), true);
  assert.equal(state.decks.A.filter, 0);
  assert.equal(audios.A.color.type, "lowpass");
  assert.equal(state.decks.B.filter, 0.5);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb6, 0x17, 64])), actions), true);
  assert.equal(state.decks.A.filter, 0.5);
  assert.equal(audios.A.color.type, "peaking");
  assert.equal(audios.A.color.gain.value, 0);
  assert.equal(formatFilter(state.decks.A.filter), "なし");
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb6, 0x17, 127])), actions), true);
  assert.equal(state.decks.A.filter, 1);
  assert.equal(audios.A.color.type, "highpass");
  assert.equal(audios.A.color.frequency.value, FILTER_HPF_MAX_HZ);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb6, 0x18, 0])), actions), true);
  assert.equal(state.decks.B.filter, 0);
  assert.equal(audios.B.color.type, "lowpass");
  assert.equal(state.decks.A.filter, 1);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb6, 0x37, 0])), actions), false);
  assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb6, 0x38, 127])), actions), false);
  assert.equal(state.decks.A.filter, 1);
  assert.equal(state.decks.B.filter, 0);
  assert.equal(state.decks.A.rate, rateA);
  assert.equal(state.decks.A.volume, volumeA);
  assert.equal(state.decks.A.eq.low, eqLow);
  assert.equal(state.crossfader, fader);
  assert.equal(audios.A.playbackRate, rateA);

  state.results = [{ id: "abcdefghijk", title: "曲", channel: "", duration: 10 }];
  await actions.loadSelected("A");
  assert.equal(state.decks.A.filter, 1);
  assert.equal(audios.A.filterUnit, 1);
  assert.equal(audios.A.color.type, "highpass");
  assert.equal(state.decks.B.filter, 0);
  assert.equal(audios.B.color.type, "lowpass");
  assert.equal(state.decks.A.rate, 1);
  assert.equal(state.decks.A.eq.high, 0.5);
  assert.equal(state.decks.A.volume, volumeA);

  audios.A.filterLive = false;
  actions.setFilter("A", 0.2);
  assert.equal(state.decks.A.filter, 0.2);
  assert.equal(state.decks.A.filterError, "フィルターを音声に接続できませんでした");
  assert.equal(audios.A.color.type, "highpass");
  audios.A.filterLive = true;
  actions.resetFilter("A");
  assert.equal(state.decks.A.filterError, "");
  assert.equal(audios.A.color.type, "peaking");
  assert.equal(audios.A.color.gain.value, 0);
});

test("the filter node is the only path from the EQ tail to the speakers", () => {
  function node() {
    return {
      type: "",
      frequency: { value: 0 },
      Q: { value: 0 },
      gain: { value: 0 },
      connections: [],
      connect(target) {
        this.connections.push(target);
      },
      disconnect() {
        throw new Error("disconnect leaves the dry signal connected to the speakers");
      },
    };
  }
  const destination = { name: "speakers" };
  const made = [];
  const previousWindow = globalThis.window;
  globalThis.window = {
    AudioContext: function AudioContext() {
      this.destination = destination;
      this.state = "running";
      this.resume = function resume() {};
      this.createMediaElementSource = function createMediaElementSource(audio) {
        const source = node();
        source.media = audio;
        made.push(source);
        return source;
      };
      this.createBiquadFilter = function createBiquadFilter() {
        const filter = node();
        made.push(filter);
        return filter;
      };
    },
  };
  try {
    const player = createDeckPlayer("A", "player-A");
    player.audio = { volume: 1 };
    assert.equal(player.setFilter(0), true);
    assert.equal(made.length, 5);
    const [source, high, mid, low, color] = made;
    assert.deepEqual(source.connections, [high]);
    assert.deepEqual(high.connections, [mid]);
    assert.deepEqual(mid.connections, [low]);
    assert.deepEqual(low.connections, [color]);
    assert.deepEqual(color.connections, [destination]);
    assert.equal(player._color, color);
    assert.equal(color.type, "lowpass");
    assert.equal(color.frequency.value, FILTER_LPF_MIN_HZ);
    assert.equal(player.setEqGain("low", -12), true);
    assert.equal(low.gain.value, -12);
    assert.deepEqual(low.connections, [color]);

    assert.equal(player.setFilter(50 / 100), true);
    assert.equal(color.type, "peaking");
    assert.equal(color.gain.value, 0);
    assert.equal(color.frequency.value, 1000);
    assert.equal(made.length, 5);

    assert.equal(player.setFilter(100 / 100), true);
    assert.equal(color.type, "highpass");
    assert.equal(color.frequency.value, FILTER_HPF_MAX_HZ);
    assert.equal(player.audio.volume, 1);

    const state = freshState();
    const other = fakeAudio();
    const actions = createActions({
      state,
      audios: { A: player, B: other },
      scheduleRender() {},
      queryValue: () => "",
    });
    assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb6, 0x17, 0])), actions), true);
    assert.equal(state.decks.A.filter, 0);
    assert.equal(color.type, "lowpass");
    assert.equal(color.frequency.value, FILTER_LPF_MIN_HZ);
    assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb6, 0x18, 127])), actions), true);
    assert.equal(state.decks.B.filter, 1);
    assert.equal(color.type, "lowpass");
    assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb6, 0x37, 10])), actions), false);
    assert.equal(dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb6, 0x38, 10])), actions), false);
    assert.equal(state.decks.A.filter, 0);
    actions.setVolume("A", 0.4);
    actions.setCrossfader(0);
    assert.equal(player.audio.volume, 0.4);
    assert.equal(color.type, "lowpass");
    assert.equal(color.frequency.value, FILTER_LPF_MIN_HZ);
    assert.equal(made.length, 5);

    const appJs = readFileSync(new URL("../../djtube/static/app.js", import.meta.url), "utf8");
    assert.match(appJs, /volume\.addEventListener\("input", \(\) => \{\s*actions\.setVolume\(deck, Number\(volume\.value\) \/ 100\);/);
    assert.match(appJs, /filter\.addEventListener\("input", \(\) => \{\s*actions\.setFilter\(deck, Number\(filter\.value\) \/ 100\);/);
    const filterJs = readFileSync(new URL("../../djtube/static/filter.js", import.meta.url), "utf8");
    assert.equal(filterJs.includes("disconnect"), false);
  } finally {
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});

test("jog direction decides scratch or a track rate that follows the spin", () => {
  const slow = jogSpinPlan(0.1, 0.05);
  const fast = jogSpinPlan(0.2, 0.05);
  assert.equal(slow.hear, "track");
  assert.equal(fast.hear, "track");
  assert.equal(slow.scratch, null);
  assert.equal(fast.scratch, null);
  assert.equal(slow.trackRate, 2);
  assert.equal(fast.trackRate, 4);
  assert.equal(fast.trackRate / slow.trackRate, 2);
  assert.notEqual(slow.trackRate, fast.trackRate);
  const sameStepSlower = jogSpinPlan(0.1, 0.1);
  assert.equal(sameStepSlower.trackRate, 1);
  assert.equal(slow.trackRate / sameStepSlower.trackRate, 2);

  const backSlow = jogSpinPlan(-0.1, 0.05);
  const backFast = jogSpinPlan(-0.2, 0.05);
  assert.equal(backSlow.hear, "scratch");
  assert.equal(backFast.hear, "scratch");
  assert.equal(backSlow.trackRate, null);
  assert.equal(backFast.trackRate, null);
  assert.notEqual(backSlow.scratch, null);
  assert.equal(backSlow.scratch.rate, 2);
  assert.equal(backFast.scratch.rate, 4);
  assert.equal(backFast.scratch.rate / backSlow.scratch.rate, 2);
  assert.ok(backFast.scratch.chirpHz > backSlow.scratch.chirpHz);
  assert.equal(backFast.scratch.chirpHz - backSlow.scratch.chirpHz, (backFast.scratch.rate - backSlow.scratch.rate) * 80);

  assert.equal(jogSpinPlan(0.1, null).trackRate, 0.1 / JOG_FIRST_TICK_SECONDS);
  assert.equal(jogSpinPlan(-0.1, null).scratch.rate, 0.1 / JOG_FIRST_TICK_SECONDS);
  const released = jogReleaseHear(1.25);
  assert.equal(released.hear, "deck");
  assert.equal(released.trackRate, 1.25);
  assert.equal(released.scratch, null);
});

function fakeScratchContext() {
  const sources = [];
  const filters = [];
  const gains = [];
  const buffers = [];
  return {
    sampleRate: 44100,
    destination: {},
    sources,
    filters,
    gains,
    buffers,
    createBuffer(_channels, length) {
      const data = new Float32Array(length);
      const buffer = {
        length,
        getChannelData() {
          return data;
        },
      };
      buffers.push(buffer);
      return buffer;
    },
    createBufferSource() {
      const node = {
        buffer: null,
        loop: false,
        playbackRate: { value: 1 },
        started: false,
        stopped: false,
        connect() {},
        disconnect() {},
        start() {
          this.started = true;
        },
        stop() {
          this.stopped = true;
        },
      };
      sources.push(node);
      return node;
    },
    createBiquadFilter() {
      const node = {
        type: "",
        frequency: { value: 0 },
        Q: { value: 0 },
        connect() {},
        disconnect() {},
      };
      filters.push(node);
      return node;
    },
    createGain() {
      const node = {
        gain: { value: 0 },
        connect() {},
        disconnect() {},
      };
      gains.push(node);
      return node;
    },
  };
}

test("a faster backward spin plays the scratch chirp faster", () => {
  const context = fakeScratchContext();
  const voice = createScratchVoice(context);
  const slow = scratchFromSpeed(2);
  const fast = scratchFromSpeed(4);
  voice.update(slow, 0.5);
  const gain = context.gains[0].gain.value;
  voice.update(fast, 0.5);
  assert.equal(context.sources.length, 1);
  assert.equal(context.sources[0].loop, true);
  assert.equal(context.sources[0].started, true);
  assert.equal(context.sources[0].stopped, false);
  assert.equal(context.sources[0].playbackRate.value, fast.rate);
  assert.equal(context.filters[0].frequency.value, fast.chirpHz);
  assert.equal(context.gains[0].gain.value, gain);
  assert.ok(gain > 0);
  assert.ok(gain <= 0.5);
  const data = context.buffers[0].getChannelData(0);
  let peak = 0;
  let changes = 0;
  let previous = data[0];
  for (let i = 0; i < data.length; i += 1) {
    peak = Math.max(peak, Math.abs(data[i]));
    if (data[i] !== previous) changes += 1;
    previous = data[i];
  }
  assert.ok(peak > 0.2);
  assert.ok(changes > 8);
  voice.update(slow, 0.5);
  assert.equal(context.sources.length, 1);
  assert.equal(context.sources[0].playbackRate.value, slow.rate);
  assert.equal(context.filters[0].frequency.value, slow.chirpHz);
  assert.ok(fast.rate > slow.rate);
  voice.stop();
  assert.equal(context.sources[0].stopped, true);
});

test("spin playback is faster than the tempo fader and still follows the spin", () => {
  const deck = createDeckPlayer("A", "player-A");
  const seen = [];
  deck.audio = {
    set playbackRate(value) {
      seen.push(value);
    },
  };
  deck.playbackRate = 1.25;
  deck.playbackRate = 3;
  assert.equal(deck.playbackRate, 2);
  assert.equal(seen.at(-1), 2);
  deck.setSpinRate(3);
  assert.equal(deck.playbackRate, 2);
  assert.equal(seen.at(-1), 3);
  deck.setSpinRate(6);
  assert.equal(seen.at(-1), 6);
  assert.equal(seen.at(-1) / 3, 2);
  deck.setSpinRate(50);
  assert.equal(seen.at(-1), SPIN_RATE_MAX);
  assert.ok(SPIN_RATE_MAX > 2);
  deck.setSpinRate(null);
  assert.equal(seen.at(-1), 2);
  assert.equal(deck.playbackRate, 2);
});

function platterHarness() {
  let now = 1000;
  const pending = new Map();
  let seq = 0;
  const { state, audios, actions } = harness({
    deps: {
      now: () => now,
      later(fn, ms) {
        seq += 1;
        pending.set(seq, { fn, ms });
        return seq;
      },
      cancelLater(id) {
        pending.delete(id);
      },
    },
  });
  const elementRates = [];
  let elementRate = 1;
  let elementTime = 10;
  const player = createDeckPlayer("A", "player-A");
  const element = {
    duration: 200,
    paused: true,
    volume: 1,
    muted: false,
    src: "",
    load() {},
    play() {
      this.paused = false;
      player.paused = false;
      return Promise.resolve();
    },
    pause() {
      this.paused = true;
      player.paused = true;
    },
    addEventListener() {},
  };
  Object.defineProperty(element, "currentTime", {
    configurable: true,
    get() {
      return elementTime;
    },
    set(value) {
      elementTime = Number(value);
    },
  });
  Object.defineProperty(element, "playbackRate", {
    configurable: true,
    get() {
      return elementRate;
    },
    set(value) {
      elementRate = value;
      elementRates.push(value);
    },
  });
  player.audio = element;
  player._context = fakeScratchContext();
  audios.A = player;
  return {
    state,
    audios,
    actions,
    player,
    element,
    elementRates,
    context: player._context,
    set now(value) {
      now = value;
    },
    get now() {
      return now;
    },
    get rate() {
      return elementRate;
    },
    get time() {
      return elementTime;
    },
    at(seconds) {
      elementTime = seconds;
    },
    release() {
      const left = [...pending.values()];
      pending.clear();
      assert.equal(left.length, 1);
      assert.equal(left[0].ms, JOG_RELEASE_MS);
      left[0].fn();
    },
  };
}

function vinylTick(actions, value) {
  return dispatchControllerEvent(messageFromMidi(new Uint8Array([0xb0, 0x22, value])), actions);
}

test("FLX4 jog scratches backward and hears the track faster as the forward spin speeds up", async () => {
  const rig = platterHarness();
  const { state, audios, actions, player, element, elementRates, context } = rig;
  state.results = [{ id: "abcdefghijk", title: "曲", channel: "", duration: 200 }];
  await actions.loadSelected("A");
  state.decks.A.status = "ready";
  rig.at(10);
  actions.setRate("A", 1.25);
  actions.setRate("B", 0.5);
  state.decks.A.volume = 0.5;
  player.volume = 0.5;
  state.decks.A.cue = 4;
  state.crossfader = 0.25;
  elementRates.length = 0;

  assert.equal(vinylTick(actions, 66), true);
  assert.equal(player.jogHear.hear, "track");
  assert.equal(player.jogHear.trackRate, 5);
  assert.equal(player.jogHear.scratch, null);
  assert.equal(rig.rate, 5);
  rig.now = 1050;
  assert.equal(vinylTick(actions, 66), true);
  assert.equal(player.jogHear.trackRate, 2);
  assert.equal(rig.rate, 2);
  rig.now = 1100;
  assert.equal(vinylTick(actions, 68), true);
  assert.equal(player.jogHear.hear, "track");
  assert.equal(player.jogHear.trackRate, 4);
  assert.equal(player.jogHear.scratch, null);
  assert.equal(rig.rate, 4);
  assert.equal(rig.rate / 2, 2);
  assert.deepEqual(
    elementRates.filter((rate) => rate !== 1.25),
    [5, 2, 4],
  );
  assert.equal(player.playbackRate, 1.25);
  assert.equal(state.decks.A.rate, 1.25);
  assert.equal(player.paused, false);
  assert.equal(element.muted, false);
  assert.equal(context.sources.length, 0);
  assert.ok(Math.abs(rig.time - 10.4) < 1e-9);

  rig.now = 1150;
  assert.equal(vinylTick(actions, 62), true);
  assert.equal(player.jogHear.hear, "scratch");
  assert.equal(player.jogHear.trackRate, null);
  assert.equal(player.jogHear.scratch.rate, 2);
  const scratchGain = context.gains[0].gain.value;
  rig.now = 1200;
  assert.equal(vinylTick(actions, 60), true);
  assert.equal(player.jogHear.hear, "scratch");
  assert.equal(player.jogHear.trackRate, null);
  assert.equal(player.jogHear.scratch.rate, 4);
  assert.equal(player.jogHear.scratch.rate / 2, 2);
  assert.equal(context.sources.length, 1);
  assert.equal(context.sources[0].loop, true);
  assert.equal(context.sources[0].started, true);
  assert.equal(context.sources[0].playbackRate.value, 4);
  assert.equal(context.filters[0].frequency.value, player.jogHear.scratch.chirpHz);
  assert.ok(context.filters[0].frequency.value > scratchFromSpeed(2).chirpHz);
  assert.equal(context.gains[0].gain.value, scratchGain);
  assert.ok(scratchGain > 0);
  assert.equal(element.muted, true);
  assert.equal(rig.rate, 1.25);
  assert.ok(rig.rate > 0);
  assert.notEqual(rig.rate, player.jogHear.scratch.rate);
  assert.ok(elementRates.every((rate) => rate > 0));
  assert.equal(player.playbackRate, 1.25);
  assert.equal(state.decks.A.rate, 1.25);
  assert.ok(Math.abs(rig.time - 10.1) < 1e-9);

  rig.release();
  assert.equal(player.jogHear.hear, "deck");
  assert.equal(player.jogHear.trackRate, 1.25);
  assert.equal(player.jogHear.scratch, null);
  assert.equal(rig.rate, 1.25);
  assert.equal(element.muted, false);
  assert.equal(context.sources[0].stopped, true);
  assert.equal(player.paused, true);
  assert.equal(state.decks.A.playing, false);
  assert.ok(Math.abs(rig.time - 10.1) < 1e-9);
  assert.equal(state.decks.A.rate, 1.25);
  assert.equal(state.decks.A.volume, 0.5);
  assert.equal(player.volume, 0.5);
  assert.equal(element.volume, 0.5);
  assert.equal(state.decks.A.cue, 4);
  assert.equal(state.crossfader, 0.25);
  assert.equal(state.decks.B.rate, 0.5);
  assert.equal(audios.B.playbackRate, 0.5);
  assert.ok(!audios.B.scratch);
  assert.ok(!audios.B.muted);
});

test("letting go of a playing deck returns to that deck's tempo and keeps playing", async () => {
  const rig = platterHarness();
  const { state, actions, player, element } = rig;
  state.results = [{ id: "abcdefghijk", title: "曲", channel: "", duration: 200 }];
  await actions.loadSelected("A");
  state.decks.A.status = "ready";
  rig.at(30);
  actions.setRate("A", 1.25);
  state.decks.A.playing = true;
  player.paused = false;
  element.paused = false;

  rig.now = 2000;
  assert.equal(vinylTick(actions, 68), true);
  assert.equal(player.jogHear.hear, "track");
  assert.equal(player.jogHear.trackRate, 0.2 / JOG_FIRST_TICK_SECONDS);
  assert.equal(rig.rate, 0.2 / JOG_FIRST_TICK_SECONDS);
  assert.notEqual(rig.rate, 1.25);
  assert.equal(player.paused, false);
  rig.release();
  assert.equal(rig.rate, 1.25);
  assert.equal(player.playbackRate, 1.25);
  assert.equal(state.decks.A.rate, 1.25);
  assert.equal(player.paused, false);
  assert.equal(state.decks.A.playing, true);
  assert.equal(element.muted, false);
  assert.equal(rig.time, 30.2);

  rig.now = 3000;
  assert.equal(vinylTick(actions, 60), true);
  assert.equal(player.jogHear.hear, "scratch");
  assert.equal(player.jogHear.trackRate, null);
  assert.equal(player.jogHear.scratch.rate, 0.2 / JOG_FIRST_TICK_SECONDS);
  assert.equal(rig.player._context.sources.at(-1).playbackRate.value, player.jogHear.scratch.rate);
  assert.equal(element.muted, true);
  assert.equal(rig.rate, 1.25);
  assert.ok(rig.rate > 0);
  rig.release();
  assert.equal(element.muted, false);
  assert.equal(rig.rate, 1.25);
  assert.equal(player.paused, false);
  assert.equal(state.decks.A.playing, true);
  assert.equal(state.decks.A.rate, 1.25);
  assert.equal(rig.time, 30);
});

test("keyboard jog uses the same scratch and spin-speed split", async () => {
  let now = 0;
  let release = null;
  const { state, audios, actions } = harness({
    deps: {
      now: () => now,
      later(fn, ms) {
        release = { fn, ms };
        return 1;
      },
      cancelLater() {
        release = null;
      },
    },
  });
  state.results = [{ id: "abcdefghijk", title: "曲", channel: "", duration: 200 }];
  await actions.loadSelected("A");
  actions.setRate("A", 1.25);
  audios.A.currentTime = 10;
  audios.A.duration = 200;
  state.decks.A.cue = 3;
  const fader = state.crossfader;

  assert.equal(handleKeydown(keyEvent("]", bodyTarget()), actions), true);
  assert.equal(audios.A.jogHear.hear, "track");
  assert.equal(audios.A.jogHear.scratch, null);
  assert.equal(audios.A.playbackRate, JOG_STEP / JOG_FIRST_TICK_SECONDS);
  assert.equal(audios.A.currentTime, 11);
  const slow = audios.A.playbackRate;
  assert.equal(release.ms, JOG_RELEASE_MS);
  release.fn();
  assert.equal(audios.A.playbackRate, 1.25);
  assert.equal(audios.A.paused, true);

  assert.equal(handleKeydown(keyEvent("]", bodyTarget(), { shiftKey: true }), actions), true);
  assert.equal(audios.A.jogHear.hear, "track");
  assert.equal(audios.A.playbackRate, JOG_STEP_LARGE / JOG_FIRST_TICK_SECONDS);
  assert.equal(audios.A.playbackRate / slow, JOG_STEP_LARGE / JOG_STEP);
  assert.notEqual(audios.A.playbackRate, slow);
  release.fn();

  assert.equal(handleKeydown(keyEvent("[", bodyTarget()), actions), true);
  assert.equal(audios.A.jogHear.hear, "scratch");
  assert.equal(audios.A.jogHear.trackRate, null);
  assert.equal(audios.A.scratch.rate, JOG_STEP / JOG_FIRST_TICK_SECONDS);
  assert.equal(audios.A.muted, true);
  assert.equal(audios.A.playbackRate, 1.25);
  assert.ok(audios.A.playbackRate > 0);
  const backSlow = audios.A.scratch.rate;
  release.fn();
  assert.equal(audios.A.muted, false);
  assert.equal(audios.A.scratch, null);
  assert.equal(audios.A.playbackRate, 1.25);

  assert.equal(handleKeydown(keyEvent("[", bodyTarget(), { shiftKey: true }), actions), true);
  assert.equal(audios.A.jogHear.hear, "scratch");
  assert.equal(audios.A.scratch.rate, JOG_STEP_LARGE / JOG_FIRST_TICK_SECONDS);
  assert.equal(audios.A.scratch.rate / backSlow, JOG_STEP_LARGE / JOG_STEP);
  assert.ok(audios.A.scratch.chirpHz > scratchFromSpeed(backSlow).chirpHz);
  assert.equal(audios.A.playbackRate, 1.25);
  assert.equal(audios.A.muted, true);
  assert.equal(state.decks.A.rate, 1.25);
  assert.equal(state.decks.A.cue, 3);
  assert.equal(state.crossfader, fader);
});

// currentTime moves with the element rate while the element is playing, and a pause
// keeps the time it had already reached. A seek replaces that time.
function coastingRig() {
  let now = 0;
  const pending = new Map();
  let seq = 0;
  const { state, audios, actions } = harness({
    deps: {
      now: () => now,
      later(fn, ms) {
        seq += 1;
        pending.set(seq, { fn, ms });
        return seq;
      },
      cancelLater(id) {
        pending.delete(id);
      },
    },
  });
  const player = createDeckPlayer("A", "player-A");
  let stored = 0;
  let rate = 1;
  let paused = true;
  let anchor = 0;
  function elapsed() {
    return paused ? 0 : ((now - anchor) / 1000) * rate;
  }
  function pull() {
    stored += elapsed();
    anchor = now;
  }
  const element = {
    duration: 200,
    volume: 1,
    muted: false,
    src: "",
    load() {},
    addEventListener() {},
    get paused() {
      return paused;
    },
    set paused(value) {
      paused = !!value;
    },
    play() {
      if (paused) anchor = now;
      paused = false;
      player.paused = false;
      return Promise.resolve();
    },
    pause() {
      if (!paused) pull();
      paused = true;
      player.paused = true;
    },
  };
  Object.defineProperty(element, "currentTime", {
    configurable: true,
    get() {
      return stored + elapsed();
    },
    set(value) {
      stored = Number(value);
      anchor = now;
    },
  });
  Object.defineProperty(element, "playbackRate", {
    configurable: true,
    get() {
      return rate;
    },
    set(value) {
      if (!paused) pull();
      rate = Number(value);
    },
  });
  player.audio = element;
  player._context = fakeScratchContext();
  audios.A = player;
  return {
    state,
    audios,
    actions,
    player,
    element,
    set now(value) {
      now = value;
    },
    get now() {
      return now;
    },
    at(seconds) {
      stored = seconds;
      anchor = now;
    },
    play() {
      paused = false;
      anchor = now;
      player.paused = false;
      state.decks.A.playing = true;
    },
    release() {
      const left = [...pending.values()];
      pending.clear();
      assert.equal(left.length, 1);
      assert.equal(left[0].ms, JOG_RELEASE_MS);
      left[0].fn();
    },
  };
}

test("a backward scratch lands on the sum of the seeks while the deck tempo would have walked the playhead", async () => {
  const rig = coastingRig();
  const { state, actions, player, element } = rig;
  state.results = [{ id: "abcdefghijk", title: "曲", channel: "", duration: 200 }];
  await actions.loadSelected("A");
  state.decks.A.status = "ready";
  actions.setRate("A", 1);
  rig.at(10);
  rig.play();
  const step = -0.1;
  let sum = 10;
  for (let i = 0; i < 3; i += 1) {
    if (i) rig.now += 50;
    actions.jog("A", step);
    sum += step;
  }
  assert.equal(player.jogHear.hear, "scratch");
  assert.equal(element.muted, true);
  assert.equal(element.paused, true);
  assert.equal(player.playbackRate, 1);
  assert.equal(state.decks.A.rate, 1);
  assert.ok(Math.abs(player.currentTime - sum) < 1e-9);
  assert.ok(Math.abs(element.currentTime - sum) < 1e-9);
  rig.release();
  assert.ok(Math.abs(player.currentTime - sum) < 1e-9);
  assert.ok(Math.abs(element.currentTime - sum) < 1e-9);
  assert.equal(element.paused, false);
  assert.equal(player.paused, false);
  assert.equal(state.decks.A.playing, true);
  assert.equal(element.muted, false);
  assert.equal(player.playbackRate, 1);
  assert.equal(state.decks.A.rate, 1);
});

test("keyboard jog release lands on the seek and drops the 16x coast", async () => {
  const rig = coastingRig();
  const { state, actions, player, element } = rig;
  state.results = [{ id: "abcdefghijk", title: "曲", channel: "", duration: 200 }];
  await actions.loadSelected("A");
  state.decks.A.status = "ready";
  actions.setRate("A", 1.25);
  rig.at(10);
  assert.equal(player.paused, true);
  assert.equal(element.paused, true);

  assert.equal(handleKeydown(keyEvent("]", bodyTarget()), actions), true);
  assert.equal(player.jogHear.hear, "track");
  assert.equal(element.playbackRate, SPIN_RATE_MAX);
  assert.equal(player.playbackRate, 1.25);
  assert.equal(state.decks.A.rate, 1.25);
  assert.ok(SPIN_RATE_MAX < JOG_STEP / JOG_FIRST_TICK_SECONDS);

  rig.now = JOG_RELEASE_MS;
  const coast = 11 + SPIN_RATE_MAX * (JOG_RELEASE_MS / 1000);
  assert.ok(Math.abs(player.currentTime - coast) < 1e-9);
  assert.ok(Math.abs(element.currentTime - coast) < 1e-9);
  assert.ok(coast > 11);

  rig.release();
  assert.ok(Math.abs(player.currentTime - 11) < 1e-9);
  assert.ok(Math.abs(element.currentTime - 11) < 1e-9);
  assert.equal(player.paused, true);
  assert.equal(element.paused, true);
  assert.equal(state.decks.A.playing, false);
  assert.equal(player.playbackRate, 1.25);
  assert.equal(state.decks.A.rate, 1.25);
  assert.equal(element.playbackRate, 1.25);
});

test("a second forward jog adds to the last seek, not the coast between ticks", async () => {
  const rig = coastingRig();
  const { state, actions, player, element } = rig;
  state.results = [{ id: "abcdefghijk", title: "曲", channel: "", duration: 200 }];
  await actions.loadSelected("A");
  state.decks.A.status = "ready";
  const start = 10;
  const step = 0.05;
  rig.at(start);
  actions.jog("A", step);
  rig.now += 20;
  assert.ok(player.currentTime > start + step);
  actions.jog("A", step);
  rig.release();
  const landing = start + step + step;
  assert.ok(Math.abs(player.currentTime - landing) < 1e-9);
  assert.ok(Math.abs(element.currentTime - landing) < 1e-9);
  assert.equal(player.paused, true);
  assert.equal(element.paused, true);
  assert.equal(state.decks.A.playing, false);
});
