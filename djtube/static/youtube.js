// Two YouTube IFrame players. The deck actions only see loadVideo / play / pause / volume / time.

export function createDeckPlayer(deck, elementId) {
  return {
    deck,
    elementId,
    player: null,
    apiReady: false,
    paused: true,
    videoId: "",
    _time: 0,
    _seekPending: false,
    _volume: 1,
    get currentTime() {
      let reported = NaN;
      try {
        const time = this.player?.getCurrentTime?.();
        if (Number.isFinite(time)) reported = time;
      } catch {
        /* player has no video yet */
      }
      if (this._seekPending && Number.isFinite(this._time)) {
        if (!Number.isFinite(reported) || Math.abs(reported - this._time) > 0.35) return this._time;
        this._seekPending = false;
        this.onSeekLanded?.();
      }
      if (Number.isFinite(reported)) return reported;
      return this._time;
    },
    set currentTime(value) {
      const next = Number(value) || 0;
      this._time = next;
      this._seekPending = true;
      try {
        this.player?.seekTo?.(next, true);
      } catch {
        /* ignore seek before the video is cued */
      }
    },
    get duration() {
      try {
        const value = this.player?.getDuration?.();
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
      const player = this.player;
      if (!player?.setVolume) return;
      const percent = Math.round(this._volume * 100);
      if (percent === 0) player.mute?.();
      else {
        player.unMute?.();
        player.setVolume(percent);
      }
    },
    play() {
      try {
        this.player?.playVideo?.();
      } catch {
        /* play before the player exists */
      }
      this.paused = false;
      return Promise.resolve();
    },
    pause() {
      try {
        this.player?.pauseVideo?.();
      } catch {
        /* pause before the player exists */
      }
      this.paused = true;
    },
    loadVideo(id) {
      this.videoId = id;
      this._time = 0;
      this._seekPending = false;
      this.paused = true;
      if (!this.player || !this.apiReady) return false;
      this.player.cueVideoById(id);
      return true;
    },
    fit() {
      const host = document.getElementById(this.elementId);
      const frame = host?.parentElement;
      if (!frame || !this.player?.setSize) return;
      const rect = frame.getBoundingClientRect();
      if (rect.width < 10 || rect.height < 10) return;
      this.player.setSize(Math.round(rect.width), Math.round(rect.height));
    },
    attach(hooks) {
      const host = document.getElementById(this.elementId);
      const frame = host?.parentElement;
      const rect = frame?.getBoundingClientRect();
      this.player = new window.YT.Player(this.elementId, {
        width: Math.max(200, Math.round(rect?.width || 320)),
        height: Math.max(112, Math.round(rect?.height || 180)),
        playerVars: {
          autoplay: 0,
          controls: 0,
          disablekb: 1,
          fs: 0,
          modestbranding: 1,
          rel: 0,
          playsinline: 1,
          origin: location.origin,
        },
        events: {
          onReady: () => {
            this.apiReady = true;
            const frame = document.getElementById(this.elementId);
            if (frame) frame.tabIndex = -1;
            this.volume = this._volume;
            if (this.videoId) this.player.cueVideoById(this.videoId);
            this.fit();
            hooks.onReady?.(this.deck);
          },
          onStateChange: (event) => hooks.onState?.(this.deck, event.data),
          onError: (event) => hooks.onError?.(this.deck, event.data),
        },
      });
    },
  };
}

function releaseIframeFocus() {
  const active = document.activeElement;
  if (active && active.tagName === "IFRAME") {
    active.tabIndex = -1;
    active.blur();
    document.body.focus();
  }
}

function allowPlayerReferrer() {
  if (document.documentElement.dataset.djtubeReferrer === "1") return;
  document.documentElement.dataset.djtubeReferrer = "1";
  const original = document.createElement.bind(document);
  document.createElement = (tagName, options) => {
    const element = original(tagName, options);
    if (String(tagName).toLowerCase() === "iframe") {
      element.referrerPolicy = "strict-origin-when-cross-origin";
    }
    return element;
  };
}

export function startYoutubeDecks(players, hooks) {
  allowPlayerReferrer();
  const boot = () => {
    for (const deck of ["A", "B"]) players[deck].attach(hooks);
    window.addEventListener("resize", () => {
      for (const deck of ["A", "B"]) players[deck].fit();
    });
    window.addEventListener("blur", () => {
      setTimeout(releaseIframeFocus, 0);
    });
  };
  if (window.YT?.Player) {
    boot();
    return;
  }
  const previous = window.onYouTubeIframeAPIReady;
  window.onYouTubeIframeAPIReady = () => {
    if (typeof previous === "function") previous();
    boot();
  };
  if (!document.querySelector("script[data-youtube-iframe]")) {
    const script = document.createElement("script");
    script.src = "https://www.youtube.com/iframe_api";
    script.dataset.youtubeIframe = "1";
    document.head.append(script);
  }
}
