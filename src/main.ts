import './style.css';
import { clampCrossfader, crossfaderLabel, equalPowerGains, nudgeCrossfader } from './crossfader';
import { dispatch, setActionHandler, type Action, type DeckId, type DeckTarget } from './dispatch';
import { formatClock, formatDuration } from './format';
import { bindKeyboard } from './input/bind-keyboard';
import type { KeyContext } from './input/keyboard';
import { handleControllerMessage, listenToMidi } from './input/controller';
import { searchTracks, type Track } from './search';
import { DeckPlayer, loadYouTubeApi, YT_STATE } from './youtube';

type Phase = 'empty' | 'loading' | 'cued' | 'paused' | 'playing' | 'error';

type DeckModel = {
  videoId: string | null;
  title: string;
  channel: string;
  cuePoint: number;
  phase: Phase;
  error: string;
  durationHint: number | null;
  holdingCue: boolean;
};

const PHASE_LABEL: Record<Phase, string> = {
  empty: '空',
  loading: '読込中',
  cued: 'キュー',
  paused: '停止',
  playing: '再生中',
  error: '不可',
};

const decks: Record<DeckId, DeckModel> = {
  A: freshDeck(),
  B: freshDeck(),
};

let target: DeckId = 'A';
let crossfader = 0.5;
let results: Track[] = [];
let selected = -1;
let searchSeq = 0;
let searchAbort: AbortController | null = null;
let midiCount = 0;
let reclaimUntil = 0;

const app = must<HTMLElement>('app');
const searchForm = must<HTMLFormElement>('search-form');
const searchInput = must<HTMLInputElement>('search');
const statusEl = must<HTMLElement>('status');
const resultsEl = must<HTMLElement>('results');
const emptyEl = must<HTMLElement>('results-empty');
const modeEl = must<HTMLElement>('mode-hint');
const slider = must<HTMLInputElement>('crossfader');
const faderReadout = must<HTMLElement>('fader-readout');
const midiStatus = must<HTMLElement>('midi-status');
const midiButton = must<HTMLButtonElement>('midi-enable');
const apiError = must<HTMLElement>('api-error');

const players: Record<DeckId, DeckPlayer> = {
  A: new DeckPlayer('player-a', hooksFor('A')),
  B: new DeckPlayer('player-b', hooksFor('B')),
};

function freshDeck(): DeckModel {
  return {
    videoId: null,
    title: '',
    channel: '',
    cuePoint: 0,
    phase: 'empty',
    error: '',
    durationHint: null,
    holdingCue: false,
  };
}

function must<T extends HTMLElement>(id: string): T {
  const node = document.getElementById(id);
  if (!node) throw new Error(`missing #${id}`);
  return node as T;
}

function suffix(deck: DeckId): 'a' | 'b' {
  return deck === 'A' ? 'a' : 'b';
}

function resolveDeck(deck: DeckTarget): DeckId {
  return deck === 'target' ? target : deck;
}

function hooksFor(deck: DeckId): ConstructorParameters<typeof DeckPlayer>[1] {
  return {
    onReady: () => applyVolumes(),
    onState: (state) => onPlayerState(deck, state),
    onError: (code) => onPlayerError(deck, code),
  };
}

function keyContext(): KeyContext {
  const active = document.activeElement;
  return {
    searchFocused: active === searchInput,
    buttonFocused: active instanceof HTMLButtonElement,
    sliderFocused: active === slider,
  };
}

function say(text: string): void {
  statusEl.textContent = text;
}

function reclaimForABit(): void {
  reclaimUntil = performance.now() + 900;
  const tick = () => {
    if (performance.now() > reclaimUntil) return;
    const active = document.activeElement;
    if (active instanceof HTMLIFrameElement) {
      active.blur();
      app.focus();
    }
    window.requestAnimationFrame(tick);
  };
  players.A.releaseFocus();
  players.B.releaseFocus();
  window.requestAnimationFrame(tick);
}

