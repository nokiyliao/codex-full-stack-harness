#!/usr/bin/env node

import crypto from "node:crypto";
import fs from "node:fs";
import fsp from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import process from "node:process";
import readline from "node:readline";
import { fileURLToPath } from "node:url";

const REQUEST_SCHEMA = "tura_codex_thread_writer_request_v1";
const RESPONSE_SCHEMA = "tura_codex_thread_writer_response_v1";
const PROTOCOL_VERSION = "tura_codex_thread_writer_v1";
const MAX_LINE_BYTES = 1024 * 1024;
const MAX_HOST_FRAME_BYTES = 8 * 1024 * 1024;
const IDENTIFIER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]*$/;

class WriterError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "WriterError";
    this.code = code;
  }
}

function sha256(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function assertPlainObject(value, code = "INVALID_REQUEST_SCHEMA") {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new WriterError(code, "request must be a JSON object");
  }
}

function assertClosedKeys(value, allowed) {
  const unknown = Object.keys(value).filter((key) => !allowed.has(key));
  if (unknown.length > 0) {
    throw new WriterError(
      "INVALID_REQUEST_SCHEMA",
      `unknown request fields: ${unknown.sort().join(",")}`,
    );
  }
}

function requiredIdentifier(value, name) {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > 256 ||
    !IDENTIFIER_PATTERN.test(value)
  ) {
    throw new WriterError(
      "INVALID_REQUEST_SCHEMA",
      `${name} must be a bounded deterministic identifier`,
    );
  }
  return value;
}

function validateRequest(raw) {
  assertPlainObject(raw);
  if (raw.schema_version !== REQUEST_SCHEMA) {
    throw new WriterError("INVALID_REQUEST_SCHEMA", "unsupported schema_version");
  }
  const requestId = requiredIdentifier(raw.request_id, "request_id");
  if (![
    "capabilities",
    "read_thread",
    "send_message_to_thread",
  ].includes(raw.operation)) {
    throw new WriterError("INVALID_REQUEST_SCHEMA", "unsupported operation");
  }

  if (raw.operation === "capabilities") {
    assertClosedKeys(raw, new Set(["schema_version", "request_id", "operation"]));
    return { operation: raw.operation, requestId };
  }

  if (raw.operation === "read_thread") {
    assertClosedKeys(
      raw,
      new Set([
        "schema_version",
        "request_id",
        "operation",
        "target_thread_id",
        "turn_id",
        "call_id",
      ]),
    );
    const targetThreadId = requiredIdentifier(raw.target_thread_id, "target_thread_id");
    const turnId = raw.turn_id === undefined
      ? `tura-read-turn-${requestId}`
      : requiredIdentifier(raw.turn_id, "turn_id");
    const callId = raw.call_id === undefined
      ? `tura-read-call-${requestId}`
      : requiredIdentifier(raw.call_id, "call_id");
    return { operation: raw.operation, requestId, targetThreadId, turnId, callId };
  }

  assertClosedKeys(
    raw,
    new Set([
      "schema_version",
      "request_id",
      "operation",
      "target_thread_id",
      "turn_id",
      "call_id",
      "message",
      "message_sha256",
    ]),
  );
  const targetThreadId = requiredIdentifier(raw.target_thread_id, "target_thread_id");
  const turnId = requiredIdentifier(raw.turn_id, "turn_id");
  const callId = requiredIdentifier(raw.call_id, "call_id");
  if (callId !== requestId) {
    throw new WriterError(
      "INVALID_REQUEST_IDENTITY",
      "send_message_to_thread requires call_id=request_id",
    );
  }
  if (typeof raw.message !== "string" || raw.message.length === 0) {
    throw new WriterError("INVALID_REQUEST_SCHEMA", "message must be a non-empty string");
  }
  const messageBytes = Buffer.byteLength(raw.message, "utf8");
  if (messageBytes > MAX_LINE_BYTES / 2) {
    throw new WriterError("REQUEST_TOO_LARGE", "message exceeds the bounded IPC limit");
  }
  if (
    typeof raw.message_sha256 !== "string" ||
    !/^[a-f0-9]{64}$/.test(raw.message_sha256) ||
    sha256(Buffer.from(raw.message, "utf8")) !== raw.message_sha256
  ) {
    throw new WriterError("INVALID_MESSAGE_SHA256", "message_sha256 does not match message");
  }
  return {
    operation: raw.operation,
    requestId,
    targetThreadId,
    turnId,
    callId,
    message: raw.message,
    messageSha256: raw.message_sha256,
  };
}

class NativePipeClient {
  constructor(pipePath) {
    this.pipePath = pipePath;
    this.socket = null;
    this.connecting = null;
    this.nextId = 1;
    this.pending = new Map();
    this.pendingData = Buffer.alloc(0);
  }

