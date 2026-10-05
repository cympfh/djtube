import { SOURCE_UNAVAILABLE, createActions, freshState, sourcePlaybackBlocked } from "./actions.js";
import {
  connectController,
  controllerStatusText,
  extinguishFlx4Leds,
  flx4LedPort,
  midiButtonState,
  paintFlx4Leds,
  resumeFlx4Leds,
} from "./controller.js";
import { deckGains } from "./gains.js";
import { eqGainDb, formatEqDb } from "./eq.js";
import { formatFilter } from "./filter.js";
import { formatTime } from "./format.js";
import { bpmText, tempoValueText } from "./bpm.js";
import { formatRate } from "./rate.js";
import { handleKeydown, isSearchTarget, isTypingTarget, legendGroups } from "./keys.js";
import { createDeckPlayer, startDeckAudio } from "./player.js";
import { bindCookies, cookiePanelOpen } from "./cookies.js";
import { discRotationDegrees, discSpinning, discVisible } from "./disc.js";
import { publicPrefix } from "./prefix.js";

const prefix = publicPrefix();
const state = freshState();
const audios = {
  A: createDeckPlayer("A", "player-A"),
  B: createDeckPlayer("B", "player-B"),
};
audios.A.onSeekLanded = () => {
  state.decks.A.jogCommand = null;
};
audios.B.onSeekLanded = () => {
  state.decks.B.jogCommand = null;
};
const searchInput = document.getElementById("search-input");
const playlistName = document.getElementById("playlist-name");

