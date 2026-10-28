import { createHash, randomBytes } from "node:crypto";
import { spawn } from "node:child_process";
import { chmod, copyFile, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import readline from "node:readline";

import {
  CATALOG_PATH,
  CLI_PATH,
  RUNTIME_PATH,
  atomicWriteJson,
  inspectOfficialInstallation,
} from "../src/catalog.mjs";

const RECEIPT_PATH = process.env.SWITCHBOARD_LIVE_TEST_RECEIPT
  || "/Users/nokiy/.codex-multi/switchboard/runtime/live-admission-receipt.json";
const TEST_CATALOG_PATH = process.env.SWITCHBOARD_LIVE_TEST_CATALOG || CATALOG_PATH;
const VERIFY_CWD = "/Users/nokiy/.codex-multi/switchboard/runtime/live-verifier-workspace";
const VERIFY_CODEX_HOME = "/Users/nokiy/.codex-multi/switchboard/runtime/live-verifier-codex-home";
const OFFICIAL_AUTH_PATH = "/Users/nokiy/.codex/auth.json";
const TURN_TIMEOUT_MS = 12 * 60 * 1_000;
const TRACE = process.env.SWITCHBOARD_LIVE_TEST_TRACE === "1";

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function typedError(code, message = code, diagnostics = undefined) {
  return Object.assign(new Error(message), { code, diagnostics });
}

function trace(value) {
  if (TRACE) process.stdout.write(`${JSON.stringify(value)}\n`);
}

class AppServerClient {
  constructor({ baseUrl }) {
    this.nextId = 1;
    this.pending = new Map();
    this.notifications = [];
    this.waiters = new Set();
    this.stderrBytes = 0;
    this.unexpectedServerRequests = [];
    this.exited = false;
    this.child = spawn(CLI_PATH, [
      "-c", `openai_base_url=${JSON.stringify(baseUrl)}`,
      "-c", `model_catalog_json=${JSON.stringify(TEST_CATALOG_PATH)}`,
      "-c", "features.multi_agent=true",
      "app-server",
      "--strict-config",
      "--listen", "stdio://",
    ], {
      cwd: VERIFY_CWD,
      env: { ...process.env, CODEX_HOME: VERIFY_CODEX_HOME, NO_COLOR: "1" },
      stdio: ["pipe", "pipe", "pipe"],
    });

    this.child.stderr.on("data", (chunk) => {
      this.stderrBytes += chunk.length;
    });
    this.child.once("exit", (code, signal) => {
      this.exited = true;
      const error = typedError("APP_SERVER_EXITED", `app-server exited (${code ?? signal})`);
      for (const entry of this.pending.values()) entry.reject(error);
      this.pending.clear();
      for (const waiter of this.waiters) waiter.reject(error);
      this.waiters.clear();
    });
    this.child.once("error", (error) => {
      const wrapped = typedError("APP_SERVER_SPAWN_FAILED", error.message);
      for (const entry of this.pending.values()) entry.reject(wrapped);
      this.pending.clear();
    });

    const lines = readline.createInterface({ input: this.child.stdout });
    lines.on("line", (line) => this.#onLine(line));
  }

  #write(message) {
    if (this.exited || !this.child.stdin.writable) throw typedError("APP_SERVER_NOT_WRITABLE");
    this.child.stdin.write(`${JSON.stringify(message)}\n`);
  }

  #onLine(line) {
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      return;
    }
    if (Object.hasOwn(message, "id") && !Object.hasOwn(message, "method")) {
      const pending = this.pending.get(String(message.id));
      if (!pending) return;
      this.pending.delete(String(message.id));
      clearTimeout(pending.timer);
      trace({ event: "response", method: pending.method, ok: !message.error });
      if (message.error) {
        pending.reject(Object.assign(typedError("APP_SERVER_REQUEST_FAILED"), {
          rpcCode: message.error.code,
        }));
      } else {
        pending.resolve(message.result);
      }
      return;
    }
    if (Object.hasOwn(message, "id") && Object.hasOwn(message, "method")) {
      this.unexpectedServerRequests.push(message.method);
      trace({ event: "server_request", method: message.method });
      this.#write({
        id: message.id,
        error: {
          code: -32001,
          message: "Live verifier does not grant interactive effects",
        },
      });
      return;
    }
    if (!message.method) return;
    const noisy = message.method.startsWith("hook/")
      || message.method.startsWith("mcpServer/startupStatus/")
      || message.method.endsWith("/delta")
      || message.method === "thread/tokenUsage/updated"
      || message.method === "account/rateLimits/updated";
    if (!noisy) {
      trace({
        event: "notification",
        method: message.method,
        thread_id_sha256: message.params?.threadId ? sha256(message.params.threadId) : undefined,
        turn_id_sha256: message.params?.turnId ? sha256(message.params.turnId) : undefined,
        turn_status: completedStatus(message.params?.turn),
        item_type: message.params?.item?.type,
        collab_tool: message.params?.item?.type === "collabAgentToolCall" ? message.params.item.tool : undefined,
        item_status: message.params?.item?.status,
      });
    }
    this.notifications.push(message);
    for (const waiter of [...this.waiters]) {
      if (waiter.predicate(message)) {
        this.waiters.delete(waiter);
        clearTimeout(waiter.timer);
        waiter.resolve(message);
      }
    }
  }

  request(method, params, timeoutMs = 60_000) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(String(id));
        reject(typedError("APP_SERVER_REQUEST_TIMEOUT"));
      }, timeoutMs);
      this.pending.set(String(id), { method, resolve, reject, timer });
      trace({ event: "request", method });
      this.#write({ id, method, params });
    });
  }

  notify(method, params) {
    this.#write(params === undefined ? { method } : { method, params });
  }

  waitForNotification(predicate, { after = 0, timeoutMs = TURN_TIMEOUT_MS } = {}) {
    const existing = this.notifications.slice(after).find(predicate);
    if (existing) return Promise.resolve(existing);
    return new Promise((resolve, reject) => {
      const waiter = { predicate, resolve, reject, timer: null };
      waiter.timer = setTimeout(() => {
        this.waiters.delete(waiter);
        reject(typedError("APP_SERVER_NOTIFICATION_TIMEOUT"));
      }, timeoutMs);
      this.waiters.add(waiter);
    });
  }

  async close() {
    if (this.exited) return;
    this.child.stdin.end();
    this.child.kill("SIGTERM");
    await new Promise((resolve) => {
      const timer = setTimeout(() => {
        if (!this.exited) this.child.kill("SIGKILL");
        resolve();
      }, 3_000);
      this.child.once("exit", () => {
        clearTimeout(timer);
        resolve();
      });
    });
  }
}

