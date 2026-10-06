import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  LIVE_BITRATE,
  LIVE_BUFFER_LIMIT,
  LIVE_OUTPUT_STALLED,
  LIVE_BUTTON_LABEL,
  LIVE_COPY_AUDIO,
  LIVE_COPY_FAILED,
  LIVE_COPY_VIDEO,
  LIVE_COPIED,
  LIVE_DROPPED,
  LIVE_MENU_CLOSE_MS,
  LIVE_MENU_COPY_FAILED,
  LIVE_MENU_HOLD_MS,
  LIVE_MENU_MOUSE_GRACE_MS,
  LIVE_MENU_NOTE_MS,
  LIVE_SEND_BACKLOG,
  LIVE_TAKEN,
  LIVE_IDLE,
  LIVE_MIME,
  LIVE_STARTING,
  LIVE_TIMESLICE_MS,
  LIVE_UNSUPPORTED,
  applyLiveStatus,
  closeReason,
  createLiveControl,
  listenerUrl,
  videoListenerUrl,
  liveButtonFace,
  livePublishUrl,
  liveStatusText,
  liveSupported,
  armClipboard,
  claimMessage,
  deckSnapshot,
  masterGapNote,
  mimeMessage,
  nowMessage,
  NOW_INTERVAL_MS,
} from "../../djtube/static/live.js";

function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function fakeButton() {
  const writes = [];
  let title = LIVE_IDLE;
  let label = LIVE_BUTTON_LABEL;
  let expanded = "false";
  let face = "off";
  let liveText = "";
  const listeners = {};
  const button = {
    writes,
    focused: false,
    get title() {
      return title;
    },
    set title(value) {
      writes.push("title");
      title = value;
    },
    getAttribute(name) {
      if (name === "aria-label") return label;
      if (name === "aria-expanded") return expanded;
      return null;
    },
    setAttribute(name, value) {
      if (name === "aria-label") {
        writes.push("aria-label");
        label = value;
        return;
      }
      if (name === "aria-expanded") expanded = value;
    },
    dataset: {
      get live() {
        return face;
      },
      set live(value) {
        writes.push("data-live");
        face = value;
      },
    },
    addEventListener(type, fn) {
      listeners[type] = fn;
    },
    focusVisible: false,
    hover: false,
    matches(selector) {
      if (selector === ":focus-visible") return this.focusVisible === true;
      if (selector === ":hover") return this.hover === true;
      return false;
    },
    click() {
      return listeners.click();
    },
    focus() {
      this.focused = true;
    },
    emit(type, event) {
      return listeners[type]?.(event);
    },
  };
  const live = {
    get textContent() {
      return liveText;
    },
    set textContent(value) {
      writes.push("status");
      liveText = value;
    },
  };
  return {
    button,
    live,
    painted() {
      return { title, label, face, live: liveText };
    },
  };
}

function fakeSocket(url) {
  return {
    url,
    readyState: 1,
    sent: [],
    closed: null,
    onmessage: null,
    onclose: null,
    send(data) {
      this.sent.push(data);
    },
    close(code) {
      this.readyState = 3;
      this.closed = code ?? 1000;
      this.onclose?.({ code: this.closed });
    },
    serverClose(code) {
      this.readyState = 3;
      this.onclose?.({ code });
    },
    receive(text) {
      this.onmessage?.({ data: text });
    },
  };
}

function openBus(state = "running") {
  const destination = { name: "speakers" };
  const master = {
    gain: { value: 1 },
    connections: [],
    connect(target) {
      this.connections.push(target);
    },
    disconnect(target) {
      this.connections = this.connections.filter((item) => item !== target);
    },
  };
  master.connect(destination);
  const context = {
    state,
    destination,
    destinations: [],
    resume() {
      this.state = "running";
      this.resumed = (this.resumed || 0) + 1;
      return Promise.resolve();
    },
    createMediaStreamDestination() {
      const track = {
        live: true,
        stop() {
          this.live = false;
        },
      };
      const dest = {
        stream: {
          id: "mix",
          getTracks() {
            return [track];
          },
        },
        track,
      };
      this.destinations.push(dest);
      return dest;
    },
    createGain() {
      return {
        gain: { value: 1 },
        connections: [],
        connect(target) {
          this.connections.push(target);
        },
        disconnect() {
          this.connections = [];
        },
      };
    },
    createConstantSource() {
      const source = {
        started: false,
        stopped: false,
        connections: [],
        connect(target) {
          this.connections.push(target);
        },
        disconnect() {
          this.connections = [];
        },
        start() {
          this.started = true;
        },
        stop() {
          this.stopped = true;
        },
      };
      this.keeps.push(source);
      return source;
    },
    keeps: [],
  };
  return { context, master, failed: false, destination };
}

function fakeClock() {
  let now = 0;
  let nextId = 1;
  const queue = [];
  return {
    schedule(fn, ms) {
      const id = nextId;
      nextId += 1;
      queue.push({ id, at: now + ms, fn });
      return id;
    },
    clear(id) {
      const item = queue.find((entry) => entry.id === id);
      if (item) item.fn = null;
    },
    advance(ms) {
      now += ms;
      const due = queue.filter((entry) => entry.fn && entry.at <= now);
      for (const entry of due) {
        const fn = entry.fn;
        entry.fn = null;
        fn();
      }
    },
  };
}

function fakeMenu() {
  const listeners = { anchor: {} };
  let headText = "";
  let resultText = "";
  const anchor = {
    dataset: {},
    hover: false,
    focus: false,
    matches(selector) {
      if (selector === ":hover") return this.hover;
      if (selector === ":focus-within") return this.focus;
      return false;
    },
    contains(node) {
      return node?.inside === true;
    },
    addEventListener(type, fn) {
      listeners.anchor[type] = fn;
    },
    emit(type, event) {
      listeners.anchor[type]?.(event);
    },
  };
  const head = {
    get textContent() {
      return headText;
    },
    set textContent(value) {
      headText = String(value);
    },
  };
  const result = {
    dataset: {},
    get textContent() {
      return resultText;
    },
    set textContent(value) {
      resultText = String(value);
    },
  };
  function pressable(slot) {
    return {
      addEventListener(type, fn) {
        if (type === "click") listeners[slot] = fn;
      },
      click() {
        return listeners[slot]?.({
          preventDefault() {},
          stopPropagation() {},
        });
      },
    };
  }
  return { anchor, head, audio: pressable("audio"), video: pressable("video"), result };
}

function setup(extra = {}) {
  const ui = fakeButton();
  const menu = extra.menu === null ? null : extra.menu || fakeMenu();
  const sockets = [];
  const recorders = [];
  const copies = [];
  const bus = extra.bus || openBus();
  const control = createLiveControl({
    button: ui.button,
    live: ui.live,
    bus,
    audios: extra.audios || null,
    prefix: "/djtube",
    origin: extra.origin || "https://s.cympfh.cc",
    supported: extra.supported !== false,
    connectSocket(url) {
      extra.onSocket?.();
      const socket = extra.makeSocket ? extra.makeSocket(url) : fakeSocket(url);
      sockets.push(socket);
      return socket;
    },
    createRecorder(stream, options) {
      const recorder = {
        stream,
        options,
        timeslice: null,
        ondataavailable: null,
        stopped: false,
        start(ms) {
          this.timeslice = ms;
        },
        stop() {
          this.stopped = true;
        },
        emit(blob) {
          this.ondataavailable?.({ data: blob });
        },
      };
      recorders.push(recorder);
      return recorder;
    },
    copyText:
      extra.copyText ||
      (async (url) => {
        copies.push(url);
      }),
    armCopy: extra.armCopy,
    nowPlaying: extra.nowPlaying,
    schedule: extra.schedule,
    unschedule: extra.unschedule,
    clock: extra.clock,
    sleep: extra.sleep,
    menu,
    setTimer: extra.setTimer,
    clearTimer: extra.clearTimer,
    now: extra.now,
    document: extra.document,
    root: extra.root,
  });
  return { ui, menu, sockets, recorders, copies, bus, control };
}

const LIVE_TOKEN = "secret-token";

function idMessage(id = "ABCD", token = LIVE_TOKEN) {
  return JSON.stringify({ type: "id", id, token });
}

const PUBLISH_URL = "wss://s.cympfh.cc/djtube/api/live/publish";

function openedClaim(seq = 1, id = "", token = "") {
  return claimMessage(id, token, seq);
}

async function goLive(harness, id = "ABCD", token = LIVE_TOKEN) {
  const pending = harness.ui.button.click();
  await flush();
  harness.sockets[0].receive(idMessage(id, token));
  await pending;
  await flush();
}

