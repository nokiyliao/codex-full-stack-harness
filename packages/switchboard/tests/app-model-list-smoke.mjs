import { spawn } from "node:child_process";
import { readFile } from "node:fs/promises";
import readline from "node:readline";

import {
  ADMISSION_PATH,
  CLI_PATH,
  atomicWriteJson,
} from "../src/catalog.mjs";

const RECEIPT_PATH = "/Users/nokiy/.codex-multi/switchboard/runtime/app-model-list-receipt.json";
const REQUEST_TIMEOUT_MS = 60_000;

class AppServerClient {
  constructor() {
    this.nextId = 1;
    this.pending = new Map();
    this.stderrBytes = 0;
    this.exited = false;
    this.child = spawn(CLI_PATH, [
      "app-server",
      "--strict-config",
      "--listen", "stdio://",
    ], {
      cwd: "/Users/nokiy/.codex-multi/switchboard",
      env: { ...process.env, NO_COLOR: "1" },
      stdio: ["pipe", "pipe", "pipe"],
    });

    this.child.stderr.on("data", (chunk) => {
      this.stderrBytes += chunk.length;
    });
    this.child.once("exit", (code, signal) => {
      this.exited = true;
      const error = new Error(`app-server exited (${code ?? signal})`);
      for (const pending of this.pending.values()) pending.reject(error);
      this.pending.clear();
    });
    this.child.once("error", (error) => {
      for (const pending of this.pending.values()) pending.reject(error);
      this.pending.clear();
    });

    const lines = readline.createInterface({ input: this.child.stdout });
    lines.on("line", (line) => this.#onLine(line));
  }

  #write(message) {
    if (this.exited || !this.child.stdin.writable) {
      throw new Error("app-server is not writable");
    }
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
      if (message.error) pending.reject(new Error(`RPC ${message.error.code}`));
      else pending.resolve(message.result);
      return;
    }
    if (Object.hasOwn(message, "id") && Object.hasOwn(message, "method")) {
      this.#write({
        id: message.id,
        error: { code: -32001, message: "Smoke verifier grants no interactive effects" },
      });
    }
  }

  request(method, params) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(String(id));
        reject(new Error(`${method} timed out`));
      }, REQUEST_TIMEOUT_MS);
      this.pending.set(String(id), { resolve, reject, timer });
      this.#write({ id, method, params });
    });
  }

  notify(method, params) {
    this.#write(params === undefined ? { method } : { method, params });
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

async function main() {
  const admission = JSON.parse(await readFile(ADMISSION_PATH, "utf8"));
  const client = new AppServerClient();
  try {
    await client.request("initialize", {
      clientInfo: { name: "codex-switchboard-model-list-smoke", title: null, version: "1.0.0" },
      capabilities: { experimentalApi: true, requestAttestation: false },
    });
    client.notify("initialized");
    const result = await client.request("model/list", {
      includeHidden: true,
      limit: 100,
    });
    const pickerResult = await client.request("model/list", {
      includeHidden: false,
      limit: 100,
    });
    const observed = result.data.map((model) => model.model);
    const pickerObserved = pickerResult.data.map((model) => model.model);
    const expected = [...admission.external_models, ...admission.official_models];
    const missing = expected.filter((slug) => !observed.includes(slug));
    const checks = {
      activation_catalog_admitted: admission.status === "ADMITTED",
      all_admitted_models_visible: missing.length === 0,
      all_external_models_visible: admission.external_models.every((slug) => observed.includes(slug)),
      all_official_models_visible: admission.official_models.every((slug) => observed.includes(slug)),
      no_pagination_truncation: result.nextCursor == null,
      astra_picker_visible: pickerObserved.includes("gpt-6-astra"),
      picker_no_pagination_truncation: pickerResult.nextCursor == null,
    };
    const falsePredicates = Object.entries(checks)
      .filter(([, ok]) => !ok)
      .map(([name]) => name);
    const receipt = {
      schema_version: "codex_switchboard_app_model_list_receipt_v1",
      generated_at: new Date().toISOString(),
      ok: falsePredicates.length === 0,
      transport: "official_codex_app_server_stdio",
      app_version: admission.app_version,
      app_build: admission.app_build,
      expected_count: expected.length,
      observed_count: observed.length,
      external_count: admission.external_models.length,
      official_count: admission.official_models.length,
      observed_models: observed,
      picker_visible_models: pickerObserved,
      missing_models: missing,
      stderr_bytes: client.stderrBytes,
      checks,
      false_predicates: falsePredicates,
    };
    await atomicWriteJson(RECEIPT_PATH, receipt, 0o600);
    process.stdout.write(`${JSON.stringify({
      ok: receipt.ok,
      receipt: RECEIPT_PATH,
      expected_count: receipt.expected_count,
      observed_count: receipt.observed_count,
      checks,
      false_predicates: falsePredicates,
    })}\n`);
    if (!receipt.ok) process.exitCode = 1;
  } finally {
    await client.close();
  }
}

await main();
