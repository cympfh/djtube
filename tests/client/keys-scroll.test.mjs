import assert from "node:assert/strict";
import test from "node:test";

import { handleKeydown } from "../../djtube/static/keys.js";

const actions = new Proxy(
  {},
  {
    get: () => () => {},
  },
);

function target(tagName, extra = {}) {
  return {
    tagName,
    closest() {
      return null;
    },
    ...extra,
  };
}

function keyEvent(key, node, extra = {}) {
  return {
    key,
    shiftKey: false,
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    target: node,
    defaultPrevented: false,
    preventDefault() {
      this.defaultPrevented = true;
    },
    ...extra,
  };
}

test("space and deck keys do not scroll the page unless a field is being typed in", () => {
  const space = keyEvent(" ", target("BODY"));
  assert.equal(handleKeydown(space, actions), false);
  assert.equal(space.defaultPrevented, true);

  const search = keyEvent(" ", target("INPUT", { id: "search-input", type: "search" }));
  assert.equal(handleKeydown(search, actions), false);
  assert.equal(search.defaultPrevented, false);

  const named = keyEvent(" ", target("INPUT", { id: "playlist-name", type: "text" }));
  assert.equal(handleKeydown(named, actions), false);
  assert.equal(named.defaultPrevented, false);

  const music = keyEvent(" ", target("INPUT", { id: "music-only", type: "checkbox" }));
  assert.equal(handleKeydown(music, actions), false);
  assert.equal(music.defaultPrevented, false);

  const down = keyEvent("ArrowDown", target("BODY"));
  assert.equal(handleKeydown(down, actions), true);
  assert.equal(down.defaultPrevented, true);

  const home = keyEvent("Home", target("BODY"));
  assert.equal(handleKeydown(home, actions), true);
  assert.equal(home.defaultPrevented, true);

  const pageDown = keyEvent("PageDown", target("BODY"));
  assert.equal(handleKeydown(pageDown, actions), true);
  assert.equal(pageDown.defaultPrevented, true);

  const button = target("BUTTON", {
    closest(selector) {
      return selector === "button" ? button : null;
    },
  });
  const onButton = keyEvent(" ", button);
  assert.equal(handleKeydown(onButton, actions), false);
  assert.equal(onButton.defaultPrevented, false);
});
