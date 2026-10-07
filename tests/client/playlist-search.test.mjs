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
    query: `https://www.youtube.com/watch?v=${VIDEO}&list=${LIST}`,
    fetchSearch: async () => ({ source: "youtube", tracks: rows }),
  });
  await actions.submitSearch();
  assert.deepEqual(calls, [[`https://www.youtube.com/watch?v=${VIDEO}&list=${LIST}`, true]]);
  assert.deepEqual(state.results, rows);
  assert.equal(state.source, "youtube");
  assert.equal(state.searchError, "");
  assert.equal(state.selected, 0);
});

test("an unimportable playlist and an api limit show the server message", async () => {
  const rejected = harness({
    query: "WL",
    fetchSearch: async () => {
      throw new Error("取り込めない種類のリストです");
    },
  });
  await rejected.actions.submitSearch();
  assert.deepEqual(rejected.state.results, []);
  assert.equal(rejected.state.searchError, "取り込めない種類のリストです");

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

test("search rows still offer both decks and playlist add", () => {
  const app = readFileSync(new URL("../../djtube/static/app.js", import.meta.url), "utf8");
  const render = app.slice(app.indexOf("function renderResults"), app.indexOf("function renderSearchStatus"));
  assert.match(render, /\$\{deck\}へ/);
  assert.match(render, /プレイリスト追加/);
  assert.equal(render.includes("function fetchSearch"), false);
});
