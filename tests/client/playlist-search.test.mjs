import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { createActions, freshState } from "../../djtube/static/actions.js";

const LIST = "PL" + "a".repeat(32);
const VIDEO = "abcdefghijk";

function track(id, title) {
  return { id, title, channel: "人", duration: 90, thumbnail: null };
}

function harness({ query, fetchSearch, musicOnly = true }) {
  const state = freshState();
  state.musicOnly = musicOnly;
  const calls = [];
  const actions = createActions({
    state,
    audios: {
      A: { paused: true, currentTime: 0, duration: 120, volume: 1 },
      B: { paused: true, currentTime: 0, duration: 120, volume: 1 },
    },
    scheduleRender() {},
    queryValue: () => query,
    fetchSearch: async (text, music) => {
      calls.push([text, music]);
      return fetchSearch(text, music);
    },
  });
  return { state, actions, calls };
}

test("a playlist url fills the same result list", async () => {
  const rows = [track(VIDEO, "夜"), track("bbbbbbbbbbb", "朝")];
  const { state, actions, calls } = harness({
    query: `https://www.youtube.com/playlist?list=${LIST}`,
    fetchSearch: async () => ({ source: "youtube", tracks: rows }),
  });
  await actions.submitSearch();
  assert.deepEqual(calls, [[`https://www.youtube.com/playlist?list=${LIST}`, true]]);
  assert.deepEqual(state.results, rows);
  assert.equal(state.source, "youtube");
  assert.equal(state.searchError, "");
  assert.equal(state.selected, 0);
});

test("a mix url shows the one track the server returned", async () => {
  const one = track(VIDEO, "一曲");
  const { state, actions, calls } = harness({
    query: `https://www.youtube.com/watch?v=${VIDEO}&list=RD${VIDEO}&start_radio=1`,
    fetchSearch: async () => ({ source: "oembed", tracks: [one] }),
  });
  await actions.submitSearch();
  assert.deepEqual(calls, [[`https://www.youtube.com/watch?v=${VIDEO}&list=RD${VIDEO}&start_radio=1`, true]]);
  assert.deepEqual(state.results, [one]);
  assert.equal(state.searchError, "");
});

test("an api limit still shows the server message", async () => {
  const limited = harness({
    query: `https://www.youtube.com/playlist?list=${LIST}`,
    fetchSearch: async () => {
      throw new Error("YouTube API の上限に達しました");
    },
  });
  await limited.actions.submitSearch();
  assert.deepEqual(limited.state.results, []);
  assert.equal(limited.state.source, "");
  assert.equal(limited.state.searchError, "YouTube API の上限に達しました");
});

test("a single video and a keyword search are unchanged", async () => {
  const one = track(VIDEO, "一曲");
  const video = harness({
    query: `https://www.youtube.com/watch?v=${VIDEO}`,
    musicOnly: false,
    fetchSearch: async () => ({ source: "oembed", tracks: [one] }),
  });
  await video.actions.submitSearch();
  assert.deepEqual(video.calls, [[`https://www.youtube.com/watch?v=${VIDEO}`, false]]);
  assert.deepEqual(video.state.results, [one]);
  assert.equal(video.state.source, "oembed");

  const found = [track("ccccccccccc", "検索結果")];
  const word = harness({
    query: "city pop",
    fetchSearch: async () => ({ source: "ytdlp", tracks: found }),
  });
  await word.actions.submitSearch();
  assert.deepEqual(word.state.results, found);
  assert.equal(word.state.source, "ytdlp");
  assert.equal(word.state.searchError, "");
});

test("an empty playlist says nothing was found", async () => {
  const { state, actions } = harness({
    query: LIST,
    fetchSearch: async () => ({ source: "youtube", tracks: [] }),
  });
  await actions.submitSearch();
  assert.deepEqual(state.results, []);
  assert.equal(state.searchError, "見つかりませんでした");
});

test("enter during a search keeps the first result", async () => {
  const rows = [track(VIDEO, "夜")];
  let release = () => {};
  const pending = new Promise((resolve) => {
    release = resolve;
  });
  let calls = 0;
  const state = freshState();
  const actions = createActions({
    state,
    audios: {
      A: { paused: true, currentTime: 0, duration: 120, volume: 1 },
      B: { paused: true, currentTime: 0, duration: 120, volume: 1 },
    },
    scheduleRender() {},
    queryValue: () => "city pop",
    isSearchFocused: () => true,
    fetchSearch: async () => {
      calls += 1;
      if (calls > 1) throw new Error("プレイリストを取得中です");
      await pending;
      return { source: "youtube", tracks: rows };
    },
  });
  const first = actions.submitSearch();
  await Promise.resolve();
  assert.equal(state.searching, true);
  await actions.onEnter();
  assert.equal(calls, 1);
  release();
  await first;
  assert.deepEqual(state.results, rows);
  assert.equal(state.searchError, "");
  assert.equal(state.searching, false);
});

