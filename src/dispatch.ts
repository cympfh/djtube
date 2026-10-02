export type DeckId = 'A' | 'B';
export type DeckTarget = DeckId | 'target';

/**
 * Every input — keyboard, mouse, and the future DDJ-FLX4 — speaks this
 * vocabulary and enters the app through `dispatch`.
 */
export type Action =
  | { type: 'search.focus' }
  | { type: 'search.blur' }
  | { type: 'search.submit' }
  | { type: 'results.move'; delta: 1 | -1 }
  | { type: 'results.select'; index: number }
  | { type: 'deck.load'; deck: DeckTarget }
  | { type: 'deck.setTarget'; deck: DeckId }
  | { type: 'deck.toggleTarget' }
  | { type: 'deck.playPause'; deck: DeckTarget }
  | { type: 'deck.cue'; deck: DeckId }
  | { type: 'deck.setCue'; deck: DeckId }
  | { type: 'crossfader.nudge'; delta: number }
  | { type: 'crossfader.set'; value: number };

type Handler = (action: Action) => void;

let handler: Handler = () => {};

export function setActionHandler(next: Handler): void {
  handler = next;
}

export function dispatch(action: Action): void {
  handler(action);
}
