import { afterEach, describe, expect, it } from 'vitest';
import { setActionHandler, type Action } from '../dispatch';
import {
  actionFromBindings,
  flx4Bindings,
  handleControllerMessage,
  midiMessageToAction,
  type ControllerBinding,
  type MidiMessage,
} from './controller';

afterEach(() => {
  setActionHandler(() => {});
});

describe('DDJ-FLX4 controller hook', () => {
  it('ships without an FLX4 map, so controller messages do not become actions', () => {
    expect(flx4Bindings).toEqual([]);
    const samples: MidiMessage[] = [
      { status: 0x90, data1: 0x0b, data2: 127 },
      { status: 0x80, data1: 0x0b, data2: 0 },
      { status: 0xb0, data1: 0x0c, data2: 64 },
      { status: 0x9f, data1: 0x7f, data2: 1 },
    ];
    for (const message of samples) {
      expect(midiMessageToAction(message)).toBeNull();
    }
  });

  it('does not dispatch when the map has nothing to say', () => {
    const seen: Action[] = [];
    setActionHandler((action) => seen.push(action));
    expect(handleControllerMessage({ status: 0x90, data1: 1, data2: 127 })).toBe(false);
    expect(seen).toEqual([]);
  });

  it('turns a matching binding into the same action the keyboard uses', () => {
    const bindings: ControllerBinding[] = [
      {
        name: 'fixture-not-an-flx4-map',
        toAction: (message) =>
          message.status === 0x90 && message.data1 === 0x10 && message.data2 > 0
            ? { type: 'deck.playPause', deck: 'A' }
            : null,
      },
    ];
    expect(actionFromBindings({ status: 0x90, data1: 0x10, data2: 127 }, bindings)).toEqual({
      type: 'deck.playPause',
      deck: 'A',
    });
    expect(actionFromBindings({ status: 0x90, data1: 0x10, data2: 0 }, bindings)).toBeNull();
    expect(actionFromBindings({ status: 0x90, data1: 0x11, data2: 127 }, [])).toBeNull();
  });
});
