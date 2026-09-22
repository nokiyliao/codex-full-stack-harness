import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import { spawn } from "node:child_process";
import { startBackendStressEnvironment } from "./full_chain_backend_fixture.mjs";

if (process.env.TURA_P5_HARD_CRASH_E2E !== "1") throw new Error("TURA_P5_HARD_CRASH_E2E=1 is required");
const fake = path.join(import.meta.dirname, "fake_official_codex_commander.mjs");
const ownerSocket = path.join("/tmp", `t5-${process.pid}-${crypto.randomBytes(4).toString("hex")}.sock`);
const backend = await startBackendStressEnvironment({ runIdPrefix: "commander-convergence-hard-crash-v6", officialCodexAppServer: fake, sessionModel: "official_codex_app_server/gpt-5.6-sol", extraEnv: { TURA_COMMANDER_APP_SERVER_SOCKET: ownerSocket }, config: { workspaces: 1, tasksPerWorkspace: 1, turnsPerSession: 1, liveSessionTarget: 0, ensureBuilds: process.env.TURA_FULL_CHAIN_ENSURE_BUILDS !== "0", totalTimeoutMs: 240_000 } });
let ownerServer;
try {
  const parent = backend.targetSession;
  const statePath = path.join(parent.workspace, ".tura", "p5-fake-commander-state.json");
  await fsp.mkdir(path.dirname(statePath), { recursive: true });
  await fsp.writeFile(statePath, JSON.stringify({ schema_version: "p5_fake_commander_state_v1", thread_id: "thread-p5-global-commander", codex_session_id: "codex-session-p5-global-commander", turn_start_count: 0, turns: [{ id: "turn-p5-existing-commander", status: "completed", items: [] }], child_turns: [] }));
  ownerServer = spawn(process.execPath, [fake, "--owner-server", ownerSocket, parent.workspace], { stdio: ["ignore", "inherit", "inherit"], env: { ...process.env, TURA_P5_HARD_CRASH_E2E: "1" } });
  const ownerReady = await waitJson(`${ownerSocket}.ready.json`, 10_000);
  assert.equal(ownerReady.ready, true);
  const watcher = spawn(process.execPath, [fake, "--watch-ledger-and-kill", parent.workspace, backend.turaHome], { detached: true, stdio: "ignore", env: { ...process.env, TURA_P5_HARD_CRASH_E2E: "1" } });
  watcher.unref();
  const armed = await waitJson(path.join(parent.workspace, ".tura", "p5-watcher-armed.json"), 10_000);
  assert.equal(armed.armed, true);
  const initial = JSON.parse(await fsp.readFile(statePath, "utf8"));
  const preRevision = revision(initial.thread_id, initial.turns.map((turn) => turn.id));
  const suffix = backend.runId.replace(/[^a-zA-Z0-9_.:-]/g, "-");
  const runtimeId = `runtime-p5-child-${suffix}`;
  const transactionId = `transaction-p5-child-${suffix}`;
  const taskId = `task-p5-child-${suffix}`;
  const missionId = `mission-p5-callback-${suffix}`;
  const prompt = "P5 child produces one deterministic callback";
  const delegatedInputSha256 = semanticSha(prompt);
  const { jspaceContract, taskContextCapsule, schedulingContract } = schedulingFixture({
    workspace: parent.workspace,
    missionId,
    taskId,
    missionRevision: preRevision,
    delegatedInputSha256,
  });
  const updated = await backend.callSessionDb({
    command: "update_session",
    command_id: `p5-ready-set:${transactionId}`,
    session_id: parent.sessionId,
    metadata: emptyMetadataPatch(),
    task_plan_patch: {
      plan_summary: null,
      tasks: [{
        task_id: taskId,
        task_summary: "P5 Commander callback hard-crash task",
        start_condition: "scheduled_task",
        start_at: new Date(Date.now() - 1_000).toISOString(),
        status: "todo",
        scheduling_contract: schedulingContract,
      }],
      task: null,
      generated_task_ids: [taskId],
      generated_task_id: taskId,
      now: new Date().toISOString(),
    },
  });
  assert.equal(updated.kind, "session_updated");
  const scheduled = updated.session.lifecycle_projection.task_plan.detailed_tasks.find((task) => task.task_id === taskId);
  assert.equal(scheduled?.scheduling_contract?.semantic_dispatch_key, schedulingContract.semantic_dispatch_key);
  assert.equal(scheduled?.status, "todo");
  const controller = new AbortController();
  const request = fetch(`${backend.gateway.url}/session/${encodeURIComponent(parent.sessionId)}/children`, { method: "POST", headers: { "content-type": "application/json", "x-opencode-directory": encodeURIComponent(parent.workspace) }, body: JSON.stringify({ parent_session_id: parent.sessionId, parent_mission_revision_sha256: preRevision, commander_thread_id: initial.thread_id, child_session_id: `child-p5-${suffix}`, child_runtime_id: runtimeId, child_transaction_id: transactionId, child_lease_id: `lease-p5-child-${suffix}`, callback_request_id: transactionId, effect_id: `${runtimeId}.message`, callback_delivery_route: "trusted_tura_direct_thread_writer", delegated_input_sha256: delegatedInputSha256, session_directory: parent.workspace, session_name: "P5 Commander convergence hard crash", created_at_ms: Date.now(), execution_payload: { prompt, directory: parent.workspace, model: "official_codex_app_server/gpt-5.6-sol", agent: "direct-text-only", task_id: taskId, task_context_capsule: taskContextCapsule, jspace_contract: jspaceContract, maximum_parallel_runtime_workers: 24 } }), signal: controller.signal }).then(async (response) => { const text = await response.text(); if (!response.ok) throw new Error(`${response.status}:${text}`); return text; });
  const crash = await waitJson(path.join(parent.workspace, ".tura", "p5-hard-crash-observed.json"), 45_000);
  assert.ok(crash.ledger);
  const routerBefore = await routerEndpoint(backend.turaHome);
  process.kill(routerBefore.pid, "SIGKILL");
  controller.abort();
  await request.catch(() => undefined);
  await waitRouter(backend, routerBefore.pid, 30_000);
  const lifecycleRoot = await waitAck(backend.turaHome, parent.sessionId, 45_000);
  assert.equal(await count(path.join(lifecycleRoot, "callbacks", "intaken")), 1);
  assert.equal(await count(path.join(lifecycleRoot, "callbacks", "acknowledged")), 1);
  assert.equal((await records(path.join(lifecycleRoot, "continuations"))).filter((r) => r.state === "acknowledged").length, 1);
  assert.equal(await count(path.join(lifecycleRoot, "receipts", "pending")), 0);
  const state = JSON.parse(await fsp.readFile(statePath, "utf8"));
  assert.equal(state.turn_start_count, 1);
  const session = await backend.callSessionDb({ command: "get_session", session_id: parent.sessionId });
  const ids = session.session.lifecycle_projection.runtime_ids.filter((id) => id.startsWith("callback-continuation-"));
  assert.equal(ids.length, 1);
  const aggregates = await Promise.all(ids.map(async (id) => (await backend.callSessionDb({ command: "replay_runtime", runtime_id: id })).runtime.aggregate));
  assert.equal(aggregates[0].state, "Finished");
  assert.equal(aggregates[0].fallback_from_id, null);
  const lease = await backend.callSessionDb({ command: "get_runtime_lease", runtime_id: aggregates[0].runtime_id });
  assert.equal(lease.runtime.terminal, true); assert.equal(lease.runtime.lease_active, false); assert.equal(aggregates[0].effects?.length || 0, 0);
  const receipts = (await records(path.join(lifecycleRoot, "receipts", "applied"))).map((r) => r.receipt).filter((r) => r?.transaction_id?.startsWith("callback-continuation-request-"));
  assert.equal(receipts.length, 1);
  assert.equal(receipts[0].terminal_state, "completed");
  console.log(JSON.stringify({ status: "PASS_P5_V6", run_root: backend.runRoot, watcher_armed: true, hard_crash_observed: true, target_turn_start_count: 1, fallback_count: 0, pending: 0, callbacks: 1, ack: 1, effects: 0 }));
} finally {
  if (ownerServer && ownerServer.exitCode === null) {
    ownerServer.kill("SIGTERM");
    await new Promise((resolve) => ownerServer.once("exit", resolve));
  }
  await fsp.rm(ownerSocket, { force: true });
  await fsp.rm(`${ownerSocket}.ready.json`, { force: true });
  await backend.cleanup();
}