test("listener and publish URLs use the public prefix", () => {
  assert.equal(listenerUrl("/djtube", "ABCD", "https://s.cympfh.cc"), "https://s.cympfh.cc/djtube/stream/ABCD");
  assert.equal(
    videoListenerUrl("/djtube", "ABCD", "https://s.cympfh.cc"),
    "https://s.cympfh.cc/djtube/stream/ABCD?thumbnail=1",
  );
  assert.equal(livePublishUrl("/djtube", "https://s.cympfh.cc"), PUBLISH_URL);
  assert.equal(livePublishUrl("/djtube", "http://127.0.0.1:8098"), "ws://127.0.0.1:8098/djtube/api/live/publish");
  assert.equal(livePublishUrl("/djtube", "https://s.cympfh.cc", "ABCD", LIVE_TOKEN), PUBLISH_URL);
  assert.equal(PUBLISH_URL.includes("token"), false);
  assert.equal(mimeMessage(), JSON.stringify({ type: "mime", mime: LIVE_MIME }));
  assert.equal(
    claimMessage("ABCD", LIVE_TOKEN, 2),
    JSON.stringify({ type: "mime", mime: LIVE_MIME, seq: 2, id: "ABCD", token: LIVE_TOKEN }),
  );
  assert.equal(closeReason(4408), "音声が届かなくなったため、配信を止めました");
  assert.equal(closeReason(4409), LIVE_TAKEN);
  assert.equal(closeReason(4429), "配信の上限に達しました");
  assert.equal(closeReason(1003), "配信の形式が受け付けられませんでした");
  assert.equal(closeReason(1008), "送信の上限に達しました");
  assert.equal(closeReason(1009), "データが大きすぎるため、配信を止めました");
  assert.equal(closeReason(1006), LIVE_DROPPED);
  assert.equal(closeReason(4410), LIVE_DROPPED);
  assert.equal(liveButtonFace({ state: "live" }), "on");
  assert.equal(liveButtonFace({ state: "idle" }), "off");
  assert.equal(liveButtonFace({ state: "error", reason: "x" }), "off");
  assert.equal(liveSupported({}), false);
  assert.equal(liveSupported({ MediaRecorder: function MediaRecorder() {} }), false);
  const unsupported = function MediaRecorder() {};
  unsupported.isTypeSupported = () => false;
  assert.equal(liveSupported({ MediaRecorder: unsupported }), false);
  const supported = function MediaRecorder() {};
  supported.isTypeSupported = (mime) => mime === LIVE_MIME;
  assert.equal(liveSupported({ MediaRecorder: supported }), true);
  assert.equal(masterGapNote(null), "");
  assert.equal(
    masterGapNote({ A: { offMaster: () => true }, B: { offMaster: () => false } }),
    "デッキ A はマスターに入っていないため、配信には入りません",
  );
  assert.equal(
    masterGapNote({ A: { offMaster: () => true }, B: { offMaster: () => true } }),
    "デッキ A と B はマスターに入っていないため、配信には入りません",
  );
});

test("applyLiveStatus writes a result once and leaves the button name", () => {
  const ui = fakeButton();
  const head = { textContent: "" };
  assert.equal(liveStatusText({ state: "idle" }), LIVE_IDLE);
  assert.equal(liveStatusText({ state: "unsupported" }), LIVE_UNSUPPORTED);
  assert.equal(liveStatusText({ state: "starting" }), LIVE_STARTING);
  assert.equal(
    liveStatusText({ state: "live", url: "https://s.cympfh.cc/djtube/stream/ABCD" }),
    "配信中：https://s.cympfh.cc/djtube/stream/ABCD",
  );

  applyLiveStatus(ui.button, ui.live, { state: "idle" }, head);
  assert.deepEqual(ui.painted(), {
    title: LIVE_IDLE,
    label: LIVE_BUTTON_LABEL,
    face: "off",
    live: LIVE_IDLE,
  });
  assert.deepEqual(ui.button.writes, ["status"]);

  ui.button.writes.length = 0;
  applyLiveStatus(ui.button, ui.live, { state: "idle" }, head);
  assert.deepEqual(ui.button.writes, []);
  assert.equal(head.textContent, "");

  applyLiveStatus(ui.button, ui.live, { state: "starting" }, head);
  assert.deepEqual(ui.painted(), {
    title: LIVE_STARTING,
    label: LIVE_BUTTON_LABEL,
    face: "off",
    live: LIVE_IDLE,
  });
  assert.deepEqual(ui.button.writes, ["title"]);

  ui.button.writes.length = 0;
  applyLiveStatus(ui.button, ui.live, { state: "starting" }, head);
  assert.deepEqual(ui.button.writes, []);
  assert.equal(head.textContent, "");

  const url = "https://s.cympfh.cc/djtube/stream/ABCD";
  applyLiveStatus(ui.button, ui.live, { state: "live", url, note: "" }, head);
  assert.deepEqual(ui.painted(), {
    title: "",
    label: LIVE_BUTTON_LABEL,
    face: "on",
    live: `配信中：${url}`,
  });
  assert.equal(head.textContent, `配信中：${url}`);
  assert.deepEqual(ui.button.writes, ["title", "status", "data-live"]);

  ui.button.writes.length = 0;
  applyLiveStatus(ui.button, ui.live, { state: "live", url, note: "" }, head);
  assert.deepEqual(ui.button.writes, []);

  applyLiveStatus(ui.button, ui.live, { state: "live", url, copyFailed: true }, head);
  assert.equal(ui.painted().title, "");
  assert.equal(head.textContent, `配信中：${url}`);
  assert.equal(ui.painted().live, `配信中：${url}。${LIVE_COPY_FAILED}`);
  assert.deepEqual(ui.button.writes, ["status"]);
  ui.button.writes.length = 0;
  applyLiveStatus(ui.button, ui.live, { state: "live", url, copyFailed: true }, head);
  assert.deepEqual(ui.button.writes, []);

  applyLiveStatus(ui.button, ui.live, { state: "error", reason: "配信の上限に達しました" }, head);
  assert.deepEqual(ui.painted(), {
    title: "配信の上限に達しました",
    label: LIVE_BUTTON_LABEL,
    face: "off",
    live: "配信の上限に達しました",
  });
  assert.deepEqual(ui.button.writes, ["title", "status", "data-live"]);
  assert.equal(head.textContent, "");

  ui.button.writes.length = 0;
  applyLiveStatus(ui.button, ui.live, { state: "error", reason: "配信の上限に達しました" }, head);
  assert.deepEqual(ui.button.writes, []);
});

test("the first click publishes the master, copies the listener URL, and sends mime once", async () => {
  const harness = setup();
  harness.ui.button.writes.length = 0;
  const pending = harness.ui.button.click();
  await flush();
  assert.equal(harness.sockets.length, 1);
  assert.equal(harness.sockets[0].url, "wss://s.cympfh.cc/djtube/api/live/publish");
  assert.equal(harness.ui.painted().title, LIVE_STARTING);
  assert.equal(harness.ui.painted().face, "off");
  assert.equal(harness.ui.painted().live, LIVE_IDLE);
  assert.equal(harness.copies.length, 0);
  assert.equal(harness.bus.context.destinations.length, 1);
  assert.equal(harness.bus.master.connections.includes(harness.bus.context.destinations[0]), true);
  assert.equal(harness.bus.master.connections.includes(harness.bus.destination), true);

  harness.sockets[0].receive(idMessage());
  await pending;
  await flush();
  const url = "https://s.cympfh.cc/djtube/stream/ABCD";
  assert.deepEqual(harness.copies, [url]);
  assert.equal(harness.ui.painted().title, "");
  assert.equal(harness.menu.head.textContent, `配信中：${url}`);
  assert.equal(harness.ui.painted().live, `配信中：${url}`);
  assert.equal(harness.ui.painted().face, "on");
  assert.equal(harness.ui.painted().label, LIVE_BUTTON_LABEL);
  assert.equal(harness.recorders.length, 1);
  assert.equal(harness.recorders[0].options.mimeType, LIVE_MIME);
  assert.equal(harness.recorders[0].options.audioBitsPerSecond, LIVE_BITRATE);
  assert.equal(harness.recorders[0].timeslice, LIVE_TIMESLICE_MS);
  assert.deepEqual(harness.sockets[0].sent, [openedClaim()]);

  harness.recorders[0].emit(new Blob([Uint8Array.of(1, 2, 3)]));
  harness.recorders[0].emit(new Blob([]));
  harness.recorders[0].emit(new Blob([Uint8Array.of(4)]));
  await flush();
  assert.equal(harness.sockets[0].sent.length, 3);
  assert.equal(harness.sockets[0].sent[0], openedClaim());
  assert.ok(harness.sockets[0].sent[1] instanceof ArrayBuffer);
  assert.ok(harness.sockets[0].sent[2] instanceof ArrayBuffer);
  assert.equal(harness.sockets[0].sent.filter((item) => typeof item === "string").length, 1);
  assert.deepEqual(new Uint8Array(harness.sockets[0].sent[1]), Uint8Array.of(1, 2, 3));
});

test("the click arms the clipboard and the id fills it in", async () => {
  const writes = [];
  let resolveText = null;
  const Item = class ClipboardItem {
    constructor(items) {
      this.items = items;
    }
  };
  const previousItem = globalThis.ClipboardItem;
  globalThis.ClipboardItem = Item;
  const nav = {
    clipboard: {
      write(items) {
        writes.push(items);
        return Promise.resolve();
      },
    },
  };
  try {
    const armed = armClipboard(nav);
    assert.equal(writes.length, 1);
    await armed.provide("https://s.cympfh.cc/djtube/stream/ABCD");
    const blob = await writes[0][0].items["text/plain"];
    assert.equal(await blob.text(), "https://s.cympfh.cc/djtube/stream/ABCD");

    const denied = armClipboard({
      clipboard: {
        write() {
          return Promise.reject(new Error("denied"));
        },
      },
    });
    await assert.rejects(() => denied.provide("https://s.cympfh.cc/djtube/stream/NOPE"), /clipboard/);
  } finally {
    if (previousItem === undefined) delete globalThis.ClipboardItem;
    else globalThis.ClipboardItem = previousItem;
  }

  const events = [];
  const harness = setup({
    armCopy() {
      resolveText = (url) => events.push(["provide", url]);
      return {
        provide(url) {
          resolveText(url);
        },
        cancel() {
          events.push(["cancel"]);
        },
      };
    },
  });
  await goLive(harness, "QRST");
  assert.deepEqual(events, [["provide", "https://s.cympfh.cc/djtube/stream/QRST"]]);
  assert.deepEqual(harness.copies, []);
  assert.equal(harness.ui.painted().face, "on");
});

test("stopping before the id arrives cancels the armed copy", async () => {
  const events = [];
  const harness = setup({
    armCopy() {
      return {
        provide(url) {
          events.push(["provide", url]);
        },
        cancel() {
          events.push(["cancel"]);
        },
      };
    },
  });
  const pending = harness.ui.button.click();
  await flush();
  harness.ui.button.click();
  await pending;
  await flush();
  assert.deepEqual(events, [["cancel"]]);
  assert.deepEqual(harness.copies, []);
  assert.equal(harness.ui.painted().title, LIVE_IDLE);
});

