import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import { copyFile, mkdir, readFile, stat } from "node:fs/promises";
import { promisify } from "node:util";

import {
  ADMISSION_PATH,
  CATALOG_PATH,
  RUNTIME_PATH,
  atomicWriteJson,
  inspectOfficialInstallation,
  verifyAdmission,
} from "./catalog.mjs";
import {
  CONFIG_PATH,
  atomicWrite,
  bridgeConfig,
  officialDirectConfig,
  prepareOfficialDirectCandidate,
  restoreRoutingSettings,
  routingSettings,
  sha256,
} from "./config-state.mjs";
import { PICKER_CATALOG_PATH, publishPickerCatalog } from "./picker-catalog.mjs";
import { synchronizeRouting } from "./route-ownership.mjs";
import { withUpdateLock } from "./update-lock.mjs";

const execFileAsync = promisify(execFile);
const USER_ID = process.getuid();
const DOMAIN = `gui/${USER_ID}`;
const OLD_LABEL = "com.nokiy.codex-provider-router";
const NEW_LABEL = "com.nokiy.codex-inference-switchboard";
const OLD_PLIST = "/Users/nokiy/Library/LaunchAgents/com.nokiy.codex-provider-router.plist";
const NEW_PLIST = "/Users/nokiy/Library/LaunchAgents/com.nokiy.codex-inference-switchboard.plist";
const WATCHER_PLIST = "/Users/nokiy/Library/LaunchAgents/com.nokiy.codex-switchboard-admission.plist";
const STATE_PATH = "/Users/nokiy/.codex-multi/switchboard/runtime/activation-state.json";
const BACKUP_ROOT = "/Users/nokiy/.codex-multi/switchboard/backup";

function output(value) {
  process.stdout.write(`${JSON.stringify(value, null, 2)}\n`);
}

async function run(command, args, options = {}) {
  const result = await execFileAsync(command, args, { timeout: 60_000, maxBuffer: 16 * 1024 * 1024, ...options });
  return result.stdout;
}

async function optionalRun(command, args) {
  try { return { ok: true, stdout: await run(command, args) }; }
  catch (error) { return { ok: false, stdout: error.stdout || "", stderr: error.stderr || "" }; }
}

async function fileSnapshot(filePath) {
  const [bytes, metadata] = await Promise.all([readFile(filePath), stat(filePath)]);
  return {
    path: filePath,
    sha256: sha256(bytes),
    bytes: bytes.length,
    mode: metadata.mode & 0o777,
  };
}

async function serviceState(label) {
  const result = await optionalRun("/bin/launchctl", ["print", `${DOMAIN}/${label}`]);
  return {
    loaded: result.ok,
    pid: result.ok ? Number(/\bpid = (\d+)/.exec(result.stdout)?.[1] || 0) || null : null,
  };
}

async function disabledState(label) {
  const result = await run("/bin/launchctl", ["print-disabled", DOMAIN]);
  return new RegExp(`"${label.replaceAll(".", "\\.")}"\\s*=>\\s*true`).test(result);
}

async function preference(key) {
  const result = await optionalRun("/usr/bin/defaults", ["read", "com.openai.codex", key]);
  return result.ok ? result.stdout.trim() : null;
}

async function listenerOwners(port) {
  const result = await optionalRun("/usr/sbin/lsof", ["-nP", `-iTCP:${port}`, "-sTCP:LISTEN", "-Fpcn"]);
  if (!result.ok) return [];
  const owners = [];
  let current = null;
  for (const line of result.stdout.split("\n")) {
    if (line.startsWith("p")) {
      current = { pid: Number(line.slice(1)), command: null, name: null };
      owners.push(current);
    } else if (current && line.startsWith("c")) current.command = line.slice(1);
    else if (current && line.startsWith("n")) current.name = line.slice(1);
  }
  return owners;
}

async function appHasPortConnection(port) {
  const processes = await run("/bin/ps", ["-axo", "pid=,command="]);
  const pids = processes.split("\n")
    .map((line) => /^\s*(\d+)\s+(.*)$/.exec(line))
    .filter((match) => match?.[2].startsWith("/Applications/ChatGPT.app/"))
    .map((match) => match[1])
    .filter(Boolean);
  for (const pid of pids) {
    const result = await optionalRun("/usr/sbin/lsof", [
      "-nP",
      "-a",
      "-p",
      pid,
      `-iTCP:${port}`,
      "-sTCP:ESTABLISHED",
    ]);
    if (result.ok && result.stdout.includes(`:${port}`)) return true;
  }
  return false;
}

async function health(url) {
  const response = await fetch(url, { signal: AbortSignal.timeout(5_000) });
  if (!response.ok) throw new Error(`health endpoint returned ${response.status}`);
  return response.json();
}

