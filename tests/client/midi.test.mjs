import assert from "node:assert/strict";
import test from "node:test";

import { controllerStatusText, createFlx4LedPort, createMidiControl, paintFlx4Leds } from "../../djtube/static/controller.js";

const PLAY_A = new Uint8Array([0x90, 0x0b, 0x7f]);

// One physical DDJ-FLX4. Like Chromium (midi_access.cc), every MIDIAccess makes
// its own MIDIInput objects, and `inputs` leaves out disconnected ports.
function fakeMidi() {
  const device = { connected: true, ports: [] };
  const accesses = [];
  let held = null;

  function makeAccess() {
    const input = { id: "in", name: "DDJ-FLX4", type: "input", state: "connected", onmidimessage: null };
    const sent = [];
    const output = { id: "out", name: "DDJ-FLX4", type: "output", state: "connected", send: (m) => sent.push([...m]) };
    device.ports.push(input);
    const all = new Map([[input.id, input]]);
    const access = {
      get inputs() {
        return new Map([...all].filter(([, port]) => port.state !== "disconnected"));
      },
      outputs: new Map([[output.id, output]]),
      onstatechange: null,
      input,
      output,
      sent,
    };
    accesses.push(access);
    return access;
  }

  return {
    accesses,
    navigator: {
      requestMIDIAccess() {
        if (held) return new Promise((resolve) => held.push(resolve));
        return Promise.resolve(makeAccess());
      },
    },
    hold() {
      held = [];
    },
    grant() {
      const list = held;
      held = null;
      for (const resolve of list) resolve(makeAccess());
    },
    emit(data) {
      let runs = 0;
      for (const port of device.ports) {
        if (port.state === "connected" && port.onmidimessage) {
          port.onmidimessage({ data });
          runs += 1;
        }
      }
      return runs;
    },
    plug(connected) {
      for (const access of accesses) {
        access.input.state = connected ? "connected" : "disconnected";
        access.onstatechange?.({ port: access.input });
      }
    },
  };
}

function harness() {
  const midi = fakeMidi();
  const presses = [];
  const statuses = [];
  const ledPort = createFlx4LedPort();
  const control = createMidiControl({ togglePlay: (deck) => presses.push(deck) }, (s) => statuses.push(s), {
    navigator: midi.navigator,
    ledPort,
  });
  return { midi, presses, statuses, control, ledPort };
}

test("one MIDI press runs the action once after one connect", async () => {
  const { midi, presses, control } = harness();
  await control.toggle();
  midi.emit(PLAY_A);
  assert.deepEqual(presses, ["A"]);
});

test("pressing the icon while connected disconnects instead of binding again", async () => {
  const { midi, presses, statuses, control } = harness();
  await control.toggle();
  assert.equal(statuses.at(-1).state, "open");
  await control.toggle();
  assert.equal(midi.accesses.length, 1);
  assert.equal(midi.accesses[0].input.onmidimessage, null);
  assert.equal(midi.accesses[0].onstatechange, null);
  assert.equal(statuses.at(-1).state, "idle");
  assert.equal(midi.emit(PLAY_A), 0);
  assert.deepEqual(presses, []);

  await control.toggle();
  assert.equal(midi.accesses.length, 2);
  midi.emit(PLAY_A);
  assert.deepEqual(presses, ["A"]);
});

test("connect is idempotent", async () => {
  const { midi, presses, control } = harness();
  await control.connect();
  await control.connect();
  await control.connect();
  assert.equal(midi.accesses.length, 1);
  midi.emit(PLAY_A);
  assert.deepEqual(presses, ["A"]);
});

test("a second press while requestMIDIAccess is pending cancels, and the late access is not bound", async () => {
  const { midi, presses, statuses, control } = harness();
  midi.hold();
  const first = control.toggle();
  assert.equal(statuses.at(-1).state, "opening");
  await control.toggle();
  assert.equal(statuses.at(-1).state, "idle");
  midi.grant();
  await first;
  assert.equal(midi.emit(PLAY_A), 0);
  assert.deepEqual(presses, []);
  assert.equal(statuses.at(-1).state, "idle");
});

test("two connects while pending still bind one handler", async () => {
  const { midi, presses, control } = harness();
  midi.hold();
  const a = control.connect();
  const b = control.connect();
  midi.grant();
  await Promise.all([a, b]);
  assert.equal(midi.accesses.length, 1);
  midi.emit(PLAY_A);
  assert.deepEqual(presses, ["A"]);
});

