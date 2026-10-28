import assert from "node:assert/strict";
import cluster from "node:cluster";
import { EventEmitter } from "node:events";
import http from "node:http";
import net from "node:net";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { runSupervisor } from "../src/server.mjs";

function fakeCluster() {
  let id = 0;
  const api = new EventEmitter();
  api.workers = {};
  api.forks = [];
  api.setupPrimary = (options) => { api.setup = options; };
  api.fork = () => {
    const worker = new EventEmitter();
    worker.id = ++id;
    worker.dead = false;
    worker.sent = [];
    worker.process = { pid: 1000 + worker.id, kill: (signal) => { worker.killed = signal; worker.die(null, signal); } };
    worker.isDead = () => worker.dead;
    worker.send = (message) => worker.sent.push(message);
    worker.die = (code = 0, signal = null) => {
      if (worker.dead) return;
      worker.dead = true;
      delete api.workers[worker.id];
      worker.emit("exit", code, signal);
      api.emit("exit", worker, code, signal);
    };
    worker.ready = () => worker.emit("message", { type: "switchboard-ready", adapter_source_sha256: `s${worker.id}` });
    api.workers[worker.id] = worker;
    api.forks.push(worker);
    return worker;
  };
  return api;
}

function supervise(options = {}) {
  const api = fakeCluster();
  const signals = new EventEmitter();
  const exits = [], logs = [];
  let clock = 0;
  const supervisor = runSupervisor({ clusterApi: api, exec: "/fixture/server.mjs", signals, readyTimeoutMs: 1000,
    drainTimeoutMs: 1234, exit: (code) => exits.push(code), log: (entry) => logs.push(entry), now: () => clock, ...options });
  return { api, signals, exits, logs, supervisor, tick: (ms) => { clock += ms; } };
}

test("a reload hands over to a ready successor and drains only the predecessor", async () => {
  const f = supervise();
  assert.equal(f.api.setup.exec, "/fixture/server.mjs");
  const [first] = f.api.forks;
  first.ready();
  const reloaded = f.supervisor.reload("SIGHUP");
  const successor = f.api.forks[1];
  assert.deepEqual(first.sent, [], "nothing drains before the successor is ready");
  successor.ready();
  assert.equal(await reloaded, true);
  assert.deepEqual(first.sent, [{ type: "switchboard-drain", timeoutMs: 1234 }]);
  assert.deepEqual(successor.sent, []);
  assert.equal(f.supervisor.current(), successor);
  first.die(0);
  assert.equal(f.api.forks.length, 2, "a drained predecessor is not restarted");
  assert.equal(f.logs.at(-1).event, "reload_ready");
  assert.equal(f.logs.at(-1).adapter_source_sha256, "s2");
});

test("a successor that dies before ready leaves the predecessor serving", async () => {
  const f = supervise();
  const [first] = f.api.forks;
  const reloaded = f.supervisor.reload("SIGHUP");
  f.api.forks[1].die(1);
  assert.equal(await reloaded, false);
  assert.deepEqual(first.sent, []);
  assert.equal(f.supervisor.current(), first);
  assert.equal(f.api.forks.length, 2);
  assert.equal(f.logs.at(-1).failure, "exit:1");
});

test("a successor that never reports ready is terminated and the predecessor keeps serving", async () => {
  const f = supervise({ readyTimeoutMs: 20 });
  const [first] = f.api.forks;
  assert.equal(await f.supervisor.reload("SIGHUP"), false);
  assert.equal(f.api.forks[1].killed, "SIGTERM");
  assert.equal(f.supervisor.current(), first);
  assert.deepEqual(first.sent, []);
});

test("signals trigger one coalesced reload", async () => {
  const f = supervise();
  f.signals.emit("SIGHUP");
  f.signals.emit("SIGUSR2");
  const again = f.supervisor.reload("manual");
  assert.equal(f.api.forks.length, 2);
  f.api.forks[1].ready();
  assert.equal(await again, true);
  assert.equal(f.api.forks[0].sent.length, 1);
});

test("an unexpected exit restarts the current worker and a crash loop is left to launchd", () => {
  const f = supervise();
  f.api.forks[0].die(1);
  assert.equal(f.api.forks.length, 2);
  assert.equal(f.supervisor.current(), f.api.forks[1]);
  for (let n = 1; n <= 4; n++) f.api.forks[n].die(1);
  assert.deepEqual(f.exits, [1]);
  f.tick(61_000);
});

test("SIGTERM stops every worker and exits once they are gone", () => {
  const f = supervise();
  f.signals.emit("SIGTERM");
  assert.equal(f.api.forks[0].killed, "SIGTERM");
  assert.deepEqual(f.exits, [0]);
  assert.equal(f.api.forks.length, 1, "stopping never restarts");
});

const freePort = () => new Promise((resolve, reject) => {
  const probe = net.createServer();
  probe.once("error", reject);
  probe.listen(0, "127.0.0.1", () => { const { port } = probe.address(); probe.close(() => resolve(port)); });
});
const get = (port, path) => new Promise((resolve, reject) => {
  const request = http.get({ host: "127.0.0.1", port, path, agent: false }, (response) => {
    let body = "";
    response.on("data", (chunk) => { body += chunk; });
    response.on("end", () => resolve(body));
  });
  request.once("error", reject);
});
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test("a real cluster reload refuses no connection and lets the predecessor finish its in-flight request", async () => {
  const port = await freePort();
  process.env.SUPERVISOR_FIXTURE_PORT = String(port);
  const signals = new EventEmitter();
  const supervisor = runSupervisor({ exec: fileURLToPath(new URL("./fixtures/supervisor-worker.mjs", import.meta.url)),
    signals, exit: () => {}, log: () => {}, readyTimeoutMs: 10_000 });
  try {
    let first;
    for (let n = 0; n < 200 && !first; n++) { first = await get(port, "/").catch(() => null); if (!first) await delay(25); }
    assert.ok(first, "first worker serves");
    const slow = get(port, "/slow");
    await delay(100);
    const refused = [];
    const seen = new Set();
    let polling = true;
    const poller = (async () => {
      while (polling) {
        try { seen.add(await get(port, "/")); } catch (error) { refused.push(error.code); }
        await delay(5);
      }
    })();
    assert.equal(await supervisor.reload("test"), true);
    await delay(150);
    polling = false;
    await poller;
    assert.equal(await slow, first, "the in-flight request finished on the predecessor");
    assert.deepEqual(refused, []);
    const successor = await get(port, "/");
    assert.notEqual(successor, first);
    assert.ok(seen.has(successor));
    for (let n = 0; n < 100 && Object.keys(cluster.workers).length > 1; n++) await delay(20);
    assert.equal(Object.keys(cluster.workers).length, 1, "the drained predecessor exited");
  } finally {
    signals.emit("SIGTERM");
    for (let n = 0; n < 100 && Object.keys(cluster.workers).length; n++) await delay(20);
  }
});