function completedStatus(turn) {
  return typeof turn?.status === "string" ? turn.status : turn?.status?.type;
}

function turnItems(client, turnId, after) {
  return client.notifications.slice(after)
    .filter((message) => message.method === "item/completed"
      && message.params?.turnId === turnId)
    .map((message) => message.params.item);
}

async function runTurn(client, {
  threadId,
  model,
  prompt,
  marker,
  requireMarker = true,
  effort = undefined,
  timeoutMs = TURN_TIMEOUT_MS,
}) {
  const after = client.notifications.length;
  const startedAt = Date.now();
  const response = await client.request("turn/start", {
    threadId,
    input: [{ type: "text", text: prompt, textElements: [] }],
    model,
    ...(effort ? { effort } : {}),
    approvalPolicy: "never",
  }, timeoutMs);
  const turnId = response?.turn?.id;
  if (!turnId) throw typedError("TURN_ID_MISSING");
  const completed = await client.waitForNotification(
    (message) => message.method === "turn/completed"
      && message.params?.threadId === threadId
      && message.params?.turn?.id === turnId,
    { after, timeoutMs },
  );
  if (completedStatus(completed.params.turn) !== "completed") {
    throw typedError("TURN_NOT_COMPLETED");
  }
  const items = turnItems(client, turnId, after);
  const assistantText = items
    .filter((item) => item?.type === "agentMessage")
    .map((item) => item.text)
    .join("\n");
  const markerPresent = assistantText.includes(marker);
  if (requireMarker && !markerPresent) throw typedError("TURN_MARKER_MISSING");
  return {
    model,
    turnId,
    latencyMs: Date.now() - startedAt,
    outputSha256: sha256(assistantText),
    markerPresent,
    items,
  };
}

