// Deck audio comes from this server, then through Web Audio filters.
// A YouTube iframe cannot feed those filters. Tempo is the audio element's playbackRate.

import { EQ_BANDS, EQ_FILTERS, connectEqGraph } from "./eq.js";
import { FILTER_CENTER, applyFilter, clampFilterUnit, connectDeckFilter } from "./filter.js";
import { clampSpinRate } from "./jogspin.js";
import { createScratchVoice } from "./scratch.js";
import { publicPrefix } from "./prefix.js";
import { clampRate } from "./rate.js";
import { commandedSeekLanded } from "./seekland.js";

export function createDeckPlayer(deck, elementId) {
  const audio = typeof document === "undefined" ? null : document.createElement("audio");
  if (audio) {
    audio.className = "deck-audio";
    audio.preload = "auto";
    audio.setAttribute("playsinline", "");
    const host = document.getElementById(elementId);
    if (host) host.append(audio);
  }

  return {
    deck,
    elementId,
    audio,
    paused: true,
    videoId: "",
    _time: 0,
    _seekFrom: 0,
    _seekPending: false,
    _volume: 1,
    _rate: 1,
    _spinRate: null,
    _trackHeld: false,
    _scratch: null,
    _eqDb: { high: 0, mid: 0, low: 0 },
    _filterUnit: FILTER_CENTER,
    _context: null,
    _filters: null,
    _color: null,
    _graphFailed: false,
    _attached: false,
    _reportedTime() {
      try {
        const time = this.audio?.currentTime;
        if (Number.isFinite(time)) return time;
      } catch {
        /* element has no media yet */
      }
      return NaN;
    },
    get currentTime() {
      const reported = this._reportedTime();
      if (this._seekPending && Number.isFinite(this._time)) {
        if (!commandedSeekLanded(this._seekFrom, this._time, reported)) return this._time;
        this._seekPending = false;
        this.onSeekLanded?.();
      }
      if (Number.isFinite(reported)) return reported;
      return this._time;
    },
    set currentTime(value) {
      const next = Number(value) || 0;
      if (!this._seekPending) {
        const reported = this._reportedTime();
        this._seekFrom = Number.isFinite(reported) ? reported : this._time;
      }
      this._time = next;
      this._seekPending = true;
      if (!this.audio) return;
      try {
        this.audio.currentTime = next;
      } catch {
        /* ignore seek before metadata */
      }
    },
    cancelPendingSeek() {
      this._seekPending = false;
    },
    get duration() {
      try {
        const value = this.audio?.duration;
        if (Number.isFinite(value) && value > 0) return value;
      } catch {
        /* duration is not known yet */
      }
      return NaN;
    },
    get volume() {
      return this._volume;
    },
    set volume(value) {
      const numeric = Number(value);
      this._volume = Number.isFinite(numeric) ? Math.min(1, Math.max(0, numeric)) : 0;
      if (this.audio) this.audio.volume = this._volume;
    },
    get playbackRate() {
      return this._rate;
    },
    set playbackRate(value) {
      this._rate = clampRate(value);
      this._applyRate();
    },
    // Platter speed while the hand is on the disc. The tempo fader stays in _rate.
    setSpinRate(value) {
      if (value == null) this._spinRate = null;
      else {
        const numeric = Number(value);
        this._spinRate = Number.isFinite(numeric) && numeric > 0 ? numeric : null;
      }
      this._applyRate();
    },
    holdTrack(held) {
      this._trackHeld = !!held;
      if (this.audio) this.audio.muted = this._trackHeld;
    },
    playScratch(scratch) {
      if (!scratch || !(Number(scratch.rate) > 0)) return;
      try {
        if (!this._context) this._ensureGraph();
        if (!this._context) return;
        if (this._context.state === "suspended") this._context.resume?.();
        if (!this._scratch) this._scratch = createScratchVoice(this._context);
        const level = Number(this.volume);
        this._scratch.update(scratch, Number.isFinite(level) ? level : 1);
      } catch {
        this._scratch = null;
      }
    },
    stopScratch() {
      this._scratch?.stop();
      this._scratch = null;
    },
    _applyRate() {
      if (!this.audio) return;
      const spinning = this._spinRate != null && !this._trackHeld;
      const rate = spinning ? clampSpinRate(this._spinRate) ?? this._rate : this._rate;
      try {
        this.audio.playbackRate = rate;
      } catch {
        /* the element applies the rate once media is ready */
      }
    },
    _ensureGraph() {
      if (this._graphFailed || !this.audio) return false;
      if (typeof window === "undefined") {
        this._graphFailed = true;
        return false;
      }
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) {
        this._graphFailed = true;
        return false;
      }
      try {
        if (!this._context) this._context = new Ctx();
        if (this._context.state === "suspended") this._context.resume();
        if (!this._filters) {
          const source = this._context.createMediaElementSource(this.audio);
          // Channel volume and the crossfader are already audio.volume.
          // Leave LOW unconnected so the filter is the next node, not a second graph.
          this._filters = connectEqGraph(source, this._context, null);
          this._color = connectDeckFilter(this._filters.low, this._context);
          for (const band of EQ_BANDS) this._filters[band].gain.value = this._eqDb[band] || 0;
          applyFilter(this._color, this._filterUnit);
        }
        return true;
      } catch {
        this._graphFailed = true;
        return false;
      }
    },
    setEqGain(band, db) {
      if (!EQ_FILTERS[band] || !Number.isFinite(Number(db))) return false;
      this._eqDb[band] = Number(db);
      if (!this._ensureGraph() || !this._filters?.[band]) return false;
      this._filters[band].gain.value = this._eqDb[band];
      return true;
    },
    setFilter(unit) {
      this._filterUnit = clampFilterUnit(unit);
      if (!this._ensureGraph() || !this._color) return false;
      applyFilter(this._color, this._filterUnit);
      return true;
    },
    play() {
      this._ensureGraph();
      this._applyRate();
      if (!this.audio) {
        this.paused = false;
        return Promise.resolve();
      }
      const pending = this.audio.play();
      this.paused = false;
      return pending && typeof pending.then === "function" ? pending : Promise.resolve();
    },
    pause() {
      if (this.audio) this.audio.pause();
      this.paused = true;
    },
    loadVideo(id) {
      this.stopScratch();
      this._spinRate = null;
      this._trackHeld = false;
      if (this.audio) this.audio.muted = false;
      this.videoId = id;
      this._time = 0;
      this._seekFrom = 0;
      this._seekPending = false;
      this.paused = true;
      if (!this.audio) return false;
      const prefix = publicPrefix();
      this.audio.src = `${prefix}/api/audio/${encodeURIComponent(id)}`;
      this.audio.load();
      this._applyRate();
      return false;
    },
    attach(hooks) {
      if (!this.audio || this._attached) return;
      this._attached = true;
      this.audio.addEventListener("canplay", () => {
        this._applyRate();
        if (this.videoId) hooks.onReady?.(this.deck);
      });
      this.audio.addEventListener("play", () => {
        this.paused = false;
        this._ensureGraph();
        hooks.onPlaying?.(this.deck);
      });
      this.audio.addEventListener("pause", () => {
        this.paused = true;
        hooks.onPaused?.(this.deck);
      });
      this.audio.addEventListener("ended", () => {
        this.paused = true;
        hooks.onEnded?.(this.deck);
      });
      this.audio.addEventListener("error", () => {
        hooks.onError?.(this.deck);
      });
    },
  };
}

export function startDeckAudio(players, hooks) {
  for (const deck of ["A", "B"]) players[deck].attach(hooks);
}
