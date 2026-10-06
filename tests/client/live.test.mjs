import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  LIVE_BITRATE,
  LIVE_BUFFER_LIMIT,
  LIVE_BUTTON_LABEL,
  LIVE_COPY_FAILED,
  LIVE_DROPPED,
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
  liveButtonFace,
  livePublishUrl,
  liveStatusText,
  liveSupported,
  armClipboard,
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
  let face = "off";
  let liveText = "";
  const listeners = {};
  const button = {
    writes,
    get title() {
      return title;
    },
    set title(value) {
      writes.push("title");
      title = value;
    },
    getAttribute(name) {
      return name === "aria-label" ? label : null;
    },
    setAttribute(name, value) {
      if (name !== "aria-label") return;
      writes.push("aria-label");
      label = value;
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
  };
  return { context, master, failed: false, destination };
}

function setup(extra = {}) {
  const ui = fakeButton();
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
  });
  return { ui, sockets, recorders, copies, bus, control };
}

const LIVE_TOKEN = "secret-token";

function idMessage(id = "ABCD", token = LIVE_TOKEN) {
  return JSON.stringify({ type: "id", id, token });
}

function pinnedPublishUrl(id = "ABCD", token = LIVE_TOKEN) {
  return `wss://s.cympfh.cc/djtube/api/live/publish?id=${id}&token=${token}`;
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
  assert.equal(livePublishUrl("/djtube", "https://s.cympfh.cc"), "wss://s.cympfh.cc/djtube/api/live/publish");
  assert.equal(livePublishUrl("/djtube", "http://127.0.0.1:8098"), "ws://127.0.0.1:8098/djtube/api/live/publish");
  assert.equal(
    livePublishUrl("/djtube", "https://s.cympfh.cc", "ABCD"),
    "wss://s.cympfh.cc/djtube/api/live/publish?id=ABCD",
  );
  assert.equal(
    livePublishUrl("/djtube", "https://s.cympfh.cc", "ABCD", LIVE_TOKEN),
    pinnedPublishUrl(),
  );
  assert.equal(mimeMessage(), JSON.stringify({ type: "mime", mime: LIVE_MIME }));
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
  assert.equal(liveStatusText({ state: "idle" }), LIVE_IDLE);
  assert.equal(liveStatusText({ state: "unsupported" }), LIVE_UNSUPPORTED);
  assert.equal(liveStatusText({ state: "starting" }), LIVE_STARTING);
  assert.equal(
    liveStatusText({ state: "live", url: "https://s.cympfh.cc/djtube/stream/ABCD" }),
    "配信中：https://s.cympfh.cc/djtube/stream/ABCD",
  );

  applyLiveStatus(ui.button, ui.live, { state: "idle" });
  assert.deepEqual(ui.painted(), {
    title: LIVE_IDLE,
    label: LIVE_BUTTON_LABEL,
    face: "off",
    live: LIVE_IDLE,
  });
  assert.deepEqual(ui.button.writes, ["status"]);

  ui.button.writes.length = 0;
  applyLiveStatus(ui.button, ui.live, { state: "idle" });
  assert.deepEqual(ui.button.writes, []);

  applyLiveStatus(ui.button, ui.live, { state: "starting" });
  assert.deepEqual(ui.painted(), {
    title: LIVE_STARTING,
    label: LIVE_BUTTON_LABEL,
    face: "off",
    live: LIVE_IDLE,
  });
  assert.deepEqual(ui.button.writes, ["title"]);

  ui.button.writes.length = 0;
  applyLiveStatus(ui.button, ui.live, { state: "starting" });
  assert.deepEqual(ui.button.writes, []);

  const url = "https://s.cympfh.cc/djtube/stream/ABCD";
  applyLiveStatus(ui.button, ui.live, { state: "live", url, note: "" });
  assert.deepEqual(ui.painted(), {
    title: `配信中：${url}`,
    label: LIVE_BUTTON_LABEL,
    face: "on",
    live: `配信中：${url}`,
  });
  assert.deepEqual(ui.button.writes, ["title", "status", "data-live"]);

  ui.button.writes.length = 0;
  applyLiveStatus(ui.button, ui.live, { state: "live", url, note: "" });
  assert.deepEqual(ui.button.writes, []);

  applyLiveStatus(ui.button, ui.live, { state: "live", url, copyFailed: true });
  assert.equal(ui.painted().title, `配信中：${url}`);
  assert.equal(ui.painted().live, `配信中：${url}。${LIVE_COPY_FAILED}`);
  assert.deepEqual(ui.button.writes, ["status"]);
  ui.button.writes.length = 0;
  applyLiveStatus(ui.button, ui.live, { state: "live", url, copyFailed: true });
  assert.deepEqual(ui.button.writes, []);

  applyLiveStatus(ui.button, ui.live, { state: "error", reason: "配信の上限に達しました" });
  assert.deepEqual(ui.painted(), {
    title: "配信の上限に達しました",
    label: LIVE_BUTTON_LABEL,
    face: "off",
    live: "配信の上限に達しました",
  });
  assert.deepEqual(ui.button.writes, ["title", "status", "data-live"]);

  ui.button.writes.length = 0;
  applyLiveStatus(ui.button, ui.live, { state: "error", reason: "配信の上限に達しました" });
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
  assert.equal(harness.ui.painted().title, `配信中：${url}`);
  assert.equal(harness.ui.painted().live, `配信中：${url}`);
  assert.equal(harness.ui.painted().face, "on");
  assert.equal(harness.ui.painted().label, LIVE_BUTTON_LABEL);
  assert.equal(harness.recorders.length, 1);
  assert.equal(harness.recorders[0].options.mimeType, LIVE_MIME);
  assert.equal(harness.recorders[0].options.audioBitsPerSecond, LIVE_BITRATE);
  assert.equal(harness.recorders[0].timeslice, LIVE_TIMESLICE_MS);
  assert.deepEqual(harness.sockets[0].sent, [mimeMessage()]);

  harness.recorders[0].emit(new Blob([Uint8Array.of(1, 2, 3)]));
  harness.recorders[0].emit(new Blob([]));
  harness.recorders[0].emit(new Blob([Uint8Array.of(4)]));
  await flush();
  assert.equal(harness.sockets[0].sent.length, 3);
  assert.equal(harness.sockets[0].sent[0], mimeMessage());
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
  assert.equal(harness.ui.painted().title, `配信中：${url}`);
  assert.equal(harness.ui.painted().live, `配信中：${url}。${LIVE_COPY_FAILED}`);
  assert.equal(harness.sockets[0].sent[0], mimeMessage());
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
  assert.equal(saved.ui.painted().title, `配信中：${savedUrl}`);
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
  assert.equal(failed.ui.painted().title, `配信中：${failedUrl}`);
  assert.equal(failed.ui.painted().live, `配信中：${failedUrl}。${LIVE_COPY_FAILED}`);
  assert.equal(failed.sockets[0].sent[0], mimeMessage());
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
  assert.equal(harness.sockets[1].url, pinnedPublishUrl());
  harness.sockets[1].receive(idMessage());
  await flush();
  assert.equal(harness.ui.painted().title, `配信中：${url}`);
  assert.equal(harness.ui.painted().live, `配信中：${url}`);
  assert.equal(harness.ui.painted().face, "on");
  assert.deepEqual(harness.copies, [url, url]);
  assert.equal(harness.sockets[1].sent[0], mimeMessage());
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
  assert.equal(harness.sockets[1].url, pinnedPublishUrl());
  assert.equal(harness.sockets[1].closed, null);
  assert.equal(typeof releaseClose, "function");
  harness.sockets[1].receive(idMessage());
  await flush();
  assert.equal(harness.ui.painted().face, "on");
  assert.equal(harness.ui.painted().title, "配信中：https://s.cympfh.cc/djtube/stream/ABCD");
  assert.equal(harness.sockets[1].sent[0], mimeMessage());
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
  assert.equal(harness.sockets[1].url, pinnedPublishUrl());
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
  assert.equal(harness.ui.painted().title, "配信中：https://s.cympfh.cc/djtube/stream/EFGH");
  assert.equal(harness.ui.painted().face, "on");
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
      const next = harness.sockets.at(-1).url;
      if (code === 4409) {
        assert.equal(next, "wss://s.cympfh.cc/djtube/api/live/publish");
      } else {
        assert.equal(next, pinnedPublishUrl());
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
  assert.equal(harness.ui.painted().title, noted);
  assert.equal(harness.ui.painted().live, noted);
  harness.ui.button.writes.length = 0;
  harness.control.refresh();
  assert.deepEqual(harness.ui.button.writes, []);
  gap.A = false;
  harness.control.refresh();
  assert.equal(harness.ui.painted().title, `配信中：${url}`);
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
  assert.equal(html.includes("おもちゃ"), false);
  assert.match(
    readme,
    /右上の配信の印を押すと配信が始まり、聴くための URL がコピーされる。VLC などにその URL を入れると聴ける。もう一度押すと止まる。/,
  );
  assert.equal(readme.includes("おもちゃ"), false);
});
