// Publish the master mix to /api/live/publish.
// The server assigns the id. One mime text frame goes out before any
// MediaRecorder blob. A socket the server closes is not opened again.

import { openAudioBus } from "./master.js";

export const LIVE_MIME = "audio/webm;codecs=opus";
export const LIVE_BITRATE = 128000;
export const LIVE_TIMESLICE_MS = 200;
export const LIVE_BUTTON_LABEL = "配信";
export const LIVE_IDLE = "配信を始める";
export const LIVE_STARTING = "配信を始めています";
export const LIVE_UNSUPPORTED = "このブラウザでは配信できません";
export const LIVE_CONNECT_FAILED = "音声を配信に接続できませんでした";
export const LIVE_DROPPED = "配信が切れました";

const CLOSE_TEXT = {
  4408: "応答が止まったため、配信を止めました",
  4409: "その ID は使われています",
  4429: "配信の上限に達しました",
  1003: "配信の形式が受け付けられませんでした",
  1008: "送信の上限に達しました",
  1009: "データが大きすぎるため、配信を止めました",
};

const SOCKET_OPEN = 1;

export function mimeMessage() {
  return JSON.stringify({ type: "mime", mime: LIVE_MIME });
}

export function liveSupported(scope = globalThis) {
  const Recorder = scope?.MediaRecorder;
  if (typeof Recorder !== "function") return false;
  if (typeof Recorder.isTypeSupported !== "function") return false;
  try {
    return Recorder.isTypeSupported(LIVE_MIME) === true;
  } catch {
    return false;
  }
}

export function livePublishUrl(prefix, origin) {
  const path = `${prefix}/api/live/publish`;
  const base = String(origin || "").replace(/\/$/, "");
  if (base.startsWith("https:")) return `wss:${base.slice("https:".length)}${path}`;
  if (base.startsWith("http:")) return `ws:${base.slice("http:".length)}${path}`;
  return path;
}

export function listenerUrl(prefix, id, origin) {
  const base = String(origin || "").replace(/\/$/, "");
  return `${base}${prefix}/stream/${encodeURIComponent(id)}`;
}

export function closeReason(code) {
  return CLOSE_TEXT[code] || LIVE_DROPPED;
}

/** Decks whose element never reached the master are missing from the stream. */
export function masterGapNote(audios) {
  const missing = ["A", "B"].filter((deck) => {
    const player = audios?.[deck];
    return typeof player?.offMaster === "function" && player.offMaster();
  });
  if (missing.length === 0) return "";
  if (missing.length === 2) return "デッキ A と B はマスターに入っていないため、配信には入りません";
  return `デッキ ${missing[0]} はマスターに入っていないため、配信には入りません`;
}

export function liveStatusText(status) {
  if (!status || status.state === "idle") return LIVE_IDLE;
  if (status.state === "unsupported") return LIVE_UNSUPPORTED;
  if (status.state === "starting") return LIVE_STARTING;
  if (status.state === "live") {
    const note = typeof status.note === "string" ? status.note.trim() : "";
    return note ? `配信中：${status.url}。${note}` : `配信中：${status.url}`;
  }
  if (status.state === "error") return status.reason || LIVE_DROPPED;
  return LIVE_IDLE;
}

export function liveButtonFace(status) {
  return status?.state === "live" ? "on" : "off";
}

/** Paint the header button and the hidden result. Skip a write when the value is unchanged. */
export function applyLiveStatus(button, live, status) {
  const text = liveStatusText(status);
  if (button.title !== text) button.title = text;
  if (button.getAttribute("aria-label") !== LIVE_BUTTON_LABEL) button.setAttribute("aria-label", LIVE_BUTTON_LABEL);
  if (status?.state !== "starting" && live.textContent !== text) live.textContent = text;
  const face = liveButtonFace(status);
  if (button.dataset.live !== face) button.dataset.live = face;
}

export async function copyLiveUrl(url, doc = globalThis.document, nav = globalThis.navigator) {
  if (nav?.clipboard?.writeText) {
    await nav.clipboard.writeText(url);
    return;
  }
  if (!doc?.body || typeof doc.execCommand !== "function") throw new Error("clipboard");
  const area = doc.createElement("textarea");
  area.value = url;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.left = "-9999px";
  doc.body.append(area);
  area.select();
  let ok = false;
  try {
    ok = doc.execCommand("copy");
  } finally {
    area.remove();
  }
  if (!ok) throw new Error("clipboard");
}

