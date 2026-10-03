// Pioneer DDJ-FLX4 MIDI, from the official message list (E1) and the Mixxx
// Pioneer-DDJ-FLX4 mapping. Channels in this file are 0–15.
// Deck 1 is channel 0, deck 2 is channel 1, the mixer and browse encoder
// are channel 6 (status 0xB6 / 0x96).
//
// Key format:
//   "note:<channel>:<note>"
//   "cc:<channel>:<controller>"
// Buttons are note-on with velocity > 0. Note-on velocity 0 is a release and
// is ignored. Jog wheels report a relative CC centered on 64 (65 is +1).
// The browse encoder is a 7-bit signed step (1 is +1, 127 is -1).
// The crossfader MSB is CC 31 and goes to setCrossfaderFromController as 0–127.
// Tempo MSB is CC 0 on the deck channel and goes to setRateFromController.
// EQ MSB is CC 7 / 11 / 15 (HI / MID / LOW) and goes to setEqFromController.
// Channel fader MSB is CC 19 (0x13) on the deck channel and goes to
// setVolumeFromController as 0–127. Deck 1 (channel 0, left) is deck A.
// Deck 2 (channel 1, right) is deck B. The LSB is CC 51 (0x33) and is not
// mapped. Notes 102 and 82 are fader-start play/cue, not the level.
// The other LSB companions (CC 32, 39, 43, 47, and crossfader CC 63) are not mapped.
// CFX (Sound Color FX) MSB is on the mixer channel, not the deck channel.
// Deck 1 (left, A) is CC 23 (0x17). Deck 2 (right, B) is CC 24 (0x18).
// Official DDJ-FLX4 MIDI Message List E1, figure 3-5, status 0xB6.
// Mixxx Pioneer-DDJ-FLX4 maps those bytes as FILTER CH1 / FILTER CH2.
// The LSB is CC 55 (0x37) and CC 56 (0x38) and is not mapped.

const DECK_A = 0;
const DECK_B = 1;
const MIXER = 6;

const PLAY = 0x0b;
const CUE = 0x0c;
const LOAD_A = 0x46;
const LOAD_B = 0x47;
const JOG_SIDE = 0x21;
const JOG_VINYL = 0x22;
const JOG_BEND = 0x23;
const JOG_SEARCH = 0x29;
const CROSSFADER = 0x1f;
const BROWSE = 0x40;
const TEMPO = 0x00;
const EQ_HI = 0x07;
const EQ_MID = 0x0b;
const EQ_LOW = 0x0f;
const CHANNEL_FADER = 0x13;
const CFX_A = 0x17;
const CFX_B = 0x18;

/** Seconds of seek for one jog tick (value 65 or 63). */
export const JOG_STEP_SECONDS = 0.05;
/** Shift+platter uses its own CC and seeks faster. */
export const JOG_SEARCH_STEP_SECONDS = 0.5;

export const MIDI_STATUS_IDLE = "MIDI未接続";

function binding(action, args, extra) {
  const spec = { action };
  if (args) spec.args = args;
  if (extra) Object.assign(spec, extra);
  return spec;
}

function deckVolume(channel, deck) {
  return {
    [`cc:${channel}:${CHANNEL_FADER}`]: binding("setVolumeFromController", [deck], { passValue: true }),
  };
}

function deckTone(channel, deck) {
  const tempo = { passValue: true };
  const eq = { passValue: true };
  return {
    [`cc:${channel}:${TEMPO}`]: binding("setRateFromController", [deck], tempo),
    [`cc:${channel}:${EQ_HI}`]: binding("setEqFromController", [deck, "high"], eq),
    [`cc:${channel}:${EQ_MID}`]: binding("setEqFromController", [deck, "mid"], eq),
    [`cc:${channel}:${EQ_LOW}`]: binding("setEqFromController", [deck, "low"], eq),
  };
}

function deckJog(channel, deck) {
  const fine = { relative: "center64", scale: JOG_STEP_SECONDS };
  const search = { relative: "center64", scale: JOG_SEARCH_STEP_SECONDS };
  return {
    [`cc:${channel}:${JOG_SIDE}`]: binding("jog", [deck], fine),
    [`cc:${channel}:${JOG_VINYL}`]: binding("jog", [deck], fine),
    [`cc:${channel}:${JOG_BEND}`]: binding("jog", [deck], fine),
    [`cc:${channel}:${JOG_SEARCH}`]: binding("jog", [deck], search),
  };
}

