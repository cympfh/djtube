import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { createActions, freshState } from "../../djtube/static/actions.js";
import { handleKeydown } from "../../djtube/static/keys.js";
import {
  IMPORT_DIALOG_POINTER_SHIELD_MS,
  createPointerShield,
  importResultMessage,
  playlistImportCommit,
  playlistImportLaunch,
} from "../../djtube/static/playlists.js";

const PLAYLIST_ID = "a".repeat(32);

function track(id, title) {
  return { id, title, channel: "人", duration: 90, thumbnail: `https://thumb.djtube.test/${id}.jpg` };
}

function keyEvent(key, target) {
  return {
    key,
    target,
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    shiftKey: false,
    preventDefault() {},
  };
}

function textTarget(id) {
  return { id, tagName: "INPUT", type: "text", closest() { return null; } };
}

function harness(overrides = {}) {
  const state = freshState();
  const audios = {
    A: { paused: true, currentTime: 0, duration: 120, volume: 1 },
    B: { paused: true, currentTime: 0, duration: 120, volume: 1 },
  };
  let url = overrides.url ?? "";
  let destination = overrides.destination ?? "new";
  let name = overrides.name ?? "";
  const calls = [];
  const actions = createActions({
    state,
    audios,
    scheduleRender() {},
    importPlaylist: overrides.importPlaylist,
    playlistImportUrl: () => url,
    setPlaylistImportUrl(value) {
      url = value;
    },
    playlistImportDestination: () => destination,
    setPlaylistImportDestination(value) {
      destination = value;
    },
    playlistImportName: () => name,
    setPlaylistImportName(value) {
      name = value;
    },
    ...overrides.deps,
  });
  return {
    state,
    actions,
    calls,
    url: () => url,
    destination: () => destination,
    name: () => name,
    setUrl: (value) => {
      url = value;
    },
    setDestination: (value) => {
      destination = value;
    },
    setName: (value) => {
      name = value;
    },
  };
}

test("the import result lists only the counts that are not zero", () => {
  assert.equal(importResultMessage({ added: 8, duplicates: 3, unavailable: 2 }), "8曲追加、3曲は重複、2曲は非公開か削除");
  assert.equal(importResultMessage({}), "");
  assert.equal(importResultMessage({ added: -4, duplicates: "nope", unavailable: 1.2 }), "1曲は非公開か削除");
  assert.equal(
    importResultMessage({ added: 2, duplicates: 1, unavailable: 1, overflow: 4 }),
    "2曲追加、1曲は重複、1曲は非公開か削除、4曲は入りきらない",
  );
  assert.equal(importResultMessage({ overflow: 3 }), "3曲は入りきらない");
});

test("importing into the selected playlist appends and reports the counts", async () => {
  let body = null;
  const ui = harness({
    url: "https://www.youtube.com/watch?v=abcdefghijk&list=PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    destination: PLAYLIST_ID,
    importPlaylist: async (payload) => {
      body = payload;
      return {
        playlist: {
          id: PLAYLIST_ID,
          name: "夜",
          tracks: [track("bbbbbbbbbbb", "先"), track("ccccccccccc", "新")],
        },
        added: 1,
        duplicates: 2,
        unavailable: 1,
      };
    },
  });
  ui.state.playlists = [{ id: PLAYLIST_ID, name: "夜", tracks: [track("bbbbbbbbbbb", "先")] }];
  ui.state.playlistId = PLAYLIST_ID;
  ui.state.playlistIndex = 0;

  await ui.actions.importPlaylist();

  assert.deepEqual(body, {
    url: "https://www.youtube.com/watch?v=abcdefghijk&list=PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    playlist_id: PLAYLIST_ID,
  });
  assert.equal(body.name, undefined);
  assert.equal(ui.state.playlistImportNote, "1曲追加、2曲は重複、1曲は非公開か削除");
  assert.equal(ui.state.playlistImportError, false);
  assert.equal(ui.state.playlistImporting, false);
  assert.equal(ui.state.playlistId, PLAYLIST_ID);
  assert.deepEqual(
    ui.state.playlists[0].tracks.map((item) => item.id),
    ["bbbbbbbbbbb", "ccccccccccc"],
  );
  assert.equal(ui.state.playlistIndex, 1);
  assert.equal(ui.url(), "");
});