async function waitFor(predicate, timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      const value = await predicate();
      if (value) return value;
    } catch (error) { lastError = error; }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw lastError || new Error("timed out waiting for state transition");
}

async function createPreimage() {
  const activationId = `${new Date().toISOString().replace(/[:.]/g, "-")}-${randomUUID()}`;
  const backupDir = `${BACKUP_ROOT}/${activationId}`;
  await mkdir(backupDir, { recursive: true, mode: 0o700 });
  const files = [
    [CONFIG_PATH, `${backupDir}/config.toml`],
    ["/Users/nokiy/.codex-multi/models.json", `${backupDir}/models.json`],
    [OLD_PLIST, `${backupDir}/provider-router.plist`],
    [NEW_PLIST, `${backupDir}/switchboard.plist`],
    [WATCHER_PLIST, `${backupDir}/watcher.plist`],
  ];
  for (const [source, destination] of files) {
    try { await copyFile(source, destination); } catch (error) { if (error.code !== "ENOENT") throw error; }
  }
  const [config, oldRouter, newService, watcher, official, owners, autoInstall, autoChecks] = await Promise.all([
    fileSnapshot(CONFIG_PATH),
    serviceState(OLD_LABEL),
    serviceState(NEW_LABEL),
    serviceState("com.nokiy.codex-switchboard-admission"),
    inspectOfficialInstallation(),
    listenerOwners(43130),
    preference("SUAutomaticallyUpdate"),
    preference("SUEnableAutomaticChecks"),
  ]);
  const state = {
    schema_version: "codex_switchboard_activation_state_v1",
    state: "STAGED",
    activation_id: activationId,
    created_at: new Date().toISOString(),
    backup_dir: backupDir,
    preimage: {
      config,
      old_router: { ...oldRouter, disabled: await disabledState(OLD_LABEL) },
      new_service: { ...newService, disabled: await disabledState(NEW_LABEL) },
      watcher,
      listener_43130: owners,
      sparkle: { automatically_update: autoInstall, automatic_checks: autoChecks },
      official: {
        app_version: official.app_version,
        app_build: official.app_build,
        cli_sha256: official.cli_sha256,
        bundled_catalog_sha256: official.bundled_catalog_sha256,
      },
    },
  };
  state.official_direct_candidate = await prepareOfficialDirectCandidate();
  await atomicWriteJson(STATE_PATH, state, 0o600);
  return state;
}

async function prepare() {
  const admission = await verifyAdmission();
  if (!admission.ok) throw Object.assign(new Error("admission is not current"), { code: "VERSION_NOT_ADMITTED" });
  const state = await createPreimage();
  output({ ok: true, state: state.state, activation_id: state.activation_id, backup_dir: state.backup_dir });
}

async function assertHandoffReady(state, runtime) {
  const config = await readFile(CONFIG_PATH);
  if (sha256(config) !== state.preimage.config.sha256) {
    throw Object.assign(new Error("config changed after staging"), { code: "CONFIG_CAS_MISMATCH" });
  }
  const old = await serviceState(OLD_LABEL);
  const owners = await listenerOwners(43130);
  if (!old.loaded || !old.pid || owners.length !== 1 || owners[0].pid !== old.pid) {
    throw Object.assign(new Error("43130 is not exclusively owned by the admitted legacy router"), { code: "PORT_OWNERSHIP_MISMATCH" });
  }
  const oldHealth = await health("http://127.0.0.1:43130/health");
  if (Number(oldHealth.activeConnections) !== 0) {
    throw Object.assign(new Error("legacy router still has active connections"), { code: "LEGACY_ROUTER_BUSY" });
  }
  if (await appHasPortConnection(43130)) {
    throw Object.assign(new Error("official App still has a connection to 43130"), { code: "APP_LOCAL_CONNECTION_ACTIVE" });
  }
  const wsOverride = await optionalRun("/bin/launchctl", ["getenv", "CODEX_APP_SERVER_WS_URL"]);
  if (wsOverride.ok && wsOverride.stdout.trim()) {
    throw Object.assign(new Error("CODEX_APP_SERVER_WS_URL must remain unset"), { code: "CUSTOM_APP_SERVER_OVERRIDE_PRESENT" });
  }
  if (runtime.port !== 43130) throw Object.assign(new Error("runtime port is not the fixed admitted port"), { code: "PORT_POLICY_MISMATCH" });
}

async function bootout(label) {
  await optionalRun("/bin/launchctl", ["bootout", `${DOMAIN}/${label}`]);
}

async function bootstrap(label, plist) {
  await run("/bin/launchctl", ["enable", `${DOMAIN}/${label}`]);
  await optionalRun("/bin/launchctl", ["bootout", `${DOMAIN}/${label}`]);
  await run("/bin/launchctl", ["bootstrap", DOMAIN, plist]);
}

