import { describe, expect, it } from 'vitest';
import { CROSSFADER_STEP } from '../crossfader';
import { keyToAction, type KeyContext, type KeyInput } from './keyboard';

const idle: KeyContext = { searchFocused: false, buttonFocused: false, sliderFocused: false };
const typing: KeyContext = { searchFocused: true, buttonFocused: false, sliderFocused: false };

function key(partial: Partial<KeyInput> & Pick<KeyInput, 'key' | 'code'>): KeyInput {
  return {
    shiftKey: false,
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    isComposing: false,
    ...partial,
  };
}

describe('keyToAction', () => {
  it('covers search, load, transport, cue, crossfader, and the load target', () => {
    expect(keyToAction(key({ key: '/', code: 'Slash' }), idle)).toEqual({ type: 'search.focus' });
    expect(keyToAction(key({ key: 'Enter', code: 'Enter' }), typing)).toEqual({ type: 'search.submit' });
    expect(keyToAction(key({ key: 'ArrowDown', code: 'ArrowDown' }), typing)).toEqual({
      type: 'results.move',
      delta: 1,
    });
    expect(keyToAction(key({ key: 'ArrowUp', code: 'ArrowUp' }), idle)).toEqual({
      type: 'results.move',
      delta: -1,
    });
    expect(keyToAction(key({ key: 'a', code: 'KeyA' }), idle)).toEqual({ type: 'deck.load', deck: 'A' });
    expect(keyToAction(key({ key: 'b', code: 'KeyB' }), idle)).toEqual({ type: 'deck.load', deck: 'B' });
    expect(keyToAction(key({ key: 'q', code: 'KeyQ' }), idle)).toEqual({ type: 'deck.playPause', deck: 'A' });
    expect(keyToAction(key({ key: 'w', code: 'KeyW' }), idle)).toEqual({ type: 'deck.cue', deck: 'A' });
    expect(keyToAction(key({ key: 'o', code: 'KeyO' }), idle)).toEqual({ type: 'deck.playPause', deck: 'B' });
    expect(keyToAction(key({ key: 'p', code: 'KeyP' }), idle)).toEqual({ type: 'deck.cue', deck: 'B' });
    expect(keyToAction(key({ key: 'ArrowLeft', code: 'ArrowLeft' }), idle)).toEqual({
      type: 'crossfader.nudge',
      delta: -CROSSFADER_STEP,
    });
    expect(keyToAction(key({ key: 'ArrowRight', code: 'ArrowRight' }), idle)).toEqual({
      type: 'crossfader.nudge',
      delta: CROSSFADER_STEP,
    });
    expect(keyToAction(key({ key: 't', code: 'KeyT' }), idle)).toEqual({ type: 'deck.toggleTarget' });
    expect(keyToAction(key({ key: 'Enter', code: 'Enter' }), idle)).toEqual({
      type: 'deck.load',
      deck: 'target',
    });
    expect(keyToAction(key({ key: ' ', code: 'Space' }), idle)).toEqual({
      type: 'deck.playPause',
      deck: 'target',
    });
  });

  it('lets the search field type, including letters used as shortcuts', () => {
    expect(keyToAction(key({ key: 'a', code: 'KeyA' }), typing)).toBeNull();
    expect(keyToAction(key({ key: '/', code: 'Slash' }), typing)).toBeNull();
    expect(keyToAction(key({ key: ' ', code: 'Space' }), typing)).toBeNull();
    expect(keyToAction(key({ key: 'ArrowLeft', code: 'ArrowLeft' }), typing)).toBeNull();
  });

  it('ignores shortcuts while an IME is composing or a modifier is held', () => {
    expect(keyToAction(key({ key: 'Enter', code: 'Enter', isComposing: true }), typing)).toBeNull();
    expect(keyToAction(key({ key: 'q', code: 'KeyQ', ctrlKey: true }), idle)).toBeNull();
    expect(keyToAction(key({ key: 'Escape', code: 'Escape' }), typing)).toEqual({ type: 'search.blur' });
  });

  it('leaves Enter and Space to a focused button, and arrows to the fader', () => {
    const onButton: KeyContext = { searchFocused: false, buttonFocused: true, sliderFocused: false };
    const onSlider: KeyContext = { searchFocused: false, buttonFocused: false, sliderFocused: true };
    expect(keyToAction(key({ key: 'Enter', code: 'Enter' }), onButton)).toBeNull();
    expect(keyToAction(key({ key: ' ', code: 'Space' }), onButton)).toBeNull();
    expect(keyToAction(key({ key: 'q', code: 'KeyQ' }), onButton)).toEqual({
      type: 'deck.playPause',
      deck: 'A',
    });
    expect(keyToAction(key({ key: 'ArrowRight', code: 'ArrowRight' }), onSlider)).toBeNull();
    expect(keyToAction(key({ key: 'ArrowDown', code: 'ArrowDown' }), onSlider)).toEqual({
      type: 'results.move',
      delta: 1,
    });
  });

  it('ignores key repeat except on arrows', () => {
    expect(keyToAction(key({ key: 'q', code: 'KeyQ', repeat: true }), idle)).toBeNull();
    expect(keyToAction(key({ key: 'ArrowRight', code: 'ArrowRight', repeat: true }), idle)).toEqual({
      type: 'crossfader.nudge',
      delta: CROSSFADER_STEP,
    });
  });

  it('sets a cue point with shift', () => {
    expect(keyToAction(key({ key: 'W', code: 'KeyW', shiftKey: true }), idle)).toEqual({
      type: 'deck.setCue',
      deck: 'A',
    });
    expect(keyToAction(key({ key: 'P', code: 'KeyP', shiftKey: true }), idle)).toEqual({
      type: 'deck.setCue',
      deck: 'B',
    });
  });
});