  async connect() {
    if (this.socket !== null && !this.socket.destroyed) return;
    if (this.connecting !== null) return this.connecting;
    this.connecting = new Promise((resolve, reject) => {
      const socket = net.createConnection(this.pipePath);
      const onInitialError = (error) => {
        socket.destroy();
        reject(new WriterError("HOST_PIPE_CONNECT_FAILED", error.message));
      };
      socket.once("error", onInitialError);
      socket.once("connect", () => {
        socket.off("error", onInitialError);
        this.socket = socket;
        socket.on("data", (chunk) => this.onData(socket, chunk));
        socket.on("error", (error) => this.onDisconnect(socket, error));
        socket.on("close", () => this.onDisconnect(socket, new Error("host pipe closed")));
        resolve();
      });
    }).finally(() => {
      this.connecting = null;
    });
    return this.connecting;
  }

  async request(method, params) {
    await this.connect();
    const socket = this.socket;
    if (socket === null || socket.destroyed) {
      throw new WriterError("HOST_PIPE_CLOSED", "Codex app tools pipe closed");
    }
    const id = this.nextId++;
    const payload = Buffer.from(JSON.stringify({ id, jsonrpc: "2.0", method, params }), "utf8");
    if (payload.length > MAX_HOST_FRAME_BYTES) {
      throw new WriterError("HOST_REQUEST_TOO_LARGE", "host request exceeds frame limit");
    }
    const frame = Buffer.allocUnsafe(payload.length + 4);
    frame.writeUInt32LE(payload.length, 0);
    payload.copy(frame, 4);

    const response = new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
    try {
      socket.write(frame);
    } catch (error) {
      this.pending.delete(id);
      throw new WriterError("HOST_PIPE_WRITE_FAILED", error.message);
    }
    return response;
  }

  onData(socket, chunk) {
    if (this.socket !== socket) return;
    this.pendingData = Buffer.concat([this.pendingData, chunk]);
    while (this.pendingData.length >= 4) {
      const length = this.pendingData.readUInt32LE(0);
      if (length > MAX_HOST_FRAME_BYTES) {
        socket.destroy(new Error("host response exceeds frame limit"));
        return;
      }
      if (this.pendingData.length < length + 4) return;
      const payload = this.pendingData.subarray(4, length + 4);
      this.pendingData = this.pendingData.subarray(length + 4);
      let response;
      try {
        response = JSON.parse(payload.toString("utf8"));
      } catch {
        socket.destroy(new Error("host returned invalid JSON"));
        return;
      }
      const pending = this.pending.get(Number(response.id));
      if (pending === undefined) continue;
      this.pending.delete(Number(response.id));
      if (response?.jsonrpc !== "2.0") {
        pending.reject(new WriterError("HOST_RESPONSE_INVALID", "host returned invalid JSON-RPC"));
      } else if (response.error !== undefined) {
        pending.reject(
          new WriterError(
            "HOST_TOOL_ERROR",
            `${response.error?.code ?? "unknown"}:${response.error?.message ?? "unknown"}`,
          ),
        );
      } else if (!("result" in response)) {
        pending.reject(new WriterError("HOST_RESPONSE_INVALID", "host response lacks result"));
      } else {
        pending.resolve(response.result);
      }
    }
  }

  onDisconnect(socket, error) {
    if (this.socket !== socket) return;
    this.socket = null;
    this.pendingData = Buffer.alloc(0);
    const wrapped = new WriterError("HOST_PIPE_CLOSED", error.message || "host pipe closed");
    for (const pending of this.pending.values()) pending.reject(wrapped);
    this.pending.clear();
  }

  close() {
    this.socket?.destroy();
    this.socket = null;
  }
}

async function discoverTool(host, name) {
  const catalog = await host.request("tools/list", { threadStartKind: "all" });
  const tools = Array.isArray(catalog?.tools) ? catalog.tools : [];
  const matches = tools.filter((tool) => tool?.name === name);
  if (matches.length !== 1) {
    throw new WriterError("HOST_TOOL_COUNT_INVALID", `${name} count was ${matches.length}`);
  }
  const tool = matches[0];
  if (typeof tool.namespace !== "string" || tool.namespace.length === 0) {
    throw new WriterError("HOST_TOOL_NAMESPACE_MISSING", `${name} namespace is missing`);
  }
  return tool;
}

async function callExactTool(host, request, toolName, argumentsValue) {
  const tool = await discoverTool(host, toolName);
  const result = await host.request("tools/call", {
    arguments: argumentsValue,
    callId: request.callId,
    namespace: tool.namespace,
    threadId: request.targetThreadId,
    tool: toolName,
    turnId: request.turnId,
  });
  if (result === null || typeof result !== "object" || result.success !== true) {
    throw new WriterError("HOST_TOOL_CALL_FAILED", `${toolName} did not report success`);
  }
  return result;
}