function applyVolumes(): void {
  const gains = equalPowerGains(crossfader);
  players.A.setVolume(gains.a * 100);
  players.B.setVolume(gains.b * 100);
  faderReadout.textContent = crossfaderLabel(crossfader);
  slider.value = String(crossfader);
  slider.setAttribute('aria-valuetext', faderReadout.textContent ?? '');
}

function renderTarget(): void {
  for (const deck of ['A', 'B'] as const) {
    const button = must<HTMLButtonElement>(`target-${suffix(deck)}`);
    const card = must<HTMLElement>(`deck-${suffix(deck)}`);
    const on = target === deck;
    button.setAttribute('aria-pressed', on ? 'true' : 'false');
    card.classList.toggle('is-target', on);
  }
  resultsEl.classList.toggle('target-a', target === 'A');
  resultsEl.classList.toggle('target-b', target === 'B');
}

function renderDeck(deck: DeckId): void {
  const model = decks[deck];
  const id = suffix(deck);
  const title = must<HTMLElement>(`title-${id}`);
  const channel = must<HTMLElement>(`channel-${id}`);
  const state = must<HTMLElement>(`state-${id}`);
  const cue = must<HTMLElement>(`cue-${id}`);
  const error = must<HTMLElement>(`error-${id}`);
  const open = must<HTMLAnchorElement>(`open-${id}`);
  const play = must<HTMLButtonElement>(`play-${id}`);
  const cueBtn = must<HTMLButtonElement>(`cue-btn-${id}`);
  const markBtn = must<HTMLButtonElement>(`mark-btn-${id}`);
  const led = must<HTMLElement>(`led-${id}`);

  title.textContent = model.title || '曲がロードされていません';
  channel.textContent = model.channel;
  state.textContent = PHASE_LABEL[model.phase];
  cue.textContent = model.videoId ? formatClock(model.cuePoint) : '--:--';
  error.textContent = model.error;
  led.classList.toggle('on', model.phase === 'playing');
  play.textContent = model.phase === 'playing' ? '停止' : '再生';
  play.setAttribute('aria-pressed', model.phase === 'playing' ? 'true' : 'false');
  const locked = !model.videoId || model.phase === 'error';
  play.disabled = locked;
  cueBtn.disabled = !model.videoId;
  markBtn.disabled = !model.videoId;
  if (model.videoId) {
    open.hidden = false;
    open.href = `https://www.youtube.com/watch?v=${encodeURIComponent(model.videoId)}`;
  } else {
    open.hidden = true;
    open.removeAttribute('href');
  }
  must<HTMLElement>(`deck-${id}`).dataset.phase = model.phase;
  must<HTMLElement>(`deck-${id}`).dataset.videoId = model.videoId ?? '';
  paintTime(deck);
}

function paintTime(deck: DeckId): void {
  const model = decks[deck];
  const id = suffix(deck);
  const time = must<HTMLElement>(`time-${id}`);
  const dur = must<HTMLElement>(`dur-${id}`);
  const bar = must<HTMLElement>(`prog-${id}`);
  if (!model.videoId) {
    time.textContent = '--:--';
    dur.textContent = '--:--';
    bar.style.width = '0%';
    return;
  }
  const current = players[deck].getTime();
  const reported = players[deck].getDuration();
  const duration = reported > 0 ? reported : model.durationHint && model.durationHint > 0 ? model.durationHint : 0;
  time.textContent = formatClock(current);
  dur.textContent = duration > 0 ? formatClock(duration) : formatDuration(model.durationHint) || '--:--';
  const ratio = duration > 0 ? Math.min(100, (current / duration) * 100) : 0;
  bar.style.width = `${ratio}%`;
}

