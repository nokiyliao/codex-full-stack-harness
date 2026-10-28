import { execFile } from "node:child_process";
import { readFile, realpath, stat } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { parse } from "smol-toml";

import {
  CATALOG_PATH,
  CLI_PATH,
  RUNTIME_PATH,
  atomicWriteJson,
  verifyAdmission,
  resolveOfficialCliPaths,
} from "./catalog.mjs";
import {
  CONFIG_PATH,
  bridgeConfig,
  configEquivalentModuloAppManagedMetadata,
  routingSettingsMatch,
  sha256,
} from "./config-state.mjs";
import { PICKER_CATALOG_PATH } from "./picker-catalog.mjs";

const execFileAsync = promisify(execFile);
const USER_ID = process.getuid();
const DOMAIN = `gui/${USER_ID}`;
const MAIN_APP = "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT";
const NEW_LABEL = "com.nokiy.codex-inference-switchboard";
const OLD_LABEL = "com.nokiy.codex-provider-router";
const WATCHER_LABEL = "com.nokiy.codex-switchboard-admission";
const STATE_PATH = "/Users/nokiy/.codex-multi/switchboard/runtime/activation-state.json";
const RECEIPT_PATH = "/Users/nokiy/.codex-multi/switchboard/runtime/post-restart-receipt.json";

async function run(command, args, options = {}) {
  const { stdout } = await execFileAsync(command, args, {
    timeout: 60_000,
    maxBuffer: 16 * 1024 * 1024,
    ...options,
  });
  return stdout;
}

async function optionalRun(command, args) {
  try { return { ok: true, stdout: await run(command, args) }; }
  catch (error) { return { ok: false, stdout: error.stdout || "" }; }
}

async function serviceState(label) {
  const result = await optionalRun("/bin/launchctl", ["print", `${DOMAIN}/${label}`]);
  return {
    loaded: result.ok,
    pid: result.ok ? Number(/\bpid = (\d+)/.exec(result.stdout)?.[1] || 0) || null : null,
    running: result.ok && /\bstate = running\b/.test(result.stdout),
  };
}

async function listenerPids(port) {
  const result = await optionalRun("/usr/sbin/lsof", [
    "-nP",
    `-iTCP:${port}`,
    "-sTCP:LISTEN",
    "-Fp",
  ]);
  if (!result.ok) return [];
  return [...new Set(result.stdout.split("\n")
    .filter((line) => /^p\d+$/.test(line))
    .map((line) => Number(line.slice(1))))];
}

async function processRows() {
  const output = await run("/bin/ps", ["-axo", "pid=,ppid=,command="]);
  return output.split("\n").flatMap((line) => {
    const match = /^\s*(\d+)\s+(\d+)\s+(.*)$/.exec(line);
    return match ? [{ pid: Number(match[1]), ppid: Number(match[2]), command: match[3] }] : [];
  });
}

function descendantPids(rows, rootPid) {
  const descendants = new Set([rootPid]);
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
  descendants.delete(rootPid);
  return descendants;
}

export async function isOfficialAppServerCommand(command, cliPath = CLI_PATH) {
  const match = /^(\S+)\s+(.*)$/.exec(command);
  if (!match || !/(?:^|\s)app-server(?:\s|$)/.test(match[2])) return false;
  try {
    const [observed, expected] = await Promise.all([realpath(match[1]), realpath(cliPath)]);
    return observed === expected;
  } catch {
    return false;
  }
}

async function mainProcessEvidence(activatedAt) {
  const rows = await processRows();
  const mainPids = rows.filter((row) => row.command === MAIN_APP).map((row) => row.pid);
  const evidence = [];
  for (const pid of mainPids) {
    const startedRaw = await run("/bin/ps", ["-p", String(pid), "-o", "lstart="]);
    const startedAt = Date.parse(startedRaw.trim());
    const descendants = descendantPids(rows, pid);
    const candidates = rows.filter((row) => descendants.has(row.pid));
    const verified = await Promise.all(candidates.map(async (row) =>
      await isOfficialAppServerCommand(row.command) ? row : null));
    const appServers = verified.filter(Boolean);
    evidence.push({
      pid,
      started_at: Number.isFinite(startedAt) ? new Date(startedAt).toISOString() : null,
      fresh: Number.isFinite(startedAt) && startedAt > activatedAt,
      official_app_server_pids: appServers.map((row) => row.pid),
    });
  }
  return evidence;
}