function publicTurnResult(result) {
  return {
    model: result.model,
    turn_id_sha256: sha256(result.turnId),
    status: "completed",
    latency_ms: result.latencyMs,
    output_sha256: result.outputSha256,
  };
}

async function run() {
  await mkdir(VERIFY_CWD, { recursive: true, mode: 0o700 });
  await rm(VERIFY_CODEX_HOME, { recursive: true, force: true });
  await mkdir(VERIFY_CODEX_HOME, { recursive: true, mode: 0o700 });
  await copyFile(OFFICIAL_AUTH_PATH, `${VERIFY_CODEX_HOME}/auth.json`);
  await chmod(`${VERIFY_CODEX_HOME}/auth.json`, 0o600);
  await writeFile(`${VERIFY_CODEX_HOME}/config.toml`, [
    'approval_policy = "never"',
    'sandbox_mode = "read-only"',
    '',
    '[analytics]',
    'enabled = false',
    '',
    '[history]',
    'persistence = "save-all"',
    '',
    '[features]',
    'multi_agent = true',
    'multi_agent_v2 = true',
    'plugins = false',
    'apps = false',
    'enable_mcp_apps = false',
    'memories = false',
    'goals = false',
    'shell_snapshot = false',
    '',
  ].join("\n"), { mode: 0o600 });
  const runtime = JSON.parse(await readFile(RUNTIME_PATH, "utf8"));
  const port = Number(process.env.SWITCHBOARD_LIVE_TEST_PORT || 43132);
  const baseUrl = `http://127.0.0.1:${port}/${runtime.installation_capability}/backend-api/codex/v1`;
  const official = await inspectOfficialInstallation();
  const code = `CTX-${randomBytes(6).toString("hex").toUpperCase()}`;
  const client = new AppServerClient({ baseUrl });
  const sequence = [];
  let threadId;
  try {
    await client.request("initialize", {
      clientInfo: { name: "codex-switchboard-live-verifier", title: null, version: "1.0.0" },
      capabilities: { experimentalApi: true, requestAttestation: false },
    });
    client.notify("initialized");
    const started = await client.request("thread/start", {
      model: "gpt-5.6-sol",
      modelProvider: "openai",
      cwd: VERIFY_CWD,
      approvalPolicy: "never",
      sandbox: "read-only",
      ephemeral: false,
      developerInstructions: "This is a read-only inference routing verifier. Use only collaboration tools when a verifier prompt explicitly requests them. Never use files, shell, network tools, browser, apps, MCP, or ask the user. Follow the requested exact reply marker.",
    });
    threadId = started?.thread?.id;
    if (!threadId || started?.modelProvider !== "openai") throw typedError("THREAD_START_INVALID");

    sequence.push(await runTurn(client, {
      threadId,
      model: "gpt-5.6-sol",
      prompt: `Remember ${code} for later turns. Reply with marker SOL_INITIAL_OK and include the code.`,
      marker: `SOL_INITIAL_OK`,
    }));
    sequence.push(await runTurn(client, {
      threadId,
      model: "grok-4.6",
      effort: "low",
      prompt: `Recover the code from canonical conversation history. Reply with marker GROK_HISTORY_OK and include ${code}.`,
      marker: `GROK_HISTORY_OK`,
    }));
    if (!sequence.at(-1).items.some((item) => item.type === "agentMessage" && item.text.includes(code))) {
      throw typedError("GROK_HISTORY_MISSING");
    }
    sequence.push(await runTurn(client, {
      threadId,
      model: "gemini-3.8-flash-high",
      effort: "high",
      prompt: `Recover the code from canonical conversation history. Reply with marker AGY_HISTORY_OK and include ${code}.`,
      marker: `AGY_HISTORY_OK`,
    }));
    if (!sequence.at(-1).items.some((item) => item.type === "agentMessage" && item.text.includes(code))) {
      throw typedError("AGY_HISTORY_MISSING");
    }
    sequence.push(await runTurn(client, {
      threadId,
      model: "chatgpt-web/high",
      effort: "high",
      prompt: `Recover the code from canonical conversation history. Reply with marker WEB_HISTORY_OK and include ${code}.`,
      marker: "WEB_HISTORY_OK",
      requireMarker: false,
    }));
    if (!sequence.at(-1).items.some((item) => item.type === "agentMessage" && item.text.trim())) {
      throw typedError("WEB_OUTPUT_MISSING");
    }
    sequence.push(await runTurn(client, {
      threadId,
      model: "gpt-5.6-sol",
      effort: "max",
      prompt: `Recover the code after both external-model turns. Reply with marker SOL_RETURN_OK and include ${code}.`,
      marker: `SOL_RETURN_OK`,
    }));
    if (!sequence.at(-1).items.some((item) => item.type === "agentMessage" && item.text.includes(code))) {
      throw typedError("SOL_RETURN_HISTORY_MISSING");
    }

    const resumed = await client.request("thread/resume", {
      threadId,
      model: "gpt-5.6-sol",
      modelProvider: "openai",
      cwd: VERIFY_CWD,
      approvalPolicy: "never",
      sandbox: "read-only",
      excludeTurns: true,
    });
    if (resumed?.thread?.id !== threadId || resumed?.modelProvider !== "openai") {
      throw typedError("THREAD_RESUME_INVALID");
    }

    const uniqueTurns = new Set(sequence.map((turn) => turn.turnId));
    const checks = [
      { name: "official_signed_cli", ok: official.team_id === "2DC432GLL2" },
      { name: "single_official_thread", ok: Boolean(threadId) && uniqueTurns.size === 5 },
      { name: "sol_grok_agy_web_sol_history", ok: sequence.length === 5 },
      { name: "thread_resume", ok: resumed.thread.id === threadId },
      { name: "no_interactive_client_requests", ok: client.unexpectedServerRequests.length === 0 },
    ];
    const receipt = {
      schema_version: "codex_switchboard_live_admission_receipt_v1",
      ok: checks.every((check) => check.ok),
      generated_at: new Date().toISOString(),
      transport: "official_codex_app_server_stdio",
      app_version: official.app_version,
      app_build: official.app_build,
      cli_sha256: official.cli_sha256,
      switchboard_port: port,
      thread_id_sha256: sha256(threadId),
      model_provider: "openai",
      isolated_durable_thread: true,
      sequence: sequence.map(publicTurnResult),
      multi_agent: { grok_advertised: false, verified: false },
      stderr_bytes: client.stderrBytes,
      checks,
    };
    await atomicWriteJson(RECEIPT_PATH, receipt, 0o600);
    process.stdout.write(`${JSON.stringify({ ok: receipt.ok, receipt: RECEIPT_PATH, checks })}\n`);
  } finally {
    await client.close();
    await rm(VERIFY_CODEX_HOME, { recursive: true, force: true });
  }
}

run().catch(async (error) => {
  const receipt = {
    schema_version: "codex_switchboard_live_admission_receipt_v1",
    ok: false,
    generated_at: new Date().toISOString(),
    error_code: error.code || "LIVE_ADMISSION_FAILED",
    ...(error.diagnostics ? { diagnostics: error.diagnostics } : {}),
  };
  await atomicWriteJson(RECEIPT_PATH, receipt, 0o600).catch(() => {});
  process.stdout.write(`${JSON.stringify(receipt)}\n`);
  process.exitCode = 1;
});
