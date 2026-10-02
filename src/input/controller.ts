import { dispatch, type Action } from '../dispatch';

/**
 * One MIDI message, the way the Web MIDI API delivers it.
 * `status` includes the channel nibble. `data1` is a note or CC number.
 * `data2` is velocity or the CC value.
 */
export type MidiMessage = {
  status: number;
  data1: number;
  data2: number;
};

export type ControllerBinding = {
  /** Stable name so a future FLX4 map can be read without guessing. */
  name: string;
  /** Return an action, or null when this message is not the one you own. */
  toAction: (message: MidiMessage) => Action | null;
};

/**
 * Pioneer DJ DDJ-FLX4 mapping.
 *
 * Not implemented. This table is intentionally empty — note and CC numbers
 * for the FLX4 are not filled in, and connecting the controller does not
 * drive the decks. Add bindings here later. `midiMessageToAction` is the
 * only place that turns controller events into the same actions the keyboard
 * uses; the decks do not need to change.
 *
 * DDJ-FLX4 の割り当ては未実装。この配列は意図的に空にしてある。
 */
export const flx4Bindings: readonly ControllerBinding[] = [];

export function actionFromBindings(
  message: MidiMessage,
  bindings: readonly ControllerBinding[],
): Action | null {
  for (const binding of bindings) {
    const action = binding.toAction(message);
    if (action) return action;
  }
  return null;
}

/** Translate one controller event. Unmapped messages (including every FLX4 message today) return null. */
export function midiMessageToAction(message: MidiMessage): Action | null {
  return actionFromBindings(message, flx4Bindings);
}

/** The single door from a controller event into `dispatch`. */
export function handleControllerMessage(message: MidiMessage): boolean {
  const action = midiMessageToAction(message);
  if (!action) return false;
  dispatch(action);
  return true;
}

export type MidiListener = (message: MidiMessage) => void;

/**
 * Open Web MIDI inputs and forward raw messages.
 * Interpretation stays in `handleControllerMessage` / `midiMessageToAction`.
 */
export async function listenToMidi(onMessage: MidiListener): Promise<() => void> {
  if (!navigator.requestMIDIAccess) {
    throw new Error('Web MIDI API is not available');
  }
  const access = await navigator.requestMIDIAccess({ sysex: false });

  const attach = (input: MIDIInput) => {
    input.onmidimessage = (event) => {
      const data = event.data;
      if (!data || data.length < 2) return;
      onMessage({
        status: data[0] ?? 0,
        data1: data[1] ?? 0,
        data2: data.length > 2 ? (data[2] ?? 0) : 0,
      });
    };
  };

  access.inputs.forEach((input) => attach(input));
  access.onstatechange = (event) => {
    const port = event.port;
    if (port && port.type === 'input' && port.state === 'connected') {
      attach(port as MIDIInput);
    }
  };

  return () => {
    access.onstatechange = null;
    access.inputs.forEach((input) => {
      input.onmidimessage = null;
    });
  };
}
