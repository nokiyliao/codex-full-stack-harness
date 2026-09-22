import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import fsp from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { spawn } from "node:child_process";
import { afterEach, test } from "node:test";
import { fileURLToPath } from "node:url";

const TEST_DIR = path.dirname(fileURLToPath(import.meta.url));
const SCRIPT = path.resolve(TEST_DIR, "..", "tura_codex_thread_writer_mcp.mjs");
const REQUEST_SCHEMA = "tura_codex_thread_writer_request_v1";
const RESPONSE_SCHEMA = "tura_codex_thread_writer_response_v1";
const cleanups = [];

afterEach(async () => {
  while (cleanups.length > 0) await cleanups.pop()();
});

function frame(value) {
  const payload = Buffer.from(JSON.stringify(value), "utf8");
  const output = Buffer.alloc(payload.length + 4);
  output.writeUInt32LE(payload.length, 0);
  payload.copy(output, 4);
  return output;
}

async function fakeHost(root, onRequest) {
  const socketPath = path.join(root, "fake-host.sock");
  const requests = [];
  const sockets = new Set();
  const server = net.createServer((socket) => {
    sockets.add(socket);
    let buffer = Buffer.alloc(0);
    socket.on("data", async (chunk) => {
      buffer = Buffer.concat([buffer, chunk]);
      while (buffer.length >= 4) {
        const length = buffer.readUInt32LE(0);
        if (buffer.length < length + 4) return;
        const request = JSON.parse(buffer.subarray(4, length + 4).toString("utf8"));
        buffer = buffer.subarray(length + 4);
        requests.push(request);
        const result = await onRequest(request, socket, requests);
        if (result !== undefined && !socket.destroyed) {
          socket.write(frame({ id: request.id, jsonrpc: "2.0", result }));
        }
      }
    });
    socket.on("close", () => sockets.delete(socket));
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(socketPath, resolve);
  });
  return {
    socketPath,
    requests,
    close: async () => {
      for (const socket of sockets) socket.destroy();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

function lineReader(stream) {
  let buffer = "";
  const waiters = [];
  stream.setEncoding("utf8");
  stream.on("data", (chunk) => {
    buffer += chunk;
    while (buffer.includes("\n")) {
      const newline = buffer.indexOf("\n");
      const line = buffer.slice(0, newline);
      buffer = buffer.slice(newline + 1);
      const waiter = waiters.shift();
      if (waiter) waiter.resolve(JSON.parse(line));
    }
  });
  return () => new Promise((resolve, reject) => waiters.push({ resolve, reject }));
}

async function waitFor(predicate, timeoutMs = 3000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await predicate();
    if (value) return value;
    await new Promise((resolve) => setTimeout(resolve, 15));
  }
  throw new Error("timed out waiting for condition");
}

async function startSidecar(hostPath) {
  const root = await fsp.mkdtemp("/tmp/tw-");
  const writerDir = path.join(root, "writer");
  const child = spawn(process.execPath, [SCRIPT], {
    env: {
      ...process.env,
      CODEX_APP_TOOLS_PIPE_PATH: hostPath,
      TURA_HOME: root,
      TURA_CODEX_THREAD_WRITER_DIR: writerDir,
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const readMcp = lineReader(child.stdout);
  const stop = async () => {
    if (child.exitCode === null) {
      child.stdin.end();
      await Promise.race([
        new Promise((resolve) => child.once("exit", resolve)),
        new Promise((_, reject) => setTimeout(() => reject(new Error(`sidecar did not exit: ${stderr}`)), 2000)),
      ]);
    }
    await fsp.rm(root, { recursive: true, force: true });
  };
  cleanups.push(stop);
  const endpointPath = await waitFor(async () => {
    try {
      const names = await fsp.readdir(writerDir);
      const name = names.find((candidate) => /^endpoint-\d+\.json$/.test(candidate));
      return name ? path.join(writerDir, name) : null;
    } catch {
      return null;
    }
  });
  const endpoint = JSON.parse(await fsp.readFile(endpointPath, "utf8"));
  await waitFor(() => fs.existsSync(endpoint.socket_path));
  return { child, endpoint, endpointPath, readMcp, writerDir, stop, stderr: () => stderr };
}

async function ipcRequest(socketPath, request) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(socketPath);
    let buffer = "";
    socket.setEncoding("utf8");
    socket.once("error", reject);
    socket.once("connect", () => socket.write(`${JSON.stringify(request)}\n`));
    socket.on("data", (chunk) => {
      buffer += chunk;
      const newline = buffer.indexOf("\n");
      if (newline === -1) return;
      const response = JSON.parse(buffer.slice(0, newline));
      socket.destroy();
      resolve(response);
    });
  });
}

function toolCatalog() {
  return {
    tools: [
      { name: "read_thread", namespace: "codex_app", inputSchema: { type: "object" } },
      { name: "send_message_to_thread", namespace: "codex_app", inputSchema: { type: "object" } },
      { name: "navigate_to_codex_page", namespace: "codex_app", inputSchema: { type: "object" } },
    ],
  };
}

test("stdio MCP exposes only the bounded status tool and a 0600 per-process endpoint", async () => {
  const root = await fsp.mkdtemp("/tmp/th-");
  cleanups.push(() => fsp.rm(root, { recursive: true, force: true }));
  const host = await fakeHost(root, async () => toolCatalog());
  cleanups.push(host.close);
  const sidecar = await startSidecar(host.socketPath);

  sidecar.child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2024-11-05" } })}\n`);
  const initialized = await sidecar.readMcp();
  assert.equal(initialized.result.serverInfo.name, "tura-codex-thread-writer");
  sidecar.child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} })}\n`);
  const listed = await sidecar.readMcp();
  assert.deepEqual(listed.result.tools.map((tool) => tool.name), ["status"]);
  assert.equal(listed.result.tools[0].inputSchema.additionalProperties, false);
  sidecar.child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id: 3, method: "tools/call", params: { name: "status", arguments: {} } })}\n`);
  const status = await sidecar.readMcp();
  assert.equal(status.result.structuredContent.ready, true);

  assert.match(path.basename(sidecar.endpointPath), /^endpoint-\d+\.json$/);
  assert.match(path.basename(sidecar.endpoint.socket_path), /^thread-writer-\d+\.sock$/);
  assert.equal(sidecar.endpoint.pid, sidecar.child.pid);
  assert.equal(sidecar.endpoint.protocol_version, "tura_codex_thread_writer_v1");
  assert.equal(sidecar.endpoint.app_tools_pipe_basename, path.basename(host.socketPath));
  assert.equal(
    sidecar.endpoint.app_tools_pipe_sha256,
    crypto.createHash("sha256").update(host.socketPath).digest("hex"),
  );
  assert.equal((await fsp.stat(sidecar.endpointPath)).mode & 0o777, 0o600);
  assert.equal((await fsp.stat(sidecar.endpoint.socket_path)).mode & 0o777, 0o600);
});