function renderResults(): void {
  emptyEl.hidden = results.length > 0;
  if (!results.length) {
    resultsEl.replaceChildren();
    searchInput.setAttribute('aria-activedescendant', '');
    return;
  }
  const nodes = results.map((track, index) => {
    const li = document.createElement('li');
    li.id = `result-${index}`;
    li.dataset.index = String(index);
    li.setAttribute('role', 'option');
    const on = index === selected;
    li.setAttribute('aria-selected', on ? 'true' : 'false');
    li.classList.toggle('selected', on);

    const img = document.createElement('img');
    img.src = track.thumbnail;
    img.alt = '';
    img.width = 96;
    img.height = 54;
    img.addEventListener('error', () => {
      img.remove();
    });

    const body = document.createElement('div');
    body.className = 'result-body';
    const title = document.createElement('p');
    title.className = 'result-title';
    title.textContent = track.title;
    const meta = document.createElement('p');
    meta.className = 'result-meta';
    const duration = formatDuration(track.durationSec);
    meta.textContent = duration ? `${track.channel} · ${duration}` : track.channel;
    body.append(title, meta);

    const actions = document.createElement('div');
    actions.className = 'result-actions';
    actions.append(loadButton('A', index), loadButton('B', index));
    li.append(img, body, actions);
    return li;
  });
  resultsEl.replaceChildren(...nodes);
  const current = selected >= 0 ? document.getElementById(`result-${selected}`) : null;
  current?.scrollIntoView({ block: 'nearest' });
  searchInput.setAttribute('aria-activedescendant', current ? current.id : '');
}

function loadButton(deck: DeckId, index: number): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  button.dataset.load = deck;
  button.dataset.index = String(index);
  button.tabIndex = -1;
  button.textContent = deck;
  button.setAttribute('aria-label', `デッキ${deck}へロード`);
  return button;
}

