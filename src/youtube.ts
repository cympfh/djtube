export const YT_STATE = {
  UNSTARTED: -1,
  ENDED: 0,
  PLAYING: 1,
  PAUSED: 2,
  BUFFERING: 3,
  CUED: 5,
} as const;

type YTPlayer = {
  cueVideoById: (videoId: string) => void;
  playVideo: () => void;
  pauseVideo: () => void;
  seekTo: (seconds: number, allowSeekAhead: boolean) => void;
  getCurrentTime: () => number;
  getDuration: () => number;
  getPlayerState: () => number;
  setVolume: (volume: number) => void;
  getIframe: () => HTMLIFrameElement;
};

type YTPlayerEvent = { data: number; target: YTPlayer };

type YTApi = {
  Player: new (
    element: string | HTMLElement,
    options: {
      width?: string | number;
      height?: string | number;
      playerVars?: Record<string, string | number>;
      events?: {
        onReady?: (event: { target: YTPlayer }) => void;
        onStateChange?: (event: YTPlayerEvent) => void;
        onError?: (event: YTPlayerEvent) => void;
      };
    },
  ) => YTPlayer;
};

declare global {
  interface Window {
    YT?: YTApi;
    onYouTubeIframeAPIReady?: () => void;
  }
}

export type DeckPlayerHooks = {
  onReady: () => void;
  onState: (state: number) => void;
  onError: (code: number) => void;
};

let apiPromise: Promise<void> | null = null;

export function loadYouTubeApi(): Promise<void> {
  if (window.YT?.Player) return Promise.resolve();
  if (apiPromise) return apiPromise;
  apiPromise = new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => reject(new Error('youtube api timeout')), 15000);
    const previous = window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady = () => {
      previous?.();
      window.clearTimeout(timeout);
      resolve();
    };
    const script = document.createElement('script');
    script.src = 'https://www.youtube.com/iframe_api';
    script.async = true;
    script.onerror = () => {
      window.clearTimeout(timeout);
      reject(new Error('youtube api failed'));
    };
    document.head.appendChild(script);
  });
  return apiPromise;
}

/**
 * One YouTube IFrame player. Transport calls are safe before the iframe is
 * ready: the last cue/play request is applied in `onReady`.
 */
export class DeckPlayer {
  private player: YTPlayer | null = null;
  private ready = false;
  private pendingId: string | null = null;
  private pendingPlay = false;
  private pendingVolume: number | null = null;

  constructor(
    private readonly elementId: string,
    private readonly hooks: DeckPlayerHooks,
  ) {}

  mount(): void {
    const api = window.YT;
    const host = document.getElementById(this.elementId);
    if (!api?.Player || !host) throw new Error(`player host #${this.elementId} is missing`);
    const frame = host.parentElement;
    const width = Math.max(280, frame?.clientWidth ?? 320);
    const height = Math.max(120, frame?.clientHeight ?? 180);
    this.player = new api.Player(this.elementId, {
      width,
      height,
      playerVars: {
        autoplay: 0,
        controls: 1,
        disablekb: 1,
        fs: 1,
        rel: 0,
        modestbranding: 1,
        playsinline: 1,
        enablejsapi: 1,
        origin: window.location.origin,
      },
      events: {
        onReady: () => {
          this.ready = true;
          this.tagIframe();
          if (this.pendingVolume != null) this.applyVolume(this.pendingVolume);
          if (this.pendingId && this.player) {
            const videoId = this.pendingId;
            this.pendingId = null;
            this.player.cueVideoById(videoId);
          }
          if (this.pendingPlay && this.player) {
            this.pendingPlay = false;
            this.player.playVideo();
          }
          this.hooks.onReady();
        },
        onStateChange: (event) => this.hooks.onState(event.data),
        onError: (event) => this.hooks.onError(event.data),
      },
    });
  }

  cue(videoId: string): void {
    this.pendingPlay = false;
    if (!this.ready || !this.player) {
      this.pendingId = videoId;
      return;
    }
    this.player.cueVideoById(videoId);
  }

  play(): void {
    if (!this.ready || !this.player) {
      this.pendingPlay = true;
      return;
    }
    this.player.playVideo();
  }

  pause(): void {
    this.pendingPlay = false;
    if (!this.ready || !this.player) return;
    this.player.pauseVideo();
  }

  seek(seconds: number): void {
    if (!this.ready || !this.player) return;
    this.player.seekTo(Math.max(0, seconds), true);
  }

  setVolume(volume: number): void {
    const next = Math.max(0, Math.min(100, Math.round(volume)));
    this.pendingVolume = next;
    if (!this.ready || !this.player) return;
    this.applyVolume(next);
  }

  getTime(): number {
    if (!this.ready || !this.player) return 0;
    try {
      return this.player.getCurrentTime() || 0;
    } catch {
      return 0;
    }
  }

  getDuration(): number {
    if (!this.ready || !this.player) return 0;
    try {
      return this.player.getDuration() || 0;
    } catch {
      return 0;
    }
  }

  getState(): number {
    if (!this.ready || !this.player) return YT_STATE.UNSTARTED;
    try {
      return this.player.getPlayerState();
    } catch {
      return YT_STATE.UNSTARTED;
    }
  }

  /** YouTube focuses its iframe on play, which would swallow our keys. */
  releaseFocus(): void {
    try {
      this.player?.getIframe()?.blur();
    } catch {
      /* iframe not inserted yet */
    }
  }

  private applyVolume(volume: number): void {
    try {
      this.player?.setVolume(volume);
    } catch {
      /* player not ready to take volume */
    }
  }

  private tagIframe(): void {
    try {
      const iframe = this.player?.getIframe();
      if (!iframe) return;
      iframe.tabIndex = -1;
      iframe.title = this.elementId === 'player-a' ? 'デッキAの動画' : 'デッキBの動画';
    } catch {
      /* ignore */
    }
  }
}