test("importing into a new playlist sends the name and selects that playlist", async () => {
  let body = null;
  const ui = harness({
    url: "  PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  ",
    destination: "new",
    name: "  朝のセット  ",
    importPlaylist: async (payload) => {
      body = payload;
      return {
        playlist: { id: "b".repeat(32), name: "朝のセット", tracks: [track("ddddddddddd", "朝")] },
        added: 1,
        duplicates: 0,
        unavailable: 0,
      };
    },
  });
  ui.state.playlists = [{ id: PLAYLIST_ID, name: "夜", tracks: [track("bbbbbbbbbbb", "先")] }];
  ui.state.playlistId = PLAYLIST_ID;

  await ui.actions.importPlaylist();

  assert.deepEqual(body, { url: "PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", name: "朝のセット" });
  assert.equal(ui.state.playlistId, "b".repeat(32));
  assert.equal(ui.state.playlists.length, 2);
  assert.equal(ui.state.playlists[0].tracks[0].id, "bbbbbbbbbbb");
  assert.equal(ui.state.playlistIndex, 0);
  assert.equal(ui.state.playlistImportNote, "1曲追加");
  assert.equal(ui.state.playlistBusy, false);
  assert.equal(ui.url(), "");
  assert.equal(ui.destination(), "b".repeat(32));
  assert.equal(ui.name(), "");
});