test("Router IPC uses exact host tool identities and one send call", async () => {
  const root = await fsp.mkdtemp("/tmp/th-");
  cleanups.push(() => fsp.rm(root, { recursive: true, force: true }));
  const host = await fakeHost(root, async (request) => {
    if (request.method === "tools/list") return toolCatalog();
    return { success: true, contentItems: [{ type: "inputText", text: "accepted" }] };
  });
  cleanups.push(host.close);
  const sidecar = await startSidecar(host.socketPath);
  const message = "bounded Commander callback";
  const send = {
    schema_version: REQUEST_SCHEMA,
    request_id: "request-001",
    operation: "send_message_to_thread",
    target_thread_id: "thread-001",
    turn_id: "turn-001",
    call_id: "request-001",
    message,
    message_sha256: crypto.createHash("sha256").update(message).digest("hex"),
  };
  const response = await ipcRequest(sidecar.endpoint.socket_path, send);
  assert.equal(response.schema_version, RESPONSE_SCHEMA);
  assert.equal(response.request_id, send.request_id);
  assert.equal(response.ok, true);

  const sendCalls = host.requests.filter(
    (request) => request.method === "tools/call" && request.params.tool === "send_message_to_thread",
  );
  assert.equal(sendCalls.length, 1);
  assert.deepEqual(sendCalls[0].params, {
    arguments: { prompt: message, threadId: "thread-001" },
    callId: "request-001",
    namespace: "codex_app",
    threadId: "thread-001",
    tool: "send_message_to_thread",
    turnId: "turn-001",
  });
  assert.deepEqual(
    host.requests.filter((request) => request.method === "tools/list")[0].params,
    { threadStartKind: "all" },
  );

  const read = await ipcRequest(sidecar.endpoint.socket_path, {
    schema_version: REQUEST_SCHEMA,
    request_id: "read-001",
    operation: "read_thread",
    target_thread_id: "thread-001",
  });
  assert.equal(read.ok, true);
  const readCall = host.requests.find(
    (request) => request.method === "tools/call" && request.params.tool === "read_thread",
  );
  assert.deepEqual(readCall.params, {
    arguments: {
      threadId: "thread-001",
      turnLimit: 10,
      includeOutputs: false,
      maxOutputCharsPerItem: 20000,
    },
    callId: "tura-read-call-read-001",
    namespace: "codex_app",
    threadId: "thread-001",
    tool: "read_thread",
    turnId: "tura-read-turn-read-001",
  });
});