async function fetchJson(path, options = {}) {
  const response = await fetch(`${prefix}${path}`, {
    ...options,
    headers: {
      ...(options.body ? { "content-type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  if (response.status === 204) return null;
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    const detail = body && typeof body.detail === "string" ? body.detail : "";
    throw new Error(detail || "プレイリストを保存できませんでした");
  }
  return body;
}

async function fetchSearch(query, musicOnly = state.musicOnly) {
  const params = new URLSearchParams({ q: query, music: musicOnly ? "true" : "false" });
  const response = await fetch(`${prefix}/api/search?${params}`);
  if (!response.ok) {
    let detail = "";
    try {
      const body = await response.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      detail = "";
    }
    throw new Error(detail || "検索できませんでした");
  }
  return response.json();
}

let frame = 0;
function paintControllerLeds() {
  for (const deck of ["A", "B"]) {
    const deckState = state.decks[deck];
    paintFlx4Leds(flx4LedPort, deck, !!deckState?.playing, !!deckState?.syncing);
  }
}
function scheduleRender() {
  paintControllerLeds();
  if (frame) return;
  frame = requestAnimationFrame(() => {
    frame = 0;
    render();
  });
}

const actions = createActions({
  state,
  audios,
  prefix,
  scheduleRender,
  queryValue: () => searchInput.value,
  isSearchFocused: () => document.activeElement === searchInput,
  focusSearchElement: () => {
    showLibrary("search");
    searchInput.focus();
  },
  blurSearchElement: () => searchInput.blur(),
  fetchSearch,
  fetchPlaylists: () => fetchJson("/api/playlists"),
  createPlaylist: (name) => fetchJson("/api/playlists", { method: "POST", body: JSON.stringify({ name }) }),
  renamePlaylist: (id, name) =>
    fetchJson(`/api/playlists/${id}`, { method: "PATCH", body: JSON.stringify({ name }) }),
  deletePlaylist: (id) => fetchJson(`/api/playlists/${id}`, { method: "DELETE" }),
  addPlaylistTrack: (id, track) =>
    fetchJson(`/api/playlists/${id}/tracks`, { method: "POST", body: JSON.stringify(track) }),
  removePlaylistTrack: (id, index) => fetchJson(`/api/playlists/${id}/tracks/${index}`, { method: "DELETE" }),
  movePlaylistTrack: (id, from, to) =>
    fetchJson(`/api/playlists/${id}/tracks/move`, { method: "POST", body: JSON.stringify({ from, to }) }),
  playlistNameValue: () => playlistName.value,
  setPlaylistNameValue: (value) => {
    playlistName.value = value;
  },
  focusPlaylistNameElement: () => {
    showLibrary("playlist");
    playlistName.focus();
    playlistName.select();
  },
  blurPlaylistNameElement: () => playlistName.blur(),
  confirmDelete: (message) => window.confirm(message),
});

function sourceLabel(source) {
  if (source === "youtube") return "取得元: YouTube Data API";
  if (source === "ytdlp") return "取得元: yt-dlp";
  return "";
}

function deckStatusText(deckState) {
  if (deckState.playError) return deckState.playError;
  if (deckState.status === "empty") return "空";
  if (deckState.status === "preparing") return "プレーヤーを読み込んでいます";
  if (deckState.status === "error") return deckState.error || "読み込めませんでした";
  if (deckState.playing) return "再生中";
  if (deckState.status === "ready") return "準備完了";
  return "";
}

function renderLegend() {
  const root = document.getElementById("key-legend");
  root.replaceChildren();
  for (const group of legendGroups()) {
    const section = document.createElement("section");
    const heading = document.createElement("h2");
    heading.textContent = group.name;
    const list = document.createElement("ul");
    for (const item of group.items) {
      const li = document.createElement("li");
      const kbd = document.createElement("kbd");
      kbd.textContent = item.keys;
      const label = document.createElement("span");
      label.textContent = item.label;
      li.append(kbd, label);
      list.append(li);
    }
    section.append(heading, list);
    root.append(section);
  }
}

let resultPicker = null;
let resultAddMessage = "";

function resultsSignature() {
  return JSON.stringify({
    results: state.results,
    selected: state.selected,
    playlists: state.playlists.map((item) => `${item.id}\t${item.name}`),
    picker: resultPicker,
    note: resultAddMessage,
  });
}

let renderedResults = "";

function renderResults() {
  const list = document.getElementById("results");
  const signature = resultsSignature();
  if (signature === renderedResults) return;
  renderedResults = signature;
  list.replaceChildren();
  state.results.forEach((track, index) => {
    const li = document.createElement("li");
    li.className = index === state.selected ? "is-selected" : "";
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", index === state.selected ? "true" : "false");
    li.id = `result-${index}`;

    if (track.thumbnail) {
      const img = document.createElement("img");
      img.className = "thumb";
      img.alt = "";
      img.src = track.thumbnail;
      li.append(img);
    } else {
      const blank = document.createElement("div");
      blank.className = "thumb";
      blank.textContent = "♪";
      li.append(blank);
    }

    const text = document.createElement("div");
    text.className = "result-copy";
    const title = document.createElement("p");
    title.className = "result-title";
    title.textContent = track.title || track.id;
    const meta = document.createElement("p");
    meta.className = "result-meta";
    const bits = [track.channel, track.duration ? formatTime(track.duration) : ""].filter(Boolean);
    meta.textContent = bits.join(" · ");
    text.append(title, meta);

    const buttons = document.createElement("div");
    buttons.className = "result-actions";
    for (const deck of ["A", "B"]) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = `${deck}へ`;
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        state.selected = index;
        actions.loadSelected(deck);
      });
      buttons.append(button);
    }
    const add = document.createElement("button");
    add.type = "button";
    add.textContent = "プレイリスト追加";
    add.addEventListener("click", (event) => {
      event.stopPropagation();
      state.selected = index;
      if (!state.playlists.length) {
        resultPicker = null;
        resultAddMessage = "プレイリストがありません";
        renderAddNote();
        renderResults();
        return;
      }
      resultAddMessage = "";
      resultPicker = index;
      renderAddNote();
      renderResults();
      list.querySelector(".playlist-add-select")?.focus();
    });
    buttons.append(add);

    li.addEventListener("click", () => {
      state.selected = index;
      renderResults();
    });
    li.append(text, buttons);
    if (resultPicker === index) {
      const select = document.createElement("select");
      select.className = "playlist-add-select";
      select.setAttribute("aria-label", "追加するプレイリスト");
      const blank = document.createElement("option");
      blank.value = "";
      blank.textContent = "プレイリストを選ぶ";
      select.append(blank);
      for (const playlist of state.playlists) {
        const option = document.createElement("option");
        option.value = playlist.id;
        option.textContent = playlist.name;
        select.append(option);
      }
      select.addEventListener("click", (event) => event.stopPropagation());
      select.addEventListener("change", () => {
        const playlistId = select.value;
        if (!playlistId) return;
        resultPicker = null;
        const pending = actions.addTrackToPlaylist(playlistId, track);
        Promise.resolve(pending).then(() => {
          if (state.playlistError) resultAddMessage = state.playlistError;
          renderAddNote();
          renderResults();
        });
      });
      li.append(select);
    }
    list.append(li);
  });
  const selected = list.children[state.selected];
  if (selected) {
    const top = selected.offsetTop;
    const bottom = top + selected.offsetHeight;
    if (top < list.scrollTop) list.scrollTop = top;
    else if (bottom > list.scrollTop + list.clientHeight) list.scrollTop = bottom - list.clientHeight;
  }
  if (selected) list.setAttribute("aria-activedescendant", selected.id);
  else list.removeAttribute("aria-activedescendant");
}

function renderDeck(deck) {
  const deckState = state.decks[deck];
  const root = document.getElementById(`deck-${deck}`);
  root.classList.toggle("is-playing", !!deckState.playing);
  root.classList.toggle("is-busy", deckState.status === "preparing");
  root.classList.toggle("is-error", deckState.status === "error" || !!deckState.playError);
  document.getElementById(`title-${deck}`).textContent = deckState.title || "曲が入っていません";
  document.getElementById(`channel-${deck}`).textContent = deckState.channel || "";
  const picture = document.getElementById(`picture-${deck}`);
  const pictureUrl = deckState.id ? deckState.thumbnail : "";
  if (pictureUrl) {
    if (picture.getAttribute("src") !== pictureUrl) picture.src = pictureUrl;
    picture.hidden = false;
  } else {
    picture.removeAttribute("src");
    picture.hidden = true;
  }
  const status = document.getElementById(`status-${deck}`);
  status.textContent = deckStatusText(deckState);
  const play = document.getElementById(`play-${deck}`);
  play.textContent = deckState.playing ? "一時停止" : "再生";
  play.disabled = sourcePlaybackBlocked(deckState);
  play.classList.toggle("is-on", !!deckState.playing);
  const cue = deckState.cue > 0.05 ? formatTime(deckState.cue) : "先頭";
  document.getElementById(`cue-readout-${deck}`).textContent = `キュー位置 ${cue}`;
  renderTempo(deck);
  renderEq(deck);
  renderFilter(deck);
  renderVolume(deck);
  updateTime(deck);
  paintDisc(deck);
}

function renderTempo(deck) {
  const deckState = state.decks[deck];
  const rate = deckState.rate ?? 1;
  const slider = document.getElementById(`rate-${deck}`);
  const shown = formatRate(rate);
  const value = String(Math.round(rate * 100));
  if (document.activeElement !== slider) slider.value = value;
  slider.setAttribute("aria-valuenow", value);
  slider.setAttribute("aria-valuetext", tempoValueText(deckState));
  document.getElementById(`rate-readout-${deck}`).textContent = shown;
  document.getElementById(`bpm-readout-${deck}`).textContent = bpmText(deckState);
  const sync = document.getElementById(`sync-${deck}`);
  const locked = !!deckState.syncing;
  sync.classList.toggle("is-on", locked);
  sync.setAttribute("aria-pressed", locked ? "true" : "false");
  if (locked) slider.value = value;
}

function renderFilter(deck) {
  const deckState = state.decks[deck];
  const unit = deckState.filter ?? 0.5;
  const slider = document.getElementById(`filter-${deck}`);
  const shown = formatFilter(unit);
  const value = String(Math.round(unit * 100));
  if (document.activeElement !== slider) slider.value = value;
  slider.setAttribute("aria-valuenow", value);
  slider.setAttribute("aria-valuetext", shown);
  document.getElementById(`filter-readout-${deck}`).textContent = shown;
  const error = document.getElementById(`filter-error-${deck}`);
  if (deckState.filterError) {
    error.hidden = false;
    error.textContent = deckState.filterError;
  } else {
    error.hidden = true;
    error.textContent = "";
  }
}

function renderVolume(deck) {
  const volume = state.decks[deck].volume ?? 1;
  const slider = document.getElementById(`volume-${deck}`);
  const shown = `${Math.round(volume * 100)}%`;
  const value = String(Math.round(volume * 100));
  if (document.activeElement !== slider) slider.value = value;
  slider.setAttribute("aria-valuenow", value);
  slider.setAttribute("aria-valuetext", shown);
  document.getElementById(`volume-readout-${deck}`).textContent = shown;
}

function renderEq(deck) {
  const deckState = state.decks[deck];
  for (const band of ["high", "mid", "low"]) {
    const unit = deckState.eq?.[band] ?? 0.5;
    const slider = document.getElementById(`eq-${band}-${deck}`);
    const shown = formatEqDb(eqGainDb(unit));
    const value = String(Math.round(unit * 1000));
    if (document.activeElement !== slider) slider.value = value;
    slider.setAttribute("aria-valuenow", value);
    slider.setAttribute("aria-valuetext", shown);
    document.getElementById(`eq-${band}-readout-${deck}`).textContent = shown;
  }
  const error = document.getElementById(`eq-error-${deck}`);
  if (deckState.eqError) {
    error.hidden = false;
    error.textContent = deckState.eqError;
  } else {
    error.hidden = true;
    error.textContent = "";
  }
}

let discFrame = 0;

function paintDisc(deck) {
  const disc = document.getElementById(`disc-${deck}`);
  const deckState = state.decks[deck];
  const spinning = discSpinning(deckState);
  const held = !!deckState.discHeld && !!deckState.discWasPlaying && !!deckState.id;
  const visible = discVisible(deckState);
  disc.toggleAttribute("hidden", !visible);
  const spin = disc.querySelector(".deck-disc-spin");
  // A hand on the disc keeps the last angle. Playback is what turns it.
  if (held && !spinning) return;
  if (!visible) {
    spin.removeAttribute("transform");
    return;
  }
  // Media time already advances with this deck's playbackRate. Stopped decks keep that angle and do not spin.
  const angle = discRotationDegrees(audios[deck].currentTime || 0);
  spin.setAttribute("transform", `rotate(${angle.toFixed(2)} 50 50)`);
  if (spinning && !discFrame) discFrame = requestAnimationFrame(tickDiscs);
}

function tickDiscs() {
  discFrame = 0;
  let live = false;
  for (const deck of ["A", "B"]) {
    if (!discSpinning(state.decks[deck])) continue;
    live = true;
    paintDisc(deck);
  }
  if (!live) discFrame = 0;
}

function updateTime(deck) {
  actions.syncJog(deck);
  const audio = audios[deck];
  const deckState = state.decks[deck];
  document.getElementById(`time-${deck}`).textContent = formatTime(audio.currentTime || 0);
  const duration = Number.isFinite(audio.duration) ? audio.duration : null;
  document.getElementById(`dur-${deck}`).textContent = formatTime(duration);
  const bar = document.getElementById(`bar-${deck}`);
  const mark = document.getElementById(`cue-${deck}`);
  if (deckState.status === "preparing") return;
  const ratio = duration ? Math.min(1, (audio.currentTime || 0) / duration) : 0;
  bar.style.width = `${ratio * 100}%`;
  if (duration && deckState.cue > 0.05) {
    mark.hidden = false;
    mark.style.left = `${Math.min(100, (deckState.cue / duration) * 100)}%`;
  } else {
    mark.hidden = true;
  }
}

function renderFader() {
  const fader = document.getElementById("fader");
  const value = Math.round(state.crossfader * 1000);
  if (document.activeElement !== fader) fader.value = String(value);
  fader.setAttribute("aria-valuenow", String(value));
  fader.setAttribute("aria-valuetext", `A ${Math.round((1 - state.crossfader) * 100)}、B ${Math.round(state.crossfader * 100)}`);
  document.getElementById("fader-readout").textContent =
    `位置 A ${Math.round((1 - state.crossfader) * 100)} / B ${Math.round(state.crossfader * 100)}`;
  const gains = deckGains(state.crossfader);
  document.getElementById("gain-A").textContent = String(Math.round(gains.a * 100));
  document.getElementById("gain-B").textContent = String(Math.round(gains.b * 100));
}

function renderSearchStatus() {
  const node = document.getElementById("search-status");
  if (state.searching) {
    node.textContent = "検索中…";
    node.classList.remove("is-error");
    return;
  }
  const parts = [];
  if (state.searchError) parts.push(state.searchError);
  const source = sourceLabel(state.source);
  if (source && state.results.length) parts.push(source);
  node.textContent = parts.join(" · ");
  node.classList.toggle("is-error", !!state.searchError && state.searchError !== "見つかりませんでした");
}

let pendingRemove = null;
let sortingTrack = false;

function trackGripElement() {
  const grip = document.createElement("span");
  grip.className = "track-grip";
  grip.title = "ドラッグして順番を変える";
  grip.setAttribute("aria-label", "ドラッグして順番を変える");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 12 18");
  svg.setAttribute("width", "16");
  svg.setAttribute("height", "24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  for (const [cx, cy] of [
    [3.5, 3],
    [8.5, 3],
    [3.5, 9],
    [8.5, 9],
    [3.5, 15],
    [8.5, 15],
  ]) {
    const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    dot.setAttribute("cx", String(cx));
    dot.setAttribute("cy", String(cy));
    dot.setAttribute("r", "1.35");
    svg.append(dot);
  }
  grip.append(svg);
  return grip;
}