function errorPayload(requestId, error) {
  const normalized = error instanceof WriterError
    ? error
    : new WriterError("INTERNAL_ERROR", error instanceof Error ? error.message : String(error));
  return {
    schema_version: RESPONSE_SCHEMA,
    request_id: typeof requestId === "string" ? requestId : "invalid",
    ok: false,
    error: { code: normalized.code, message: normalized.message },
  };
}

function resolvePaths() {
  const pipePath = process.env.CODEX_APP_TOOLS_PIPE_PATH?.trim();
  if (!pipePath || !path.isAbsolute(pipePath)) {
    throw new WriterError(
      "CODEX_APP_TOOLS_PIPE_PATH_REQUIRED",
      "CODEX_APP_TOOLS_PIPE_PATH must be an absolute Unix socket path",
    );
  }
  const turaHome = process.env.TURA_HOME?.trim() || process.cwd();
  const configuredDir = process.env.TURA_CODEX_THREAD_WRITER_DIR?.trim();
  const writerDir = path.resolve(configuredDir || path.join(turaHome, ".tura", "codex-thread-writer"));
  const socketPath = path.join(writerDir, `thread-writer-${process.pid}.sock`);
  const endpointPath = path.join(writerDir, `endpoint-${process.pid}.json`);
  if (Buffer.byteLength(socketPath, "utf8") > 100) {
    throw new WriterError(
      "IPC_SOCKET_PATH_TOO_LONG",
      "TURA_CODEX_THREAD_WRITER_DIR is too long for a portable Unix socket path",
    );
  }
  return { pipePath, writerDir, socketPath, endpointPath };
}

function makeEndpoint(paths) {
  return {
    socket_path: paths.socketPath,
    pid: process.pid,
    app_tools_pipe_sha256: sha256(Buffer.from(paths.pipePath, "utf8")),
    app_tools_pipe_basename: path.basename(paths.pipePath),
    protocol_version: PROTOCOL_VERSION,
    created_at_ms: Date.now(),
  };
}

async function writeEndpoint(paths, endpoint) {
  const temporary = `${paths.endpointPath}.tmp-${process.pid}`;
  await fsp.writeFile(temporary, `${JSON.stringify(endpoint)}\n`, { mode: 0o600 });
  await fsp.chmod(temporary, 0o600);
  await fsp.rename(temporary, paths.endpointPath);
}

async function cleanupOwnedEndpoint(paths) {
  try {
    const endpoint = JSON.parse(await fsp.readFile(paths.endpointPath, "utf8"));
    if (endpoint.pid === process.pid && endpoint.socket_path === paths.socketPath) {
      await fsp.unlink(paths.endpointPath);
    }
  } catch (error) {
    if (error?.code !== "ENOENT") process.stderr.write(`endpoint cleanup failed: ${error.message}\n`);
  }
  try {
    const stat = await fsp.lstat(paths.socketPath);
    if (stat.isSocket()) await fsp.unlink(paths.socketPath);
  } catch (error) {
    if (error?.code !== "ENOENT") process.stderr.write(`socket cleanup failed: ${error.message}\n`);
  }
}