test("Router IPC rejects unknown fields and mismatched message hashes before host access", async () => {
  const root = await fsp.mkdtemp("/tmp/th-");
  cleanups.push(() => fsp.rm(root, { recursive: true, force: true }));
  const host = await fakeHost(root, async () => toolCatalog());
  cleanups.push(host.close);
  const sidecar = await startSidecar(host.socketPath);

  const closed = await ipcRequest(sidecar.endpoint.socket_path, {
    schema_version: REQUEST_SCHEMA,
    request_id: "cap-001",
    operation: "capabilities",
    arbitrary_tool: "navigate_to_codex_page",
  });
  assert.equal(closed.ok, false);
  assert.equal(closed.error.code, "INVALID_REQUEST_SCHEMA");

  const hashMismatch = await ipcRequest(sidecar.endpoint.socket_path, {
    schema_version: REQUEST_SCHEMA,
    request_id: "send-002",
    operation: "send_message_to_thread",
    target_thread_id: "thread-001",
    turn_id: "turn-002",
    call_id: "send-002",
    message: "message",
    message_sha256: "0".repeat(64),
  });
  assert.equal(hashMismatch.ok, false);
  assert.equal(hashMismatch.error.code, "INVALID_MESSAGE_SHA256");
  assert.equal(host.requests.length, 0);
});

test("uncertain host EOF returns an error without retrying send", async () => {
  const root = await fsp.mkdtemp("/tmp/th-");
  cleanups.push(() => fsp.rm(root, { recursive: true, force: true }));
  let sendCount = 0;
  const host = await fakeHost(root, async (request, socket) => {
    if (request.method === "tools/list") return toolCatalog();
    if (request.method === "tools/call" && request.params.tool === "send_message_to_thread") {
      sendCount += 1;
      socket.destroy();
      return undefined;
    }
    return { success: true, contentItems: [] };
  });
  cleanups.push(host.close);
  const sidecar = await startSidecar(host.socketPath);
  const message = "delivery may have happened";
  const response = await ipcRequest(sidecar.endpoint.socket_path, {
    schema_version: REQUEST_SCHEMA,
    request_id: "uncertain-001",
    operation: "send_message_to_thread",
    target_thread_id: "thread-001",
    turn_id: "turn-uncertain-001",
    call_id: "uncertain-001",
    message,
    message_sha256: crypto.createHash("sha256").update(message).digest("hex"),
  });
  assert.equal(response.ok, false);
  assert.equal(response.error.code, "HOST_PIPE_CLOSED");
  await new Promise((resolve) => setTimeout(resolve, 100));
  assert.equal(sendCount, 1);
  assert.equal(
    host.requests.filter(
      (request) => request.method === "tools/call" && request.params.tool === "send_message_to_thread",
    ).length,
    1,
  );
});