test("a failed copy still starts, and the URL stays on the hover", async () => {
  const harness = setup({
    copyText: async () => {
      throw new Error("denied");
    },
  });
  await goLive(harness, "WXYZ");
  const url = "https://s.cympfh.cc/djtube/stream/WXYZ";
  assert.equal(harness.ui.painted().face, "on");
  assert.equal(harness.ui.painted().title, "");
  assert.equal(harness.menu.head.textContent, `配信中：${url}`);
  assert.equal(harness.ui.painted().live, `配信中：${url}。${LIVE_COPY_FAILED}`);
  assert.equal(harness.sockets[0].sent[0], openedClaim());
});

test("a rejected clipboard reservation falls back, and a failed fallback stays on the air", async () => {
  const saved = setup({
    armCopy() {
      return {
        provide() {
          return Promise.reject(new Error("denied"));
        },
        cancel() {},
      };
    },
  });
  await goLive(saved, "COPY");
  const savedUrl = "https://s.cympfh.cc/djtube/stream/COPY";
  assert.deepEqual(saved.copies, [savedUrl]);
  assert.equal(saved.ui.painted().face, "on");
  assert.equal(saved.ui.painted().title, "");
  assert.equal(saved.menu.head.textContent, `配信中：${savedUrl}`);
  assert.equal(saved.ui.painted().live, `配信中：${savedUrl}`);

  const failed = setup({
    armCopy() {
      return {
        provide() {
          return Promise.reject(new Error("denied"));
        },
        cancel() {},
      };
    },
    copyText: async () => {
      throw new Error("denied");
    },
  });
  await goLive(failed, "FAIL");
  const failedUrl = "https://s.cympfh.cc/djtube/stream/FAIL";
  assert.equal(failed.ui.painted().face, "on");
  assert.equal(failed.ui.painted().title, "");
  assert.equal(failed.menu.head.textContent, `配信中：${failedUrl}`);
  assert.equal(failed.ui.painted().live, `配信中：${failedUrl}。${LIVE_COPY_FAILED}`);
  assert.equal(failed.sockets[0].sent[0], openedClaim());
  assert.equal(failed.sockets.length, 1);
});

test("the second click stops without copying or asking", async () => {
  const harness = setup();
  await goLive(harness);
  harness.ui.button.writes.length = 0;
  harness.ui.button.click();
  await flush();
  assert.deepEqual(harness.copies, ["https://s.cympfh.cc/djtube/stream/ABCD"]);
  assert.equal(harness.ui.painted().title, LIVE_IDLE);
  assert.equal(harness.menu.head.textContent, "");
  assert.equal(harness.ui.painted().live, LIVE_IDLE);
  assert.equal(harness.ui.painted().face, "off");
  assert.equal(harness.recorders[0].stopped, true);
  assert.equal(harness.sockets[0].closed, 1000);
  assert.equal(harness.bus.context.destinations[0].track.live, false);
  assert.deepEqual(harness.bus.master.connections, [harness.bus.destination]);
  assert.equal(harness.sockets.length, 1);
  harness.ui.button.writes.length = 0;
  harness.sockets[0].onclose?.({ code: 1000 });
  assert.deepEqual(harness.ui.button.writes, []);
});

test("silence keeps a zero-gain source on the stream until the broadcast stops", async () => {
  const harness = setup();
  await goLive(harness);
  const keep = harness.bus.context.keeps[0];
  const dest = harness.bus.context.destinations[0];
  assert.equal(harness.bus.context.keeps.length, 1);
  assert.equal(keep.started, true);
  assert.equal(keep.stopped, false);
  assert.equal(keep.connections.length, 1);
  assert.equal(keep.connections[0].gain.value, 0);
  assert.deepEqual(keep.connections[0].connections, [dest]);
  assert.equal(harness.bus.master.connections.includes(keep.connections[0]), false);
  assert.equal(harness.ui.painted().face, "on");
  assert.equal(harness.ui.painted().title, "");
  assert.equal(harness.menu.head.textContent, "配信中：https://s.cympfh.cc/djtube/stream/ABCD");
  harness.ui.button.click();
  await flush();
  assert.equal(keep.stopped, true);
  assert.equal(harness.ui.painted().title, LIVE_IDLE);
});

const RENDER_TIME0 = 3.2;
const RENDER_WALL0 = 12000;

function renderHarness() {
  let wall = RENDER_WALL0;
  const bus = openBus();
  bus.context.currentTime = RENDER_TIME0;
  const harness = setup({
    bus,
    now() {
      return wall;
    },
  });
  return {
    bus,
    harness,
    setWall(ms) {
      wall = ms;
    },
    blob() {
      harness.recorders[0].emit(new Blob([Uint8Array.of(1)]));
    },
  };
}

function stillLive(harness) {
  assert.equal(harness.ui.painted().face, "on");
  assert.equal(harness.ui.painted().title, "");
  assert.equal(harness.recorders[0].stopped, false);
  assert.equal(harness.sockets[0].closed, null);
}

test("a frozen clock and a 4408 names the output device", async () => {
  const { harness, setWall } = renderHarness();
  await goLive(harness);
  setWall(RENDER_WALL0 + 10000);
  stillLive(harness);
  harness.sockets[0].serverClose(4408);
  await flush();
  assert.equal(harness.ui.painted().title, LIVE_OUTPUT_STALLED);
  assert.equal(harness.ui.painted().live, LIVE_OUTPUT_STALLED);
  assert.equal(harness.ui.painted().face, "off");
  assert.equal(harness.ui.painted().label, LIVE_BUTTON_LABEL);
  assert.equal(harness.menu.head.textContent, "");
  assert.equal(harness.recorders.length, 1);
  assert.equal(harness.sockets.length, 1);
});

test("a clock that stalls mid-stream and a 4408 idle close names the output device", async () => {
  const { bus, harness, setWall, blob } = renderHarness();
  await goLive(harness);
  bus.context.currentTime = RENDER_TIME0 + 12;
  setWall(RENDER_WALL0 + 12000);
  blob();
  stillLive(harness);
  setWall(RENDER_WALL0 + 42000);
  harness.sockets[0].serverClose(4408);
  await flush();
  assert.equal(harness.ui.painted().title, LIVE_OUTPUT_STALLED);
  assert.equal(harness.ui.painted().live, LIVE_OUTPUT_STALLED);
  assert.equal(harness.ui.painted().face, "off");
  assert.equal(harness.recorders[0].stopped, true);
  assert.equal(harness.sockets.length, 1);
});

test("a clock that keeps running keeps the 4408 wording", async () => {
  const { bus, harness, setWall, blob } = renderHarness();
  await goLive(harness);
  bus.context.currentTime = RENDER_TIME0 + 10;
  setWall(RENDER_WALL0 + 10000);
  blob();
  assert.equal(harness.ui.painted().face, "on");
  harness.sockets[0].serverClose(4408);
  await flush();
  assert.equal(harness.ui.painted().title, closeReason(4408));
  assert.equal(harness.ui.painted().live, closeReason(4408));
  assert.equal(harness.ui.painted().face, "off");
  assert.equal(harness.sockets.length, 1);
  assert.equal(harness.recorders.length, 1);
});

test("a late start at 9.9 seconds and a 4408 names the output device", async () => {
  const { bus, harness, setWall, blob } = renderHarness();
  await goLive(harness);
  stillLive(harness);
  bus.context.currentTime = RENDER_TIME0 + 0.1;
  setWall(RENDER_WALL0 + 9900);
  blob();
  harness.sockets[0].serverClose(4408);
  await flush();
  assert.equal(harness.ui.painted().title, LIVE_OUTPUT_STALLED);
  assert.equal(harness.ui.painted().live, LIVE_OUTPUT_STALLED);
  assert.equal(harness.ui.painted().face, "off");
  assert.equal(harness.sockets.length, 1);
});

test("a normal stream shows nothing about the output device", async () => {
  const { bus, harness, setWall, blob } = renderHarness();
  await goLive(harness);
  bus.context.currentTime = RENDER_TIME0 + 8;
  setWall(RENDER_WALL0 + 8000);
  blob();
  assert.equal(harness.ui.painted().face, "on");
  assert.equal(harness.ui.painted().title, "");
  assert.equal(harness.ui.painted().live.includes("配信中"), true);
  assert.equal(harness.ui.painted().live.includes(LIVE_OUTPUT_STALLED), false);
  assert.equal(harness.recorders[0].stopped, false);
  assert.equal(harness.sockets[0].closed, null);
  assert.equal(harness.sockets.length, 1);
});

test("a 4.9 second start gap keeps the 4408 wording", async () => {
  const { harness, setWall, blob } = renderHarness();
  await goLive(harness);
  setWall(RENDER_WALL0 + 4900);
  blob();
  harness.sockets[0].serverClose(4408);
  await flush();
  assert.equal(harness.ui.painted().title, closeReason(4408));
  assert.equal(harness.ui.painted().live, closeReason(4408));
});

test("a 5 second start gap names the output device", async () => {
  const { harness, setWall, blob } = renderHarness();
  await goLive(harness);
  setWall(RENDER_WALL0 + 5000);
  blob();
  harness.sockets[0].serverClose(4408);
  await flush();
  assert.equal(harness.ui.painted().title, LIVE_OUTPUT_STALLED);
  assert.equal(harness.ui.painted().live, LIVE_OUTPUT_STALLED);
});

test("a blob 4.9 seconds ago keeps the 4408 wording", async () => {
  const { bus, harness, setWall, blob } = renderHarness();
  await goLive(harness);
  setWall(RENDER_WALL0 + 5100);
  blob();
  bus.context.currentTime = RENDER_TIME0 + 10;
  setWall(RENDER_WALL0 + 10000);
  harness.sockets[0].serverClose(4408);
  await flush();
  assert.equal(harness.ui.painted().title, closeReason(4408));
  assert.equal(harness.ui.painted().live, closeReason(4408));
});

