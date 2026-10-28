import { randomUUID } from "node:crypto";
import { lstat, mkdir, readFile } from "node:fs/promises";
import path from "node:path";

import { CATALOG_PATH, RUNTIME_PATH } from "./catalog.mjs";
import { CONFIG_PATH, atomicWrite, bridgeConfig, routingSettings, routingSettingsMatch, sha256 } from "./config-state.mjs";
import { loadClaudeBridge, CLAUDE_BRIDGE_ROOT } from "./claude-bridge-route.mjs";
import { PICKER_CATALOG_PATH, publishPickerCatalog } from "./picker-catalog.mjs";

export const ACTIVATION_STATE_PATH = new URL("../runtime/activation-state.json", import.meta.url).pathname;

export function assertRoutingOwnership(source, runtime, binding = null) {
  const expectedBase = `http://127.0.0.1:${runtime.port}/${runtime.installation_capability}/backend-api/codex/v1`;
  const current = routingSettings(source);
  if (binding?.owner === "switchboard") {
    if (!routingSettingsMatch(source, binding.expected)) {
      throw Object.assign(new Error("Routing settings changed after Switchboard binding"), { code: "ROUTING_CAS_MISMATCH" });
    }
  } else if (current.openai_base_url !== expectedBase || ![
    CATALOG_PATH, PICKER_CATALOG_PATH, `${CLAUDE_BRIDGE_ROOT}/combined-models.json`,
  ].includes(current.model_catalog_json)) {
    throw Object.assign(new Error("Current routing is not the admitted Switchboard entry"), { code: "ROUTING_OWNER_CONFLICT" });
  }
  return { openai_base_url: expectedBase, model_catalog_json: PICKER_CATALOG_PATH };
}

export async function synchronizeRouting({
  configPath = CONFIG_PATH,
  statePath = ACTIVATION_STATE_PATH,
  runtimePath = RUNTIME_PATH,
  claudeEnabled,
  loadBridge = loadClaudeBridge,
  publish = publishPickerCatalog,
} = {}) {
  const [source, stateRaw, runtimeRaw, metadata] = await Promise.all([
    readFile(configPath, "utf8"), readFile(statePath, "utf8"), readFile(runtimePath, "utf8"), lstat(configPath),
  ]);
  if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.uid !== process.getuid()) {
    throw Object.assign(new Error("Unexpected configuration file identity"), { code: "CONFIG_IDENTITY_MISMATCH" });
  }
  const state = JSON.parse(stateRaw), runtime = JSON.parse(runtimeRaw);
  const expected = assertRoutingOwnership(source, runtime, state.routing_binding);
  const enabled = claudeEnabled ?? state.routing_binding?.claude_enabled ?? true;
  const bridge = enabled ? await loadBridge({ enabled: true }) : null;
  if (enabled && !bridge) {
    throw Object.assign(new Error("Claude bridge installation is unavailable; routing unchanged"), { code: "CLAUDE_BRIDGE_NOT_INSTALLED" });
  }
  const catalog = await publish({ claudeBridge: bridge });
  const candidate = bridgeConfig(source, { openaiBaseUrl: expected.openai_base_url, catalogPath: expected.model_catalog_json });
  const changed = candidate !== source || state.routing_binding?.claude_enabled !== enabled;
  if (!changed && state.routing_binding?.status === "CONFIGURED") {
    return { ok: true, changed: false, catalog, restart_required: state.state === "ACTIVE_PENDING_RESTART", owner: "switchboard" };
  }
  const snapshot = path.join(path.dirname(statePath), "deployments", `routing-${randomUUID()}`);
  await mkdir(snapshot, { recursive: true, mode: 0o700 });
  await atomicWrite(path.join(snapshot, "config.toml.preimage"), source);
  await atomicWrite(path.join(snapshot, "activation-state.json.preimage"), stateRaw);
  if (await readFile(configPath, "utf8") !== source || await readFile(statePath, "utf8") !== stateRaw) {
    throw Object.assign(new Error("Configuration changed during route synchronization"), { code: "CONFIG_CAS_MISMATCH" });
  }
  const now = new Date().toISOString();
  state.routing_binding = {
    owner: "switchboard", status: "PREPARED", config_path: configPath, expected,
    claude_enabled: enabled, configured_at: now, snapshot_dir: snapshot,
    previous: routingSettings(source), config_sha256: sha256(candidate),
    previous_config_sha256: sha256(source),
  };
  await atomicWrite(statePath, JSON.stringify(state, null, 2) + "\n");
  // Only these two routing fields are owned here; all later user preferences
  // remain in the original TOML and are never restored from an old full image.
  if (await readFile(configPath, "utf8") !== source) {
    throw Object.assign(new Error("Configuration changed before route adoption"), { code: "CONFIG_CAS_MISMATCH" });
  }
  await atomicWrite(configPath, candidate, metadata.mode & 0o777);
  state.routing_binding.status = "CONFIGURED";
  state.state = "ACTIVE_PENDING_RESTART";
  state.activated_at = now;
  state.activated_config_sha256 = sha256(candidate);
  await atomicWrite(statePath, JSON.stringify(state, null, 2) + "\n");
  return { ok: true, changed, owner: "switchboard", catalog, restart_required: true,
    config_sha256: sha256(candidate), snapshot_dir: snapshot, claude_enabled: enabled };
}
