import { publicPrefix } from "./prefix.js";

// How far playback may sit behind the buffered live edge before it jumps.
export const LIVE_LAG = 1.5;
export const LIVE_MARGIN = 0.3;
const QUEUE_LIMIT = 8;
const MIME = "audio/webm;codecs=opus";

export function listenerPathId(pathname, prefix) {
  let path = pathname || "/";
  if (prefix && (path === prefix || path.startsWith(`${prefix}/`))) {
    path = path.slice(prefix.length) || "/";
  }
  const match = path.match(/^\/stream\/([A-Z]{4})\/?$/);
  return match ? match[1] : null;
}

export function liveSocketUrl(protocol, host, prefix, streamId) {
  const ws = protocol === "https:" ? "wss:" : "ws:";
  return `${ws}//${host}${prefix}/api/live/${streamId}`;
}

// null means keep the playhead. A target means jump to the live edge.
export function liveCatchUp(current, start, end, maxLag = LIVE_LAG, margin = LIVE_MARGIN) {
  if (!Number.isFinite(current) || !Number.isFinite(start) || !Number.isFinite(end) || end <= start) return null;
  let target = null;
  if (current + 0.05 < start) target = Math.max(start, end - margin);
  else if (end - current > maxLag) target = Math.max(start, end - margin);
  if (target == null) return null;
  if (target < start) target = start;
  if (target > end) target = end;
  if (Math.abs(target - current) < 0.05) return null;
  return target;
}

function boot() {
  const statusEl = document.querySelector("#stream-status");
  const audio = document.querySelector("#stream-audio");
  const playButton = document.querySelector("#stream-play");
  const idEl = document.querySelector("#stream-id");
  if (!statusEl || !audio || !playButton) return;

  const prefix = publicPrefix();
  const streamId = listenerPathId(location.pathname, prefix);
  if (idEl && streamId && !idEl.textContent) idEl.textContent = streamId;
  if (document.body.dataset.state !== "live" || !streamId) return;

  const socket = new WebSocket(liveSocketUrl(location.protocol, location.host, prefix, streamId));
  socket.binaryType = "arraybuffer";

  let mime = MIME;
  let mediaSource = null;
  let sourceBuffer = null;
  let objectUrl = "";
  const queued = [];
  let appended = false;
  let settled = false;
  let wantEnd = false;
  let playing = false;

  function setStatus(text) {
    statusEl.textContent = text;
  }

  function tryPlay() {
    const pending = audio.play();
    if (pending && typeof pending.catch === "function") {
      pending.catch(() => {
        if (settled) return;
        playButton.hidden = false;
        setStatus("再生を押すと聞こえます");
      });
    }
  }

  function openMedia() {
    if (mediaSource) return;
    if (!("MediaSource" in window) || !MediaSource.isTypeSupported(mime)) {
      settled = true;
      setStatus("このブラウザでは再生できません");
      return;
    }
    mediaSource = new MediaSource();
    objectUrl = URL.createObjectURL(mediaSource);
    audio.src = objectUrl;
    audio.hidden = false;
    mediaSource.addEventListener("sourceopen", () => {
      sourceBuffer = mediaSource.addSourceBuffer(mime);
      try {
        sourceBuffer.mode = "sequence";
      } catch {
        /* segments mode: liveCatchUp seeks to the buffered range */
      }
      sourceBuffer.addEventListener("updateend", () => {
        catchUp();
        pump();
        maybeEnd();
        if (!playing && !settled && audio.buffered.length) tryPlay();
      });
      sourceBuffer.addEventListener("error", () => {
        if (!settled) setStatus("再生できませんでした");
      });
      pump();
    });
  }

  function enqueue(buffer) {
    queued.push(buffer);
    const floor = appended ? 0 : 1;
    while (queued.length > QUEUE_LIMIT + (appended ? 0 : 1)) queued.splice(floor, 1);
    pump();
  }

  function bufferedRange() {
    if (!audio.buffered.length) return null;
    return {
      start: audio.buffered.start(0),
      end: audio.buffered.end(audio.buffered.length - 1),
    };
  }

  function catchUp() {
    const range = bufferedRange();
    if (!range || audio.seeking) return;
    const target = liveCatchUp(audio.currentTime, range.start, range.end);
    if (target == null) return;
    try {
      audio.currentTime = target;
    } catch {
      /* the range can move while seeking */
    }
  }

  function trimIfNeeded() {
    if (!sourceBuffer || sourceBuffer.updating || audio.currentTime <= 20 || !audio.buffered.length) return false;
    const until = audio.currentTime - 10;
    if (until <= audio.buffered.start(0) + 1) return false;
    try {
      sourceBuffer.remove(0, until);
      return true;
    } catch {
      return false;
    }
  }

  function pump() {
    if (!sourceBuffer || sourceBuffer.updating || queued.length === 0) return;
    if (trimIfNeeded()) return;
    const chunk = queued.shift();
    try {
      sourceBuffer.appendBuffer(chunk);
      appended = true;
    } catch (error) {
      if (error && error.name === "QuotaExceededError" && audio.buffered.length) {
        queued.unshift(chunk);
        try {
          sourceBuffer.remove(0, Math.max(audio.buffered.start(0), audio.currentTime - 1));
        } catch {
          if (!settled) setStatus("再生できませんでした");
        }
        return;
      }
      if (!settled) setStatus("再生できませんでした");
    }
  }

  function maybeEnd() {
    if (!wantEnd || !mediaSource || mediaSource.readyState !== "open") return;
    if ((sourceBuffer && sourceBuffer.updating) || queued.length) return;
    try {
      mediaSource.endOfStream();
    } catch {
      /* already ended */
    }
  }

  playButton.addEventListener("click", () => {
    audio.play().then(() => {
      playButton.hidden = true;
    }).catch(() => {});
  });

  audio.addEventListener("playing", () => {
    playing = true;
    playButton.hidden = true;
    if (!settled) setStatus("再生しています");
  });

  socket.addEventListener("message", (event) => {
    if (typeof event.data === "string") {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      if (message.type === "start") {
        if (message.mime) mime = message.mime;
        setStatus("配信を待っています");
        openMedia();
        return;
      }
      if (message.type === "absent") {
        settled = true;
        audio.hidden = true;
        setStatus("この配信はありません");
        return;
      }
      if (message.type === "full") {
        settled = true;
        audio.hidden = true;
        setStatus("いまは聴く人が多いです");
        return;
      }
      if (message.type === "end") {
        settled = true;
        wantEnd = true;
        setStatus("配信が終了しました");
        maybeEnd();
      }
      return;
    }
    enqueue(event.data);
  });

  socket.addEventListener("close", (event) => {
    if (settled) return;
    settled = true;
    if (event.code === 4429) {
      setStatus("いまは聴く人が多いです");
      return;
    }
    if (event.code === 1013) {
      setStatus("受信が遅れたので切れました");
      return;
    }
    if (playing || wantEnd) {
      wantEnd = true;
      setStatus("配信が終了しました");
      maybeEnd();
      return;
    }
    audio.hidden = true;
    setStatus("この配信はありません");
  });
}

if (typeof document !== "undefined" && document.querySelector("#stream-status")) boot();
