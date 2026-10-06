// One AudioContext for both decks and the scratch voices.
// `master` is the mix that reaches the speakers. A later stream can connect
// a MediaStreamAudioDestinationNode to this same gain.

export function createAudioBus() {
  return {
    context: null,
    master: null,
    failed: false,
  };
}

export function openAudioBus(bus) {
  if (!bus || bus.failed) return false;
  if (bus.context && bus.master) return true;
  if (typeof window === "undefined") {
    bus.failed = true;
    return false;
  }
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) {
    bus.failed = true;
    return false;
  }
  try {
    const context = new Ctx();
    const master = context.createGain();
    master.gain.value = 1;
    master.connect(context.destination);
    bus.context = context;
    bus.master = master;
    return true;
  } catch {
    bus.failed = true;
    return false;
  }
}