function bindTrackReorder(grip, row, fromIndex, trackId) {
  grip.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
  });
  grip.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || sortingTrack || state.playlistBusy) return;
    const list = row.parentElement;
    if (!list || list.id !== "playlist-tracks") return;
    event.preventDefault();
    event.stopPropagation();
    const playlistId = state.playlistId;
    const originY = event.clientY;
    let moved = false;
    let done = false;
    sortingTrack = true;
    state.playlistIndex = fromIndex;
    for (const item of list.children) {
      const selected = item === row;
      item.classList.toggle("is-selected", selected);
      item.setAttribute("aria-selected", selected ? "true" : "false");
    }
    if (row.id) list.setAttribute("aria-activedescendant", row.id);
    row.classList.add("is-dragging");
    row.setAttribute("aria-grabbed", "true");
    list.classList.add("is-sorting");
    document.body.classList.add("is-track-sorting");
    try {
      grip.setPointerCapture(event.pointerId);
    } catch {
      /* the window listeners still receive the release */
    }

    function placeRow(clientY) {
      const bounds = list.getBoundingClientRect();
      const y = Math.min(bounds.bottom - 1, Math.max(bounds.top + 1, clientY));
      const siblings = [...list.children].filter((item) => item !== row);
      let before = null;
      for (const item of siblings) {
        const rect = item.getBoundingClientRect();
        if (y < rect.top + rect.height / 2) {
          before = item;
          break;
        }
      }
      if (before) {
        if (row.nextElementSibling !== before) list.insertBefore(row, before);
      } else if (list.lastElementChild !== row) {
        list.append(row);
      }
    }

    function onMove(ev) {
      if (done) return;
      if (!moved && Math.abs(ev.clientY - originY) <= 4) return;
      moved = true;
      const bounds = list.getBoundingClientRect();
      if (ev.clientY < bounds.top + 24) list.scrollTop -= 12;
      else if (ev.clientY > bounds.bottom - 24) list.scrollTop += 12;
      placeRow(ev.clientY);
    }

    function finish(ev) {
      if (done) return;
      if (ev.type === "mouseup" && ev.button !== 0) return;
      if (ev.pointerId != null && ev.type !== "mouseup" && ev.pointerId !== event.pointerId) return;
      done = true;
      window.removeEventListener("pointermove", onMove, true);
      window.removeEventListener("pointerup", finish, true);
      window.removeEventListener("pointercancel", finish, true);
      window.removeEventListener("mouseup", finish, true);
      window.removeEventListener("keydown", onKey, true);
      sortingTrack = false;
      list.classList.remove("is-sorting");
      document.body.classList.remove("is-track-sorting");
      if (moved) {
        const stopClick = (clickEvent) => {
          clickEvent.preventDefault();
          clickEvent.stopPropagation();
        };
        window.addEventListener("click", stopClick, true);
        setTimeout(() => window.removeEventListener("click", stopClick, true), 0);
      }
      if (moved && typeof ev.clientY === "number" && (ev.type === "pointerup" || ev.type === "mouseup")) placeRow(ev.clientY);
      const order = [...list.children].indexOf(row);
      const released = ev.type === "pointerup" || ev.type === "mouseup";
      const commit = released && moved && state.playlistId === playlistId;
      if (commit) {
        const pending = actions.placePlaylistTrack(fromIndex, order, trackId);
        if (pending) return;
      }
      renderPlaylists();
    }

    function onKey(ev) {
      if (ev.key !== "Escape") return;
      ev.preventDefault();
      ev.stopPropagation();
      moved = false;
      try {
        grip.releasePointerCapture(event.pointerId);
      } catch {
        /* already released */
      }
      finish({ type: "pointercancel" });
    }

    window.addEventListener("pointermove", onMove, true);
    window.addEventListener("pointerup", finish, true);
    window.addEventListener("pointercancel", finish, true);
    window.addEventListener("mouseup", finish, true);
    window.addEventListener("keydown", onKey, true);
  });
}

