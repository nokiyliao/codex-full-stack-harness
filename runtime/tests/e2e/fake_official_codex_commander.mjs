#!/usr/bin/env node
import crypto from "node:crypto";
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import readline from "node:readline";

const args = process.argv.slice(2);
if (args[0] === "--owner-server") {
  await runOwnerServer(args[1], args[2]);
  process.exit(0);
}
if (args[0] === "--watch-ledger-and-kill") {
  await watchLedgerAndKill(args[1], args[2]);
  process.exit(0);
}
if (args.includes("--version")) {
  process.stdout.write("codex-cli 9.9.9-p5-e2e\n");
  process.exit(0);
}
if (!args.includes("app-server")) throw new Error(`unsupported invocation: ${args.join(" ")}`);
if (process.env.TURA_P5_HARD_CRASH_E2E !== "1") throw new Error("P5 hard-crash fault is not armed");

const workspace = process.env.TURA_CWD || process.cwd();
const turaDir = path.join(workspace, ".tura");
const statePath = path.join(turaDir, "p5-fake-commander-state.json");
fs.mkdirSync(turaDir, { recursive: true });
const state = fs.existsSync(statePath) ? JSON.parse(fs.readFileSync(statePath, "utf8")) : defaultState();
const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of lines) {
  if (!line.trim()) continue;
  const message = JSON.parse(line);
  const id = message.id;
  if (message.method === "initialize") respond(id, { userAgent: "p5-e2e", platformFamily: "unix", platformOs: "test" });
  else if (message.method === "initialized") continue;
  else if (message.method === "thread/start") respond(id, { thread: { id: "thread-p5-child", sessionId: "codex-session-p5-child" } });
  else if (message.method === "thread/resume") respond(id, { thread: { id: state.thread_id, sessionId: state.codex_session_id } });
  else if (message.method === "thread/loaded/list") respond(id, { data: [state.thread_id], nextCursor: null });
  else if (message.method === "thread/turns/list") respond(id, { data: state.turns, nextCursor: null, backwardsCursor: null });
  else if (message.method === "thread/read") {
    const commander = message.params?.threadId === state.thread_id;
    respond(id, { thread: {
      id: commander ? state.thread_id : "thread-p5-child",
      sessionId: commander ? state.codex_session_id : "codex-session-p5-child",
      turns: commander ? state.turns : state.child_turns,
    } });
  } else if (message.method === "turn/start") {
    const commander = message.params?.threadId === state.thread_id;
    const turns = commander ? state.turns : state.child_turns;
    const turnId = commander ? `turn-p5-${state.turn_start_count + 1}` : `turn-p5-child-${turns.length + 1}`;
    const text = commander ? JSON.stringify({ schema_version: "tura_commander_convergence_result_v1", requested_action: "MISSION_VERIFICATION", disposition: "route_selected", first_false_predicate: "P6_CONTINUATION_RECOVERY_NO_BLIND_RETRY", selected_route: "P6_CONTINUATION_RECOVERY_FAULT_MATRIX" }) : "P5 child completed";
    if (commander) state.turn_start_count += 1;
    const clientId = message.params?.clientUserMessageId;
    const input = message.params?.input ?? [];
    turns.push({ id: turnId, status: "completed", items: [
      ...(commander ? [{ type: "userMessage", id: `item-guidance-${turnId}`, clientId, content: input }] : []),
      { type: "agentMessage", id: `item-${turnId}`, text, phase: "final_answer" },
    ] });
    writeState();
    respond(id, { turn: { id: turnId, status: "inProgress", items: [] } });
    notify("item/completed", { threadId: commander ? state.thread_id : "thread-p5-child", turnId, item: { type: "agentMessage", id: `item-${turnId}`, text, phase: "final_answer" } });
    notify("turn/completed", { threadId: commander ? state.thread_id : "thread-p5-child", turn: turns.at(-1) });
  } else throw new Error(`unexpected method ${message.method}`);
}

function defaultState() { return { schema_version: "p5_fake_commander_state_v1", thread_id: "thread-p5-global-commander", codex_session_id: "codex-session-p5-global-commander", turn_start_count: 0, turns: [], child_turns: [] }; }
function writeState() { const tmp = `${statePath}.${crypto.randomUUID()}.tmp`; fs.writeFileSync(tmp, JSON.stringify(state)); fs.renameSync(tmp, statePath); }
function respond(id, result) { process.stdout.write(`${JSON.stringify({ id, result })}\n`); }
function notify(method, params) { process.stdout.write(`${JSON.stringify({ method, params })}\n`); }