test("a blob 5 seconds ago names the output device", async () => {
  const { bus, harness, setWall, blob } = renderHarness();
  await goLive(harness);
  setWall(RENDER_WALL0 + 5000);
  blob();
  bus.context.currentTime = RENDER_TIME0 + 10;
  setWall(RENDER_WALL0 + 10000);
  harness.sockets[0].serverClose(4408);
  await flush();
  assert.equal(harness.ui.painted().title, LIVE_OUTPUT_STALLED);
  assert.equal(harness.ui.painted().live, LIVE_OUTPUT_STALLED);
});

test("an empty blob does not count as arrived output", async () => {
  const { bus, harness, setWall, blob } = renderHarness();
  await goLive(harness);
  blob();
  bus.context.currentTime = RENDER_TIME0 + 5;
  setWall(RENDER_WALL0 + 5000);
  harness.recorders[0].emit(new Blob([]));
  harness.sockets[0].serverClose(4408);
  await flush();
  assert.equal(harness.ui.painted().title, LIVE_OUTPUT_STALLED);
  assert.equal(harness.ui.painted().live, LIVE_OUTPUT_STALLED);
});

test("an init 4408 at 15 seconds still names the output device", async () => {
  // A proxied init 4408 arrived at 12.0 s, so a 10 s window would miss it.
  const { harness, setWall, blob } = renderHarness();
  await goLive(harness);
  setWall(RENDER_WALL0 + 15000);
  blob();
  stillLive(harness);
  harness.sockets[0].serverClose(4408);
  await flush();
  assert.equal(harness.ui.painted().title, LIVE_OUTPUT_STALLED);
  assert.equal(harness.ui.painted().live, LIVE_OUTPUT_STALLED);
});

test("a start gap at 20 seconds still names the output device", async () => {
  const { harness, setWall, blob } = renderHarness();
  await goLive(harness);
  setWall(RENDER_WALL0 + 20000);
  blob();
  harness.sockets[0].serverClose(4408);
  await flush();
  assert.equal(harness.ui.painted().title, LIVE_OUTPUT_STALLED);
  assert.equal(harness.ui.painted().live, LIVE_OUTPUT_STALLED);
});

test("a late start at 6 seconds then a network choke keeps the 4408 wording", async () => {
  const { bus, harness, setWall, blob } = renderHarness();
  await goLive(harness);
  setWall(RENDER_WALL0 + 6000);
  stillLive(harness);
  bus.context.currentTime = RENDER_TIME0 + (40.13 - 5.89);
  setWall(RENDER_WALL0 + 40130);
  blob();
  stillLive(harness);
  harness.sockets[0].serverClose(4408);
  await flush();
  assert.equal(harness.ui.painted().title, closeReason(4408));
  assert.equal(harness.ui.painted().live, closeReason(4408));
  assert.equal(harness.ui.painted().face, "off");
});

test("a temporary dropout then a network choke keeps the 4408 wording", async () => {
  const { bus, harness, setWall, blob } = renderHarness();
  await goLive(harness);
  bus.context.currentTime = RENDER_TIME0 + 4;
  setWall(RENDER_WALL0 + 4000);
  blob();
  stillLive(harness);
  setWall(RENDER_WALL0 + 10000);
  stillLive(harness);
  bus.context.currentTime = RENDER_TIME0 + (40.65 - 7.27);
  setWall(RENDER_WALL0 + 40650);
  blob();
  stillLive(harness);
  harness.sockets[0].serverClose(4408);
  await flush();
  assert.equal(harness.ui.painted().title, closeReason(4408));
  assert.equal(harness.ui.painted().live, closeReason(4408));
  assert.equal(harness.ui.painted().face, "off");
});

test("a 4408 without a numeric currentTime keeps the 4408 wording", async () => {
  const { bus, harness, setWall } = renderHarness();
  await goLive(harness);
  bus.context.currentTime = undefined;
  setWall(RENDER_WALL0 + 10000);
  harness.sockets[0].serverClose(4408);
  await flush();
  assert.equal(harness.ui.painted().title, closeReason(4408));
  assert.equal(harness.ui.painted().live, closeReason(4408));
  assert.equal(harness.ui.painted().face, "off");
});

test("a non-4408 close does not name the output device", async () => {
  const { harness, setWall } = renderHarness();
  await goLive(harness);
  setWall(RENDER_WALL0 + 10000);
  harness.sockets[0].serverClose(4409);
  await flush();
  assert.equal(harness.ui.painted().title, closeReason(4409));
  assert.equal(harness.ui.painted().live, closeReason(4409));
  assert.equal(harness.ui.painted().face, "off");
  assert.equal(harness.ui.painted().title.includes(LIVE_OUTPUT_STALLED), false);
});

test("a context without a constant source still publishes", async () => {
  const bus = openBus();
  delete bus.context.createConstantSource;
  delete bus.context.createGain;
  const harness = setup({ bus });
  await goLive(harness);
  assert.equal(harness.ui.painted().face, "on");
  assert.equal(bus.context.keeps.length, 0);
  assert.equal(harness.sockets[0].sent[0], openedClaim());
});

test("stopping before the socket opens closes it and sends nothing after it opens", async () => {
  const harness = setup({
    makeSocket(url) {
      return {
        url,
        readyState: 0,
        sent: [],
        closed: null,
        onmessage: null,
        onclose: null,
        send(data) {
          this.sent.push(data);
        },
        close(code) {
          this.closed = code ?? 1000;
          this.readyState = 2;
        },
      };
    },
  });
  harness.ui.button.click();
  await flush();
  assert.equal(harness.sockets[0].readyState, 0);
  assert.equal(harness.sockets[0].closed, null);
  harness.ui.button.click();
  await flush();
  assert.equal(harness.sockets[0].closed, 1000);
  assert.equal(harness.ui.painted().title, LIVE_IDLE);
  assert.equal(harness.bus.context.destinations[0].track.live, false);
  harness.sockets[0].readyState = 1;
  harness.sockets[0].onmessage?.({ data: idMessage() });
  await flush();
  assert.deepEqual(harness.sockets[0].sent, []);
  assert.equal(harness.recorders.length, 0);
  assert.equal(harness.sockets.length, 1);
});

test("starting again reuses the id and copies the same listener URL", async () => {
  const harness = setup();
  await goLive(harness, "ABCD");
  const url = "https://s.cympfh.cc/djtube/stream/ABCD";
  harness.ui.button.click();
  await flush();
  assert.equal(harness.ui.painted().title, LIVE_IDLE);
  harness.ui.button.click();
  await flush();
  assert.equal(harness.sockets.length, 2);
  assert.equal(harness.sockets[0].url, "wss://s.cympfh.cc/djtube/api/live/publish");
  assert.equal(harness.sockets[1].url, PUBLISH_URL);
  assert.equal(harness.sockets[1].sent[0], claimMessage("ABCD", LIVE_TOKEN, 2));
  harness.sockets[1].receive(idMessage());
  await flush();
  assert.equal(harness.ui.painted().title, "");
  assert.equal(harness.menu.head.textContent, `配信中：${url}`);
  assert.equal(harness.ui.painted().live, `配信中：${url}`);
  assert.equal(harness.ui.painted().face, "on");
  assert.deepEqual(harness.copies, [url, url]);
});

test("an immediate restart reuses the id and token without waiting for the old close", async () => {
  let releaseClose = null;
  const harness = setup({
    makeSocket(url) {
      return {
        url,
        readyState: 1,
        sent: [],
        closed: null,
        onmessage: null,
        onclose: null,
        send(data) {
          this.sent.push(data);
        },
        receive(text) {
          this.onmessage?.({ data: text });
        },
        close(code) {
          this.closed = code ?? 1000;
          this.readyState = 2;
          releaseClose = () => {
            if (this.readyState === 3) return;
            this.readyState = 3;
            this.onclose?.({ code: this.closed });
          };
        },
        serverClose(code) {
          this.readyState = 3;
          this.onclose?.({ code });
        },
      };
    },
  });
  await goLive(harness, "ABCD");
  harness.ui.button.click();
  await flush();
  assert.equal(harness.ui.painted().title, LIVE_IDLE);
  assert.equal(harness.sockets[0].readyState, 2);
  harness.ui.button.click();
  await flush();
  assert.equal(harness.sockets.length, 2);
  assert.equal(harness.sockets[1].url, PUBLISH_URL);
  assert.equal(harness.sockets[1].closed, null);
  assert.equal(typeof releaseClose, "function");
  assert.equal(harness.sockets[1].sent[0], claimMessage("ABCD", LIVE_TOKEN, 2));
  harness.sockets[1].receive(idMessage());
  await flush();
  assert.equal(harness.ui.painted().face, "on");
  assert.equal(harness.ui.painted().title, "");
  assert.equal(harness.menu.head.textContent, "配信中：https://s.cympfh.cc/djtube/stream/ABCD");
  releaseClose();
  await flush();
  assert.equal(harness.ui.painted().face, "on");
  assert.equal(harness.sockets.length, 2);
});

