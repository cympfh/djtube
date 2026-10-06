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
export const LIVE_COPIED = "コピーしました";
export const LIVE_MENU_COPY_FAILED = "コピーできませんでした";
export const LIVE_MENU_CLOSE_MS = 250;
export const LIVE_MENU_NOTE_MS = 4000;
export const LIVE_MENU_HOLD_MS = 500;
// Chromium follows a pen with mouse boundary events that did not move.
export const LIVE_MENU_MOUSE_GRACE_MS = 500;
export const LIVE_COPY_AUDIO = "音声ストリーミングURLをコピー";
export const LIVE_COPY_VIDEO = "動画ストリーミングURLをコピー";
export const LIVE_SEND_BACKLOG = "送信が追いつかないため、配信を止めました";
export const LIVE_TAKEN = "この配信 ID は他の人が使っています。もう一度押すと新しい ID で配信します";
export const LIVE_BUFFER_LIMIT = 1024 * 1024;
export const LIVE_OUTPUT_STALLED = "音声出力が動いていません（出力デバイスを確認）";
// How often to sample AudioContext.currentTime. The page does not stop
// itself from this timer. A 4408 uses the samples only to choose hover text.
export const LIVE_RENDER_WATCH_MS = 3000;

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

export function videoListenerUrl(prefix, id, origin) {
  return `${listenerUrl(prefix, id, origin)}?thumbnail=1`;
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

/** The screen reader hears the live URL. A failed copy is only added there. */
export function liveAnnounceText(status) {
  const text = liveStatusText(status);
  if (status?.state === "live" && status.copyFailed) return `${text}。${LIVE_COPY_FAILED}`;
  return text;
}

/**
 * Paint the header button, the hidden result, and the live menu heading.
 * While live the browser title stays empty so it does not sit on top of the menu.
 * Skip a write when the value is unchanged.
 */
export function applyLiveStatus(button, live, status, menuHead) {
  const hover = liveStatusText(status);
  const announced = liveAnnounceText(status);
  const nativeTitle = status?.state === "live" ? "" : hover;
  if (button.title !== nativeTitle) button.title = nativeTitle;
  if (button.getAttribute("aria-label") !== LIVE_BUTTON_LABEL) button.setAttribute("aria-label", LIVE_BUTTON_LABEL);
  if (status?.state !== "starting" && live.textContent !== announced) live.textContent = announced;
  const face = liveButtonFace(status);
  if (button.dataset.live !== face) button.dataset.live = face;
  const menuText = status?.state === "live" ? hover : "";
  if (menuHead && menuHead.textContent !== menuText) menuHead.textContent = menuText;
}

function readyMenu(menu) {
  if (!menu?.anchor || !menu.head || !menu.audio || !menu.video || !menu.result) return null;
  return menu;
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
  const menu = readyMenu(options.menu);
  const setTimer = options.setTimer || ((fn, ms) => {
    const id = setTimeout(fn, ms);
    if (id && typeof id.unref === "function") id.unref();
    return id;
  });
  const clearTimer = options.clearTimer || ((id) => clearTimeout(id));
  const root = options.root;
  const doc = options.document ?? (typeof document === "undefined" ? undefined : document);
  let session = null;
  // Why the panel is open. null while it is closed.
  let openBy = null;
  // True after touch or pen, until a real mouse event. A tap must not open from :hover.
  let coarse = false;
  let suppressMouse = false;
  let suppressTimer = 0;
  // Esc while the icon is hovered. Cleared on pointerleave.
  let blockPointer = false;
  // Esc restored focus onto the icon. The next focusin is that restoration.
  let ignoreNextFocus = false;
  let swallowClick = false;
  let closeTimer = 0;
  let noteTimer = 0;
  let holdTimer = 0;
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
    const wasOn = button.dataset.live === "on";
    applyLiveStatus(button, live, status, menu?.head);
    const liveNow = status?.state === "live";
    if (!liveNow) {
      if (clearHold()) swallowClick = true;
      closeMenu();
      return;
    }
    if (wasOn) return;
    // A tap leaves :hover on Chrome's mobile emulation and on WebKit.
    if (!coarse && menuIsHovered()) openAs("pointer");
    if (focusVisible(button)) openAs("focus");
  }

  function menuIsHovered() {
    const anchor = menu?.anchor;
    if (!anchor || typeof anchor.matches !== "function") return false;
    try {
      return anchor.matches(":hover") === true;
    } catch {
      return false;
    }
  }

  function focusVisible(node) {
    if (!node || typeof node.matches !== "function") return false;
    try {
      return node.matches(":focus-visible") === true;
    } catch {
      return false;
    }
  }

  function pointerMoved(event) {
    return (event?.movementX || 0) !== 0 || (event?.movementY || 0) !== 0;
  }

  function markCoarse() {
    coarse = true;
    suppressMouse = true;
    if (suppressTimer) clearTimer(suppressTimer);
    suppressTimer = setTimer(() => {
      suppressTimer = 0;
      suppressMouse = false;
    }, LIVE_MENU_MOUSE_GRACE_MS);
  }

  function rememberPointer(event) {
    const type = event?.pointerType;
    if (type === "touch" || type === "pen") {
      markCoarse();
      return;
    }
    if (type === "mouse" && !suppressMouse) coarse = false;
  }

  function stillMouse(event) {
    return event?.pointerType === "mouse" && !pointerMoved(event);
  }

  function quietMouse(event) {
    return suppressMouse && menu?.anchor?.dataset?.menu === "open" && stillMouse(event);
  }

  function acceptMouse() {
    coarse = false;
    suppressMouse = false;
    if (!suppressTimer) return;
    clearTimer(suppressTimer);
    suppressTimer = 0;
  }

  function clearMenuResult() {
    if (noteTimer) {
      clearTimer(noteTimer);
      noteTimer = 0;
    }
    if (!menu?.result) return;
    if (menu.result.textContent) menu.result.textContent = "";
    if (menu.result.dataset?.result) delete menu.result.dataset.result;
  }

  function cancelClose() {
    if (!closeTimer) return;
    clearTimer(closeTimer);
    closeTimer = 0;
  }

  function iconHovered() {
    try {
      return button.matches(":hover") === true;
    } catch {
      return false;
    }
  }

  function closeMenu(kind) {
    openBy = null;
    cancelClose();
    clearMenuResult();
    if (kind === "esc") {
      if (iconHovered()) blockPointer = true;
      // button.focus() on an already-focused button fires no focusin.
      if (doc?.activeElement !== button) ignoreNextFocus = true;
    }
    const anchor = menu?.anchor;
    if (anchor?.dataset?.menu) delete anchor.dataset.menu;
  }

  function openAs(reason) {
    const anchor = menu?.anchor;
    if (!anchor || button.dataset.live !== "on") return;
    if (reason === "pointer" && blockPointer) return;
    openBy = reason;
    cancelClose();
    if (anchor.dataset.menu !== "open") anchor.dataset.menu = "open";
  }

  function scheduleClose() {
    cancelClose();
    closeTimer = setTimer(() => {
      closeTimer = 0;
      closeMenu();
    }, LIVE_MENU_CLOSE_MS);
  }

  function showMenuResult(text, kind) {
    if (!menu?.result) return;
    if (noteTimer) {
      clearTimer(noteTimer);
      noteTimer = 0;
    }
    if (menu.result.textContent !== text) menu.result.textContent = text;
    if (menu.result.dataset) menu.result.dataset.result = kind;
    noteTimer = setTimer(() => {
      noteTimer = 0;
      const viaTouch = openBy === "touch";
      clearMenuResult();
      if (viaTouch) closeMenu();
    }, LIVE_MENU_NOTE_MS);
  }

  async function copyMenu(kind) {
    const mine = session;
    if (!mine || mine.ended || !mine.url) return;
    const url = kind === "video" ? `${mine.url}?thumbnail=1` : mine.url;
    try {
      if (typeof copyText !== "function") throw new Error("clipboard");
      await copyText(url);
      if (mine.ended || session !== mine) return;
      showMenuResult(LIVE_COPIED, "ok");
    } catch {
      if (mine.ended || session !== mine) return;
      showMenuResult(LIVE_MENU_COPY_FAILED, "fail");
    }
  }

  function teardown(mine) {
    clearWatch(mine);
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

  function clearWatch(mine) {
    if (!mine || mine.watch == null) return;
    clearTimer(mine.watch);
    mine.watch = null;
  }

  function renderTime() {
    const time = bus?.context?.currentTime;
    return typeof time === "number" ? time : null;
  }

  function noteRender(mine) {
    const now = renderTime();
    if (now == null) return;
    if (mine.renderMark == null) {
      mine.renderMark = now;
      return;
    }
    if (now > mine.renderMark) mine.renderMoved = true;
  }

  function renderFrozen(mine) {
    noteRender(mine);
    if (mine.renderMoved || mine.renderMark == null) return false;
    const now = renderTime();
    return now != null && now <= mine.renderMark;
  }

  function armWatch(mine) {
    clearWatch(mine);
    if (mine.ended || session !== mine) return;
    noteRender(mine);
    mine.watch = setTimer(() => {
      mine.watch = null;
      if (mine.ended || session !== mine) return;
      noteRender(mine);
      armWatch(mine);
    }, LIVE_RENDER_WATCH_MS);
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
    armWatch(mine);
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
      const stalled = code === 4408 && renderFrozen(mine);
      finish(mine, { state: "error", reason: stalled ? LIVE_OUTPUT_STALLED : closeReason(code) });
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
    const mine = {
      ended: false,
      socket: null,
      recorder: null,
      dest: null,
      url: "",
      clip,
      watch: null,
      renderMark: null,
      renderMoved: false,
    };
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

  if (menu) {
    const copyClick = (kind) => (event) => {
      event.preventDefault?.();
      event.stopPropagation?.();
      return copyMenu(kind);
    };
    menu.audio.addEventListener("click", copyClick("audio"));
    menu.video.addEventListener("click", copyClick("video"));
    const pointerArrived = (event) => {
      if (quietMouse(event)) return;
      // A still mouse must not turn a pen or touch open into a hover.
      if (openBy === "touch" && stillMouse(event)) return;
      rememberPointer(event);
      if (event?.pointerType !== "mouse") return;
      acceptMouse();
      if (openBy === "focus") return;
      openAs("pointer");
    };
    menu.anchor.addEventListener("pointerenter", pointerArrived);
    menu.anchor.addEventListener("pointermove", pointerArrived);
    menu.anchor.addEventListener("pointerdown", (event) => {
      if (quietMouse(event)) return;
      rememberPointer(event);
      if (event?.pointerType !== "mouse") return;
      acceptMouse();
      if (openBy === "touch") openBy = "pointer";
    });
    menu.anchor.addEventListener("pointerleave", (event) => {
      if (quietMouse(event)) return;
      blockPointer = false;
      const type = event?.pointerType;
      if (type === "touch" || type === "pen") {
        rememberPointer(event);
        return;
      }
      if (type === "mouse") acceptMouse();
      if (button.dataset.live !== "on") return;
      if (openBy !== "pointer") return;
      scheduleClose();
    });
    menu.anchor.addEventListener("focusin", (event) => {
      if (ignoreNextFocus) {
        ignoreNextFocus = false;
        return;
      }
      if (button.dataset.live !== "on" || !focusVisible(event?.target)) return;
      openAs("focus");
    });
    menu.anchor.addEventListener("focusout", (event) => {
      const next = event?.relatedTarget;
      if (next == null) return;
      if (typeof menu.anchor.contains === "function" && menu.anchor.contains(next)) return;
      closeMenu();
    });
    menu.anchor.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || menu.anchor.dataset.menu !== "open") return;
      event.preventDefault?.();
      event.stopPropagation?.();
      closeMenu("esc");
      button.focus?.();
    });
    if (root && typeof root.addEventListener === "function") {
      // Capture, so a target that stops the bubble (the playlist grip) still closes
      // the panel. Do not preventDefault: the tap has to reach that target.
      root.addEventListener("pointerdown", (event) => {
        if (menu.anchor.dataset.menu !== "open") return;
        const target = event?.target;
        if (target && typeof menu.anchor.contains === "function" && menu.anchor.contains(target)) return;
        closeMenu();
      }, true);
    }
  }

  function onClick() {
    if (!supported) return;
    if (session) {
      finish(session, { state: "idle" });
      return;
    }
    return start();
  }

  function clearHold() {
    if (!holdTimer) return false;
    clearTimer(holdTimer);
    holdTimer = 0;
    return true;
  }
  button.addEventListener("pointerdown", (event) => {
    if (quietMouse(event)) return;
    rememberPointer(event);
    const type = event?.pointerType;
    if (type === "mouse") {
      acceptMouse();
      if (openBy === "touch") openBy = "pointer";
      swallowClick = false;
      clearHold();
      return;
    }
    if (type !== "touch" && type !== "pen") {
      swallowClick = false;
      return;
    }
    swallowClick = false;
    clearHold();
    if (button.dataset.live !== "on") return;
    holdTimer = setTimer(() => {
      holdTimer = 0;
      swallowClick = true;
      openAs("touch");
    }, LIVE_MENU_HOLD_MS);
  });
  button.addEventListener("pointerup", () => {
    clearHold();
  });
  button.addEventListener("pointercancel", () => {
    clearHold();
    swallowClick = false;
  });
  // A pen dragged off the icon never receives pointercancel.
  button.addEventListener("pointerleave", () => {
    clearHold();
  });
  button.addEventListener("contextmenu", (event) => {
    event?.preventDefault?.();
  });
  button.addEventListener("click", (event) => {
    if (swallowClick) {
      swallowClick = false;
      event?.preventDefault?.();
      event?.stopPropagation?.();
      return;
    }
    const pendingHold = holdTimer !== 0;
    try {
      Promise.resolve(onClick()).catch(() => {});
    } finally {
      if (pendingHold) swallowClick = false;
    }
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
    menu: options.menu,
    root: options.root || (typeof document === "undefined" ? null : document),
  });
}