async function watchLedgerAndKill(root, turaHome) {
  const marker = path.join(root, ".tura", "p5-watcher-armed.json");
  if (!path.isAbsolute(turaHome)) throw new Error("watcher TURA_HOME must be absolute");
  fs.mkdirSync(path.dirname(marker), { recursive: true });
  fs.writeFileSync(marker, JSON.stringify({ armed: true, watcher_pid: process.pid }));
  const deadline = Date.now() + 45_000;
  while (Date.now() < deadline) {
    const ledgerRoot = path.join(root, ".tura", "run", "effect_ledgers");
    if (fs.existsSync(ledgerRoot)) {
      for (const name of fs.readdirSync(ledgerRoot)) {
        const file = path.join(ledgerRoot, name);
        if (!name.endsWith(".json") || !fs.readFileSync(file, "utf8").includes('"commander_convergence_proof"')) continue;
        const runtimePid = onlyRouterRuntimePid(turaHome);
        if (!runtimePid) continue;
        fs.writeFileSync(path.join(root, ".tura", "p5-hard-crash-observed.json"), JSON.stringify({ ledger: name, runtime_pid: runtimePid }));
        process.kill(runtimePid, "SIGKILL");
        return;
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
  process.exitCode = 2;
}

function onlyRouterRuntimePid(turaHome) {
  const endpointPath = path.join(turaHome, "db", "session_log", "router.addr");
  if (!fs.existsSync(endpointPath)) return null;
  const routerPid = Number(JSON.parse(fs.readFileSync(endpointPath, "utf8")).pid);
  if (!Number.isSafeInteger(routerPid) || routerPid <= 1) return null;
  const rows = execFileSync("ps", ["-axo", "pid=,ppid=,command="], { encoding: "utf8" })
    .split("\n")
    .map((line) => line.trim().match(/^(\d+)\s+(\d+)\s+(.+)$/u))
    .filter(Boolean)
    .map((match) => ({ pid: Number(match[1]), ppid: Number(match[2]), command: match[3] }));
  const descendants = new Set([routerPid]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const row of rows) {
      if (descendants.has(row.ppid) && !descendants.has(row.pid)) {
        descendants.add(row.pid);
        changed = true;
      }
    }
  }
  const runtimes = rows.filter((row) => descendants.has(row.pid) && /(?:^|\/)tura_runtime(?:\s|$)/u.test(row.command));
  return runtimes.length === 1 ? runtimes[0].pid : null;
}

async function runOwnerServer(socketPath, workspace) {
  if (!path.isAbsolute(socketPath) || !path.isAbsolute(workspace)) throw new Error("owner paths must be absolute");
  fs.rmSync(socketPath, { force: true });
  const server = net.createServer((connection) => bridgeOwnerConnection(connection, workspace));
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(socketPath, resolve);
  });
  fs.writeFileSync(`${socketPath}.ready.json`, JSON.stringify({ ready: true, socket_path: socketPath, pid: process.pid }));
  await new Promise((resolve) => {
    const stop = () => server.close(resolve);
    process.once("SIGTERM", stop);
    process.once("SIGINT", stop);
  });
  fs.rmSync(socketPath, { force: true });
  fs.rmSync(`${socketPath}.ready.json`, { force: true });
}

function bridgeOwnerConnection(connection, workspace) {
  const backend = spawn(process.execPath, [process.argv[1], "app-server", "--listen", "stdio://"], {
    env: { ...process.env, TURA_CWD: workspace, TURA_P5_HARD_CRASH_E2E: "1", TURA_FAKE_OWNER_BACKEND: "1" },
    stdio: ["pipe", "pipe", "inherit"],
  });
  const backendLines = readline.createInterface({ input: backend.stdout, crlfDelay: Infinity });
  backendLines.on("line", (line) => sendFrame(connection, 0x1, Buffer.from(line)));
  backend.once("exit", () => {
    if (!connection.destroyed) {
      sendFrame(connection, 0x8, Buffer.alloc(0));
      connection.end();
    }
  });
  connection.once("close", () => {
    backendLines.close();
    if (!backend.killed) backend.kill("SIGTERM");
  });

  let upgraded = false;
  let buffered = Buffer.alloc(0);
  connection.on("data", (chunk) => {
    buffered = Buffer.concat([buffered, chunk]);
    if (!upgraded) {
      const boundary = buffered.indexOf("\r\n\r\n");
      if (boundary < 0) return;
      const headers = buffered.subarray(0, boundary + 4).toString("utf8");
      const key = headers.match(/^Sec-WebSocket-Key:\s*(.+)$/imu)?.[1]?.trim();
      if (!key) throw new Error("missing WebSocket key");
      const accept = crypto.createHash("sha1").update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`).digest("base64");
      connection.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
      buffered = buffered.subarray(boundary + 4);
      upgraded = true;
    }
    buffered = consumeFrames(buffered, (opcode, payload) => {
      if (opcode === 0x1) backend.stdin.write(`${payload.toString("utf8")}\n`);
      else if (opcode === 0x8) connection.end();
      else if (opcode === 0x9) sendFrame(connection, 0xA, payload);
    });
  });
}

function consumeFrames(buffer, onFrame) {
  let offset = 0;
  while (buffer.length - offset >= 2) {
    const first = buffer[offset];
    const second = buffer[offset + 1];
    let length = second & 0x7f;
    let header = 2;
    if (length === 126) {
      if (buffer.length - offset < 4) break;
      length = buffer.readUInt16BE(offset + 2);
      header = 4;
    } else if (length === 127) {
      if (buffer.length - offset < 10) break;
      length = Number(buffer.readBigUInt64BE(offset + 2));
      header = 10;
    }
    const masked = (second & 0x80) !== 0;
    const maskBytes = masked ? 4 : 0;
    if (buffer.length - offset < header + maskBytes + length) break;
    const mask = masked ? buffer.subarray(offset + header, offset + header + 4) : null;
    const payload = Buffer.from(buffer.subarray(offset + header + maskBytes, offset + header + maskBytes + length));
    if (mask) for (let index = 0; index < payload.length; index += 1) payload[index] ^= mask[index % 4];
    onFrame(first & 0x0f, payload);
    offset += header + maskBytes + length;
  }
  return buffer.subarray(offset);
}

function sendFrame(socket, opcode, payload) {
  if (socket.destroyed) return;
  let header;
  if (payload.length < 126) {
    header = Buffer.from([0x80 | opcode, payload.length]);
  } else if (payload.length <= 0xffff) {
    header = Buffer.alloc(4);
    header[0] = 0x80 | opcode;
    header[1] = 126;
    header.writeUInt16BE(payload.length, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x80 | opcode;
    header[1] = 127;
    header.writeBigUInt64BE(BigInt(payload.length), 2);
  }
  socket.write(Buffer.concat([header, payload]));
}
