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
export const LIVE_COPY_FAILED = "URL をコピーできませんでした";
export const LIVE_SEND_BACKLOG = "送信が追いつかないため、配信を止めました";
export const LIVE_TAKEN = "この配信 ID は他の人が使っています。もう一度押すと新しい ID で配信します";
export const LIVE_BUFFER_LIMIT = 1024 * 1024;

const CLOSE_TEXT = {
  4408: "音声が届かなくなったため、配信を止めました",
  4409: LIVE_TAKEN,
  4429: "配信の上限に達しました",
  1003: "配信の形式が受け付けられませんでした",
  1008: "送信の上限に達しました",
  1009: "データが大きすぎるため、配信を止めました",
};

const SOCKET_OPEN = 1;

export function mimeMessage() {
  return JSON.stringify({ type: "mime", mime: LIVE_MIME });
}

/** First text frame. The id and token stay out of the URL nginx logs. */
export function claimMessage(id, token, seq) {
  const payload = { type: "mime", mime: LIVE_MIME, seq };
  if (typeof id === "string" && id) payload.id = id;
  if (typeof token === "string" && token) payload.token = token;
  return JSON.stringify(payload);
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

/** Hover stays the live URL. A failed copy is only added for the screen reader. */
export function liveAnnounceText(status) {
  const text = liveStatusText(status);
  if (status?.state === "live" && status.copyFailed) return `${text}。${LIVE_COPY_FAILED}`;
  return text;
}

/** Paint the header button and the hidden result. Skip a write when the value is unchanged. */
export function applyLiveStatus(button, live, status) {
  const hover = liveStatusText(status);
  const announced = liveAnnounceText(status);
  if (button.title !== hover) button.title = hover;
  if (button.getAttribute("aria-label") !== LIVE_BUTTON_LABEL) button.setAttribute("aria-label", LIVE_BUTTON_LABEL);
  if (status?.state !== "starting" && live.textContent !== announced) live.textContent = announced;
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

/**
 * Start a clipboard write during the click. The URL arrives later, after the
 * server assigns an id, and a late writeText no longer counts as a user gesture.
 */
export function armClipboard(nav = globalThis.navigator) {
  const Item = globalThis.ClipboardItem;
  if (!nav?.clipboard?.write || typeof Item !== "function") return null;
  let resolveText = null;
  let rejectText = null;
  const text = new Promise((resolve, reject) => {
    resolveText = resolve;
    rejectText = reject;
  });
  const blob = text.then((value) => new Blob([value], { type: "text/plain" }));
  blob.catch(() => {});
  let writing = null;
  try {
    writing = nav.clipboard.write([new Item({ "text/plain": blob })]);
  } catch {
    rejectText(new Error("clipboard"));
    return null;
  }
  const settled = Promise.resolve(writing).then(
    () => true,
    () => false,
  );
  return {
    provide(value) {
      resolveText(String(value));
      return settled.then((ok) => {
        if (!ok) throw new Error("clipboard");
      });
    },
    cancel() {
      rejectText(new Error("cancel"));
      return settled.then(() => {});
    },
  };
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
  const armCopy = options.armCopy;
  let session = null;
  let pinnedId = "";
  let pinnedToken = "";
  let claimSeq = 0;

  function markCopyFailed(mine) {
    if (mine.ended || mine.copyFailed) return;
    mine.copyFailed = true;
    paint(liveStatus(mine));
  }

  function fallbackCopy(mine, url) {
    if (typeof copyText !== "function") {
      markCopyFailed(mine);
      return;
    }
    try {
      Promise.resolve(copyText(url)).then(
        () => {},
        () => markCopyFailed(mine),
      );
    } catch {
      markCopyFailed(mine);
    }
  }

  function deliverCopy(mine, url) {
    if (mine.clip) {
      try {
        Promise.resolve(mine.clip.provide(url)).then(
          (ok) => {
            if (ok === false) fallbackCopy(mine, url);
          },
          () => fallbackCopy(mine, url),
        );
        return;
      } catch {
        /* the late copy is the fallback */
      }
    }
    fallbackCopy(mine, url);
  }

  function paint(status) {
    applyLiveStatus(button, live, status);
  }

  function teardown(mine) {
    const recorder = mine.recorder;
    const socket = mine.socket;
    const dest = mine.dest;
    const silence = mine.silence;
    mine.recorder = null;
    mine.socket = null;
    mine.dest = null;
    mine.silence = null;
    try {
      recorder?.stop();
    } catch {
      /* already stopped */
    }
    stopSilence(silence);
    try {
      if (dest) bus?.master?.disconnect(dest);
    } catch {
      /* the tap is already gone */
    }
    try {
      if (dest?.stream && typeof dest.stream.getTracks === "function") {
        dest.stream.getTracks().forEach((track) => track.stop());
      }
    } catch {
      /* the stream is already stopped */
    }
    closeSocket(socket);
  }

  function closeSocket(socket) {
    try {
      if (socket && socket.readyState <= SOCKET_OPEN) socket.close(1000);
    } catch {
      /* already closing */
    }
  }

  function startSilence(context, dest) {
    // Chrome stops MediaRecorder clusters once the last playing source ends,
    // even while AudioContext stays running. A gain of 0 is silence, and the
    // source still keeps a cluster coming so the server's idle limit is not hit.
    if (typeof context.createConstantSource !== "function" || typeof context.createGain !== "function") return null;
    try {
      const source = context.createConstantSource();
      const gain = context.createGain();
      gain.gain.value = 0;
      source.connect(gain);
      gain.connect(dest);
      source.start();
      return { source, gain };
    } catch {
      return null;
    }
  }

  function stopSilence(silence) {
    if (!silence) return;
    try {
      silence.source.stop();
    } catch {
      /* already stopped */
    }
    try {
      silence.source.disconnect();
    } catch {
      /* already gone */
    }
    try {
      silence.gain.disconnect();
    } catch {
      /* already gone */
    }
  }

  function finish(mine, status) {
    if (mine.ended) return;
    mine.ended = true;
    if (session === mine) session = null;
    if (!mine.url && mine.clip) {
      const clip = mine.clip;
      mine.clip = null;
      try {
        clip.cancel();
      } catch {
        /* nothing was copied */
      }
    }
    teardown(mine);
    paint(status);
  }

  function liveStatus(mine) {
    return {
      state: "live",
      url: mine.url,
      note: masterGapNote(audios),
      copyFailed: mine.copyFailed === true,
    };
  }

  function begin(mine, id, token) {
    if (mine.ended || mine.recorder) return;
    if (pinnedId && id !== pinnedId) {
      finish(mine, { state: "error", reason: LIVE_TAKEN });
      return;
    }
    if (!pinnedId) pinnedId = id;
    if (typeof token === "string" && token) pinnedToken = token;
    const url = listenerUrl(prefix, id, origin);
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
          if (mine.socket.bufferedAmount > LIVE_BUFFER_LIMIT) {
            finish(mine, { state: "error", reason: LIVE_SEND_BACKLOG });
            return;
          }
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
    mine.url = url;
    paint(liveStatus(mine));
    deliverCopy(mine, url);
  }

  function sendClaim(socket) {
    claimSeq += 1;
    socket.send(claimMessage(pinnedId, pinnedToken, claimSeq));
  }

  function bindSocket(mine, socket) {
    mine.socket = socket;
    const deliver = () => {
      if (mine.ended || mine.claimed) return;
      mine.claimed = true;
      try {
        sendClaim(socket);
      } catch {
        finish(mine, { state: "error", reason: LIVE_DROPPED });
      }
    };
    socket.onopen = deliver;
    if (socket.readyState === SOCKET_OPEN) deliver();
    socket.onmessage = (event) => {
      if (mine.ended || mine.recorder || typeof event?.data !== "string") return;
      let payload = null;
      try {
        payload = JSON.parse(event.data);
      } catch {
        return;
      }
      if (!payload || payload.type !== "id" || typeof payload.id !== "string" || !payload.id) return;
      if (typeof payload.token !== "string" || !payload.token) return;
      begin(mine, payload.id, payload.token);
    };
    socket.onclose = (event) => {
      if (mine.ended) return;
      const code = event?.code;
      if (code === 4409) {
        pinnedId = "";
        pinnedToken = "";
      }
      finish(mine, { state: "error", reason: closeReason(code) });
    };
  }

  function openPublish(mine) {
    let socket = null;
    try {
      socket = connectSocket(livePublishUrl(prefix, origin));
    } catch {
      finish(mine, { state: "error", reason: LIVE_DROPPED });
      return;
    }
    if (mine.ended) {
      closeSocket(socket);
      return;
    }
    bindSocket(mine, socket);
  }

  async function start() {
    if (!openAudioBus(bus) || !bus.context || !bus.master) {
      paint({ state: "error", reason: LIVE_CONNECT_FAILED });
      return;
    }
    const context = bus.context;
    let clip = null;
    try {
      clip = typeof armCopy === "function" ? armCopy() : null;
    } catch {
      clip = null;
    }
    const mine = { ended: false, socket: null, recorder: null, dest: null, url: "", clip };
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
      mine.silence = startSilence(context, dest);
    } catch {
      finish(mine, { state: "error", reason: LIVE_CONNECT_FAILED });
      return;
    }
    if (mine.ended) return;
    openPublish(mine);
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
    armCopy: options.armCopy ?? (() => armClipboard()),
    connectSocket: options.connectSocket || ((url) => new WebSocket(url)),
    createRecorder: options.createRecorder || ((stream, recorderOptions) => new MediaRecorder(stream, recorderOptions)),
    copyText: options.copyText || ((url) => copyLiveUrl(url)),
  });
}