async function createIpcServer(paths, host, endpoint) {
  await fsp.mkdir(paths.writerDir, { recursive: true, mode: 0o700 });
  await fsp.chmod(paths.writerDir, 0o700);
  try {
    const stat = await fsp.lstat(paths.socketPath);
    if (!stat.isSocket()) {
      throw new WriterError("IPC_PATH_OCCUPIED", "writer socket path is not a socket");
    }
    await fsp.unlink(paths.socketPath);
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }

  const clients = new Set();
  const server = net.createServer((socket) => {
    clients.add(socket);
    let buffer = Buffer.alloc(0);
    let chain = Promise.resolve();
    const respond = (payload) => {
      if (!socket.destroyed) socket.write(`${JSON.stringify(payload)}\n`);
    };
    const processLine = async (line) => {
      let raw;
      try {
        raw = JSON.parse(line.toString("utf8"));
        const request = validateRequest(raw);
        let result;
        if (request.operation === "capabilities") {
          result = {
            protocol_version: PROTOCOL_VERSION,
            operations: ["capabilities", "read_thread", "send_message_to_thread"],
            socket_path: endpoint.socket_path,
            pid: endpoint.pid,
          };
        } else if (request.operation === "read_thread") {
          result = await callExactTool(host, request, "read_thread", {
            threadId: request.targetThreadId,
            turnLimit: 10,
            includeOutputs: false,
            maxOutputCharsPerItem: 20000,
          });
        } else {
          result = await callExactTool(host, request, "send_message_to_thread", {
            prompt: request.message,
            threadId: request.targetThreadId,
          });
        }
        respond({
          schema_version: RESPONSE_SCHEMA,
          request_id: request.requestId,
          ok: true,
          result,
        });
      } catch (error) {
        respond(errorPayload(raw?.request_id, error));
      }
    };
    socket.on("data", (chunk) => {
      buffer = Buffer.concat([buffer, chunk]);
      if (buffer.length > MAX_LINE_BYTES && buffer.indexOf(0x0a) === -1) {
        respond(errorPayload("invalid", new WriterError("REQUEST_TOO_LARGE", "IPC line exceeds limit")));
        socket.destroy();
        return;
      }
      while (true) {
        const newline = buffer.indexOf(0x0a);
        if (newline === -1) break;
        const line = buffer.subarray(0, newline);
        buffer = buffer.subarray(newline + 1);
        if (line.length === 0) continue;
        if (line.length > MAX_LINE_BYTES) {
          respond(errorPayload("invalid", new WriterError("REQUEST_TOO_LARGE", "IPC line exceeds limit")));
          continue;
        }
        chain = chain.then(() => processLine(line));
      }
    });
    socket.on("close", () => clients.delete(socket));
    socket.on("error", () => clients.delete(socket));
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(paths.socketPath, () => {
      server.off("error", reject);
      resolve();
    });
  });
  await fsp.chmod(paths.socketPath, 0o600);
  return {
    server,
    close: async () => {
      for (const client of clients) client.destroy();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

function startMcpStdio(status) {
  const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  const send = (payload) => process.stdout.write(`${JSON.stringify(payload)}\n`);
  lines.on("line", (line) => {
    if (Buffer.byteLength(line, "utf8") > MAX_LINE_BYTES) {
      send({ jsonrpc: "2.0", id: null, error: { code: -32600, message: "request too large" } });
      return;
    }
    let request;
    try {
      request = JSON.parse(line);
    } catch {
      send({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "parse error" } });
      return;
    }
    if (request.id === undefined) return;
    const base = { jsonrpc: "2.0", id: request.id };
    if (request.method === "initialize") {
      send({
        ...base,
        result: {
          protocolVersion: request.params?.protocolVersion || "2024-11-05",
          capabilities: { tools: { listChanged: false } },
          serverInfo: { name: "tura-codex-thread-writer", version: "1.0.0" },
          instructions: "Status-only MCP surface for the Tura Codex thread writer transport.",
        },
      });
    } else if (request.method === "tools/list") {
      send({
        ...base,
        result: {
          tools: [{
            name: "status",
            description: "Read the bounded Tura Codex thread writer transport status.",
            inputSchema: { type: "object", properties: {}, additionalProperties: false },
          }],
        },
      });
    } else if (request.method === "tools/call") {
      const args = request.params?.arguments;
      if (
        request.params?.name !== "status" ||
        args === null ||
        typeof args !== "object" ||
        Array.isArray(args) ||
        Object.keys(args).length !== 0
      ) {
        send({ ...base, error: { code: -32602, message: "invalid status tool arguments" } });
      } else {
        send({
          ...base,
          result: {
            content: [{ type: "text", text: JSON.stringify(status) }],
            structuredContent: status,
            isError: false,
          },
        });
      }
    } else if (request.method === "ping") {
      send({ ...base, result: {} });
    } else {
      send({ ...base, error: { code: -32601, message: "method not found" } });
    }
  });
  return lines;
}

export async function main() {
  const paths = resolvePaths();
  const host = new NativePipeClient(paths.pipePath);
  let ipc = null;
  let shuttingDown = false;
  await fsp.mkdir(paths.writerDir, { recursive: true, mode: 0o700 });
  const endpoint = makeEndpoint(paths);
  try {
    ipc = await createIpcServer(paths, host, endpoint);
    await writeEndpoint(paths, endpoint);
  } catch (error) {
    await ipc?.close();
    await cleanupOwnedEndpoint(paths);
    throw error;
  }
  const status = {
    ready: true,
    protocol_version: PROTOCOL_VERSION,
    pid: process.pid,
    socket_path: paths.socketPath,
    endpoint_path: paths.endpointPath,
  };
  const stdio = startMcpStdio(status);
  const shutdown = async () => {
    if (shuttingDown) return;
    shuttingDown = true;
    stdio.close();
    host.close();
    await ipc?.close();
    await cleanupOwnedEndpoint(paths);
  };
  process.stdin.once("end", () => void shutdown().then(() => process.exit(0)));
  process.stdin.once("close", () => void shutdown().then(() => process.exit(0)));
  process.once("SIGINT", () => void shutdown().then(() => process.exit(0)));
  process.once("SIGTERM", () => void shutdown().then(() => process.exit(0)));
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  main().catch((error) => {
    process.stderr.write(`${error?.code || "STARTUP_FAILED"}:${error?.message || String(error)}\n`);
    process.exitCode = 1;
  });
}