function renderPlaylists() {
  const select = document.getElementById("playlist-select");
  const signature = state.playlists.map((item) => `${item.id}\t${item.name}`).join("\n");
  if (select.dataset.signature !== signature) {
    select.dataset.signature = signature;
    select.replaceChildren();
    if (!state.playlists.length) {
      const empty = document.createElement("option");
      empty.value = "";
      empty.textContent = "プレイリストはありません";
      select.append(empty);
    }
    for (const playlist of state.playlists) {
      const option = document.createElement("option");
      option.value = playlist.id;
      option.textContent = playlist.name;
      select.append(option);
    }
  }
  select.value = state.playlistId || "";
  select.disabled = state.playlistBusy || state.playlists.length === 0;

  const note = document.getElementById("playlist-name-note");
  note.textContent = state.playlistNaming === "rename" ? "Enter で変える" : "";
  const status = document.getElementById("playlist-status");
  status.textContent = state.playlistError || "";
  status.classList.toggle("is-error", !!state.playlistError);

  const busy = state.playlistBusy;
  const playlist = state.playlists.find((item) => item.id === state.playlistId) || null;
  const canEdit = !!playlist && !busy;
  document.getElementById("playlist-create").disabled = busy;
  document.getElementById("playlist-edit").disabled = !canEdit;
  document.getElementById("playlist-rename").disabled = !canEdit;
  document.getElementById("playlist-delete").disabled = !canEdit;
  const editDialog = document.getElementById("playlist-edit-dialog");
  const editOpen = editDialog.open;
  document.getElementById("playlist-edit").setAttribute("aria-expanded", editOpen ? "true" : "false");
  const editError = document.getElementById("playlist-edit-error");
  editError.textContent = editOpen ? state.playlistError || "" : "";
  editError.classList.toggle("is-error", editOpen && !!state.playlistError);
  const tracks = playlist?.tracks || [];
  const pendingTrack =
    pendingRemove && pendingRemove.playlistId === playlist?.id ? tracks[pendingRemove.index] : null;
  if (!pendingTrack || pendingTrack.id !== pendingRemove?.trackId) pendingRemove = null;
  const list = document.getElementById("playlist-tracks");
  if (sortingTrack) return;
  list.replaceChildren();
  tracks.forEach((track, index) => {
    const li = document.createElement("li");
    li.className = index === state.playlistIndex ? "is-selected" : "";
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", index === state.playlistIndex ? "true" : "false");
    li.id = `playlist-track-${index}`;

    const grip = trackGripElement();
    bindTrackReorder(grip, li, index, track.id);
    li.append(grip);

    if (track.thumbnail) {
      const img = document.createElement("img");
      img.className = "thumb";
      img.alt = "";
      img.draggable = false;
      img.src = track.thumbnail;
      li.append(img);
    } else {
      const blank = document.createElement("div");
      blank.className = "thumb";
      blank.textContent = "♪";
      li.append(blank);
    }

    const text = document.createElement("div");
    text.className = "result-copy";
    const title = document.createElement("p");
    title.className = "result-title";
    title.textContent = track.title || track.id;
    const meta = document.createElement("p");
    meta.className = "result-meta";
    const bits = [track.channel, track.duration ? formatTime(track.duration) : ""].filter(Boolean);
    meta.textContent = bits.join(" · ");
    text.append(title, meta);
    const buttons = document.createElement("div");
    buttons.className = "result-actions";
    for (const deck of ["A", "B"]) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = `${deck}へ`;
      button.disabled = busy;
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        state.playlistIndex = index;
        actions.loadPlaylistTrack(deck);
      });
      buttons.append(button);
    }
    const confirming = pendingRemove?.index === index;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "is-remove";
    remove.textContent = confirming ? "本当に削除？" : "削除";
    remove.disabled = busy;
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      state.playlistIndex = index;
      if (!confirming) {
        pendingRemove = { playlistId: playlist.id, index, trackId: track.id };
        renderPlaylists();
        return;
      }
      pendingRemove = null;
      actions.removePlaylistTrack();
    });
    buttons.append(remove);
    li.addEventListener("click", () => {
      state.playlistIndex = index;
      renderPlaylists();
    });
    li.append(text, buttons);
    list.append(li);
  });
  const selected = list.children[state.playlistIndex];
  if (selected) {
    const top = selected.offsetTop;
    const bottom = top + selected.offsetHeight;
    if (top < list.scrollTop) list.scrollTop = top;
    else if (bottom > list.scrollTop + list.clientHeight) list.scrollTop = bottom - list.clientHeight;
    list.setAttribute("aria-activedescendant", selected.id);
  } else {
    list.removeAttribute("aria-activedescendant");
  }
}