test("unplug and replug while connected keeps one handler and shows the state", async () => {
  const { midi, presses, statuses, control } = harness();
  await control.toggle();
  midi.plug(false);
  assert.equal(statuses.at(-1).connected, false);
  assert.equal(midi.accesses[0].input.onmidimessage, null);
  midi.plug(true);
  midi.plug(true);
  assert.equal(statuses.at(-1).connected, true);
  midi.emit(PLAY_A);
  assert.deepEqual(presses, ["A"]);
});

test("pressing while unplugged disconnects", async () => {
  const { midi, presses, statuses, control } = harness();
  await control.toggle();
  midi.plug(false);
  assert.equal(statuses.at(-1).state, "open");
  assert.equal(statuses.at(-1).connected, false);
  assert.equal(control.active, true);
  assert.equal(control.connected, false);
  assert.equal(controllerStatusText(statuses.at(-1)), "MIDI接続済み：機器なし（挿すと使えます）");
  await control.toggle();
  assert.equal(control.active, false);
  assert.equal(control.connected, false);
  assert.equal(statuses.at(-1).state, "idle");
  assert.equal(midi.accesses.length, 1);
  assert.equal(midi.accesses[0].onstatechange, null);
  midi.plug(true);
  assert.equal(midi.emit(PLAY_A), 0);
  assert.deepEqual(presses, []);
});

test("connected is true only while a live input is attached", async () => {
  const { midi, control } = harness();
  assert.equal(control.active, false);
  assert.equal(control.connected, false);
  midi.hold();
  const pending = control.toggle();
  assert.equal(control.active, true);
  assert.equal(control.connected, false);
  midi.grant();
  await pending;
  assert.equal(control.active, true);
  assert.equal(control.connected, true);
  midi.plug(false);
  assert.equal(control.active, true);
  assert.equal(control.connected, false);
  midi.plug(true);
  assert.equal(control.connected, true);
  await control.toggle();
  assert.equal(control.active, false);
  assert.equal(control.connected, false);
});

test("an open with no devices can be disconnected", async () => {
  const statuses = [];
  const control = createMidiControl({}, (status) => statuses.push(status), {
    navigator: {
      requestMIDIAccess: () => Promise.resolve({ inputs: new Map(), outputs: new Map(), onstatechange: null }),
    },
  });
  await control.toggle();
  assert.equal(control.active, true);
  assert.equal(control.connected, false);
  assert.equal(statuses.at(-1).state, "open");
  assert.deepEqual(statuses.at(-1).names, []);
  assert.equal(controllerStatusText(statuses.at(-1)), "MIDI接続済み：機器なし（挿すと使えます）");
  await control.toggle();
  assert.equal(control.active, false);
  assert.equal(statuses.at(-1).state, "idle");
});

test("two live inputs each fire once", async () => {
  const deck = { id: "a", name: "DDJ-FLX4", state: "connected", onmidimessage: null };
  const keys = { id: "b", name: "Keyboard", state: "connected", onmidimessage: null };
  const presses = [];
  const statuses = [];
  const control = createMidiControl({ togglePlay: (deckName) => presses.push(deckName) }, (status) => statuses.push(status), {
    navigator: {
      requestMIDIAccess: () =>
        Promise.resolve({
          inputs: new Map([
            [deck.id, deck],
            [keys.id, keys],
          ]),
          outputs: new Map(),
          onstatechange: null,
        }),
    },
  });
  await control.connect();
  assert.equal(control.connected, true);
  assert.deepEqual(statuses.at(-1).names, ["DDJ-FLX4", "Keyboard"]);
  deck.onmidimessage({ data: PLAY_A });
  keys.onmidimessage({ data: PLAY_A });
  assert.deepEqual(presses, ["A", "A"]);
});

test("replug with a different input object keeps one live handler", async () => {
  const first = { id: "in", name: "DDJ-FLX4", state: "connected", onmidimessage: null };
  const inputs = new Map([[first.id, first]]);
  const access = {
    get inputs() {
      return new Map([...inputs].filter(([, port]) => port.state !== "disconnected"));
    },
    outputs: new Map(),
    onstatechange: null,
  };
  const presses = [];
  const control = createMidiControl({ togglePlay: (deck) => presses.push(deck) }, () => {}, {
    navigator: { requestMIDIAccess: () => Promise.resolve(access) },
  });
  await control.toggle();
  first.state = "disconnected";
  const next = { id: "in2", name: "DDJ-FLX4", state: "connected", onmidimessage: null };
  inputs.clear();
  inputs.set(next.id, next);
  access.onstatechange({ port: next });
  assert.equal(first.onmidimessage, null);
  next.onmidimessage({ data: PLAY_A });
  first.onmidimessage?.({ data: PLAY_A });
  assert.deepEqual(presses, ["A"]);
  assert.equal(control.connected, true);
});