test("a 4409 drops the id and the next click takes a new one", async () => {
  let taken = false;
  const harness = setup({
    makeSocket(url) {
      const socket = fakeSocket(url);
      if (taken) queueMicrotask(() => socket.serverClose(4409));
      return socket;
    },
  });
  await goLive(harness, "ABCD");
  harness.ui.button.click();
  await flush();
  taken = true;
  harness.ui.button.click();
  await flush();
  assert.equal(harness.sockets.length, 2);
  assert.equal(harness.sockets[1].url, PUBLISH_URL);
  assert.equal(JSON.parse(harness.sockets[1].sent[0]).id, "ABCD");
  assert.equal(JSON.parse(harness.sockets[1].sent[0]).token, LIVE_TOKEN);
  assert.equal(harness.ui.painted().face, "off");
  assert.equal(harness.ui.painted().title, LIVE_TAKEN);
  assert.equal(harness.ui.painted().live, LIVE_TAKEN);
  assert.equal(harness.recorders.length, 1);
  assert.deepEqual(harness.copies, ["https://s.cympfh.cc/djtube/stream/ABCD"]);
  await flush();
  assert.equal(harness.sockets.length, 2);
  taken = false;
  harness.ui.button.click();
  await flush();
  assert.equal(harness.sockets.length, 3);
  assert.equal(harness.sockets[2].url, "wss://s.cympfh.cc/djtube/api/live/publish");
  harness.sockets[2].receive(idMessage("EFGH", "next-token"));
  await flush();
  assert.equal(harness.ui.painted().title, "");
  assert.equal(harness.menu.head.textContent, "配信中：https://s.cympfh.cc/djtube/stream/EFGH");
  assert.equal(harness.ui.painted().face, "on");
  assert.equal(JSON.parse(harness.sockets[2].sent[0]).seq, 3);
});

test("a new token from the server is what the next claim sends", async () => {
  const harness = setup();
  await goLive(harness, "ABCD", "first-token");
  harness.ui.button.click();
  await flush();
  harness.ui.button.click();
  await flush();
  assert.equal(JSON.parse(harness.sockets[1].sent[0]).token, "first-token");
  harness.sockets[1].receive(idMessage("ABCD", "second-token"));
  await flush();
  harness.ui.button.click();
  await flush();
  harness.ui.button.click();
  await flush();
  const claim = JSON.parse(harness.sockets[2].sent[0]);
  assert.equal(claim.id, "ABCD");
  assert.equal(claim.token, "second-token");
  assert.equal(claim.seq, 3);
  assert.equal(harness.sockets[2].url.includes("?"), false);
});

test("cancelling before the id arrives does not copy or open another socket", async () => {
  const harness = setup();
  const pending = harness.ui.button.click();
  await flush();
  harness.ui.button.click();
  await pending;
  await flush();
  assert.equal(harness.copies.length, 0);
  assert.equal(harness.recorders.length, 0);
  assert.equal(harness.sockets.length, 1);
  assert.equal(harness.sockets[0].closed, 1000);
  assert.equal(harness.ui.painted().title, LIVE_IDLE);
  harness.sockets[0].receive(idMessage());
  await flush();
  assert.equal(harness.recorders.length, 0);
  assert.equal(harness.copies.length, 0);
  assert.equal(harness.sockets.length, 1);
});

test("a suspended context is resumed on the click before the socket opens", async () => {
  const order = [];
  const bus = openBus("suspended");
  let release = null;
  bus.context.resume = () => {
    order.push("resume");
    return new Promise((resolve) => {
      release = () => {
        bus.context.state = "running";
        resolve();
      };
    });
  };
  const harness = setup({ bus, onSocket: () => order.push("socket") });
  const pending = harness.ui.button.click();
  await flush();
  assert.deepEqual(order, ["resume"]);
  assert.equal(harness.sockets.length, 0);
  assert.equal(bus.context.destinations.length, 0);
  release();
  await pending;
  await flush();
  assert.deepEqual(order, ["resume", "socket"]);
  assert.equal(bus.context.state, "running");
  assert.equal(harness.sockets.length, 1);
});

test("stopping while the context is resuming does not open a socket", async () => {
  const bus = openBus("suspended");
  let release = null;
  bus.context.resume = () =>
    new Promise((resolve) => {
      release = () => {
        bus.context.state = "running";
        resolve();
      };
    });
  const harness = setup({ bus });
  const pending = harness.ui.button.click();
  await flush();
  harness.ui.button.click();
  release();
  await pending;
  await flush();
  assert.equal(harness.sockets.length, 0);
  assert.equal(harness.copies.length, 0);
  assert.equal(harness.ui.painted().title, LIVE_IDLE);
  assert.equal(harness.ui.painted().face, "off");
});

test("a server close stops the broadcast, shows the reason, and does not reconnect", async () => {
  const cases = [
    [4429, "配信の上限に達しました"],
    [4408, "音声が届かなくなったため、配信を止めました"],
    [4409, LIVE_TAKEN],
    [1003, "配信の形式が受け付けられませんでした"],
    [1008, "送信の上限に達しました"],
    [1009, "データが大きすぎるため、配信を止めました"],
    [1006, LIVE_DROPPED],
  ];
  for (const [code, text] of cases) {
    const harness = setup();
    await goLive(harness);
    harness.ui.button.writes.length = 0;
    harness.sockets[0].serverClose(code);
    await flush();
    assert.equal(harness.ui.painted().title, text);
    assert.equal(harness.ui.painted().live, text);
    assert.equal(harness.ui.painted().face, "off");
    assert.equal(harness.recorders[0].stopped, true);
    assert.deepEqual(harness.ui.button.writes, ["title", "status", "data-live"]);
    harness.ui.button.writes.length = 0;
    harness.sockets[0].onclose?.({ code });
    assert.deepEqual(harness.ui.button.writes, []);
    await flush();
    assert.equal(harness.sockets.length, 1);
    assert.equal(harness.copies.length, 1);
    if (code === 4409 || code === 1006) {
      harness.ui.button.click();
      await flush();
      const next = JSON.parse(harness.sockets.at(-1).sent[0]);
      assert.equal(harness.sockets.at(-1).url, PUBLISH_URL);
      if (code === 4409) {
        assert.equal(next.id, undefined);
        assert.equal(next.token, undefined);
      } else {
        assert.equal(next.id, "ABCD");
        assert.equal(next.token, LIVE_TOKEN);
      }
      harness.ui.button.click();
      await flush();
    }
  }
});

test("a backed-up socket stops the broadcast instead of sending more", async () => {
  const harness = setup();
  await goLive(harness);
  harness.sockets[0].bufferedAmount = LIVE_BUFFER_LIMIT;
  harness.recorders[0].emit(new Blob([Uint8Array.of(1)]));
  await flush();
  assert.equal(harness.sockets[0].sent.length, 2);
  assert.equal(harness.ui.painted().face, "on");

  harness.sockets[0].bufferedAmount = LIVE_BUFFER_LIMIT + 1;
  harness.ui.button.writes.length = 0;
  harness.recorders[0].emit(new Blob([Uint8Array.of(2)]));
  await flush();
  assert.equal(harness.sockets[0].sent.length, 2);
  assert.equal(harness.ui.painted().title, LIVE_SEND_BACKLOG);
  assert.equal(harness.ui.painted().live, LIVE_SEND_BACKLOG);
  assert.equal(harness.ui.painted().face, "off");
  assert.equal(harness.recorders[0].stopped, true);
  assert.equal(harness.sockets.length, 1);
  assert.deepEqual(harness.ui.button.writes, ["title", "status", "data-live"]);
});

test("a deck that missed the master is named on the live hover", async () => {
  const gap = { A: true, B: false };
  const harness = setup({
    audios: {
      A: { offMaster: () => gap.A },
      B: { offMaster: () => gap.B },
    },
  });
  await goLive(harness);
  const url = "https://s.cympfh.cc/djtube/stream/ABCD";
  const noted = `${`配信中：${url}`}。デッキ A はマスターに入っていないため、配信には入りません`;
  assert.equal(harness.ui.painted().title, "");
  assert.equal(harness.menu.head.textContent, noted);
  assert.equal(harness.ui.painted().live, noted);
  harness.ui.button.writes.length = 0;
  harness.control.refresh();
  assert.deepEqual(harness.ui.button.writes, []);
  gap.A = false;
  harness.control.refresh();
  assert.equal(harness.ui.painted().title, "");
  assert.equal(harness.menu.head.textContent, `配信中：${url}`);
  assert.equal(harness.ui.painted().live, `配信中：${url}`);
  harness.ui.button.writes.length = 0;
  harness.control.refresh();
  assert.deepEqual(harness.ui.button.writes, []);
});

test("an unsupported browser keeps the hover and does not open a socket", async () => {
  const harness = setup({ supported: false });
  assert.equal(harness.ui.painted().title, LIVE_UNSUPPORTED);
  assert.equal(harness.ui.painted().live, LIVE_UNSUPPORTED);
  assert.equal(harness.ui.painted().face, "off");
  harness.ui.button.writes.length = 0;
  harness.ui.button.click();
  await flush();
  assert.equal(harness.sockets.length, 0);
  assert.equal(harness.copies.length, 0);
  assert.deepEqual(harness.ui.button.writes, []);
  assert.equal(harness.ui.painted().title, LIVE_UNSUPPORTED);
});

test("the live menu copies the audio and video URLs without stopping", async () => {
  const harness = setup();
  await harness.menu.audio.click();
  await flush();
  assert.deepEqual(harness.copies, []);
  assert.equal(harness.menu.result.textContent, "");

  await goLive(harness);
  const url = "https://s.cympfh.cc/djtube/stream/ABCD";
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  harness.menu.anchor.emit("pointerenter", { pointerType: "mouse" });
  assert.equal(harness.menu.anchor.dataset.menu, "open");

  await harness.menu.audio.click();
  await flush();
  assert.deepEqual(harness.copies, [url, url]);
  assert.equal(harness.menu.result.textContent, LIVE_COPIED);
  assert.equal(harness.menu.result.dataset.result, "ok");
  assert.equal(harness.ui.painted().face, "on");
  assert.equal(harness.sockets[0].closed, null);

  await harness.menu.video.click();
  await flush();
  assert.equal(harness.copies[2], `${url}?thumbnail=1`);
  assert.equal(harness.menu.result.textContent, LIVE_COPIED);
  assert.equal(harness.ui.painted().face, "on");
  assert.equal(harness.sockets.length, 1);
});