function renderAddNote() {
  const note = document.getElementById("playlist-add-note");
  note.textContent = resultAddMessage;
  note.classList.toggle("is-error", !!resultAddMessage);
}

function showLibrary(name) {
  state.library = name === "playlist" ? "playlist" : "search";
  const search = name !== "playlist";
  document.getElementById("search-panel").hidden = !search;
  document.getElementById("playlist-panel").hidden = search;
  document.getElementById("tab-search").setAttribute("aria-selected", search ? "true" : "false");
  document.getElementById("tab-playlist").setAttribute("aria-selected", search ? "false" : "true");
  if (search && document.activeElement === playlistName) playlistName.blur();
  if (!search && document.activeElement === searchInput) searchInput.blur();
}

function render() {
  renderSearchStatus();
  renderAddNote();
  renderResults();
  renderPlaylists();
  renderDeck("A");
  renderDeck("B");
  renderFader();
  cookiesUi.setOpen(cookiePanelOpen(state.decks));
  document.getElementById("search-button").disabled = state.searching;
  document.getElementById("search-button").textContent = state.searching ? "検索中" : "検索";
  const music = document.getElementById("music-only");
  if (music) music.checked = state.musicOnly;
}

renderLegend();

document.getElementById("search-button").addEventListener("click", () => actions.submitSearch());
document.getElementById("tab-search").addEventListener("click", () => showLibrary("search"));
document.getElementById("tab-playlist").addEventListener("click", () => showLibrary("playlist"));
document.getElementById("playlist-create").addEventListener("click", () => actions.createPlaylist());
const playlistEditDialog = document.getElementById("playlist-edit-dialog");
const playlistRenameName = document.getElementById("playlist-rename-name");

