import assert from "node:assert/strict";
import { test } from "node:test";
import { restartRegisteredService, waitForIdleService } from "../src/workflow-update.mjs";

// The deploy restart waits for a moment with no turn in flight, bounded, and refuses otherwise.
const clock = (step = 200) => {
  let t = 0;
  return { now: () => t, sleep: async (ms) => { t += ms ?? step; } };
};

test("the restart happens in the first moment with no request or turn in flight", async () => {
  const seen = [
    { active_http_requests: 1, active_ws_turns: 0 },
    { active_http_requests: 0, active_ws_turns: 2 },
    { active_http_requests: 0, active_ws_turns: 0, websocket_connections: 4 },
  ];
  const calls = [];
  const { now, sleep } = clock();
  const wait = () => waitForIdleService({ fetchActivity: async () => { calls.push("health"); return seen.shift(); }, now, sleep });
  const window = await restartRegisteredService(async (file, args) => { calls.push(args[0]); }, wait);
  assert.deepEqual(calls, ["print", "health", "health", "health", "kickstart"]);
  assert.deepEqual(window, { idle: true, health: "idle", waited_ms: 400 });
});

test("a busy service is not restarted once the bound passes", async () => {
  const { now, sleep } = clock();
  const busy = { active_http_requests: 0, active_ws_turns: 1 };
  const window = await waitForIdleService({ fetchActivity: async () => busy, timeoutMs: 1000, now, sleep });
  assert.equal(window.idle, false);
  assert.equal(window.health, "busy");
  assert.ok(window.waited_ms >= 1000);
  assert.deepEqual(window.activity, busy);
  const calls = [];
  await assert.rejects(restartRegisteredService(async (file, args) => { calls.push(args[0]); }, async () => window),
    (error) => error.code === "RELOAD_REFUSED_NOT_IDLE" && error.window === window);
  assert.deepEqual(calls, ["print"]);
});

test("an unreadable health endpoint refuses the restart at once", async () => {
  const { now, sleep } = clock();
  let slept = false;
  const window = await waitForIdleService({ fetchActivity: async () => { throw new Error("ECONNREFUSED"); },
    now, sleep: async () => { slept = true; await sleep(); } });
  assert.deepEqual(window, { idle: false, health: "unknown", waited_ms: 0 });
  assert.equal(slept, false);
  const calls = [];
  await assert.rejects(restartRegisteredService(async (file, args) => { calls.push(args[0]); }, async () => window),
    (error) => error.code === "RELOAD_REFUSED_NOT_IDLE");
  assert.deepEqual(calls, ["print"]);
});

test("malformed activity is not mistaken for idle", async () => {
  const { now, sleep } = clock();
  const window = await waitForIdleService({ fetchActivity: async () => ({}), timeoutMs: 400, now, sleep });
  assert.equal(window.idle, false);
});

test("a supervised service hot-reloads with SIGHUP and never waits for idle or kickstarts", async () => {
  const calls = [], kills = [];
  const window = await restartRegisteredService(async (file, args) => { calls.push(args[0]); },
    async () => { throw new Error("a hot reload must not wait for idle"); },
    { supervisor_pid: 41, worker_pid: 77 }, (pid, signal) => kills.push([pid, signal]));
  assert.deepEqual(calls, ["print"]);
  assert.deepEqual(kills, [[41, "SIGHUP"]]);
  assert.deepEqual(window, { mode: "hot_reload", supervisor_pid: 41, prior_worker_pid: 77 });
});