async function main() {
  const [state, runtime, admission, configBytes, installMode, runtimeMode, catalogMode] = await Promise.all([
    readFile(STATE_PATH, "utf8").then(JSON.parse),
    readFile(RUNTIME_PATH, "utf8").then(JSON.parse),
    verifyAdmission(),
    readFile(CONFIG_PATH),
    stat("/Users/nokiy/.codex-multi/switchboard"),
    stat(RUNTIME_PATH),
    stat(CATALOG_PATH),
  ]);
  const [service, watcher, oldService, pids, wsOverride, processes] = await Promise.all([
    serviceState(NEW_LABEL),
    serviceState(WATCHER_LABEL),
    serviceState(OLD_LABEL),
    listenerPids(43130),
    optionalRun("/bin/launchctl", ["getenv", "CODEX_APP_SERVER_WS_URL"]),
    mainProcessEvidence(Date.parse(state.activated_at || "")),
  ]);
  const config = parse(configBytes.toString("utf8"));
  const expectedBaseUrl = `http://127.0.0.1:43130/${runtime.installation_capability}/backend-api/codex/v1`;
  const boundSnapshot = state.routing_binding?.status === "CONFIGURED" && state.routing_binding?.owner === "switchboard"
    ? `${state.routing_binding.snapshot_dir}/config.toml.preimage` : `${state.backup_dir}/config.toml`;
  const preimageConfig = await readFile(boundSnapshot, "utf8");
  const expectedCatalog = state.routing_binding?.expected?.model_catalog_json || PICKER_CATALOG_PATH;
  const pickerMode = await stat(expectedCatalog);
  const expectedConfig = parse(bridgeConfig(preimageConfig, {
    openaiBaseUrl: expectedBaseUrl,
    catalogPath: expectedCatalog,
  }));
  const configSha256 = sha256(configBytes);
  const exactConfigShaMatch = configSha256 === state.activated_config_sha256;
  const officialCliPaths = await resolveOfficialCliPaths();
  const onlyAppManagedConfigDrift = configEquivalentModuloAppManagedMetadata(expectedConfig, config, { officialCliPaths });
  const binding = state.routing_binding;
  const scopedRoutingMatch = binding?.owner === "switchboard" && binding.status === "CONFIGURED"
    && binding.config_path === CONFIG_PATH && routingSettingsMatch(config, binding.expected);
  const readyResponse = await fetch(`http://127.0.0.1:43130/${runtime.installation_capability}/ready`, {
    signal: AbortSignal.timeout(5_000),
  });
  const ready = await readyResponse.json();
  const checks = {
    activation_pending_restart: ["ACTIVE_PENDING_RESTART", "ACTIVE_VERIFIED"].includes(state.state),
    admission_current: admission.ok === true,
    config_cas_matches: exactConfigShaMatch || onlyAppManagedConfigDrift || scopedRoutingMatch,
    config_routes_to_capability: config.openai_base_url === expectedBaseUrl,
    config_uses_admitted_catalog: config.model_catalog_json === expectedCatalog,
    switchboard_service_running: service.loaded && service.running && Boolean(service.pid),
    switchboard_owns_fixed_port: pids.length === 1 && pids[0] === service.pid,
    switchboard_ready_admitted: readyResponse.ok && ready.status === "ADMITTED",
    update_watcher_loaded: watcher.loaded,
    legacy_router_unloaded: !oldService.loaded,
    custom_app_server_override_absent: !wsOverride.ok || !wsOverride.stdout.trim(),
    app_restarted_after_activation: processes.length === 1 && processes[0].fresh,
    official_app_server_is_app_descendant: processes.length === 1
      && processes[0].official_app_server_pids.length >= 1,
    install_directory_mode_0700: (installMode.mode & 0o777) === 0o700,
    runtime_manifest_mode_0600: (runtimeMode.mode & 0o777) === 0o600,
    catalog_mode_0600: (catalogMode.mode & 0o777) === 0o600,
    picker_catalog_mode_0600: (pickerMode.mode & 0o777) === 0o600,
  };
  const falsePredicates = Object.entries(checks).filter(([, ok]) => !ok).map(([name]) => name);
  const receipt = {
    schema_version: "codex_switchboard_post_restart_receipt_v1",
    ok: falsePredicates.length === 0,
    generated_at: new Date().toISOString(),
    activation_id: state.activation_id,
    app_version: admission.current.app_version,
    app_build: admission.current.app_build,
    team_id: admission.current.team_id,
    cli_sha256: admission.current.cli_sha256,
    catalog_sha256: admission.admission.catalog_sha256,
    config_sha256: configSha256,
    config_exact_sha_match: exactConfigShaMatch,
    app_managed_config_drift_accepted: !exactConfigShaMatch && onlyAppManagedConfigDrift,
    config_validation_scope: scopedRoutingMatch ? "switchboard_owned_routing_keys" : "activation_snapshot",
    unowned_config_drift_observed: scopedRoutingMatch && !exactConfigShaMatch && !onlyAppManagedConfigDrift,
    switchboard_pid: service.pid,
    app_processes: processes,
    checks,
    false_predicates: falsePredicates,
  };
  await atomicWriteJson(RECEIPT_PATH, receipt, 0o600);
  process.stdout.write(`${JSON.stringify({ ok: receipt.ok, receipt: RECEIPT_PATH, checks, false_predicates: falsePredicates })}\n`);
  if (!receipt.ok) process.exitCode = 3;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) main().catch(async (error) => {
  const receipt = {
    schema_version: "codex_switchboard_post_restart_receipt_v1",
    ok: false,
    generated_at: new Date().toISOString(),
    error_code: error.code || "POST_RESTART_VERIFICATION_FAILED",
    message: error.message || "post-restart verification failed",
  };
  await atomicWriteJson(RECEIPT_PATH, receipt, 0o600).catch(() => {});
  process.stdout.write(`${JSON.stringify(receipt)}\n`);
  process.exitCode = 1;
});