/** @type {Record<string, {action: string, args?: unknown[], passValue?: boolean, relative?: string, scale?: number}>} */
export const FLX4_MAP = {
  [`note:${DECK_A}:${PLAY}`]: binding("togglePlay", ["A"]),
  [`note:${DECK_B}:${PLAY}`]: binding("togglePlay", ["B"]),
  [`note:${DECK_A}:${CUE}`]: binding("cue", ["A"]),
  [`note:${DECK_B}:${CUE}`]: binding("cue", ["B"]),
  [`note:${MIXER}:${LOAD_A}`]: binding("loadOpenSelection", ["A"]),
  [`note:${MIXER}:${LOAD_B}`]: binding("loadOpenSelection", ["B"]),
  [`cc:${MIXER}:${CROSSFADER}`]: binding("setCrossfaderFromController", undefined, { passValue: true }),
  [`cc:${MIXER}:${CFX_A}`]: binding("setFilterFromController", ["A"], { passValue: true }),
  [`cc:${MIXER}:${CFX_B}`]: binding("setFilterFromController", ["B"], { passValue: true }),
  [`cc:${MIXER}:${BROWSE}`]: binding("moveSelection", undefined, { relative: "signed7", scale: 1 }),
  ...deckJog(DECK_A, "A"),
  ...deckJog(DECK_B, "B"),
  ...deckTone(DECK_A, "A"),
  ...deckTone(DECK_B, "B"),
  ...deckVolume(DECK_A, "A"),
  ...deckVolume(DECK_B, "B"),
};

export function controllerEventKey(msg) {
  return `${msg.type}:${msg.channel}:${msg.number}`;
}

export function relativeMidiTicks(value, mode) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 0;
  const midi = Math.round(numeric);
  if (mode === "center64") return midi - 64;
  if (mode === "signed7") {
    if (midi <= 0 || midi >= 128 || midi === 64) return 0;
    if (midi < 64) return midi;
    return midi - 128;
  }
  return 0;
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
  if (spec.relative) {
    const ticks = relativeMidiTicks(msg.value, spec.relative);
    if (!ticks) return true;
    const scale = Number(spec.scale);
    const amount = ticks * (Number.isFinite(scale) ? scale : 1);
    if (!amount) return true;
    args.push(amount);
  } else if (spec.passValue) {
    args.push(msg.value);
  }
  fn(...args);
  return true;
}

export function controllerStatusText(status) {
  if (!status || status.state === "idle") return MIDI_STATUS_IDLE;
  if (status.state === "unsupported") return "Web MIDI 非対応";
  if (status.state === "insecure") return "未接続 — HTTPS が必要です";
  if (status.state === "denied") return "MIDI が拒否されました";
  if (status.state === "open" && status.names?.length) return `接続: ${status.names.join("、")}`;
  if (status.state === "opening") return "MIDI を開いています…";
  return "未接続";
}

/** Header button face. "on" only after a device is actually connected. */
export function midiButtonState(status) {
  const connected = !!(status?.connected || (status?.state === "open" && status.names?.length));
  return connected ? "on" : "off";
}

export async function connectController(actions, onStatus) {
  const nav = typeof navigator === "undefined" ? undefined : navigator;
  const secure = typeof window === "undefined" || window.isSecureContext !== false;
  if (!nav?.requestMIDIAccess) {
    onStatus?.({
      state: secure ? "unsupported" : "insecure",
      names: [],
      ignored: 0,
      connected: false,
      mapped: Object.keys(FLX4_MAP).length,
    });
    return;
  }
  let ignored = 0;
  try {
    const access = await nav.requestMIDIAccess();
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
            connected: names.length > 0,
            mapped: Object.keys(FLX4_MAP).length,
          });
        };
      }
      onStatus?.({
        state: "open",
        names,
        ignored,
        connected: names.length > 0,
        mapped: Object.keys(FLX4_MAP).length,
      });
    };
    bind();
    access.onstatechange = bind;
  } catch {
    onStatus?.({
      state: "denied",
      names: [],
      ignored: 0,
      connected: false,
      mapped: Object.keys(FLX4_MAP).length,
    });
  }
}