function openPlaylistEdit() {
  const playlist = state.playlists.find((item) => item.id === state.playlistId);
  if (!playlist || state.playlistBusy || playlistEditDialog.open) return;
  playlistRenameName.value = playlist.name;
  state.playlistError = "";
  playlistEditDialog.showModal();
  playlistRenameName.focus();
  playlistRenameName.select();
  scheduleRender();
}

async function submitPlaylistRename() {
  if (!playlistEditDialog.open || state.playlistBusy) return;
  const pending = actions.renamePlaylist(playlistRenameName.value);
  if (pending) await pending;
  if (playlistEditDialog.open && !state.playlistError) playlistEditDialog.close();
}

async function submitPlaylistDelete() {
  if (!playlistEditDialog.open || state.playlistBusy) return;
  const id = state.playlistId;
  if (!id) return;
  const pending = actions.deletePlaylist();
  if (pending) await pending;
  const removed = !state.playlistError && !state.playlists.some((item) => item.id === id);
  if (playlistEditDialog.open && removed) playlistEditDialog.close();
}

document.getElementById("playlist-edit").addEventListener("click", openPlaylistEdit);
document.getElementById("playlist-rename").addEventListener("click", () => {
  submitPlaylistRename();
});
document.getElementById("playlist-delete").addEventListener("click", () => {
  submitPlaylistDelete();
});
document.getElementById("playlist-edit-close").addEventListener("click", () => playlistEditDialog.close());
playlistEditDialog.addEventListener("click", (event) => {
  if (event.target === playlistEditDialog) playlistEditDialog.close();
});
playlistRenameName.addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  event.preventDefault();
  submitPlaylistRename();
});
document.getElementById("playlist-select").addEventListener("change", (event) => {
  actions.selectPlaylist(event.target.value);
});
playlistName.addEventListener("blur", () => {
  if (state.playlistNaming !== "create") {
    state.playlistNaming = "create";
    scheduleRender();
  }
});
document.getElementById("music-only").addEventListener("change", (event) => {
  actions.setMusicOnly(event.target.checked);
});
document.getElementById("play-A").addEventListener("click", () => actions.togglePlay("A"));
document.getElementById("play-B").addEventListener("click", () => actions.togglePlay("B"));
for (const deck of ["A", "B"]) {
  const disc = document.getElementById(`disc-${deck}`);
  disc.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    const pointerId = event.pointerId;
    actions.pressDisc(deck, true);
    const end = (ev) => {
      if (ev.pointerId !== pointerId) return;
      window.removeEventListener("pointerup", end, true);
      window.removeEventListener("pointercancel", end, true);
      actions.pressDisc(deck, false);
    };
    window.addEventListener("pointerup", end, true);
    window.addEventListener("pointercancel", end, true);
  });
}
document.getElementById("cue-btn-A").addEventListener("click", () => actions.cue("A"));
document.getElementById("cue-btn-B").addEventListener("click", () => actions.cue("B"));
for (const deck of ["A", "B"]) {
  const slider = document.getElementById(`rate-${deck}`);
  slider.addEventListener("input", () => {
    actions.setRate(deck, Number(slider.value) / 100);
  });
  document.getElementById(`rate-reset-${deck}`).addEventListener("click", () => {
    actions.resetRate(deck);
  });
  document.getElementById(`sync-${deck}`).addEventListener("click", () => {
    actions.syncBeat(deck);
  });
  const volume = document.getElementById(`volume-${deck}`);
  volume.addEventListener("input", () => {
    actions.setVolume(deck, Number(volume.value) / 100);
  });
  document.getElementById(`volume-reset-${deck}`).addEventListener("click", () => {
    actions.resetVolume(deck);
  });
  const filter = document.getElementById(`filter-${deck}`);
  filter.addEventListener("input", () => {
    actions.setFilter(deck, Number(filter.value) / 100);
  });
  document.getElementById(`filter-reset-${deck}`).addEventListener("click", () => {
    actions.resetFilter(deck);
  });
  for (const band of ["high", "mid", "low"]) {
    const slider = document.getElementById(`eq-${band}-${deck}`);
    slider.addEventListener("input", () => {
      actions.setEq(deck, band, Number(slider.value) / 1000);
    });
    document.getElementById(`eq-${band}-reset-${deck}`).addEventListener("click", () => {
      actions.resetEq(deck, band);
    });
  }
}
const fader = document.getElementById("fader");
fader.addEventListener("input", () => {
  actions.setCrossfader(Number(fader.value) / 1000);
});