test("a failed menu copy stays on the air and says so", async () => {
  let fail = false;
  const harness = setup({
    async copyText(url) {
      if (fail) throw new Error("denied");
      harness.copies.push(url);
    },
  });
  await goLive(harness);
  fail = true;
  await harness.menu.audio.click();
  await flush();
  assert.equal(harness.menu.result.textContent, LIVE_MENU_COPY_FAILED);
  assert.equal(harness.menu.result.dataset.result, "fail");
  assert.equal(harness.ui.painted().face, "on");
  assert.equal(harness.copies.length, 1);
});

test("the live menu opens from the pointer and closes after it has left", async () => {
  const clock = fakeClock();
  const harness = setup({ setTimer: clock.schedule, clearTimer: clock.clear });
  await goLive(harness);
  harness.menu.anchor.emit("pointerenter", { pointerType: "touch" });
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  harness.menu.anchor.emit("pointerenter", { pointerType: "mouse" });
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  harness.menu.anchor.emit("pointerleave");
  clock.advance(LIVE_MENU_CLOSE_MS - 1);
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  harness.menu.anchor.emit("pointerenter", { pointerType: "mouse" });
  clock.advance(LIVE_MENU_CLOSE_MS);
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  harness.menu.anchor.emit("pointerleave");
  clock.advance(LIVE_MENU_CLOSE_MS);
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
});

test("a mouse click does not open the menu, and a keyboard focus does", async () => {
  const harness = setup();
  await goLive(harness);
  harness.ui.button.emit("pointerdown", { pointerType: "mouse" });
  harness.menu.anchor.emit("focusin", { target: harness.ui.button });
  harness.ui.button.emit("pointerup");
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  harness.ui.button.focusVisible = true;
  harness.menu.anchor.emit("focusin", { target: harness.ui.button });
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  harness.menu.anchor.emit("focusout", { relatedTarget: { inside: true } });
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  harness.menu.anchor.emit("focusout", { relatedTarget: { inside: false } });
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
});

test("escape closes the live menu and does not reopen from the returned focus", async () => {
  const harness = setup();
  await goLive(harness);
  harness.ui.button.focus = () => {
    harness.ui.button.focused = true;
    harness.ui.button.focusVisible = true;
    harness.menu.anchor.emit("focusin", { target: harness.ui.button });
  };
  harness.ui.button.focusVisible = true;
  harness.menu.anchor.emit("focusin", { target: harness.ui.button });
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  const event = {
    key: "Escape",
    prevented: false,
    stopped: false,
    preventDefault() {
      this.prevented = true;
    },
    stopPropagation() {
      this.stopped = true;
    },
  };
  harness.menu.anchor.emit("keydown", event);
  assert.equal(event.prevented, true);
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  assert.equal(harness.ui.button.focused, true);
  assert.equal(harness.ui.painted().face, "on");
});

test("a long press opens the menu and the following click does not stop", async () => {
  const clock = fakeClock();
  const harness = setup({ setTimer: clock.schedule, clearTimer: clock.clear });
  await goLive(harness);
  harness.ui.button.emit("pointerdown", { pointerType: "touch" });
  clock.advance(LIVE_MENU_HOLD_MS - 1);
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  clock.advance(1);
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  harness.ui.button.click();
  await flush();
  assert.equal(harness.ui.painted().face, "on");
  assert.equal(harness.sockets[0].closed, null);
});

test("lifting a touch long-press sends pointerleave and leaves the menu open", async () => {
  const clock = fakeClock();
  const root = {
    listener: null,
    addEventListener(_type, fn) {
      this.listener = fn;
    },
    emit(event) {
      this.listener?.(event);
    },
  };
  const harness = setup({ setTimer: clock.schedule, clearTimer: clock.clear, root });
  await goLive(harness);
  harness.ui.button.emit("pointerdown", { pointerType: "touch" });
  clock.advance(LIVE_MENU_HOLD_MS);
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  harness.ui.button.emit("pointerup");
  harness.menu.anchor.emit("pointerleave", { pointerType: "touch" });
  clock.advance(LIVE_MENU_CLOSE_MS);
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  root.emit({ target: { inside: false } });
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
});

test("a long-press that ends without a click does not swallow the next tap", async () => {
  const clock = fakeClock();
  const harness = setup({ setTimer: clock.schedule, clearTimer: clock.clear });
  await goLive(harness);
  harness.ui.button.emit("pointerdown", { pointerType: "touch" });
  clock.advance(LIVE_MENU_HOLD_MS);
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  harness.ui.button.emit("pointercancel");
  harness.ui.button.click();
  await flush();
  assert.equal(harness.ui.painted().face, "off");
  assert.equal(harness.sockets[0].closed, 1000);

  const early = setup({ setTimer: clock.schedule, clearTimer: clock.clear });
  await goLive(early);
  early.ui.button.emit("pointerdown", { pointerType: "touch" });
  clock.advance(LIVE_MENU_HOLD_MS - 1);
  early.ui.button.emit("pointercancel");
  clock.advance(1);
  early.ui.button.click();
  await flush();
  assert.equal(early.ui.painted().face, "off");
  assert.equal(early.sockets[0].closed, 1000);
});

test("pointerdown clears a leftover long-press so the next click stops", async () => {
  const clock = fakeClock();
  const harness = setup({ setTimer: clock.schedule, clearTimer: clock.clear });
  await goLive(harness);
  harness.ui.button.emit("pointerdown", { pointerType: "touch" });
  clock.advance(LIVE_MENU_HOLD_MS);
  assert.equal(harness.ui.painted().face, "on");
  harness.ui.button.emit("pointerdown", { pointerType: "touch" });
  harness.ui.button.click();
  await flush();
  assert.equal(harness.ui.painted().face, "off");
  assert.equal(harness.sockets[0].closed, 1000);
});

test("a pointer already on the icon opens the menu when the broadcast starts", async () => {
  const harness = setup();
  harness.menu.anchor.emit("pointerenter", { pointerType: "mouse" });
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  harness.menu.anchor.hover = true;
  await goLive(harness);
  assert.equal(harness.menu.anchor.dataset.menu, "open");
});

test("a pointermove after the start click opens the menu for a mouse", async () => {
  const harness = setup();
  harness.menu.anchor.emit("pointerenter", { pointerType: "mouse" });
  await goLive(harness);
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  harness.menu.anchor.emit("pointermove", { pointerType: "touch" });
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  harness.menu.anchor.emit("pointermove", { pointerType: "pen" });
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  harness.menu.anchor.emit("pointermove", { pointerType: "mouse" });
  assert.equal(harness.menu.anchor.dataset.menu, "open");
});

test("stopping closes the menu and drops a pending long-press", async () => {
  const clock = fakeClock();
  const harness = setup({ setTimer: clock.schedule, clearTimer: clock.clear });
  await goLive(harness);
  harness.menu.anchor.emit("pointerenter", { pointerType: "mouse" });
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  harness.ui.button.emit("pointerdown", { pointerType: "touch" });
  harness.ui.button.click();
  await flush();
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  assert.equal(harness.ui.painted().title, LIVE_IDLE);
  clock.advance(LIVE_MENU_HOLD_MS);
  harness.ui.button.click();
  await flush();
  assert.equal(harness.sockets.length, 2);
  harness.sockets[1].receive(idMessage());
  await flush();
  assert.equal(harness.ui.painted().face, "on");
});

test("a second copy extends the note, and a touch copy closes the menu when it fades", async () => {
  const clock = fakeClock();
  const harness = setup({ setTimer: clock.schedule, clearTimer: clock.clear });
  await goLive(harness);
  harness.menu.anchor.emit("pointerenter", { pointerType: "mouse" });
  await harness.menu.audio.click();
  await flush();
  assert.equal(harness.menu.result.textContent, LIVE_COPIED);
  clock.advance(LIVE_MENU_NOTE_MS - 1);
  assert.equal(harness.menu.result.textContent, LIVE_COPIED);
  await harness.menu.audio.click();
  await flush();
  clock.advance(1);
  assert.equal(harness.menu.result.textContent, LIVE_COPIED);
  clock.advance(LIVE_MENU_NOTE_MS - 2);
  assert.equal(harness.menu.result.textContent, LIVE_COPIED);
  clock.advance(1);
  assert.equal(harness.menu.result.textContent, "");
  assert.equal(harness.menu.result.dataset.result, undefined);
  assert.equal(harness.menu.anchor.dataset.menu, "open");

  harness.ui.button.emit("pointerdown", { pointerType: "touch" });
  clock.advance(LIVE_MENU_HOLD_MS);
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  await harness.menu.audio.click();
  await flush();
  assert.equal(harness.menu.result.textContent, LIVE_COPIED);
  clock.advance(LIVE_MENU_NOTE_MS);
  assert.equal(harness.menu.result.textContent, "");
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
});

test("the live icon swallows the long-press context menu", () => {
  const harness = setup();
  const event = {
    prevented: false,
    preventDefault() {
      this.prevented = true;
    },
  };
  harness.ui.button.emit("contextmenu", event);
  assert.equal(event.prevented, true);
});

test("a tap outside closes the menu and stopping clears the copy note", async () => {
  const clock = fakeClock();
  const root = {
    listener: null,
    addEventListener(_type, fn) {
      this.listener = fn;
    },
    emit(event) {
      this.listener?.(event);
    },
  };
  const harness = setup({ setTimer: clock.schedule, clearTimer: clock.clear, root });
  await goLive(harness);
  harness.menu.anchor.emit("pointerenter", { pointerType: "mouse" });
  root.emit({ target: { inside: true } });
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  root.emit({ target: { inside: false } });
  assert.equal(harness.menu.anchor.dataset.menu, undefined);

  await harness.menu.audio.click();
  await flush();
  assert.equal(harness.menu.result.textContent, LIVE_COPIED);
  clock.advance(LIVE_MENU_NOTE_MS);
  assert.equal(harness.menu.result.textContent, "");

  await harness.menu.audio.click();
  await flush();
  assert.equal(harness.menu.result.textContent, LIVE_COPIED);
  harness.ui.button.click();
  await flush();
  assert.equal(harness.menu.result.textContent, "");
  assert.equal(harness.ui.painted().title, LIVE_IDLE);
});