function sha256(v) { return crypto.createHash("sha256").update(v).digest("hex"); }
function canonicalJson(value) {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
}
function semanticSha(value) { return sha256(canonicalJson(value)); }
function schedulingFixture({ workspace, missionId, taskId, missionRevision, delegatedInputSha256 }) {
  const jspaceContract = {
    schema_version: "jspace_contract_v2",
    repo_root: workspace,
    dcf_generation: { repo_root: workspace, generation_id: `generation-${taskId}`, required_domain_bindings: {} },
    provenance: { matched_surface_ids: [] },
    matched_surface_ids: [],
    read_scopes: ["src/**"],
    write_scopes: [],
    allowed_operations: ["read"],
    denied_operations: ["create", "modify", "network", "install", "system_mutation"],
    command_templates: [],
    focused_verifiers: [],
    declared_targets: [],
    expansion: { mode: "exact_target_only", error_code: "JSPACE_EXPANSION_REQUIRED", mutation_on_expansion: false },
  };
  const authorizationIdentity = {
    schema_version: "jspace_authorization_v1",
    repo_root: jspaceContract.repo_root,
    required_domain_bindings: jspaceContract.dcf_generation.required_domain_bindings,
    matched_surface_ids: jspaceContract.matched_surface_ids,
    read_scopes: jspaceContract.read_scopes,
    write_scopes: jspaceContract.write_scopes,
    allowed_operations: jspaceContract.allowed_operations,
    denied_operations: jspaceContract.denied_operations,
    command_templates: jspaceContract.command_templates,
    declared_targets: jspaceContract.declared_targets,
    expansion: jspaceContract.expansion,
  };
  jspaceContract.authorization_semantic_sha256 = semanticSha(authorizationIdentity);
  jspaceContract.content_sha256 = semanticSha(jspaceContract);

  const taskContextCapsule = {
    schema_version: "task_context_capsule_v1",
    mission: {
      mission_id: missionId,
      task_id: taskId,
      mode: "DELIVERY",
      current_predicate: "CALLBACK_CONTINUATION_ACKS_EXACTLY_ONCE",
      objective: "Deliver one child terminal callback to the exact Commander turn and acknowledge it once",
    },
    context_summary: "Isolated hard-crash callback acceptance fixture with no protected effects.",
    dcf_generation: { generation_id: `generation-${taskId}` },
    surface: { repo_root: workspace, matched_surface_ids: [] },
    authority: { forbidden_effects: ["protected_effect"] },
    evidence_refs: [],
    focused_verifiers: [],
    jspace_semantic_sha256: jspaceContract.authorization_semantic_sha256,
  };
  taskContextCapsule.semantic_sha256 = semanticSha(taskContextCapsule);

  const schedulingContract = {
    schema_version: "tura_task_scheduling_contract_v1",
    mission_id: missionId,
    semantic_dispatch_key: "0".repeat(64),
    authority_mission_revision_sha256: missionRevision,
    delegated_input_sha256: delegatedInputSha256,
    task_context_capsule_semantic_sha256: taskContextCapsule.semantic_sha256,
    dependency_task_ids: [],
    exact_input_sha256s: [],
    jspace_authorization_semantic_sha256: jspaceContract.authorization_semantic_sha256,
    read_scopes: jspaceContract.read_scopes,
    write_scopes: jspaceContract.write_scopes,
    declared_targets: jspaceContract.declared_targets,
    conflict_identities: [],
    maximum_parallel_runtime_workers: 24,
  };
  schedulingContract.semantic_dispatch_key = semanticSha({
    schema_version: "tura_task_semantic_dispatch_identity_v1",
    contract_schema_version: schedulingContract.schema_version,
    mission_id: schedulingContract.mission_id,
    authority_mission_revision_sha256: schedulingContract.authority_mission_revision_sha256,
    task_id: taskId,
    delegated_input_sha256: schedulingContract.delegated_input_sha256,
    task_context_capsule_semantic_sha256: schedulingContract.task_context_capsule_semantic_sha256,
    dependency_task_ids: schedulingContract.dependency_task_ids,
    exact_input_sha256s: schedulingContract.exact_input_sha256s,
    jspace_authorization_semantic_sha256: schedulingContract.jspace_authorization_semantic_sha256,
    read_scopes: schedulingContract.read_scopes,
    write_scopes: schedulingContract.write_scopes,
    declared_targets: schedulingContract.declared_targets,
    conflict_identities: schedulingContract.conflict_identities,
    maximum_parallel_runtime_workers: schedulingContract.maximum_parallel_runtime_workers,
  });
  return { jspaceContract, taskContextCapsule, schedulingContract };
}
function emptyMetadataPatch() {
  return { name: null, model: null, agent: null, clear_agent: false, session_type: null, kill_processes_on_start: null, validator_enabled: null, force_planning: null, disable_permission_restrictions: null, use_last_tool_call_response: null, auto_session_name: null };
}
function revision(thread_id, ordered_turn_ids) { return sha256(JSON.stringify({ ordered_turn_ids, thread_id })); }
async function waitJson(file, ms) { const end = Date.now() + ms; while (Date.now() < end) { try { return JSON.parse(await fsp.readFile(file, "utf8")); } catch {} await new Promise((r) => setTimeout(r, 25)); } throw new Error(`timeout ${file}`); }
async function routerEndpoint(home) { return JSON.parse(await fsp.readFile(path.join(home, "db", "session_log", "router.addr"), "utf8")); }
async function waitRouter(backend, pid, ms) {
  const end = Date.now() + ms;
  let lastServiceStatus = null;
  let lastHealth = null;
  let lastError = null;
  while (Date.now() < end) {
    try {
      lastServiceStatus = await fetch(`${backend.gateway.url}/service/status`).then((r) => r.json());
      const endpoint = await routerEndpoint(backend.turaHome);
      if (endpoint.pid && endpoint.pid !== pid) {
        lastHealth = await fetch(`${backend.gateway.url}/global/health`).then((r) => r.json());
        if (lastHealth.healthy === true && lastHealth.ready === true) return endpoint;
      }
    } catch (error) {
      lastError = error?.message || String(error);
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error(`router replacement timeout: ${JSON.stringify({ lastServiceStatus, lastHealth, lastError })}`);
}
async function count(dir) { return fs.existsSync(dir) ? (await fsp.readdir(dir)).filter((n) => n.endsWith(".json")).length : 0; }
async function records(dir) { if (!fs.existsSync(dir)) return []; return Promise.all((await fsp.readdir(dir)).filter((n) => n.endsWith(".json")).map(async (n) => JSON.parse(await fsp.readFile(path.join(dir, n), "utf8")))); }
async function waitAck(home, session, ms) { const root = path.join(home, "db", "session_log", "session_lifecycle_v1", sha256(session)); const end = Date.now() + ms; while (Date.now() < end) { if (await count(path.join(root, "callbacks", "acknowledged")) === 1) return root; await new Promise((r) => setTimeout(r, 100)); } throw new Error("ack timeout"); }