for (const deck of ["A", "B"]) {
  const audio = audios[deck];
  document.getElementById(`pos-${deck}`).addEventListener("click", (event) => {
    if (!Number.isFinite(audio.duration) || audio.duration <= 0) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const ratio = (event.clientX - rect.left) / rect.width;
    actions.seek(deck, ratio * audio.duration);
    updateTime(deck);
  });
}

window.addEventListener(
  "keydown",
  (event) => {
    if (playlistEditDialog.open) return;
    if (
      sortingTrack &&
      (event.key === "5" || event.key === "6") &&
      !event.shiftKey &&
      !event.metaKey &&
      !event.ctrlKey &&
      !event.altKey &&
      !isSearchTarget(event.target) &&
      !isTypingTarget(event.target)
    ) {
      event.preventDefault();
      return;
    }
    handleKeydown(event, actions, state.library);
  },
  true,
);

function showMidiStatus(status) {
  const node = document.getElementById("midi-status");
  const text = controllerStatusText(status);
  if (node.textContent !== text) node.textContent = text;
  node.classList.toggle("is-connected", !!(status?.connected || (status?.state === "open" && status.names?.length)));
}

const midiButton = document.getElementById("midi-button");

function paintMidiButton(status) {
  midiButton.dataset.midi = midiButtonState(status);
}