async function rollbackInternal(state, { preimage = true } = {}) {
  let original = null;
  if (preimage && state.activated_config_sha256) {
    const current = await readFile(CONFIG_PATH);
    const acceptedHashes = new Set([
      state.activated_config_sha256,
      state.verified_config_sha256,
    ].filter(Boolean));
    if (!acceptedHashes.has(sha256(current))) {
      throw Object.assign(new Error("current config diverged after activation"), { code: "ROLLBACK_CONFIG_DIVERGED" });
    }
    original = await readFile(`${state.backup_dir}/config.toml`);
  }
  // Reject drift before stopping a healthy service, including rollback refusal.
  await bootout(NEW_LABEL);
  await bootout("com.nokiy.codex-switchboard-admission");
  if (original) await atomicWrite(CONFIG_PATH, original, state.preimage.config.mode);
  if (state.preimage.old_router.loaded) {
    await run("/bin/launchctl", ["enable", `${DOMAIN}/${OLD_LABEL}`]);
    await optionalRun("/bin/launchctl", ["bootstrap", DOMAIN, OLD_PLIST]);
  }
  if (state.preimage.sparkle.automatically_update !== null) {
    await run("/usr/bin/defaults", ["write", "com.openai.codex", "SUAutomaticallyUpdate", "-bool", state.preimage.sparkle.automatically_update === "1" ? "true" : "false"]);
  }
}

async function activate() {
  const [state, runtime, admission] = await Promise.all([
    readFile(STATE_PATH, "utf8").then(JSON.parse),
    readFile(RUNTIME_PATH, "utf8").then(JSON.parse),
    verifyAdmission(),
  ]);
  if (state.state !== "STAGED") throw Object.assign(new Error("activation state is not STAGED"), { code: "ACTIVATION_STATE_INVALID" });
  if (!admission.ok) throw Object.assign(new Error("admission is not current"), { code: "VERSION_NOT_ADMITTED" });
  await assertHandoffReady(state, runtime);
  state.state = "ACTIVATING";
  state.activating_at = new Date().toISOString();
  await atomicWriteJson(STATE_PATH, state, 0o600);
  try {
    await bootout(OLD_LABEL);
    await run("/bin/launchctl", ["disable", `${DOMAIN}/${OLD_LABEL}`]);
    await waitFor(async () => (await listenerOwners(43130)).length === 0);
    await bootstrap(NEW_LABEL, NEW_PLIST);
    const capability = runtime.installation_capability;
    await waitFor(async () => {
      const ready = await health(`http://127.0.0.1:43130/${capability}/ready`);
      return ready.status === "ADMITTED";
    });
    await bootstrap("com.nokiy.codex-switchboard-admission", WATCHER_PLIST);
    const source = await readFile(CONFIG_PATH, "utf8");
    if (sha256(Buffer.from(source)) !== state.preimage.config.sha256) {
      throw Object.assign(new Error("config changed during activation"), { code: "CONFIG_CAS_MISMATCH" });
    }
    const candidate = bridgeConfig(source, {
      openaiBaseUrl: `http://127.0.0.1:43130/${capability}/backend-api/codex/v1`,
      catalogPath: PICKER_CATALOG_PATH,
    });
    await publishPickerCatalog();
    const written = await atomicWrite(CONFIG_PATH, candidate, 0o600);
    state.activated_config_sha256 = written.sha256;
    await atomicWriteJson(STATE_PATH, state, 0o600);
    await run("/usr/bin/defaults", ["write", "com.openai.codex", "SUEnableAutomaticChecks", "-bool", "true"]);
    await run("/usr/bin/defaults", ["write", "com.openai.codex", "SUAutomaticallyUpdate", "-bool", "false"]);
    state.state = "ACTIVE_PENDING_RESTART";
    state.activated_at = new Date().toISOString();
    state.catalog_sha256 = admission.admission.catalog_sha256;
    await atomicWriteJson(STATE_PATH, state, 0o600);
    output({ ok: true, state: state.state, activation_id: state.activation_id, restart_required: true });
  } catch (error) {
    await rollbackInternal(state, { preimage: Boolean(state.activated_config_sha256) }).catch(() => {});
    state.state = "ACTIVATION_ABORTED";
    state.abort_code = error.code || "ACTIVATION_FAILED";
    await atomicWriteJson(STATE_PATH, state, 0o600);
    throw error;
  }
}

