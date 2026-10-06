import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  LIVE_BITRATE,
  LIVE_BUFFER_LIMIT,
  LIVE_BUTTON_LABEL,
  LIVE_COPY_AUDIO,
  LIVE_COPY_FAILED,
  LIVE_COPY_VIDEO,
  LIVE_COPIED,
  LIVE_DROPPED,
  LIVE_MENU_CLOSE_MS,
  LIVE_MENU_COPY_FAILED,
  LIVE_MENU_HOLD_MS,
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
  masterGapNote,
  mimeMessage,
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
    sleep: extra.sleep,
    menu,
    schedule: extra.schedule,
    clearSchedule: extra.clearSchedule,
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
  const harness = setup({ schedule: clock.schedule, clearSchedule: clock.clear });
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
  harness.ui.button.emit("pointerdown");
  harness.menu.anchor.emit("focusin");
  harness.ui.button.emit("pointerup");
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  harness.menu.anchor.emit("focusin");
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  harness.menu.anchor.emit("focusout", { relatedTarget: { inside: true } });
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  harness.menu.anchor.emit("focusout", { relatedTarget: null });
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
});

test("escape closes the live menu and does not reopen from the returned focus", async () => {
  const harness = setup();
  await goLive(harness);
  harness.ui.button.focus = () => {
    harness.ui.button.focused = true;
    harness.menu.anchor.emit("focusin");
  };
  harness.menu.anchor.emit("focusin");
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
  const harness = setup({ schedule: clock.schedule, clearSchedule: clock.clear });
  await goLive(harness);
  harness.ui.button.emit("pointerdown");
  clock.advance(LIVE_MENU_HOLD_MS - 1);
  assert.equal(harness.menu.anchor.dataset.menu, undefined);
  clock.advance(1);
  assert.equal(harness.menu.anchor.dataset.menu, "open");
  harness.ui.button.click();
  await flush();
  assert.equal(harness.ui.painted().face, "on");
  assert.equal(harness.sockets[0].closed, null);
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
  const harness = setup({ schedule: clock.schedule, clearSchedule: clock.clear, root });
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
  assert.match(html, /id="live-button"[^>]*aria-describedby="live-menu"/);
  assert.equal(/id="live-button"[^>]*aria-expanded/.test(html), false);
  assert.equal(/id="live-button"[^>]*aria-controls/.test(html), false);
  assert.match(app, /getElementById\("live-copy-audio"\)/);
  assert.match(app, /getElementById\("live-copy-video"\)/);
  assert.equal(html.includes("おもちゃ"), false);
  assert.match(
    readme,
    /右上の配信の印を押すと配信が始まり、聴くための URL がコピーされる。VLC などにその URL を入れると聴ける。もう一度押すと止まる。/,
  );
  assert.equal(readme.includes("おもちゃ"), false);
});