midiButton.addEventListener("click", () => {
  const opening = { state: "opening", names: [], connected: false };
  showMidiStatus(opening);
  paintMidiButton(opening);
  connectController(actions, (status) => {
    paintMidiButton(status);
    showMidiStatus(status);
  });
});

const cookiesUi = bindCookies(prefix);
actions.loadPlaylists();

fetch(`${prefix}/api/health`)
  .then((response) => (response.ok ? response.json() : null))
  .then((body) => {
    const node = document.getElementById("health-search");
    if (!body) {
      node.textContent = "サーバに接続できません";
      return;
    }
    node.textContent = body.search === "youtube" ? "検索: YouTube Data API" : "検索: yt-dlp（APIキーなし）";
  })
  .catch(() => {
    document.getElementById("health-search").textContent = "サーバに接続できません";
  });

actions.setCrossfader(state.crossfader);
startDeckAudio(audios, {
  onReady(deck) {
    actions.onDeckReady(deck);
  },
  onPlaying(deck) {
    const deckState = state.decks[deck];
    if (!deckState.id) return;
    deckState.playing = true;
    deckState.playError = "";
    deckState.error = "";
    deckState.cookies = false;
    if (deckState.status !== "error") deckState.status = "ready";
    updateTime(deck);
    renderDeck(deck);
    paintControllerLeds();
  },
  onPaused(deck) {
    const deckState = state.decks[deck];
    if (!deckState.id) return;
    deckState.playing = false;
    updateTime(deck);
    renderDeck(deck);
    paintControllerLeds();
  },
  onEnded(deck) {
    const deckState = state.decks[deck];
    if (!deckState.id) return;
    deckState.playing = false;
    updateTime(deck);
    renderDeck(deck);
    paintControllerLeds();
  },
  onError(deck) {
    const deckState = state.decks[deck];
    if (!deckState.id) return;
    actions.onDeckError(deck);
    const videoId = deckState.id;
    deckState.status = "error";
    deckState.playing = false;
    deckState.error = SOURCE_UNAVAILABLE;
    deckState.cookies = false;
    renderDeck(deck);
    paintControllerLeds();
    fetch(`${prefix}/api/audio/${encodeURIComponent(videoId)}/cause`)
      .then((response) => (response.ok ? response.json() : null))
      .then((body) => {
        if (state.decks[deck].id !== videoId) return;
        state.decks[deck].cookies = body?.cookies === true;
        scheduleRender();
      })
      .catch(() => {
        if (state.decks[deck].id !== videoId) return;
        state.decks[deck].cookies = false;
        scheduleRender();
      });
  },
});
setInterval(() => {
  if (state.decks.A.playing) updateTime("A");
  if (state.decks.B.playing) updateTime("B");
}, 250);
render();
paintControllerLeds();
window.addEventListener("pagehide", () => {
  extinguishFlx4Leds(flx4LedPort);
});
window.addEventListener("pageshow", () => {
  resumeFlx4Leds(flx4LedPort);
});

window.djtube = { actions, state, audios };