export function createLiveControl(options) {
  const button = options.button;
  const live = options.live;
  const bus = options.bus;
  const audios = options.audios;
  const prefix = options.prefix;
  const origin = options.origin || "";
  const supported = options.supported !== false;
  const connectSocket = options.connectSocket;
  const createRecorder = options.createRecorder;
  const copyText = options.copyText;
  let session = null;

  function paint(status) {
    applyLiveStatus(button, live, status);
  }

  function teardown(mine) {
    const recorder = mine.recorder;
    const socket = mine.socket;
    const dest = mine.dest;
    mine.recorder = null;
    mine.socket = null;
    mine.dest = null;
    try {
      recorder?.stop();
    } catch {
      /* already stopped */
    }
    try {
      if (dest) bus?.master?.disconnect(dest);
    } catch {
      /* the tap is already gone */
    }
    try {
      if (socket && socket.readyState === SOCKET_OPEN) socket.close(1000);
    } catch {
      /* already closing */
    }
  }

  function finish(mine, status) {
    if (mine.ended) return;
    mine.ended = true;
    if (session === mine) session = null;
    teardown(mine);
    paint(status);
  }

  function liveStatus(mine) {
    return { state: "live", url: mine.url, note: masterGapNote(audios) };
  }

  function begin(mine, id) {
    if (mine.ended || mine.recorder) return;
    const url = listenerUrl(prefix, id, origin);
    mine.url = url;
    try {
      mine.socket.send(mimeMessage());
    } catch {
      finish(mine, { state: "error", reason: LIVE_DROPPED });
      return;
    }
    let recorder = null;
    try {
      recorder = createRecorder(mine.dest.stream, {
        mimeType: LIVE_MIME,
        audioBitsPerSecond: LIVE_BITRATE,
      });
    } catch {
      finish(mine, { state: "error", reason: LIVE_UNSUPPORTED });
      return;
    }
    mine.recorder = recorder;
    recorder.ondataavailable = (event) => {
      const blob = event?.data;
      if (!blob || !blob.size || mine.ended) return;
      Promise.resolve(blob.arrayBuffer())
        .then((data) => {
          if (mine.ended || mine.socket?.readyState !== SOCKET_OPEN) return;
          try {
            mine.socket.send(data);
          } catch {
            /* onclose reports the drop */
          }
        })
        .catch(() => {});
    };
    try {
      recorder.start(LIVE_TIMESLICE_MS);
    } catch {
      finish(mine, { state: "error", reason: LIVE_UNSUPPORTED });
      return;
    }
    paint(liveStatus(mine));
    if (typeof copyText !== "function") return;
    try {
      Promise.resolve(copyText(url)).catch(() => {});
    } catch {
      /* the URL stays on the hover */
    }
  }

  function bindSocket(mine, socket) {
    mine.socket = socket;
    socket.onmessage = (event) => {
      if (mine.ended || mine.recorder || typeof event?.data !== "string") return;
      let payload = null;
      try {
        payload = JSON.parse(event.data);
      } catch {
        return;
      }
      if (!payload || payload.type !== "id" || typeof payload.id !== "string" || !payload.id) return;
      begin(mine, payload.id);
    };
    socket.onclose = (event) => {
      if (mine.ended) return;
      finish(mine, { state: "error", reason: closeReason(event?.code) });
    };
  }

  async function start() {
    if (!openAudioBus(bus) || !bus.context || !bus.master) {
      paint({ state: "error", reason: LIVE_CONNECT_FAILED });
      return;
    }
    const context = bus.context;
    const mine = { ended: false, socket: null, recorder: null, dest: null, url: "" };
    session = mine;
    paint({ state: "starting" });
    let pending = null;
    if (context.state === "suspended" && typeof context.resume === "function") {
      try {
        pending = context.resume();
      } catch {
        finish(mine, { state: "error", reason: LIVE_CONNECT_FAILED });
        return;
      }
    }
    if (pending && typeof pending.then === "function") {
      try {
        await pending;
      } catch {
        finish(mine, { state: "error", reason: LIVE_CONNECT_FAILED });
        return;
      }
    }
    if (mine.ended) return;
    try {
      if (typeof context.createMediaStreamDestination !== "function") throw new Error("destination");
      const dest = context.createMediaStreamDestination();
      bus.master.connect(dest);
      mine.dest = dest;
    } catch {
      finish(mine, { state: "error", reason: LIVE_CONNECT_FAILED });
      return;
    }
    if (mine.ended) return;
    let socket = null;
    try {
      socket = connectSocket(livePublishUrl(prefix, origin));
    } catch {
      finish(mine, { state: "error", reason: LIVE_DROPPED });
      return;
    }
    if (mine.ended) {
      try {
        if (socket && socket.readyState === SOCKET_OPEN) socket.close(1000);
      } catch {
        /* already gone */
      }
      return;
    }
    bindSocket(mine, socket);
  }

  function onClick() {
    if (!supported) return;
    if (session) {
      finish(session, { state: "idle" });
      return;
    }
    return start();
  }

  button.addEventListener("click", () => {
    Promise.resolve(onClick()).catch(() => {});
  });

  paint(supported ? { state: "idle" } : { state: "unsupported" });

  return {
    refresh() {
      if (!session || session.ended || !session.recorder || !session.url) return;
      paint(liveStatus(session));
    },
  };
}

export function bindLive(options) {
  const button = options.button;
  const live = options.live;
  if (!button || !live) return { refresh() {} };
  const origin = options.origin || (typeof location === "undefined" ? "" : location.origin);
  return createLiveControl({
    button,
    live,
    bus: options.bus,
    audios: options.audios,
    prefix: options.prefix,
    origin,
    supported: options.supported ?? liveSupported(),
    connectSocket: options.connectSocket || ((url) => new WebSocket(url)),
    createRecorder: options.createRecorder || ((stream, recorderOptions) => new MediaRecorder(stream, recorderOptions)),
    copyText: options.copyText || ((url) => copyLiveUrl(url)),
  });
}