test("toggling music during a keyword search searches again when it finishes", async () => {
  const firstRows = [track(VIDEO, "夜")];
  const secondRows = [track("bbbbbbbbbbb", "朝")];
  let release = () => {};
  const pending = new Promise((resolve) => {
    release = resolve;
  });
  const calls = [];
  const state = freshState();
  const actions = createActions({
    state,
    audios: {
      A: { paused: true, currentTime: 0, duration: 120, volume: 1 },
      B: { paused: true, currentTime: 0, duration: 120, volume: 1 },
    },
    scheduleRender() {},
    queryValue: () => "city pop",
    fetchSearch: async (text, music) => {
      calls.push([text, music]);
      if (calls.length === 1) {
        await pending;
        return { source: "youtube", tracks: firstRows };
      }
      return { source: "ytdlp", tracks: secondRows };
    },
  });
  const first = actions.submitSearch();
  await Promise.resolve();
  assert.equal(state.searching, true);
  actions.setMusicOnly(false);
  assert.equal(state.musicOnly, false);
  assert.deepEqual(calls, [["city pop", true]]);
  release();
  await first;
  assert.deepEqual(calls, [
    ["city pop", true],
    ["city pop", false],
  ]);
  assert.deepEqual(state.results, secondRows);
  assert.equal(state.source, "ytdlp");
  assert.equal(state.searchError, "");
  assert.equal(state.searching, false);
  assert.equal(state.musicOnly, false);
});

test("toggling music back during a keyword search does not search again", async () => {
  const rows = [track(VIDEO, "夜")];
  let release = () => {};
  const pending = new Promise((resolve) => {
    release = resolve;
  });
  let calls = 0;
  const state = freshState();
  const actions = createActions({
    state,
    audios: {
      A: { paused: true, currentTime: 0, duration: 120, volume: 1 },
      B: { paused: true, currentTime: 0, duration: 120, volume: 1 },
    },
    scheduleRender() {},
    queryValue: () => "city pop",
    fetchSearch: async () => {
      calls += 1;
      await pending;
      return { source: "youtube", tracks: rows };
    },
  });
  const first = actions.submitSearch();
  await Promise.resolve();
  actions.setMusicOnly(false);
  actions.setMusicOnly(true);
  assert.equal(calls, 1);
  release();
  await first;
  assert.equal(calls, 1);
  assert.deepEqual(state.results, rows);
  assert.equal(state.musicOnly, true);
  assert.equal(state.searching, false);
});

test("toggling music during a playlist page search does not search again", async () => {
  const rows = [track(VIDEO, "夜")];
  const pages = [
    `https://www.youtube.com/playlist?list=${LIST}`,
    `https://music.youtube.com/playlist?list=${LIST}`,
    `https://m.youtube.com/playlist?list=${LIST}`,
  ];
  for (const query of pages) {
    let release = () => {};
    const pending = new Promise((resolve) => {
      release = resolve;
    });
    let calls = 0;
    const state = freshState();
    const actions = createActions({
      state,
      audios: {
        A: { paused: true, currentTime: 0, duration: 120, volume: 1 },
        B: { paused: true, currentTime: 0, duration: 120, volume: 1 },
      },
      scheduleRender() {},
      queryValue: () => query,
      fetchSearch: async () => {
        calls += 1;
        if (calls > 1) throw new Error("やり直した");
        await pending;
        return { source: "youtube", tracks: rows };
      },
    });
    const first = actions.submitSearch();
    await Promise.resolve();
    assert.equal(state.searching, true);
    actions.setMusicOnly(false);
    assert.equal(state.musicOnly, false);
    assert.equal(calls, 1);
    release();
    await first;
    assert.equal(calls, 1);
    assert.deepEqual(state.results, rows);
    assert.equal(state.searching, false);
    assert.equal(state.musicOnly, false);
  }
});

test("search rows still offer both decks and playlist add", () => {
  const app = readFileSync(new URL("../../djtube/static/app.js", import.meta.url), "utf8");
  const render = app.slice(app.indexOf("function renderResults"), app.indexOf("function renderSearchStatus"));
  assert.match(render, /\$\{deck\}へ/);
  assert.match(render, /プレイリスト追加/);
  assert.equal(render.includes("function fetchSearch"), false);
});