function outsidePointer() {
  return {
    target: { inside: false },
    prevented: false,
    stopped: false,
    preventDefault() {
      this.prevented = true;
    },
    stopPropagation() {
      this.stopped = true;
    },
  };
}

function rootHarness(clock) {
  const root = {
    listener: null,
    capture: false,
    addEventListener(_type, fn, options) {
      this.listener = fn;
      this.capture = options === true || options?.capture === true;
    },
    emit(event) {
      this.listener?.(event);
    },
  };
  const harness = setup({ setTimer: clock.schedule, clearTimer: clock.clear, root });
  return { root, harness };
}

test("pressing the url text does not close the menu", async () => {
  const { root, harness } = rootHarness(fakeClock());
  await goLive(harness);
  harness.menu.anchor.emit("pointerenter", { pointerType: "mouse" });
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  const head = harness.menu.head.textContent;
  root.emit({ target: { inside: true } });
  harness.menu.anchor.emit("focusout", { relatedTarget: null });
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  assert.equal(harness.menu.head.textContent, head);
  assert.equal(head.includes("配信中："), true);
});

test("closing clears the copy note so a reopened menu stays up", async () => {
  const clock = fakeClock();
  const { root, harness } = rootHarness(clock);
  await goLive(harness);
  harness.ui.button.emit("pointerdown", { pointerType: "touch" });
  clock.advance(LIVE_MENU_HOLD_MS);
  await harness.menu.audio.click();
  await flush();
  assert.equal(harness.menu.result.textContent, LIVE_COPIED);
  root.emit(outsidePointer());
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  assert.equal(harness.menu.result.textContent, "");
  harness.ui.button.emit("pointerdown", { pointerType: "touch" });
  clock.advance(LIVE_MENU_HOLD_MS);
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  assert.equal(harness.menu.result.textContent, "");
  clock.advance(LIVE_MENU_NOTE_MS);
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  assert.equal(harness.menu.result.textContent, "");
});

test("a mouse after a touch open keeps the menu up, and a mouse hold still stops", async () => {
  const clock = fakeClock();
  const harness = setup({ setTimer: clock.schedule, clearTimer: clock.clear });
  await goLive(harness);
  harness.ui.button.emit("pointerdown", { pointerType: "touch" });
  clock.advance(LIVE_MENU_HOLD_MS);
  harness.menu.anchor.emit("pointerdown", { pointerType: "mouse" });
  await harness.menu.audio.click();
  await flush();
  clock.advance(LIVE_MENU_NOTE_MS);
  assert.equal(harness.menu.result.textContent, "");
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  assert.equal(harness.ui.painted().face, "on");

  harness.ui.button.emit("pointerdown", { pointerType: "mouse" });
  clock.advance(700);
  harness.ui.button.click();
  await flush();
  assert.equal(harness.ui.painted().face, "off");
  assert.equal(harness.sockets[0].closed, 1000);
});

test("an outside tap is caught on capture and is not prevented", async () => {
  const clock = fakeClock();
  const { root, harness } = rootHarness(clock);
  await goLive(harness);
  assert.equal(root.capture, true);
  harness.ui.button.emit("pointerdown", { pointerType: "touch" });
  clock.advance(LIVE_MENU_HOLD_MS);
  const tap = outsidePointer();
  tap.stopPropagation();
  root.emit(tap);
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  assert.equal(tap.prevented, false);
  assert.equal(harness.ui.painted().face, "on");
  const again = outsidePointer();
  root.emit(again);
  assert.equal(again.prevented, false);
  assert.equal(again.stopped, false);
});

test("a touch tap that leaves :hover does not open the menu when the broadcast starts", async () => {
  const clock = fakeClock();
  const harness = setup({ setTimer: clock.schedule, clearTimer: clock.clear });
  harness.menu.anchor.hover = true;
  harness.ui.button.emit("pointerdown", { pointerType: "touch" });
  harness.ui.button.emit("pointerup");
  await goLive(harness);
  clock.advance(60);
  harness.menu.anchor.emit("focusin", { target: harness.ui.button });
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  assert.equal(harness.ui.painted().face, "on");
});

test("enter from the keyboard opens the menu when the broadcast starts", async () => {
  const harness = setup();
  harness.ui.button.focusVisible = true;
  harness.ui.button.emit("keydown", { key: "Enter" });
  await goLive(harness);
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  assert.equal(harness.ui.painted().face, "on");
});

test("escape stays closed until the pointer leaves and comes back", async () => {
  const harness = setup();
  await goLive(harness);
  harness.menu.anchor.hover = true;
  harness.ui.button.hover = true;
  harness.menu.anchor.emit("pointerenter", { pointerType: "mouse" });
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  harness.ui.button.focus = () => {
    harness.ui.button.focused = true;
    harness.ui.button.focusVisible = true;
    harness.menu.anchor.emit("focusin", { target: harness.ui.button });
  };
  harness.menu.anchor.emit("keydown", {
    key: "Escape",
    preventDefault() {},
    stopPropagation() {},
  });
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  harness.menu.anchor.emit("pointermove", { pointerType: "mouse" });
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  harness.control.refresh();
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  harness.menu.anchor.emit("pointerleave", { pointerType: "mouse" });
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  harness.menu.anchor.emit("pointerenter", { pointerType: "mouse" });
  assert.equal(harness.menu.anchor.dataset.menu, "open");
});

test("a socket drop during a long-press does not start another broadcast on the lift click", async () => {
  const clock = fakeClock();
  const during = setup({ setTimer: clock.schedule, clearTimer: clock.clear });
  await goLive(during);
  during.ui.button.emit("pointerdown", { pointerType: "touch" });
  clock.advance(LIVE_MENU_HOLD_MS - 1);
  during.sockets[0].serverClose(1006);
  await flush();
  during.ui.button.emit("pointerup");
  during.ui.button.click();
  await flush();
  assert.equal(during.sockets.length, 1);
  assert.equal(during.ui.painted().face, "off");

  const after = setup({ setTimer: clock.schedule, clearTimer: clock.clear });
  await goLive(after);
  after.ui.button.emit("pointerdown", { pointerType: "touch" });
  clock.advance(LIVE_MENU_HOLD_MS);
  assert.equal(after.menu.anchor.dataset.menu, "open");
  after.sockets[0].serverClose(1006);
  await flush();
  after.ui.button.emit("pointerup");
  after.ui.button.click();
  await flush();
  assert.equal(after.sockets.length, 1);
  assert.equal(after.menu.anchor.dataset.menu, undefined);
  assert.equal(after.ui.painted().face, "off");
});

test("a mouse outside tap closes the menu and is not prevented", async () => {
  const { root, harness } = rootHarness(fakeClock());
  await goLive(harness);
  harness.menu.anchor.emit("pointerenter", { pointerType: "mouse" });
  const mouse = outsidePointer();
  root.emit(mouse);
  assert.equal(mouse.prevented, false);
  assert.equal(mouse.stopped, false);
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  assert.equal(harness.ui.painted().face, "on");
});

test("a mouse pointerenter after a touch open keeps the menu when the note fades", async () => {
  const clock = fakeClock();
  const harness = setup({ setTimer: clock.schedule, clearTimer: clock.clear });
  await goLive(harness);
  harness.ui.button.emit("pointerdown", { pointerType: "touch" });
  clock.advance(LIVE_MENU_HOLD_MS);
  await harness.menu.audio.click();
  await flush();
  harness.menu.anchor.emit("pointerenter", { pointerType: "mouse", movementX: 1, movementY: 0 });
  clock.advance(LIVE_MENU_NOTE_MS);
  assert.equal(harness.menu.result.textContent, "");
  assert.equal(harness.menu.anchor.dataset.menu, "open");
});

test("a mouse pointermove after a touch open keeps the menu when the note fades", async () => {
  const clock = fakeClock();
  const harness = setup({ setTimer: clock.schedule, clearTimer: clock.clear });
  await goLive(harness);
  harness.ui.button.emit("pointerdown", { pointerType: "touch" });
  clock.advance(LIVE_MENU_HOLD_MS);
  await harness.menu.audio.click();
  await flush();
  harness.menu.anchor.emit("pointermove", { pointerType: "mouse", movementX: 0, movementY: 2 });
  clock.advance(LIVE_MENU_NOTE_MS);
  assert.equal(harness.menu.result.textContent, "");
  assert.equal(harness.menu.anchor.dataset.menu, "open");
});

test("escape with the pointer outside does not block the next hover", async () => {
  const harness = setup();
  await goLive(harness);
  harness.ui.button.focusVisible = true;
  harness.menu.anchor.emit("focusin", { target: harness.ui.button });
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  harness.ui.button.focus = () => {
    harness.ui.button.focused = true;
  };
  harness.menu.anchor.emit("keydown", {
    key: "Escape",
    preventDefault() {},
    stopPropagation() {},
  });
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  harness.ui.button.focusVisible = false;
  harness.menu.anchor.emit("focusout", { relatedTarget: { inside: false } });
  harness.menu.anchor.emit("pointerenter", { pointerType: "mouse" });
  assert.equal(harness.menu.anchor.dataset.menu, "open");
});

