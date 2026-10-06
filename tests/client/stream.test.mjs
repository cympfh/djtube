import assert from "node:assert/strict";
import test from "node:test";

import { listenerPathId, liveCatchUp, liveSocketUrl } from "../../djtube/static/stream.js";

test("listener path id is four uppercase letters under the public prefix", () => {
  assert.equal(listenerPathId("/djtube/stream/ABCD", "/djtube"), "ABCD");
  assert.equal(listenerPathId("/stream/WXYZ", "/djtube"), "WXYZ");
  assert.equal(listenerPathId("/djtube/stream/ABCD/", "/djtube"), "ABCD");
  assert.equal(listenerPathId("/djtube/stream/abcd", "/djtube"), null);
  assert.equal(listenerPathId("/djtube/stream/ABCDE", "/djtube"), null);
  assert.equal(listenerPathId("/djtube/", "/djtube"), null);
});

test("socket url keeps the public prefix and the page protocol", () => {
  assert.equal(liveSocketUrl("https:", "s.cympfh.cc", "/djtube", "ABCD"), "wss://s.cympfh.cc/djtube/api/live/ABCD");
  assert.equal(liveSocketUrl("http:", "127.0.0.1:8098", "/djtube", "WXYZ"), "ws://127.0.0.1:8098/djtube/api/live/WXYZ");
});

test("playback jumps to the live edge instead of replaying a backlog", () => {
  assert.equal(liveCatchUp(0, 0, 1.0), null);
  assert.equal(liveCatchUp(0, 0, 2.0), 1.7);
  assert.equal(liveCatchUp(0, 10, 12), 11.7);
  assert.equal(liveCatchUp(11.2, 10, 12), null);
  assert.equal(liveCatchUp(0, 1.2, 1.4), 1.2);
  assert.equal(liveCatchUp(Number.NaN, 0, 1), null);
});