test("an empty url or a new playlist without a name does not call the server", async () => {
  let calls = 0;
  const ui = harness({
    destination: "new",
    name: "",
    importPlaylist: async () => {
      calls += 1;
      return {};
    },
  });
  await ui.actions.importPlaylist();
  assert.equal(calls, 0);
  assert.equal(ui.state.playlistImportNote, "プレイリストのURLを入れてください");
  assert.equal(ui.state.playlistImportError, true);

  ui.setUrl("https://www.youtube.com/playlist?list=PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
  await ui.actions.importPlaylist();
  assert.equal(calls, 0);
  assert.equal(ui.state.playlistImportNote, "名前を入れてください");
});

test("a failed import leaves the playlist and shows the server message", async () => {
  const ui = harness({
    url: "PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    destination: PLAYLIST_ID,
    importPlaylist: async () => {
      throw new Error("プレイリストを取れませんでした");
    },
  });
  ui.state.playlists = [{ id: PLAYLIST_ID, name: "夜", tracks: [track("bbbbbbbbbbb", "先")] }];
  ui.state.playlistId = PLAYLIST_ID;
  await ui.actions.importPlaylist();
  assert.equal(ui.state.playlists[0].tracks.length, 1);
  assert.equal(ui.state.playlistImportNote, "プレイリストを取れませんでした");
  assert.equal(ui.state.playlistImportError, true);
  assert.equal(ui.state.playlistImporting, false);
  assert.equal(ui.url(), "PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
});

test("a second import cancels the first and does not mark the playlist busy", async () => {
  let calls = 0;
  const ui = harness({
    url: "PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    destination: PLAYLIST_ID,
    importPlaylist: (_body, options) =>
      new Promise((_resolve, reject) => {
        calls += 1;
        const abort = () => reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
        if (options?.signal?.aborted) {
          abort();
          return;
        }
        options?.signal?.addEventListener("abort", abort);
      }),
  });
  ui.state.playlists = [{ id: PLAYLIST_ID, name: "夜", tracks: [track("bbbbbbbbbbb", "先")] }];
  ui.state.playlistId = PLAYLIST_ID;
  const first = ui.actions.importPlaylist();
  assert.equal(ui.state.playlistImporting, true);
  assert.equal(ui.state.playlistBusy, false);
  ui.actions.importPlaylist();
  await first;
  assert.equal(calls, 1);
  assert.equal(ui.state.playlistImporting, false);
  assert.equal(ui.state.playlistBusy, false);
  assert.equal(ui.state.playlistImportError, false);
  assert.equal(ui.state.playlistImportNote, "");
  assert.equal(ui.state.playlists[0].tracks.length, 1);
});

test("the import passes the abort signal through to the request", async () => {
  let signal;
  const ui = harness({
    url: "PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    destination: PLAYLIST_ID,
    importPlaylist: (_body, options) => {
      signal = options && options.signal;
      return new Promise(() => {});
    },
  });
  ui.state.playlists = [{ id: PLAYLIST_ID, name: "夜", tracks: [track("bbbbbbbbbbb", "先")] }];
  ui.actions.importPlaylist();
  await Promise.resolve();
  assert.ok(signal instanceof AbortSignal);
});

test("a result that arrives after cancel is not applied", async () => {
  let resolveImport;
  const ui = harness({
    url: "PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    destination: PLAYLIST_ID,
    importPlaylist: () =>
      new Promise((resolve) => {
        resolveImport = resolve;
      }),
  });
  ui.state.playlists = [{ id: PLAYLIST_ID, name: "夜", tracks: [track("bbbbbbbbbbb", "先")] }];
  ui.state.playlistId = PLAYLIST_ID;
  const pending = ui.actions.importPlaylist();
  await Promise.resolve();
  ui.actions.importPlaylist();
  resolveImport({
    playlist: {
      id: PLAYLIST_ID,
      name: "夜",
      tracks: [track("bbbbbbbbbbb", "先"), track("ccccccccccc", "後")],
    },
    added: 1,
  });
  await pending;
  assert.equal(ui.state.playlists[0].tracks.length, 1);
  assert.equal(ui.state.playlistImporting, false);
});

test("an older import does not clear a newer import", async () => {
  let firstResolve;
  let secondResolve;
  let calls = 0;
  const ui = harness({
    url: "PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    destination: PLAYLIST_ID,
    importPlaylist: () =>
      new Promise((resolve) => {
        calls += 1;
        if (calls === 1) firstResolve = resolve;
        else secondResolve = resolve;
      }),
  });
  ui.state.playlists = [{ id: PLAYLIST_ID, name: "夜", tracks: [track("bbbbbbbbbbb", "先")] }];
  ui.state.playlistId = PLAYLIST_ID;
  const first = ui.actions.importPlaylist();
  await Promise.resolve();
  ui.actions.importPlaylist();
  const second = ui.actions.importPlaylist();
  await Promise.resolve();
  assert.equal(ui.state.playlistImporting, true);
  assert.equal(calls, 2);
  firstResolve({ playlist: { id: PLAYLIST_ID, name: "夜", tracks: [] }, added: 0 });
  await first;
  assert.equal(ui.state.playlistImporting, true);
  assert.equal(ui.state.playlists[0].tracks.length, 1);
  secondResolve({
    playlist: { id: PLAYLIST_ID, name: "夜", tracks: [track("bbbbbbbbbbb", "先"), track("ccccccccccc", "後")] },
    added: 1,
  });
  await second;
  assert.equal(ui.state.playlistImporting, false);
  assert.equal(ui.state.playlists[0].tracks.length, 2);
});

test("typing in the import fields does not trigger deck or playlist keys", () => {
  const ui = harness();
  let played = false;
  ui.actions.togglePlay = () => {
    played = true;
  };
  const url = textTarget("playlist-import-url");
  const name = textTarget("playlist-import-name");
  assert.equal(handleKeydown(keyEvent("q", url), ui.actions, "playlist"), false);
  assert.equal(handleKeydown(keyEvent("Enter", url), ui.actions, "playlist"), false);
  assert.equal(handleKeydown(keyEvent("p", name), ui.actions, "playlist"), false);
  assert.equal(handleKeydown(keyEvent("Enter", name), ui.actions, "playlist"), false);
  assert.equal(played, false);
});

test("an empty url does not ask for the import dialog", () => {
  assert.equal(playlistImportLaunch(""), "need-url");
  assert.equal(playlistImportLaunch("   "), "need-url");
  assert.equal(playlistImportLaunch(null), "need-url");
  assert.equal(
    playlistImportLaunch(" https://www.youtube.com/playlist?list=PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "),
    "open",
  );
  assert.equal(playlistImportLaunch("PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", { importing: true }), "abort");
  assert.equal(playlistImportLaunch("PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", { busy: true }), "ignore");
  assert.equal(playlistImportLaunch("PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", { open: true }), "ignore");
});

function pointerEvent(type) {
  return {
    type,
    defaultPrevented: false,
    stopped: false,
    preventDefault() {
      this.defaultPrevented = true;
    },
    stopPropagation() {
      this.stopped = true;
    },
  };
}

test("the second click after the import dialog closes does not reach the deck", () => {
  assert.equal(IMPORT_DIALOG_POINTER_SHIELD_MS, 400);
  let now = 10_000;
  const shield = createPointerShield(IMPORT_DIALOG_POINTER_SHIELD_MS, () => now);
  const deck = { cue: 0, rate: 100, eqHigh: 500 };
  function deliver(type) {
    const event = pointerEvent(type);
    if (!shield.swallow(event)) {
      deck.cue += type === "click" || type === "dblclick" ? 1 : 0;
      if (type === "pointerdown" || type === "mousedown" || type === "click") {
        deck.rate = 130;
        deck.eqHigh = 800;
      }
    }
    return event;
  }

  const closing = pointerEvent("click");
  assert.equal(shield.swallow(closing), false);
  shield.arm();
  for (const type of ["pointerdown", "mousedown", "click", "dblclick"]) {
    const event = deliver(type);
    assert.equal(event.defaultPrevented, true, type);
    assert.equal(event.stopped, true, type);
  }
  assert.deepEqual(deck, { cue: 0, rate: 100, eqHigh: 500 });

  now += 399;
  assert.equal(deliver("click").stopped, true);
  assert.equal(deck.rate, 100);
  now += 1;
  assert.equal(deliver("click").stopped, false);
  assert.equal(deck.cue, 1);
  assert.equal(deck.rate, 130);
});

test("a touch after the import dialog closes does not move a slider", () => {
  let now = 5_000;
  const shield = createPointerShield(IMPORT_DIALOG_POINTER_SHIELD_MS, () => now);
  const deck = { rate: 100, eqHigh: 500 };
  function deliver(type) {
    const event = pointerEvent(type);
    if (!shield.swallow(event)) {
      if (type === "touchstart") deck.rate = 89;
      if (type === "touchmove") {
        deck.rate = 56;
        deck.eqHigh = 230;
      }
      if (type === "pointerup" || type === "mouseup") deck.rate = 111;
    }
    return event;
  }

  shield.arm();
  now += 60;
  for (const type of ["touchstart", "touchmove", "pointerup", "mouseup"]) {
    const event = deliver(type);
    assert.equal(event.defaultPrevented, true, type);
    assert.equal(event.stopped, true, type);
  }
  assert.deepEqual(deck, { rate: 100, eqHigh: 500 });

  now += 20;
  assert.equal(deliver("touchstart").stopped, true);
  assert.equal(deliver("touchmove").stopped, true);
  assert.equal(deck.rate, 100);
  assert.equal(deck.eqHigh, 500);

  now = 5_000 + IMPORT_DIALOG_POINTER_SHIELD_MS;
  assert.equal(deliver("touchstart").stopped, false);
  assert.equal(deck.rate, 89);
});

test("submitting the import dialog arms the shield before it closes", () => {
  const app = readFileSync(new URL("../../djtube/static/app.js", import.meta.url), "utf8");
  const html = readFileSync(new URL("../../djtube/templates/index.html", import.meta.url), "utf8");
  const css = readFileSync(new URL("../../djtube/static/app.css", import.meta.url), "utf8");
  const submitAt = app.indexOf("function submitPlaylistImport");
  const submitFn = app.slice(submitAt, app.indexOf('document.getElementById("playlist-import")', submitAt));
  const armAt = submitFn.indexOf("armImportPointerShield()");
  const closeAt = submitFn.indexOf("playlistImportDialog.close()");
  assert.ok(armAt >= 0);
  assert.ok(closeAt > armAt);
  const helperAt = app.indexOf("function armImportPointerShield");
  const helper = app.slice(helperAt, app.indexOf("function openPlaylistEdit", helperAt));
  assert.match(helper, /importPointerShield\.arm\(\)/);
  assert.match(helper, /importShield\.hidden = false/);
  assert.match(helper, /importShield\.hidden = true/);
  assert.match(helper, /IMPORT_DIALOG_POINTER_SHIELD_MS/);
  assert.match(html, /id="playlist-import-shield"/);
  const shieldCss = css.slice(css.indexOf("#playlist-import-shield"));
  assert.match(shieldCss, /position:\s*fixed/);
  assert.match(shieldCss, /inset:\s*0/);
});

test("the import dialog commits only after a destination is ready", () => {
  assert.equal(playlistImportCommit({ open: true, destination: "new", name: "  " }), "need-name");
  assert.equal(playlistImportCommit({ open: true, destination: "new", name: " 朝のセット " }), "import");
  assert.equal(playlistImportCommit({ open: true, destination: PLAYLIST_ID, name: "" }), "import");
  assert.equal(playlistImportCommit({ open: false, destination: PLAYLIST_ID, name: "朝" }), "ignore");
  assert.equal(playlistImportCommit({ open: true, importing: true, destination: PLAYLIST_ID, name: "朝" }), "ignore");
  assert.equal(playlistImportCommit({ open: true, busy: true, destination: "new", name: "朝" }), "ignore");
});

test("the playlist panel keeps the url field and opens a destination dialog", () => {
  const html = readFileSync(new URL("../../djtube/templates/index.html", import.meta.url), "utf8");
  const app = readFileSync(new URL("../../djtube/static/app.js", import.meta.url), "utf8");
  const css = readFileSync(new URL("../../djtube/static/app.css", import.meta.url), "utf8");
  const panel = html.slice(html.indexOf('id="playlist-panel"'), html.indexOf('id="playlist-tracks"'));
  const dialog = html.slice(html.indexOf('id="playlist-import-dialog"'), html.indexOf("</dialog>", html.indexOf('id="playlist-import-dialog"')));
  assert.match(panel, /id="playlist-import-url"/);
  assert.match(panel, /placeholder="プレイリストのURL"/);
  assert.match(panel, /id="playlist-import"/);
  assert.match(panel, /aria-haspopup="dialog"/);
  assert.match(panel, /aria-controls="playlist-import-dialog"/);
  assert.match(panel, />取り込む</);
  assert.match(panel, /id="playlist-import-note"/);
  assert.doesNotMatch(panel, /id="playlist-import-dest"/);
  assert.doesNotMatch(panel, /id="playlist-import-name"/);
  assert.ok(panel.indexOf('id="playlist-edit"') < panel.indexOf('id="playlist-import-url"'));
  assert.match(dialog, /id="playlist-import-dest"/);
  assert.match(dialog, /新しいプレイリスト/);
  assert.match(dialog, /id="playlist-import-name"/);
  assert.match(dialog, /id="playlist-import-run"/);
  assert.match(dialog, /id="playlist-import-cancel"/);
  assert.match(dialog, />キャンセル</);
  assert.match(app, /\/api\/playlists\/import/);
  assert.match(app, /playlistImportUrl\.addEventListener\("keydown"/);
  assert.match(app, /playlistImportName\.addEventListener\("keydown"/);
  assert.match(app, /actions\.importPlaylist\(\)/);
  assert.match(app, /function renderImport\(\)/);
  assert.match(app, /function openPlaylistImportDialog\(\)/);
  assert.match(app, /function submitPlaylistImport\(\)/);
  assert.match(app, /playlistImportLaunch\(/);
  assert.match(app, /playlistImportCommit\(/);
  assert.match(app, /showModal\(\)/);
  const keys = app.slice(app.indexOf('window.addEventListener(\n  "keydown"'), app.indexOf("const midiButton"));
  assert.match(keys, /playlistEditDialog\.open \|\| playlistImportDialog\.open/);
  assert.ok(keys.indexOf("playlistImportDialog.open") < keys.indexOf("handleKeydown("));
  const openFn = app.slice(app.indexOf("function openPlaylistImportDialog"), app.indexOf("function submitPlaylistImport"));
  assert.ok(openFn.indexOf('launch === "need-url"') < openFn.indexOf("showModal()"));
  assert.match(openFn, /actions\.importPlaylist\(\)/);
  const submitAt = app.indexOf("function submitPlaylistImport");
  const submitFn = app.slice(submitAt, app.indexOf('document.getElementById("playlist-import")', submitAt));
  assert.match(submitFn, /commit === "need-name"/);
  assert.match(submitFn, /名前を入れてください/);
  assert.ok(submitFn.indexOf("armImportPointerShield()") >= 0);
  assert.ok(submitFn.indexOf("armImportPointerShield()") < submitFn.indexOf("playlistImportDialog.close()"));
  assert.ok(submitFn.indexOf("playlistImportDialog.close()") < submitFn.indexOf("actions.importPlaylist()"));
  const dismissAt = app.indexOf('document.getElementById("playlist-import-cancel")');
  const dismissFn = app.slice(dismissAt, app.indexOf('playlistImportDialog.addEventListener("close"', dismissAt));
  assert.equal(dismissFn.match(/armImportPointerShield\(\);\n\s*playlistImportDialog\.close\(\)/g).length, 2);
  const closeAt = app.indexOf('playlistImportDialog.addEventListener("close"');
  const closeFn = app.slice(closeAt, app.indexOf("playlistImportDest.addEventListener", closeAt));
  assert.match(closeFn, /armImportPointerShield\(\)/);
  assert.ok(closeFn.indexOf("armImportPointerShield()") < closeFn.indexOf("scheduleRender()"));
  assert.match(closeFn, /pointerdown/);
  assert.match(closeFn, /mousedown/);
  assert.match(closeFn, /pointerup/);
  assert.match(closeFn, /mouseup/);
  assert.match(closeFn, /"click"/);
  assert.match(closeFn, /dblclick/);
  assert.match(closeFn, /touchstart/);
  assert.match(closeFn, /touchmove/);
  assert.match(closeFn, /passive:\s*false/);
  assert.match(closeFn, /importPointerShield\.swallow\(event\)/);
  assert.match(closeFn, /true\)/);
  const midi = app.slice(app.indexOf("const midiControl = createMidiControl"));
  assert.match(midi, /blocked:\s*\(\)\s*=>\s*playlistEditDialog\.open \|\| playlistImportDialog\.open/);
  assert.match(css, /#playlist-import-url/);
  assert.match(css, /#playlist-import-dialog/);
  assert.match(css, /#search-button, #playlist-import, #playlist-import-run/);
  assert.doesNotMatch(`${html}\n${app}\n${css}`, /googleapis|YOUTUBE_API_KEY|AIza/);
  assert.doesNotMatch(html, /id="playlist-import-dest"[^>]*aria-labelledby/);
  assert.match(html, /for="playlist-import-dest">追加先/);
  const renderImport = app.slice(app.indexOf("function renderImport"), app.indexOf("function renderAddNote"));
  assert.equal(renderImport.includes("innerHTML"), false);
  assert.match(renderImport, /option\.textContent = playlist\.name/);
  assert.match(renderImport, /note\.textContent = state\.playlistImportNote/);
  assert.match(renderImport, /やめる/);
  const confirmingAt = app.indexOf("const confirming = pendingRemove");
  const decks = app.slice(app.lastIndexOf('for (const deck of ["A", "B"])', confirmingAt), confirmingAt);
  assert.match(decks, /button\.disabled = busy;/);
  assert.equal(decks.includes("playlistImporting"), false);
  const playlists = readFileSync(new URL("../../djtube/static/playlists.js", import.meta.url), "utf8");
  const importer = playlists.slice(playlists.indexOf("function importPlaylist"), playlists.indexOf("function loadPlaylistTrack"));
  assert.match(importer, /new AbortController/);
  assert.equal(importer.includes("playlistBusy = true"), false);
  assert.match(importer, /setPlaylistImportDestination/);
  assert.match(importer, /setPlaylistImportName/);
});

test("importPlaylist passes the abort signal through to fetch", async () => {
  const app = readFileSync(new URL("../../djtube/static/app.js", import.meta.url), "utf8");
  const call = app.slice(app.indexOf("importPlaylist: (body, options = {})"), app.indexOf("playlistImportUrl:"));
  assert.match(call, /signal:\s*options\.signal/);
  const fetchSrc = app.slice(app.indexOf("async function fetchJson"), app.indexOf("async function fetchSearch"));
  const calls = [];
  const fetchJson = new Function("fetch", "prefix", `${fetchSrc}\nreturn fetchJson;`)(
    async (url, options) => {
      calls.push({ url, options });
      return { ok: true, status: 200, json: async () => ({ added: 1 }) };
    },
    "",
  );
  const controller = new AbortController();
  await fetchJson("/api/playlists/import", {
    method: "POST",
    body: "{}",
    signal: controller.signal,
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.signal, controller.signal);
});