function updateSelection(): void {
  const items = resultsEl.querySelectorAll<HTMLElement>('li[data-index]');
  items.forEach((item) => {
    const on = Number(item.dataset.index) === selected;
    item.classList.toggle('selected', on);
    item.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  if (selected >= 0) {
    const current = document.getElementById(`result-${selected}`);
    current?.scrollIntoView({ block: 'nearest' });
    searchInput.setAttribute('aria-activedescendant', current?.id ?? '');
  }
}

function syncMode(): void {
  const typing = document.activeElement === searchInput;
  modeEl.textContent = typing
    ? '検索入力中。Enter で検索すると、A / B などのショートカットに戻ります。'
    : 'ショートカット可。/ で検索欄へ。Space はロード先の再生 / 停止。';
  searchInput.setAttribute('aria-expanded', results.length > 0 ? 'true' : 'false');
}

function selectedTrack(): Track | null {
  if (selected < 0 || selected >= results.length) return null;
  return results[selected] ?? null;
}

function loadDeck(deck: DeckId): void {
  const track = selectedTrack();
  if (!track) {
    say('先に検索結果を選んでください');
    return;
  }
  const model = decks[deck];
  model.videoId = track.videoId;
  model.title = track.title;
  model.channel = track.channel;
  model.cuePoint = 0;
  model.phase = 'loading';
  model.error = '';
  model.durationHint = track.durationSec;
  model.holdingCue = false;
  players[deck].cue(track.videoId);
  applyVolumes();
  renderDeck(deck);
  say(`デッキ${deck}にロード: ${track.title}`);
  reclaimForABit();
}

function playPause(deck: DeckId): void {
  const model = decks[deck];
  if (!model.videoId || model.phase === 'error') return;
  model.holdingCue = false;
  const state = players[deck].getState();
  if (state === YT_STATE.PLAYING || state === YT_STATE.BUFFERING) {
    players[deck].pause();
    model.phase = 'paused';
  } else {
    players[deck].play();
    model.phase = 'playing';
  }
  renderDeck(deck);
  reclaimForABit();
}

function cueDeck(deck: DeckId): void {
  const model = decks[deck];
  if (!model.videoId) return;
  model.holdingCue = true;
  model.phase = 'cued';
  players[deck].seek(model.cuePoint);
  players[deck].pause();
  renderDeck(deck);
  say(`デッキ${deck}をキュー ${formatClock(model.cuePoint)} へ`);
  reclaimForABit();
}

function markCue(deck: DeckId): void {
  const model = decks[deck];
  if (!model.videoId) return;
  model.cuePoint = Math.max(0, players[deck].getTime());
  renderDeck(deck);
  say(`デッキ${deck}のキュー地点を ${formatClock(model.cuePoint)} に設定`);
}

function onPlayerState(deck: DeckId, state: number): void {
  const model = decks[deck];
  if (model.holdingCue && (state === YT_STATE.PLAYING || state === YT_STATE.BUFFERING)) {
    players[deck].pause();
    return;
  }
  if (state === YT_STATE.PAUSED || state === YT_STATE.CUED || state === YT_STATE.ENDED) {
    model.holdingCue = false;
  }
  if (!model.videoId || model.phase === 'error') return;
  if (state === YT_STATE.PLAYING || state === YT_STATE.BUFFERING) model.phase = 'playing';
  else if (state === YT_STATE.CUED) model.phase = 'cued';
  else if (state === YT_STATE.PAUSED || state === YT_STATE.ENDED) model.phase = 'paused';
  renderDeck(deck);
}

function onPlayerError(deck: DeckId, code: number): void {
  const model = decks[deck];
  model.phase = 'error';
  model.holdingCue = false;
  model.error =
    code === 101 || code === 150
      ? 'YouTubeがこの再生を拒否しました'
      : 'この動画を再生できません';
  renderDeck(deck);
  say(`デッキ${deck}: ${model.error}`);
}

async function submitSearch(): Promise<void> {
  const query = searchInput.value.trim();
  if (!query) {
    say('検索語を入力してください');
    return;
  }
  searchAbort?.abort();
  const abort = new AbortController();
  searchAbort = abort;
  const seq = ++searchSeq;
  say(`「${query}」を検索中…`);
  app.focus();
  try {
    const tracks = await searchTracks(query, abort.signal);
    if (seq !== searchSeq) return;
    results = tracks;
    selected = tracks.length ? 0 : -1;
    emptyEl.textContent = '該当する動画がありません';
    renderResults();
    say(tracks.length ? `${tracks.length}件` : '該当する動画がありません');
  } catch {
    if (seq !== searchSeq || abort.signal.aborted) return;
    say('検索に失敗しました。しばらくして再試行してください');
  }
}

function apply(action: Action): void {
  switch (action.type) {
    case 'search.focus':
      searchInput.focus();
      searchInput.select();
      break;
    case 'search.blur':
      players.A.releaseFocus();
      players.B.releaseFocus();
      app.focus();
      break;
    case 'search.submit':
      void submitSearch();
      break;
    case 'results.move': {
      if (!results.length) break;
      const start = selected < 0 ? 0 : selected;
      const next = Math.min(results.length - 1, Math.max(0, start + action.delta));
      selected = next;
      updateSelection();
      break;
    }
    case 'results.select':
      if (action.index >= 0 && action.index < results.length) {
        selected = action.index;
        updateSelection();
      }
      break;
    case 'deck.load':
      loadDeck(resolveDeck(action.deck));
      break;
    case 'deck.setTarget':
      target = action.deck;
      renderTarget();
      say(`次のロード先はデッキ${target}`);
      break;
    case 'deck.toggleTarget':
      target = target === 'A' ? 'B' : 'A';
      renderTarget();
      say(`次のロード先はデッキ${target}`);
      break;
    case 'deck.playPause':
      playPause(resolveDeck(action.deck));
      break;
    case 'deck.cue':
      cueDeck(action.deck);
      break;
    case 'deck.setCue':
      markCue(action.deck);
      break;
    case 'crossfader.nudge':
      crossfader = nudgeCrossfader(crossfader, action.delta);
      applyVolumes();
      break;
    case 'crossfader.set':
      crossfader = clampCrossfader(action.value);
      applyVolumes();
      break;
    default: {
      const unreachable: never = action;
      throw new Error(`unknown action ${JSON.stringify(unreachable)}`);
    }
  }
  syncMode();
}

function bindUi(): void {
  setActionHandler(apply);
  bindKeyboard(keyContext);

  searchForm.addEventListener('submit', (event) => {
    event.preventDefault();
    dispatch({ type: 'search.submit' });
  });

  resultsEl.addEventListener('click', (event) => {
    const targetNode = event.target;
    if (!(targetNode instanceof Element)) return;
    const item = targetNode.closest<HTMLElement>('li[data-index]');
    if (!item) return;
    const index = Number(item.dataset.index);
    const button = targetNode.closest<HTMLElement>('button[data-load]');
    dispatch({ type: 'results.select', index });
    const which = button?.dataset.load;
    if (which === 'A' || which === 'B') dispatch({ type: 'deck.load', deck: which });
  });

  resultsEl.addEventListener('dblclick', (event) => {
    const targetNode = event.target;
    if (!(targetNode instanceof Element) || targetNode.closest('button')) return;
    if (!targetNode.closest('li[data-index]')) return;
    dispatch({ type: 'deck.load', deck: 'target' });
  });

  for (const deck of ['A', 'B'] as const) {
    const id = suffix(deck);
    must<HTMLButtonElement>(`target-${id}`).addEventListener('click', () => {
      dispatch({ type: 'deck.setTarget', deck });
    });
    must<HTMLButtonElement>(`play-${id}`).addEventListener('click', () => {
      dispatch({ type: 'deck.playPause', deck });
    });
    must<HTMLButtonElement>(`cue-btn-${id}`).addEventListener('click', () => {
      dispatch({ type: 'deck.cue', deck });
    });
    must<HTMLButtonElement>(`mark-btn-${id}`).addEventListener('click', () => {
      dispatch({ type: 'deck.setCue', deck });
    });
  }

  slider.addEventListener('input', () => {
    dispatch({ type: 'crossfader.set', value: Number(slider.value) });
  });

  app.addEventListener('click', (event) => {
    const node = event.target;
    if (!(node instanceof Element) || !node.closest('button')) return;
    queueMicrotask(() => {
      if (document.activeElement instanceof HTMLButtonElement) app.focus();
    });
  });

  document.addEventListener('focusin', syncMode);
  document.addEventListener('focusout', () => {
    window.setTimeout(syncMode, 0);
  });

  // YouTube focuses its iframe when playback starts. That focus never
  // delivers keydown events to the page, so hand it back immediately.
  let reclaiming = false;
  window.addEventListener('blur', () => {
    window.setTimeout(() => {
      if (reclaiming) return;
      const active = document.activeElement;
      if (!(active instanceof HTMLIFrameElement)) return;
      reclaiming = true;
      active.blur();
      if (document.activeElement !== searchInput) app.focus();
      reclaiming = false;
    }, 0);
  });

  midiButton.addEventListener('click', () => {
    void enableMidi();
  });
}

async function enableMidi(): Promise<void> {
  midiButton.disabled = true;
  try {
    await listenToMidi((message) => {
      midiCount += 1;
      const handled = handleControllerMessage(message);
      midiStatus.textContent = handled
        ? `MIDI操作を実行（${midiCount}件）`
        : `MIDI受信 ${midiCount}件。DDJ-FLX4 の割り当ては未実装です`;
    });
    midiStatus.textContent = 'MIDI入力オン。DDJ-FLX4 の割り当ては未実装です';
  } catch {
    midiButton.disabled = false;
    midiStatus.textContent = 'MIDIを開始できませんでした';
  }
}

async function mountPlayers(): Promise<void> {
  try {
    await loadYouTubeApi();
    players.A.mount();
    players.B.mount();
    apiError.hidden = true;
  } catch {
    apiError.hidden = false;
    for (const deck of ['A', 'B'] as const) {
      const fallback = document.querySelector(`#frame-${suffix(deck)} .player-fallback`);
      if (fallback) fallback.textContent = 'YouTubeを読み込めませんでした';
    }
  }
}

function boot(): void {
  bindUi();
  renderTarget();
  renderDeck('A');
  renderDeck('B');
  applyVolumes();
  renderResults();
  syncMode();
  window.setInterval(() => {
    if (decks.A.videoId) paintTime('A');
    if (decks.B.videoId) paintTime('B');
  }, 250);
  void mountPlayers();
  dispatch({ type: 'search.focus' });
}

boot();
