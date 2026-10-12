import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import http from "node:http";
import test from "node:test";

const CHROME = ["/usr/bin/google-chrome", "/usr/bin/chromium"].find((path) => existsSync(path));
const PAGE = "http://127.0.0.1:8098/djtube/";

function request(url, method = "GET") {
  return new Promise((resolve, reject) => {
    const req = http.request(url, { method }, (res) => {
      let data = "";
      res.on("data", (chunk) => {
        data += chunk;
      });
      res.on("end", () => resolve({ status: res.statusCode, data }));
    });
    req.on("error", reject);
    req.end();
  });
}

async function serverUp() {
  try {
    const response = await request(PAGE);
    return response.status === 200;
  } catch {
    return false;
  }
}

function connect(url) {
  const ws = new WebSocket(url);
  let next = 0;
  const pending = new Map();
  const ready = new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve);
    ws.addEventListener("error", reject);
  });
  ws.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    const done = pending.get(message.id);
    if (!done) return;
    pending.delete(message.id);
    if (message.error) done.reject(new Error(JSON.stringify(message.error)));
    else done.resolve(message.result);
  });
  return {
    ready,
    send(method, params = {}) {
      const id = ++next;
      ws.send(JSON.stringify({ id, method, params }));
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
      });
    },
    close() {
      ws.close();
    },
  };
}

async function openPage(chromePort, width, height) {
  const created = await request(`http://127.0.0.1:${chromePort}/json/new?${encodeURIComponent(PAGE)}`, "PUT");
  const tab = JSON.parse(created.data);
  const session = connect(tab.webSocketDebuggerUrl);
  await session.ready;
  await session.send("Emulation.setDeviceMetricsOverride", {
    width,
    height,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await session.send("Page.enable");
  await session.send("Runtime.enable");
  await session.send("Page.reload", { ignoreCache: true });
  await session.send("Runtime.evaluate", {
    expression: "new Promise((resolve) => { const wait = () => (window.djtube?.actions ? resolve(true) : setTimeout(wait, 20)); wait(); })",
    awaitPromise: true,
  });
  return { session, tabId: tab.id };
}

async function evaluate(session, expression) {
  const result = await session.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || "evaluate failed");
  return result.result.value;
}

function rowScript(listId) {
  return `new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => {
    const selected = document.querySelector(${JSON.stringify(`#${listId} .is-selected`)});
    const rect = selected ? selected.getBoundingClientRect() : null;
    const list = document.getElementById(${JSON.stringify(listId)});
    resolve({
      top: rect ? rect.top : null,
      bottom: rect ? rect.bottom : null,
      left: rect ? rect.left : null,
      right: rect ? rect.right : null,
      height: rect ? rect.height : 0,
      innerH: window.innerHeight,
      innerW: window.innerWidth,
      index: ${listId === "playlist-tracks" ? "window.djtube.state.playlistIndex" : "window.djtube.state.selected"},
      listClient: list.clientHeight,
      listScroll: list.scrollHeight,
      pageH: document.documentElement.scrollHeight,
    });
  })))`;
}

test("chosen rows stay inside a 768px-tall window while j moves down the list", async (t) => {
  if (!CHROME || !(await serverUp())) {
    t.skip("the local page and Chrome are required");
    return;
  }
  const port = 9340 + Math.floor(Math.random() * 200);
  const chrome = spawn(CHROME, [
    "--headless=new",
    `--remote-debugging-port=${port}`,
    "--no-sandbox",
    "--disable-dev-shm-usage",
    `--user-data-dir=/tmp/chrome-selection-${port}`,
    "about:blank",
  ], { stdio: "ignore" });
  try {
    let version = null;
    for (let attempt = 0; attempt < 40 && !version; attempt += 1) {
      try {
        version = await request(`http://127.0.0.1:${port}/json/version`);
      } catch {
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
    }
    assert.equal(version?.status, 200);
    const { session, tabId } = await openPage(port, 1366, 768);
    try {
      await evaluate(session, `(() => {
        const { state } = window.djtube;
        state.library = "search";
        state.results = Array.from({ length: 42 }, (_, index) => ({
          id: "t" + String(index).padStart(10, "0"),
          title: "生成曲 " + (index + 1),
          channel: "生成",
          duration: 180,
        }));
        state.selected = 0;
        if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur();
      })()`);
      const hidden = [];
      for (let step = 0; step < 41; step += 1) {
        await session.send("Input.dispatchKeyEvent", { type: "keyDown", key: "j", code: "KeyJ", text: "j", windowsVirtualKeyCode: 74 });
        await session.send("Input.dispatchKeyEvent", { type: "keyUp", key: "j", code: "KeyJ", windowsVirtualKeyCode: 74 });
        const row = await evaluate(session, rowScript("results"));
        const inside = row.height > 0 && row.top >= -0.5 && row.bottom <= row.innerH + 0.5 && row.left >= -0.5 && row.right <= row.innerW + 0.5;
        if (!inside) hidden.push({ step, top: row.top, bottom: row.bottom, index: row.index });
        if (step === 40) {
          assert.equal(row.index, 41);
          assert.ok(row.listScroll > row.listClient);
          assert.ok(row.pageH < 2000);
        }
      }
      assert.deepEqual(hidden, []);
    } finally {
      session.close();
      await request(`http://127.0.0.1:${port}/json/close/${tabId}`, "GET").catch(() => {});
    }
  } finally {
    chrome.kill();
  }
});
