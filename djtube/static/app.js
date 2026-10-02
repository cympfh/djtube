import { createActions, freshState } from "./actions.js";
import { connectController } from "./controller.js";
import { deckGains } from "./gains.js";
import { formatTime } from "./format.js";
import { handleKeydown, legendGroups } from "./keys.js";
import { publicPrefix } from "./prefix.js";
import { createDeckPlayer, startYoutubeDecks } from "./youtube.js";

const prefix = publicPrefix();
const state = freshState();
const audios = {
  A: createDeckPlayer("A", "yt-A"),
  B: createDeckPlayer("B", "yt-B"),
};
audios.A.onSeekLanded = () => {
  state.decks.A.jogCommand = null;
};
audios.B.onSeekLanded = () => {
  state.decks.B.jogCommand = null;
};
const searchInput = document.getElementById("search-input");

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
function scheduleRender() {
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
  focusSearchElement: () => searchInput.focus(),
  blurSearchElement: () => searchInput.blur(),
  fetchSearch,
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

function renderResults() {
  const list = document.getElementById("results");
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

    li.addEventListener("click", () => {
      state.selected = index;
      renderResults();
    });
    li.append(text, buttons);
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
  root.classList.toggle("is-target", state.loadTarget === deck);
  root.classList.toggle("is-playing", !!deckState.playing);
  root.classList.toggle("is-busy", deckState.status === "preparing");
  root.classList.toggle("is-error", deckState.status === "error" || !!deckState.playError);
  document.getElementById(`title-${deck}`).textContent = deckState.title || "曲が入っていません";
  document.getElementById(`channel-${deck}`).textContent = deckState.channel || "";
  const status = document.getElementById(`status-${deck}`);
  status.textContent = deckStatusText(deckState);
  const play = document.getElementById(`play-${deck}`);
  play.textContent = deckState.playing ? "一時停止" : "再生";
  play.classList.toggle("is-on", !!deckState.playing);
  const cue = deckState.cue > 0.05 ? formatTime(deckState.cue) : "先頭";
  document.getElementById(`cue-readout-${deck}`).textContent = `キュー位置 ${cue}`;
  updateTime(deck);
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
  document.getElementById("target-A").setAttribute("aria-pressed", state.loadTarget === "A" ? "true" : "false");
  document.getElementById("target-B").setAttribute("aria-pressed", state.loadTarget === "B" ? "true" : "false");
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

function render() {
  renderSearchStatus();
  renderResults();
  renderDeck("A");
  renderDeck("B");
  renderFader();
  document.getElementById("search-button").disabled = state.searching;
  document.getElementById("search-button").textContent = state.searching ? "検索中" : "検索";
  const music = document.getElementById("music-only");
  if (music) music.checked = state.musicOnly;
}

renderLegend();

document.getElementById("search-button").addEventListener("click", () => actions.submitSearch());
document.getElementById("music-only").addEventListener("change", (event) => {
  actions.setMusicOnly(event.target.checked);
});
document.getElementById("play-A").addEventListener("click", () => actions.togglePlay("A"));
document.getElementById("play-B").addEventListener("click", () => actions.togglePlay("B"));
document.getElementById("cue-btn-A").addEventListener("click", () => actions.cue("A"));
document.getElementById("cue-btn-B").addEventListener("click", () => actions.cue("B"));
document.getElementById("target-A").addEventListener("click", () => {
  state.loadTarget = "A";
  scheduleRender();
});
document.getElementById("target-B").addEventListener("click", () => {
  state.loadTarget = "B";
  scheduleRender();
});

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

window.addEventListener("keydown", (event) => handleKeydown(event, actions), true);

function midiStatusText(status) {
  if (!status || status.state === "idle") return "DDJ-FLX4 の割り当ては未実装です。";
  if (status.state === "unsupported") return "このブラウザは Web MIDI に未対応です。DDJ-FLX4 の割り当ては未実装です。";
  if (status.state === "denied") return "MIDI の使用が拒否されました。割り当ては未実装です。";
  const names = status.names?.length ? status.names.join("、") : "入力なし";
  return `MIDI 入力: ${names}。割り当て ${status.mapped || 0} 件。未割り当て信号 ${status.ignored || 0} 件。DDJ-FLX4 のマップは未実装です。`;
}

const midiButton = document.getElementById("midi-button");

function paintMidiButton(status) {
  midiButton.dataset.midi = status && status.state === "open" ? "on" : "off";
}

midiButton.addEventListener("click", () => {
  const node = document.getElementById("midi-status");
  node.textContent = "MIDI を開いています…";
  midiButton.dataset.midi = "wait";
  connectController(actions, (status) => {
    paintMidiButton(status);
    node.textContent = midiStatusText(status);
  });
});

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
startYoutubeDecks(audios, {
  onReady(deck) {
    if (state.decks[deck].id && state.decks[deck].status === "preparing") {
      state.decks[deck].status = "ready";
      scheduleRender();
    }
  },
  onState(deck, code) {
    const deckState = state.decks[deck];
    if (!deckState.id) return;
    audios[deck].paused = code !== 1;
    if ((code === 1 || code === 2 || code === 5) && deckState.status !== "error") deckState.status = "ready";
    if (code === 1) {
      deckState.playing = true;
      deckState.playError = "";
      deckState.error = "";
      deckState.status = "ready";
    }
    if (code === 0 || code === 2) deckState.playing = false;
    updateTime(deck);
    renderDeck(deck);
  },
  onError(deck, code) {
    const deckState = state.decks[deck];
    deckState.status = "error";
    deckState.playing = false;
    deckState.playerCode = code;
    deckState.error = code === 101 || code === 150 || code === 153 ? "この動画は埋め込み再生できません" : "動画を再生できません";
    renderDeck(deck);
  },
});
setInterval(() => {
  if (state.decks.A.playing) updateTime("A");
  if (state.decks.B.playing) updateTime("B");
}, 250);
setTimeout(() => {
  if (!audios.A.apiReady && !audios.B.apiReady) {
    const node = document.getElementById("health-search");
    if (!node.textContent.includes("プレーヤー")) node.textContent = "YouTube プレーヤーを読み込めませんでした";
  }
}, 8000);
render();

window.djtube = { actions, state, audios };