async function rollback(mode) {
  const state = await readFile(STATE_PATH, "utf8").then(JSON.parse);
  if (state.routing_binding?.owner === "switchboard" && state.routing_binding.status === "CONFIGURED") {
    return withUpdateLock(async () => {
      const stateRaw = await readFile(STATE_PATH, "utf8");
      const currentState = JSON.parse(stateRaw);
      const binding = currentState.routing_binding;
      const current = await readFile(CONFIG_PATH, "utf8");
      const previous = mode === "official-direct"
        ? { openai_base_url: null, model_catalog_json: null } : binding.previous;
      const restored = restoreRoutingSettings(current, { expected: binding.expected, previous });
      if (await readFile(STATE_PATH, "utf8") !== stateRaw || await readFile(CONFIG_PATH, "utf8") !== current) {
        throw Object.assign(new Error("Routing changed before rollback"), { code: "CONFIG_CAS_MISMATCH" });
      }
      await atomicWrite(CONFIG_PATH, restored);
      binding.status = "ROLLED_BACK";
      binding.restored = routingSettings(restored);
      currentState.state = "ROUTING_BINDING_ROLLED_BACK";
      currentState.rolled_back_at = new Date().toISOString();
      await atomicWriteJson(STATE_PATH, currentState, 0o600);
      output({ ok: true, state: currentState.state, restart_required: true, service_retained: true });
    });
  }
  if (mode === "official-direct") {
    const current = await readFile(CONFIG_PATH);
    const acceptedHashes = new Set([
      state.activated_config_sha256,
      state.verified_config_sha256,
    ].filter(Boolean));
    if (acceptedHashes.size && !acceptedHashes.has(sha256(current))) {
      throw Object.assign(new Error("current config diverged after activation"), { code: "ROLLBACK_CONFIG_DIVERGED" });
    }
    const direct = officialDirectConfig(current.toString("utf8"));
    await atomicWrite(CONFIG_PATH, direct, 0o600);
    await bootout(NEW_LABEL);
    await bootout("com.nokiy.codex-switchboard-admission");
    state.state = "ROLLED_BACK_OFFICIAL_DIRECT";
  } else {
    await rollbackInternal(state, { preimage: true });
    state.state = "ROLLED_BACK_PREIMAGE";
  }
  state.rolled_back_at = new Date().toISOString();
  await atomicWriteJson(STATE_PATH, state, 0o600);
  output({ ok: true, state: state.state, restart_required: true });
}

async function markVerified(receiptPath) {
  const state = await readFile(STATE_PATH, "utf8").then(JSON.parse);
  const receipt = JSON.parse(await readFile(receiptPath, "utf8"));
  const checksPassed = receipt.checks
    && !Array.isArray(receipt.checks)
    && Object.values(receipt.checks).every((value) => value === true);
  if (state.state !== "ACTIVE_PENDING_RESTART"
    || receipt.schema_version !== "codex_switchboard_post_restart_receipt_v1"
    || receipt.activation_id !== state.activation_id
    || receipt.ok !== true
    || !checksPassed
    || typeof receipt.config_sha256 !== "string") {
    throw Object.assign(new Error("post-restart receipt cannot verify this activation"), { code: "ACTIVATION_VERIFY_REJECTED" });
  }
  state.state = "ACTIVE_VERIFIED";
  state.verified_at = new Date().toISOString();
  state.verification_receipt = receiptPath;
  state.verified_config_sha256 = receipt.config_sha256;
  await atomicWriteJson(STATE_PATH, state, 0o600);
  output({ ok: true, state: state.state });
}

async function main() {
  const args = process.argv.slice(2);
  if (args.includes("--sync-routes")) {
    const explicit = args.includes("--without-claude") ? false : args.includes("--with-claude") ? true : undefined;
    return withUpdateLock(async () => {
      const verified = await verifyAdmission();
      if (!verified.ok) throw Object.assign(new Error("Official installation admission is not current"), { code: "VERSION_NOT_ADMITTED" });
      output(await synchronizeRouting({ claudeEnabled: explicit }));
    });
  }
  if (args[0] === "--workflow-update") {
    const { workflowUpdate } = await import("./workflow-update.mjs");
    return output(await workflowUpdate(args[1], args[2]));
  }
  if (args.includes("--prepare")) return prepare();
  if (args.includes("--activate")) return activate();
  if (args.includes("--rollback-official-direct")) return rollback("official-direct");
  if (args.includes("--rollback-preimage")) return rollback("preimage");
  if (args.includes("--mark-verified")) {
    const index = args.indexOf("--receipt");
    return markVerified(index >= 0 ? args[index + 1] : null);
  }
  throw new Error("usage: activation.mjs --prepare | --activate | --sync-routes [--with-claude | --without-claude] | --rollback-official-direct | --rollback-preimage | --mark-verified --receipt <path>");
}

main().catch((error) => {
  output({ ok: false, error_code: error.code || "ACTIVATION_FAILED", message: error.message });
  process.exitCode = 1;
});