test("a reused MIDIInput across accesses runs one action", async () => {
  const input = { id: "in", name: "DDJ-FLX4", state: "connected", onmidimessage: null };
  let requests = 0;
  const presses = [];
  const control = createMidiControl({ togglePlay: (deck) => presses.push(deck) }, () => {}, {
    navigator: {
      requestMIDIAccess() {
        requests += 1;
        return Promise.resolve({
          inputs: new Map([[input.id, input]]),
          outputs: new Map(),
          onstatechange: null,
        });
      },
    },
  });
  await control.toggle();
  await control.toggle();
  assert.equal(input.onmidimessage, null);
  await control.toggle();
  assert.equal(requests, 2);
  input.onmidimessage({ data: PLAY_A });
  assert.deepEqual(presses, ["A"]);
});

test("a disconnected port that stays listed is not bound", async () => {
  const input = { id: "in", name: "DDJ-FLX4", state: "connected", onmidimessage: null };
  const sent = [];
  const output = { id: "out", name: "DDJ-FLX4", state: "connected", send: (message) => sent.push([...message]) };
  const access = {
    inputs: new Map([[input.id, input]]),
    outputs: new Map([[output.id, output]]),
    onstatechange: null,
  };
  const presses = [];
  const statuses = [];
  const ledPort = createFlx4LedPort();
  const control = createMidiControl({ togglePlay: (deck) => presses.push(deck) }, (status) => statuses.push(status), {
    navigator: { requestMIDIAccess: () => Promise.resolve(access) },
    ledPort,
  });
  await control.connect();
  paintFlx4Leds(ledPort, "A", true, false);
  const painted = sent.length;
  input.state = "disconnected";
  output.state = "disconnected";
  access.onstatechange({ port: input });
  assert.equal(statuses.at(-1).connected, false);
  assert.deepEqual(statuses.at(-1).names, []);
  assert.equal(input.onmidimessage, null);
  assert.equal(sent.length, painted);
  assert.deepEqual(ledPort.outputs, []);
  assert.equal(control.connected, false);
  input.onmidimessage?.({ data: PLAY_A });
  assert.deepEqual(presses, []);
});

test("a statechange after disconnect does not bind again", async () => {
  const { midi, presses, control } = harness();
  await control.toggle();
  const access = midi.accesses[0];
  const late = access.onstatechange;
  control.disconnect();
  late?.({ port: access.input });
  assert.equal(midi.emit(PLAY_A), 0);
  assert.deepEqual(presses, []);
});

test("a rejected open can be tried again, and cancel wins over a late rejection", async () => {
  const statuses = [];
  let rejectAccess = null;
  let requests = 0;
  const control = createMidiControl({ togglePlay() {} }, (status) => statuses.push(status), {
    navigator: {
      requestMIDIAccess() {
        requests += 1;
        if (requests === 1) {
          return new Promise((_, reject) => {
            rejectAccess = reject;
          });
        }
        return Promise.reject(new Error("denied"));
      },
    },
  });
  const first = control.toggle();
  assert.equal(statuses.at(-1).state, "opening");
  await control.toggle();
  assert.equal(statuses.at(-1).state, "idle");
  rejectAccess(new Error("denied"));
  await first;
  assert.equal(statuses.at(-1).state, "idle");
  assert.equal(control.active, false);

  await control.toggle();
  assert.equal(requests, 2);
  assert.equal(statuses.at(-1).state, "denied");
  assert.equal(control.active, false);
  await control.toggle();
  assert.equal(requests, 3);
  assert.equal(statuses.at(-1).state, "denied");
});

test("disconnect turns the lamps off and connect paints them again", async () => {
  const { midi, control, ledPort } = harness();
  await control.toggle();
  paintFlx4Leds(ledPort, "A", true, false);
  const sent = midi.accesses[0].sent;
  sent.length = 0;
  control.disconnect();
  assert.deepEqual(sent, [
    [0x90, 0x0b, 0x00],
    [0x90, 0x47, 0x00],
    [0x90, 0x58, 0x00],
    [0x91, 0x0b, 0x00],
    [0x91, 0x47, 0x00],
    [0x91, 0x58, 0x00],
  ]);
  assert.deepEqual(ledPort.outputs, []);
  await control.toggle();
  assert.deepEqual(midi.accesses[1].sent.slice(0, 2), [
    [0x90, 0x0b, 0x7f],
    [0x90, 0x47, 0x7f],
  ]);
});