test("escape while the icon is focused opens again when focus returns", async () => {
  const doc = { activeElement: null };
  const harness = setup({ document: doc });
  await goLive(harness);
  harness.ui.button.focusVisible = true;
  doc.activeElement = harness.ui.button;
  harness.menu.anchor.emit("focusin", { target: harness.ui.button });
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  harness.menu.anchor.emit("keydown", {
    key: "Escape",
    preventDefault() {},
    stopPropagation() {},
  });
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  assert.equal(harness.ui.button.focused, true);
  harness.ui.button.focusVisible = false;
  harness.menu.anchor.emit("focusout", { relatedTarget: { inside: false } });
  harness.ui.button.focusVisible = true;
  harness.menu.anchor.emit("focusin", { target: harness.ui.button });
  assert.equal(harness.menu.anchor.dataset.menu, "open");
});

test("a pen already on the icon does not open the menu when the broadcast starts", async () => {
  const harness = setup();
  harness.menu.anchor.hover = true;
  harness.ui.button.emit("pointerdown", { pointerType: "pen" });
  harness.ui.button.emit("pointerup");
  await goLive(harness);
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  assert.equal(harness.ui.painted().face, "on");
});

test("a pen leave then a still mouse leaves the menu open", async () => {
  const clock = fakeClock();
  const harness = setup({ setTimer: clock.schedule, clearTimer: clock.clear });
  await goLive(harness);
  harness.ui.button.emit("pointerdown", { pointerType: "pen" });
  clock.advance(LIVE_MENU_HOLD_MS);
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  await harness.menu.audio.click();
  await flush();
  harness.menu.anchor.emit("pointerdown", { pointerType: "mouse", movementX: 0, movementY: 0 });
  harness.menu.anchor.emit("pointerleave", { pointerType: "pen" });
  harness.menu.anchor.emit("pointerenter", { pointerType: "mouse", movementX: 0, movementY: 0 });
  harness.menu.anchor.emit("pointermove", { pointerType: "mouse", movementX: 0, movementY: 0 });
  harness.menu.anchor.emit("pointerleave", { pointerType: "mouse", movementX: 0, movementY: 0 });
  clock.advance(LIVE_MENU_CLOSE_MS);
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  assert.equal(harness.ui.painted().face, "on");
});

test("a pen dragged off the icon before the hold does not open the menu", async () => {
  const clock = fakeClock();
  const harness = setup({ setTimer: clock.schedule, clearTimer: clock.clear });
  await goLive(harness);
  harness.ui.button.emit("pointerdown", { pointerType: "pen" });
  clock.advance(LIVE_MENU_HOLD_MS - 1);
  harness.ui.button.emit("pointerleave", { pointerType: "pen" });
  clock.advance(LIVE_MENU_HOLD_MS);
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  assert.equal(harness.ui.painted().face, "on");
  clock.advance(LIVE_MENU_MOUSE_GRACE_MS);
  harness.ui.button.click();
  await flush();
  assert.equal(harness.ui.painted().face, "off");
});

test("a pen hold opens the menu and a pen leave does not close it", async () => {
  const clock = fakeClock();
  const harness = setup({ setTimer: clock.schedule, clearTimer: clock.clear });
  await goLive(harness);
  harness.ui.button.emit("pointerdown", { pointerType: "pen" });
  clock.advance(LIVE_MENU_HOLD_MS - 1);
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  clock.advance(1);
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  harness.ui.button.click();
  await flush();
  assert.equal(harness.ui.painted().face, "on");
  harness.menu.anchor.emit("pointerleave", { pointerType: "pen" });
  clock.advance(LIVE_MENU_CLOSE_MS);
  assert.equal(harness.menu.anchor.dataset.menu, "open");
});

test("a mouse passing over a keyboard-opened menu leaves it open", async () => {
  const clock = fakeClock();
  const harness = setup({ setTimer: clock.schedule, clearTimer: clock.clear });
  await goLive(harness);
  const panel = {
    inside: true,
    matches() {
      return true;
    },
  };
  harness.menu.anchor.emit("focusin", { target: panel });
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  let blurred = false;
  harness.ui.button.blur = () => {
    blurred = true;
  };
  harness.menu.anchor.emit("pointerenter", { pointerType: "mouse" });
  harness.menu.anchor.emit("pointermove", { pointerType: "mouse" });
  harness.menu.anchor.emit("pointerleave", { pointerType: "mouse" });
  clock.advance(LIVE_MENU_CLOSE_MS);
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  assert.equal(blurred, false);
});

test("the header button matches the MIDI control and the page tells a DJ how to start", () => {
  const app = readFileSync(new URL("../../djtube/static/app.js", import.meta.url), "utf8");
  const css = readFileSync(new URL("../../djtube/static/app.css", import.meta.url), "utf8");
  const html = readFileSync(new URL("../../djtube/templates/index.html", import.meta.url), "utf8");
  const readme = readFileSync(new URL("../../README.md", import.meta.url), "utf8");
  assert.match(app, /bindLive\(\{/);
  assert.match(app, /getElementById\("live-button"\)/);
  assert.match(app, /getElementById\("live-status"\)/);
  assert.match(app, /liveControl\.refresh\(\)/);
  assert.match(css, /\.midi-button,\s*\.live-button\s*\{[^}]*width:\s*36px/);
  assert.match(css, /\.midi-button svg,\s*\.live-button svg\s*\{[^}]*width:\s*22px/);
  assert.match(
    css,
    /\.midi-button\[data-midi="on"\],\s*\.live-button\[data-live="on"\]\s*\{[^}]*background:\s*var\(--a\)/,
  );
  assert.match(html, /id="live-button"/);
  assert.match(html, /id="live-status" role="status"/);
  assert.match(html, /title="配信を始める"/);
  assert.match(html, /id="live-copy-audio">音声ストリーミングURLをコピー/);
  assert.match(html, /id="live-copy-video">動画ストリーミングURLをコピー/);
  assert.equal(LIVE_COPY_AUDIO, "音声ストリーミングURLをコピー");
  assert.equal(LIVE_COPY_VIDEO, "動画ストリーミングURLをコピー");
  assert.match(css, /\.live-anchor\[data-menu="open"\] > \.live-menu/);
  assert.match(css, /\.live-menu::before\s*\{[^}]*width:\s*14px/);
  assert.equal(css.includes(":focus-within"), false);
  assert.match(css, /\.live-menu\s*\{[^}]*padding-top:\s*8px/);
  assert.match(css, /\.live-menu\s*\{[^}]*max-width:\s*min\(320px,\s*calc\(100vw - 32px\)\)/);
  assert.match(css, /\.live-menu button\s*\{[^}]*overflow-wrap:\s*anywhere/);
  assert.match(html, /id="live-button"[^>]*aria-describedby="live-menu-head"/);
  assert.match(css, /\.live-button\s*\{[^}]*-webkit-touch-callout:\s*none/);
  assert.match(css, /\.live-button\s*\{[^}]*-webkit-user-select:\s*none/);
  assert.match(css, /\.live-button\s*\{[^}]*user-select:\s*none/);
  assert.equal(/id="live-button"[^>]*aria-expanded/.test(html), false);
  assert.equal(/id="live-button"[^>]*aria-controls/.test(html), false);
  assert.match(app, /getElementById\("live-copy-audio"\)/);
  assert.match(app, /getElementById\("live-copy-video"\)/);
  assert.equal(html.includes("おもちゃ"), false);
  assert.match(
    readme,
    /右上の配信の印を押すと配信が始まり、聴くための URL がコピーされる。VLC などにその URL を入れると聴ける。もう一度押すと止まる。/,
  );
  assert.match(readme, /動画で見せたいときは、その URL の末尾に \?thumbnail=1 を付ける。/);
  assert.equal(readme.includes("おもちゃ"), false);
});

test("deckSnapshot keeps the decks that are actually in the master", () => {
  const audios = {
    A: { paused: false, videoId: "abcdefghijk", volume: 0.5, offMaster: () => false },
    B: { paused: true, videoId: "bbbbbbbbbbb", volume: 1, offMaster: () => false },
  };
  assert.deepEqual(deckSnapshot(audios), [{ video: "abcdefghijk", gain: 0.5 }]);
  audios.B.paused = false;
  audios.B.offMaster = () => true;
  assert.deepEqual(deckSnapshot(audios), [{ video: "abcdefghijk", gain: 0.5 }]);
  audios.A.videoId = "not-an-id";
  assert.deepEqual(deckSnapshot(audios), []);
  audios.A.videoId = "abcdefghijk";
  audios.A.volume = 2;
  assert.equal(deckSnapshot(audios)[0].gain, 1);
  audios.A.volume = 0.3339;
  assert.equal(deckSnapshot(audios)[0].gain, 0.334);
  audios.B.offMaster = () => false;
  audios.B.volume = 0;
  assert.deepEqual(deckSnapshot(audios), [
    { video: "abcdefghijk", gain: 0.334 },
    { video: "bbbbbbbbbbb", gain: 0 },
  ]);
});

test("now playing is sent when the picture changes, and not faster than four a second", async () => {
  assert.equal(NOW_INTERVAL_MS, 250);
  let now = 1000;
  const timers = [];
  let decks = [{ video: "abcdefghijk", gain: 1 }];
  const harness = setup({
    nowPlaying: () => decks,
    clock: () => now,
    schedule(fn) {
      timers.push(fn);
      return timers.length;
    },
    unschedule(id) {
      timers[id - 1] = null;
    },
  });
  await goLive(harness);
  const first = nowMessage(decks);
  assert.deepEqual(harness.sockets[0].sent, [openedClaim(), first]);
  decks = [{ video: "abcdefghijk", gain: 0.5 }];
  now = 1100;
  timers[0]();
  assert.equal(harness.sockets[0].sent.length, 2);
  now = 1000 + NOW_INTERVAL_MS;
  timers[0]();
  assert.equal(harness.sockets[0].sent.at(-1), nowMessage(decks));
  assert.equal(harness.sockets[0].sent.length, 3);
  timers[0]();
  assert.equal(harness.sockets[0].sent.length, 3);
  harness.ui.button.click();
  await flush();
  assert.equal(timers[0], null);
});
