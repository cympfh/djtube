// Pioneer DDJ-FLX4 is intentionally not mapped.
// Keyboard and a future controller both call the same action functions.
//
// Key format, once real DDJ-FLX4 numbers are known:
//   "note:<channel 0-15>:<note number>"
//   "cc:<channel 0-15>:<controller number>"
// Value is note velocity or CC 0–127.
// A crossfader entry should call setCrossfaderFromController with passValue true.
// That action takes the raw MIDI value 0–127. This file does not guess those numbers.

/** @type {Record<string, {action: string, args?: unknown[], passValue?: boolean}>} */
export const FLX4_MAP = {};

export function controllerEventKey(msg) {
  return `${msg.type}:${msg.channel}:${msg.number}`;
}

export function messageFromMidi(data) {
  if (!data || data.length < 2) return null;
  const status = data[0];
  const number = data[1];
  const value = data.length > 2 ? data[2] : 0;
  const kind = status & 0xf0;
  const channel = status & 0x0f;
  if (kind === 0x90 && value > 0) return { type: "note", channel, number, value };
  if (kind === 0xb0) return { type: "cc", channel, number, value };
  return null;
}

export function dispatchControllerEvent(msg, actions, map = FLX4_MAP) {
  if (!msg) return false;
  const spec = map[controllerEventKey(msg)];
  if (!spec) return false;
  const fn = actions[spec.action];
  if (typeof fn !== "function") return false;
  const args = Array.isArray(spec.args) ? spec.args.slice() : [];
  if (spec.passValue) args.push(msg.value);
  fn(...args);
  return true;
}

export async function connectController(actions, onStatus) {
  if (typeof navigator === "undefined" || !navigator.requestMIDIAccess) {
    onStatus?.({ state: "unsupported", names: [], ignored: 0 });
    return;
  }
  let ignored = 0;
  try {
    const access = await navigator.requestMIDIAccess();
    const bind = () => {
      const names = [];
      for (const input of access.inputs.values()) {
        names.push(input.name || "MIDI");
        input.onmidimessage = (event) => {
          const msg = messageFromMidi(event.data);
          if (!msg) return;
          const handled = dispatchControllerEvent(msg, actions);
          if (!handled) ignored += 1;
          onStatus?.({
            state: "open",
            names,
            ignored,
            mapped: Object.keys(FLX4_MAP).length,
          });
        };
      }
      onStatus?.({
        state: "open",
        names,
        ignored,
        mapped: Object.keys(FLX4_MAP).length,
      });
    };
    bind();
    access.onstatechange = bind;
  } catch {
    onStatus?.({ state: "denied", names: [], ignored: 0 });
  }
}
