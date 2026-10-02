import { CROSSFADER_STEP } from '../crossfader';
import type { Action } from '../dispatch';

export type KeyContext = {
  searchFocused: boolean;
  buttonFocused: boolean;
  sliderFocused: boolean;
};

export type KeyInput = {
  key: string;
  code: string;
  shiftKey?: boolean;
  metaKey?: boolean;
  ctrlKey?: boolean;
  altKey?: boolean;
  isComposing?: boolean;
  keyCode?: number;
  repeat?: boolean;
};

/**
 * Map a key into an app action.
 * While the search field is focused, printable keys fall through so the
 * query can be typed. Enter still submits, and the arrows still move results.
 */
export function keyToAction(event: KeyInput, ctx: KeyContext): Action | null {
  if (event.isComposing || event.key === 'Process' || event.keyCode === 229) return null;
  // Holding a transport key must not toggle play on every repeat. Arrows may repeat.
  if (event.repeat && !event.key.startsWith('Arrow')) return null;
  if (event.key === 'Escape') return { type: 'search.blur' };
  if (event.metaKey || event.ctrlKey || event.altKey) return null;

  if (ctx.searchFocused) {
    if (event.key === 'Enter') return { type: 'search.submit' };
    if (event.key === 'ArrowDown') return { type: 'results.move', delta: 1 };
    if (event.key === 'ArrowUp') return { type: 'results.move', delta: -1 };
    return null;
  }

  if (ctx.buttonFocused && (event.key === 'Enter' || event.key === ' ')) return null;
  if (ctx.sliderFocused && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) return null;

  if (!event.shiftKey && (event.key === '/' || event.code === 'Slash')) {
    return { type: 'search.focus' };
  }
  if (event.key === 'Enter') return { type: 'deck.load', deck: 'target' };
  if (event.key === 'ArrowDown') return { type: 'results.move', delta: 1 };
  if (event.key === 'ArrowUp') return { type: 'results.move', delta: -1 };
  if (event.key === 'ArrowLeft') return { type: 'crossfader.nudge', delta: -CROSSFADER_STEP };
  if (event.key === 'ArrowRight') return { type: 'crossfader.nudge', delta: CROSSFADER_STEP };
  if (!event.shiftKey && (event.key === ' ' || event.code === 'Space')) {
    return { type: 'deck.playPause', deck: 'target' };
  }
  if (event.shiftKey && event.code === 'KeyW') return { type: 'deck.setCue', deck: 'A' };
  if (event.shiftKey && event.code === 'KeyP') return { type: 'deck.setCue', deck: 'B' };
  if (event.shiftKey) return null;

  switch (event.code) {
    case 'KeyA':
      return { type: 'deck.load', deck: 'A' };
    case 'KeyB':
      return { type: 'deck.load', deck: 'B' };
    case 'KeyT':
      return { type: 'deck.toggleTarget' };
    case 'KeyQ':
      return { type: 'deck.playPause', deck: 'A' };
    case 'KeyW':
      return { type: 'deck.cue', deck: 'A' };
    case 'KeyO':
      return { type: 'deck.playPause', deck: 'B' };
    case 'KeyP':
      return { type: 'deck.cue', deck: 'B' };
    default:
      return null;
  }
}
