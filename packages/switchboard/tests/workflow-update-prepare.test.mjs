import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { constants } from "node:fs";
import * as fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

const source = await fs.readFile(new URL("../src/workflow-update.mjs", import.meta.url), "utf8");
const hash = bytes => createHash("sha256").update(bytes).digest("hex");
const encoded = value => JSON.stringify(value, null, 2) + "\n";
const sorted = value => Array.isArray(value) ? value.map(sorted) : value && typeof value === "object"
  ? Object.fromEntries(Object.keys(value).sort().map(key => [key, sorted(value[key])])) : value;
const planHash = plan => hash(JSON.stringify(sorted(plan)));
const SCOPE = "switchboard_exact_source_update_v1";

// Match the existing publisher tests' lexical fixture linker. Real temporary
// files exercise owned paths and exclusive outputs; deployment effects throw.
async function fixture(t, { previousExpiresAt, commandExecutable } = {}) {
  const root = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), "workflow-prepare-")));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const home = path.join(root, "home"), target = path.join(home, ".codex-multi/switchboard");
  const candidate = path.join(root, "candidate"), workspace = path.join(root, "task"), previous = path.join(root, "previous");
  const CATALOG_PATH = path.join(home, ".codex-multi/models/admitted-models.json");
  const ADMISSION_PATH = path.join(home, ".codex-multi/models/admission.json");
  const PICKER_CATALOG_PATH = path.join(home, ".codex-multi/models/unified-models.json");
  const WORKFLOW_PATH = path.join(target, "config/nokiy-workflows.json");
  const native = path.join(home, "bin/codex"), node = path.join(home, "bin/node");
  const originals = new Map();
  async function put(name, bytes) {
    await fs.mkdir(path.dirname(name), { recursive: true });
    await fs.writeFile(name, bytes, { mode: 0o600 });
    originals.set(name, Buffer.from(bytes));
    return { path: name, sha256: hash(bytes) };
  }
  const json = (name, value) => put(name, encoded(value));
  const current = {};
  for (const name of [CATALOG_PATH, ADMISSION_PATH, PICKER_CATALOG_PATH, WORKFLOW_PATH, native, node,
    ...executorFiles.map(name => path.join(target, name)),
    ...["server", "widget"].map(x => path.join(target, `src/${x}.mjs`))]) {
    current[name] = (await put(name, `installed ${path.basename(name)}\n`)).sha256;
  }
  await fs.mkdir(path.join(home, ".codex"));
  await fs.mkdir(path.join(target, "tests"));
  await fs.mkdir(workspace);
  const expiry = Math.floor(Date.now() / 1000) + 3600, thread = "019fd4f0-1079-77e2-8be7-cbf75ca28df5";
  const previousExpiry = previousExpiresAt ?? expiry;
  const files = [];
  for (const name of ["src/widget.mjs", "tests/widget.test.mjs"]) {
    const ref = await put(path.join(candidate, name), `candidate ${name}\n`);
    files.push({ target: name, source: ref.path, preimage: current[path.join(target, name)] ?? null, sha256: ref.sha256 });
  }
  const oldFile = { ...files[0], source: path.join(root, "old-candidate/src/widget.mjs"),
    preimage: "a".repeat(64), sha256: current[path.join(target, files[0].target)] };
  const priorAuth = await json(path.join(previous, "authorization.json"), { action_id: "old-action" });
  const manifest = { schema_version: "switchboard_workflow_update_v1", update_scope: SCOPE,
    action_id: "old-action", native_thread_id: thread, target, release: "old-release", expires_at: previousExpiry,
    candidate: path.join(root, "old-candidate"), artifact_root: previous, native, authorization_ref: priorAuth,
    catalog_preimage: current[CATALOG_PATH], admission_preimage: "b".repeat(64), files: [oldFile],
    invariants: Object.fromEntries([WORKFLOW_PATH, PICKER_CATALOG_PATH, native,
      ...["server", "activation", "workflow-update", "update-lock"].map(x => path.join(target, `src/${x}.mjs`))]
      .map(name => [name, current[name]])) };
  const manifestRef = await json(path.join(previous, "manifest.json"), manifest);
  const executable = commandExecutable ?? { path: node, sha256: current[node] };
  const pins = [executable, ...executorFiles.map(name => path.join(target, name))
    .map(name => ({ path: name, sha256: current[name] }))];
  const plan = { schema_version: "nokiy_deployment_plan_v1", action_id: manifest.action_id,
    native_thread_id: thread, target, release: manifest.release, expires_at: previousExpiry,
    authorization_ref: priorAuth, workspace: previous, artifact_root: path.join(previous, "native-artifacts"),
    commands: Object.fromEntries(["preflight", "apply", "verify"].map(phase => [phase, {
      argv: [executable.path, path.join(target, "src/activation.mjs"), "--workflow-update", phase, manifestRef.path],
      timeout_seconds: 180, files: [...pins, manifestRef, priorAuth, { path: oldFile.source, sha256: oldFile.sha256 }],
    }])) };
  const bindings = { action_id: manifest.action_id, native_thread_id: thread, target, release: manifest.release,
    plan_sha256: planHash(plan), manifest_sha256: manifestRef.sha256 };
  const previous_packet = { plan_ref: await json(path.join(previous, "native-plan.json"), plan), manifest_ref: manifestRef,
    apply_state_ref: await json(path.join(previous, "apply-state.json"), { ...bindings, state: "APPLIED_PENDING_VERIFY",
      written: [oldFile.target], admission_sha256: current[ADMISSION_PATH], server_sha256: current[path.join(target, "src/server.mjs")] }),
    verification_ref: await json(path.join(previous, "verification.json"), { ...bindings,
      admitted: true, verified: true, healthy: true, observed_release: manifest.release }) };
  const auth = { schema_version: "operator_deployment_authorization_record_v1", action_id: "new-action", native_thread_id: thread,
    target, operator_request: "直接部署", restart_existing_switchboard: true, modify_signed_app: false, new_service: false,
    expires_at: expiry, source_update: { schema_version: SCOPE, files }, source: { basis: "existing operator evidence" } };
  const log = await put(path.join(workspace, "test.log"), "existing passing TAP\n");
  const context = await put(path.join(candidate, "src/context.mjs"), "unchanged test context\n");
  const binding = await json(path.join(workspace, "source-binding.json"), { candidate, files, context });
  const evidence = { parent_thread_id: thread, test_exit_code: 0, tested_policy_sha256: files[0].sha256,
    tested_test_sha256: files[1].sha256, test_log_sha256: log.sha256, original_test_log: log.path,
    tap_summary: { tests: 1, pass: 1, fail: 0, cancelled: 0, skipped: 0, todo: 0 },
    test_command: { argv: [node, "--test", files[1].target], cwd: candidate },
    source_binding_ref: binding, unchanged_test_context: [context], worker_reran_tests: false };
  const request = { schema_version: "switchboard_workflow_update_prepare_v1", identity: { uid: process.getuid(), HOME: home, CODEX_HOME: path.join(home, ".codex") },
    native_thread_id: thread, action_id: auth.action_id, release: "new-release", expires_at: expiry,
    candidate, files, workspace, output: path.join(workspace, auth.action_id), previous_packet,
    authorization_ref: await json(path.join(workspace, "existing-authorization.json"), auth),
    test_evidence_ref: await json(path.join(workspace, "test-evidence.json"), evidence) };
  const env = { HOME: home, CODEX_HOME: request.identity.CODEX_HOME, CODEX_THREAD_ID: thread };
  const effects = [];
  const forbidden = () => { throw new Error("Unexpected deployment effect"); };
  // Enumerate imports: an ESM namespace also exports the reserved parameter name default.
  const dependencies = { copyFile: fs.copyFile, lstat: fs.lstat, readFile: fs.readFile,
    realpath: fs.realpath, rename: fs.rename, unlink: fs.unlink, assert, createHash, constants, path,
    process: { env, getuid: () => process.getuid(), pid: process.pid },
    execFile: forbidden, promisify: () => forbidden, withUpdateLock: forbidden, fetch: forbidden,
    CATALOG_PATH, ADMISSION_PATH, WORKFLOW_PATH, PICKER_CATALOG_PATH,
    mkdir: async (...args) => { effects.push(["mkdir", args[0]]); return fs.mkdir(...args); },
    open: async (...args) => { if (args[1] === "wx") effects.push(["open", args[0]]); return fs.open(...args); } };
  const body = source.replace(/^import [^\n]+;\n/gm, "").replace(/^export /gm, "")
    .replace('const HOME = "/Users/nokiy";', `const HOME = ${JSON.stringify(home)};`);
  assert.doesNotMatch(body, /^import |^export /m);
  const api = new Function(...Object.keys(dependencies), `"use strict";\n${body}\nreturn { workflowUpdate, boundManifest };`)(...Object.values(dependencies));
  const requestPath = path.join(workspace, "prepare.json");
  const run = async () => { await json(requestPath, request); return api.workflowUpdate("prepare", requestPath); };
  const sealAuth = async () => { request.authorization_ref = await json(request.authorization_ref.path, auth); };
  return { request, auth, evidence, env, effects, originals, current, context, target, manifest, plan,
    run, sealAuth, put, json, api, read: async name => JSON.parse(await fs.readFile(name, "utf8")) };
}

const executorFiles = ["package-lock.json", "package.json", "src/activation.mjs", "src/catalog.mjs",
  "src/claude-bridge-route.mjs", "src/config-state.mjs", "src/external-admission.mjs",
  "src/nokiy-coordinator-policy.mjs", "src/nokiy-direct-route.mjs", "src/nokiy-native-contract.json",
  "src/nokiy-native-observation.mjs", "src/nokiy-native-tool.mjs", "src/nokiy-result-delivery.mjs",
  "src/nokiy-workflow-profiles.mjs", "src/picker-catalog.mjs", "src/route-ownership.mjs",
  "src/shared.mjs", "src/update-lock.mjs", "src/workflow-update.mjs"];

async function immutableExecutor(f, root = f.manifest.candidate) {
  // Reuse a saved predecessor candidate to exercise retention of its code pins.
  const bindings = [];
  for (const relative_path of executorFiles) {
    const installed = path.join(f.target, relative_path), executor = path.join(root, relative_path);
    if (!f.originals.has(installed)) await f.put(installed, `installed ${relative_path}\n`);
    const bytes = await fs.readFile(installed);
    await f.put(executor, bytes);
    bindings.push({ relative_path, installed, executor, sha256: hash(bytes) });
  }
  const nodeModules = path.join(f.target, "node_modules");
  await fs.mkdir(nodeModules, { recursive: true });
  await fs.symlink(nodeModules, path.join(root, "node_modules"), "dir");
  const proof = { status: "EXACT_SAME_BYTE_EXECUTOR_VERIFIED", installed_root: f.target,
    executor_root: root, bindings, node_modules_same_physical_directory: nodeModules };
  f.request.executor_bindings_ref = await f.json(path.join(f.request.workspace, `${path.basename(root)}-executor-bindings.json`), proof);
  return proof;
}

async function publisherUpdate(f) {
  const relative = "src/workflow-update.mjs", ref = await f.put(path.join(f.request.candidate, relative), "new publisher bytes\n");
  f.request.files.push({ target: relative, source: ref.path, sha256: ref.sha256, preimage: f.current[path.join(f.target, relative)] });
  f.evidence.tested_publisher_sha256 = ref.sha256;
  const binding = await f.read(f.evidence.source_binding_ref.path);
  binding.files = f.request.files;
  f.evidence.source_binding_ref = await f.json(f.evidence.source_binding_ref.path, binding);
  f.request.test_evidence_ref = await f.json(f.request.test_evidence_ref.path, f.evidence);
  await f.sealAuth();
}

async function sealPrevious(f) {
  const prior = f.request.previous_packet;
  prior.manifest_ref = await f.json(prior.manifest_ref.path, f.manifest);
  for (const c of Object.values(f.plan.commands)) {
    c.files.find(ref => ref.path === prior.manifest_ref.path).sha256 = prior.manifest_ref.sha256;
  }
  prior.plan_ref = await f.json(prior.plan_ref.path, f.plan);
  for (const key of ["apply_state_ref", "verification_ref"]) {
    const record = await f.read(prior[key].path);
    record.plan_sha256 = planHash(f.plan);
    record.manifest_sha256 = prior.manifest_ref.sha256;
    prior[key] = await f.json(prior[key].path, record);
  }
}

async function nativeHostSuccessor(f) {
  const host = "src/nokiy-native-host.py", installed = await f.put(path.join(f.target, host), "installed native host\n");
  f.manifest.invariants[installed.path] = installed.sha256;
  for (const c of Object.values(f.plan.commands)) c.files.push(installed);
  await sealPrevious(f);
  const predecessor = await immutableExecutor(f);
  await publisherUpdate(f);
  const first = await f.run(), manifest = await f.read(first.manifest_ref.path), plan = await f.read(first.plan_ref.path);
  // Model completed native receipts entirely inside the temporary fixture.
  for (const file of manifest.files) await f.put(path.join(f.target, file.target), await fs.readFile(file.source));
  const bindings = { action_id: manifest.action_id, native_thread_id: manifest.native_thread_id, target: f.target,
    release: manifest.release, plan_sha256: first.plan_sha256, manifest_sha256: first.manifest_ref.sha256 };
  f.request.previous_packet = { plan_ref: first.plan_ref, manifest_ref: first.manifest_ref,
    apply_state_ref: await f.json(path.join(manifest.artifact_root, "apply-state.json"), {
      ...bindings, state: "APPLIED_PENDING_VERIFY", written: manifest.files.map(file => file.target),
      admission_sha256: manifest.admission_preimage, server_sha256: manifest.invariants[path.join(f.target, "src/server.mjs")] }),
    verification_ref: await f.json(path.join(manifest.artifact_root, "verification.json"), {
      ...bindings, admitted: true, verified: true, healthy: true, observed_release: manifest.release }) };
  f.manifest = manifest; f.plan = plan;
  f.request.action_id = f.auth.action_id = "host-action";
  f.request.release = "host-release";
  f.request.output = path.join(f.request.workspace, f.request.action_id);
  f.request.candidate = path.join(path.dirname(f.request.candidate), "host-candidate");
  const ref = await f.put(path.join(f.request.candidate, host), "# fixed native host\n");
  f.request.files = f.auth.source_update.files = [{ target: host, source: ref.path, sha256: ref.sha256, preimage: installed.sha256 }];
  await f.sealAuth();
  f.evidence.test_command.cwd = f.request.candidate;
  f.evidence.tested_host_sha256 = ref.sha256;
  f.evidence.unchanged_test_context = [];
  f.evidence.source_binding_ref = await f.json(path.join(f.request.workspace, "host-source-binding.json"), {
    candidate: f.request.candidate, files: f.request.files });
  f.request.test_evidence_ref = await f.json(path.join(f.request.workspace, "host-test-evidence.json"), f.evidence);
  const executor = await immutableExecutor(f, path.join(path.dirname(f.request.candidate), "host-executor"));
  f.effects.length = 0;
  return { predecessor, executor };
}

test("prepare rotates a verified publisher executor before preparing the exact native host update", async t => {
  const f = await fixture(t), { predecessor, executor } = await nativeHostSuccessor(f);
  const historical = structuredClone(f.request.previous_packet);
  const result = await f.run(), manifest = await f.read(result.manifest_ref.path), plan = await f.read(result.plan_ref.path);
  assert.deepEqual(manifest.files, f.request.files);
  assert.equal(manifest.files[0].target, "src/nokiy-native-host.py");
  assert.equal(manifest.invariants[path.join(f.target, "src/workflow-update.mjs")],
    executor.bindings.find(b => b.relative_path === "src/workflow-update.mjs").sha256);
  assert.notEqual(executor.bindings.find(b => b.relative_path === "src/workflow-update.mjs").sha256,
    predecessor.bindings.find(b => b.relative_path === "src/workflow-update.mjs").sha256);
  for (const phase of ["preflight", "apply", "verify"]) {
    const c = plan.commands[phase], pins = new Map(c.files.map(ref => [ref.path, ref.sha256]));
    assert.deepEqual(c.argv, [f.plan.commands[phase].argv[0], path.join(executor.executor_root, "src/activation.mjs"),
      "--workflow-update", phase, result.manifest_ref.path]);
    for (const b of executor.bindings) assert.equal(pins.get(b.executor), b.sha256);
    for (const b of predecessor.bindings) assert.equal(pins.has(b.executor), false);
    for (const ref of Object.values(historical)) assert.equal(pins.has(ref.path), false);
    assert.equal(pins.has(path.join(f.target, "src/nokiy-native-host.py")), false);
    assert.equal(pins.get(manifest.files[0].source), manifest.files[0].sha256);
    assert.equal(pins.get(f.request.executor_bindings_ref.path), f.request.executor_bindings_ref.sha256);
    assert.ok(c.files.length <= 64);
    for (const ref of c.files) assert.equal(hash(await fs.readFile(ref.path)), ref.sha256);
  }
  for (const ref of Object.values(historical)) assert.equal(hash(await fs.readFile(ref.path)), ref.sha256);
  f.env.NOKIY_DEPLOYMENT_ACTION_ID = f.request.action_id;
  f.env.NOKIY_DEPLOYMENT_PLAN_SHA256 = result.plan_sha256;
  assert.deepEqual(await f.api.boundManifest(result.manifest_ref.path), manifest);
});

for (const stale of [false, true]) test(`executor rotation rejects ${stale ? "stale same-byte bindings" : "mismatched successor bytes"} before publication`, async t => {
  const f = await fixture(t), { predecessor, executor } = await nativeHostSuccessor(f);
  const binding = executor.bindings.find(b => b.relative_path === "src/workflow-update.mjs");
  if (stale) {
    const old = predecessor.bindings.find(b => b.relative_path === binding.relative_path);
    await f.put(binding.executor, await fs.readFile(old.executor));
    binding.sha256 = old.sha256;
    f.request.executor_bindings_ref = await f.json(f.request.executor_bindings_ref.path, executor);
  } else await f.put(binding.executor, "mismatched successor publisher\n");
  await assert.rejects(f.run(), stale ? /Executor installed binding drift: src\/workflow-update.mjs/ : /Executor digest drift: src\/workflow-update.mjs/);
  assert.deepEqual(f.effects, []);
  await assert.rejects(fs.lstat(f.request.output), { code: "ENOENT" });
});

test("executor rotation requires every predecessor dependency pin in every phase", async t => {
  const f = await fixture(t), { predecessor } = await nativeHostSuccessor(f);
  const missing = path.join(predecessor.executor_root, "src/nokiy-coordinator-policy.mjs");
  f.plan.commands.verify.files = f.plan.commands.verify.files.filter(ref => ref.path !== missing);
  await sealPrevious(f);
  await assert.rejects(f.run(), /Missing previous command pin: .*src\/nokiy-coordinator-policy.mjs/);
  assert.deepEqual(f.effects, []);
  await assert.rejects(fs.lstat(f.request.output), { code: "ENOENT" });
});

test("self-update command pins survive installed postimages with the complete same-byte executor", async t => {
  const f = await fixture(t), proof = await immutableExecutor(f);
  await publisherUpdate(f);
  const result = await f.run(), manifest = await f.read(result.manifest_ref.path), plan = await f.read(result.plan_ref.path);
  assert.deepEqual(manifest.files, [...f.request.files].sort((a, b) => a.target.localeCompare(b.target)));
  assert.deepEqual(manifest.invariants, Object.fromEntries(Object.entries(f.manifest.invariants)
    .filter(([name]) => !manifest.files.some(file => name === path.join(f.target, file.target)))));
  for (const key of ["action_id", "native_thread_id", "release", "expires_at"]) assert.equal(plan[key], f.request[key]);
  assert.equal(plan.target, f.target);
  assert.deepEqual(plan.authorization_ref, manifest.authorization_ref);
  assert.equal(await fs.readFile(manifest.authorization_ref.path, "utf8"), encoded(f.auth));
  for (const phase of ["preflight", "apply", "verify"]) {
    const c = plan.commands[phase], pins = new Map(c.files.map(ref => [ref.path, ref.sha256]));
    assert.deepEqual(c.argv, [f.plan.commands[phase].argv[0], path.join(proof.executor_root, "src/activation.mjs"),
      "--workflow-update", phase, result.manifest_ref.path]);
    assert.equal(pins.get(c.argv[0]), f.plan.commands[phase].files.find(ref => ref.path === c.argv[0]).sha256);
    assert.equal(pins.get(f.request.executor_bindings_ref.path), f.request.executor_bindings_ref.sha256);
    for (const b of proof.bindings) assert.equal(pins.get(b.executor), b.sha256);
    for (const [name, sha256] of Object.entries(manifest.invariants)) assert.equal(pins.get(name), sha256);
    for (const file of manifest.files) {
      assert.equal(pins.has(path.join(f.target, file.target)), false);
      assert.equal(hash(await fs.readFile(path.join(f.target, file.target)).catch(error => {
        if (error.code === "ENOENT") return Buffer.alloc(0);
        throw error;
      })), file.preimage ?? hash(Buffer.alloc(0)));
    }
  }
  // Model native's all-command freshness scan after applying every postimage.
  for (const file of manifest.files) await f.put(path.join(f.target, file.target), await fs.readFile(file.source));
  for (const c of Object.values(plan.commands)) for (const ref of c.files) {
    assert.equal(hash(await fs.readFile(ref.path)), ref.sha256, `Post-apply pin drift: ${ref.path}`);
  }
  assert.notEqual(hash(await fs.readFile(path.join(f.target, "src/workflow-update.mjs"))),
    proof.bindings.find(b => b.relative_path === "src/workflow-update.mjs").sha256);
});

test("prepare rejects changed immutable dependency bytes before publication", async t => {
  const f = await fixture(t), proof = await immutableExecutor(f);
  await publisherUpdate(f);
  await f.put(path.join(proof.executor_root, "src/nokiy-coordinator-policy.mjs"), "different policy\n");
  await assert.rejects(f.run(), /Executor digest drift: src\/nokiy-coordinator-policy.mjs/);
  assert.deepEqual(f.effects, []);
  await assert.rejects(fs.lstat(f.request.output), { code: "ENOENT" });
});

test("prepare rejects self-update without an explicit immutable executor before publication", async t => {
  const f = await fixture(t);
  await publisherUpdate(f);
  await assert.rejects(f.run(), /Unsafe execution pin target overlap.*executor_bindings_ref/);
  assert.deepEqual(f.effects, []);
  await assert.rejects(fs.lstat(f.request.output), { code: "ENOENT" });
});

test("prepare rejects a conflicting previous executor dependency pin", async t => {
  const f = await fixture(t);
  await immutableExecutor(f);
  const installed = path.join(f.target, "src/catalog.mjs");
  for (const c of Object.values(f.plan.commands)) {
    c.files.find(ref => ref.path === installed).sha256 = "a".repeat(64);
  }
  await sealPrevious(f);
  await assert.rejects(f.run(), /previous executor binding mismatch/);
  assert.deepEqual(f.effects, []);
  await assert.rejects(fs.lstat(f.request.output), { code: "ENOENT" });
});

test("prepare admits the current immutable closure without accumulating historical pins", async t => {
  const f = await fixture(t), proof = await immutableExecutor(f);
  await publisherUpdate(f);
  const dependencies = [
    await f.put(path.join(f.request.identity.HOME, "bin/python3"), "protected interpreter\n"),
    await f.put(path.join(f.target, "src/nokiy-native-host.py"), "protected native script\n"),
  ];
  for (const ref of dependencies) {
    f.manifest.invariants[ref.path] = ref.sha256;
    for (const c of Object.values(f.plan.commands)) c.files.push(ref);
  }
  // These inputs live outside the predecessor packet/candidate, reproducing
  // the old location-based inheritance bug even with an admissible predecessor.
  const historical = [], count = 64 - f.plan.commands.preflight.files.length;
  for (let i = 0; i < count; i++) {
    const name = ["test-evidence.json", "source-binding.json", "authorization.json", "test.log"][i]
      ?? `older-release-${i}.json`;
    const ref = await f.put(path.join(f.request.workspace, "historical-inputs", name), `historical ${i}\n`);
    historical.push(ref);
    for (const c of Object.values(f.plan.commands)) c.files.push(ref);
  }
  await sealPrevious(f);
  for (const c of Object.values(f.plan.commands)) assert.equal(c.files.length, 64);
  const result = await f.run(), manifest = await f.read(result.manifest_ref.path), plan = await f.read(result.plan_ref.path);
  const required = [result.manifest_ref, manifest.authorization_ref, f.request.executor_bindings_ref,
    f.request.test_evidence_ref, f.evidence.source_binding_ref,
    { path: f.evidence.original_test_log, sha256: f.evidence.test_log_sha256 }, ...f.evidence.unchanged_test_context,
    ...proof.bindings.map(b => ({ path: b.executor, sha256: b.sha256 })),
    ...Object.entries(manifest.invariants).map(([path, sha256]) => ({ path, sha256 })),
    ...manifest.files.map(file => ({ path: file.source, sha256: file.sha256 }))];
  const previousEvidence = [...historical, ...Object.values(f.request.previous_packet), f.plan.authorization_ref];
  for (const phase of ["preflight", "apply", "verify"]) {
    const c = plan.commands[phase], pins = new Map(c.files.map(ref => [ref.path, ref.sha256]));
    const executable = f.plan.commands[phase].files.find(ref => ref.path === c.argv[0]);
    assert.deepEqual(pins, new Map([executable, ...required].map(ref => [ref.path, ref.sha256])));
    assert.equal(c.files.length, pins.size);
    assert.ok(c.files.length <= 64);
    for (const ref of previousEvidence) assert.equal(pins.has(ref.path), false, ref.path);
  }
  // Publication preserves the original receipts and other historical evidence.
  for (const ref of previousEvidence) assert.equal(hash(await fs.readFile(ref.path)), ref.sha256);
  for (const [name, bytes] of f.originals) assert.deepEqual(await fs.readFile(name), bytes, name);
});

test("prepare rejects 65 required current pins before writes and admits exactly 64", async t => {
  const f = await fixture(t);
  const required = new Set([f.plan.commands.preflight.argv[0],
    ...executorFiles.map(name => path.join(f.target, name)),
    ...Object.keys(f.manifest.invariants).filter(name => !f.request.files.some(file => name === path.join(f.target, file.target))),
    ...f.request.files.map(file => file.source), path.join(f.request.output, "manifest.json"),
    path.join(f.request.output, "authorization.json"), f.request.test_evidence_ref.path,
    f.evidence.source_binding_ref.path, f.evidence.original_test_log, ...f.evidence.unchanged_test_context.map(ref => ref.path)]);
  while (required.size < 65) {
    const ref = await f.put(path.join(f.request.candidate, "tests", `context-${required.size}.test.mjs`), `required context ${required.size}\n`);
    f.evidence.unchanged_test_context.push(ref);
    required.add(ref.path);
  }
  f.request.test_evidence_ref = await f.json(f.request.test_evidence_ref.path, f.evidence);
  await assert.rejects(f.run(), /commands\.preflight\.files count 65 exceeds native admission limit 64/);
  assert.deepEqual(f.effects, []);
  await assert.rejects(fs.lstat(f.request.output), { code: "ENOENT" });
  required.delete(f.evidence.unchanged_test_context.pop().path);
  f.request.test_evidence_ref = await f.json(f.request.test_evidence_ref.path, f.evidence);
  const result = await f.run(), plan = await f.read(result.plan_ref.path);
  for (const c of Object.values(plan.commands)) {
    assert.equal(c.files.length, 64);
    assert.deepEqual(new Set(c.files.map(ref => ref.path)), required);
    for (const ref of c.files) assert.equal(hash(await fs.readFile(ref.path)), ref.sha256);
  }
});

test("prepare accepts a completed immutable-executor self-update as the next release's predecessor", async t => {
  const f = await fixture(t), proof = await immutableExecutor(f);
  await publisherUpdate(f);
  const first = await f.run(), manifest = await f.read(first.manifest_ref.path), plan = await f.read(first.plan_ref.path);
  for (const file of manifest.files) await f.put(path.join(f.target, file.target), await fs.readFile(file.source));
  const bindings = { action_id: manifest.action_id, native_thread_id: manifest.native_thread_id, target: f.target,
    release: manifest.release, plan_sha256: first.plan_sha256, manifest_sha256: first.manifest_ref.sha256 };
  f.request.previous_packet = { plan_ref: first.plan_ref, manifest_ref: first.manifest_ref,
    apply_state_ref: await f.json(path.join(manifest.artifact_root, "apply-state.json"), {
      ...bindings, state: "APPLIED_PENDING_VERIFY", written: manifest.files.map(file => file.target),
      admission_sha256: manifest.admission_preimage, server_sha256: manifest.invariants[path.join(f.target, "src/server.mjs")] }),
    verification_ref: await f.json(path.join(manifest.artifact_root, "verification.json"), {
      ...bindings, admitted: true, verified: true, healthy: true, observed_release: manifest.release }) };
  // The next release uses a separate candidate and a new exact source authorization.
  f.request.action_id = f.auth.action_id = "next-action";
  f.request.release = "next-release";
  f.request.output = path.join(f.request.workspace, f.request.action_id);
  f.request.candidate = path.join(path.dirname(f.request.candidate), "next-candidate");
  const ref = await f.put(path.join(f.request.candidate, "src/next.mjs"), "next release bytes\n");
  f.request.files = f.auth.source_update.files = [{ target: "src/next.mjs", source: ref.path, sha256: ref.sha256, preimage: null }];
  await f.sealAuth();
  f.evidence.test_command.cwd = f.request.candidate;
  f.evidence.test_command.argv = [plan.commands.preflight.argv[0], "--test", "tests/next.test.mjs"];
  f.evidence.tested_next_sha256 = ref.sha256;
  f.evidence.unchanged_test_context = [];
  f.evidence.source_binding_ref = await f.json(path.join(f.request.workspace, "next-source-binding.json"), {
    candidate: f.request.candidate, files: f.request.files });
  f.request.test_evidence_ref = await f.json(path.join(f.request.workspace, "next-test-evidence.json"), f.evidence);
  f.effects.length = 0;
  const result = await f.run(), next = await f.read(result.plan_ref.path), nextManifest = await f.read(result.manifest_ref.path);
  assert.deepEqual(nextManifest.files, f.request.files);
  assert.equal(nextManifest.invariants[path.join(f.target, "src/workflow-update.mjs")],
    manifest.files.find(file => file.target === "src/workflow-update.mjs").sha256);
  for (const phase of ["preflight", "apply", "verify"]) {
    assert.deepEqual(next.commands[phase].argv, [...plan.commands[phase].argv.slice(0, 4), result.manifest_ref.path]);
    const pins = new Map(next.commands[phase].files.map(ref => [ref.path, ref.sha256]));
    for (const b of proof.bindings) assert.equal(pins.get(b.executor), b.sha256);
    for (const ref of next.commands[phase].files) assert.equal(hash(await fs.readFile(ref.path)), ref.sha256);
  }
});

test("prepare produces the existing native packet and validates with the unchanged manifest contract", async t => {
  const f = await fixture(t), before = structuredClone(f.request);
  const result = await f.run(), manifest = await f.read(result.manifest_ref.path), plan = await f.read(result.plan_ref.path);
  assert.deepEqual(f.request, before);
  assert.equal(result.evidence_only, true);
  assert.equal(result.plan_sha256, planHash(plan));
  for (const ref of [result.manifest_ref, result.plan_ref]) assert.equal(hash(await fs.readFile(ref.path)), ref.sha256);
  assert.equal(await fs.readFile(manifest.authorization_ref.path, "utf8"), encoded(f.auth));
  assert.equal(manifest.catalog_preimage, f.current[Object.keys(f.current).find(x => x.endsWith("admitted-models.json"))]);
  assert.equal(manifest.admission_preimage, f.current[Object.keys(f.current).find(x => x.endsWith("admission.json"))]);
  assert.deepEqual(manifest.files, f.request.files);
  assert.deepEqual(manifest.invariants, Object.fromEntries(Object.entries(f.manifest.invariants).filter(([name]) => !f.request.files.some(x => name === path.join(f.target, x.target)))));
  for (const phase of ["preflight", "apply", "verify"]) {
    assert.deepEqual(plan.commands[phase].argv, [...f.plan.commands[phase].argv.slice(0, 4), result.manifest_ref.path]);
    assert.equal(plan.commands[phase].timeout_seconds, 180);
    const pins = new Map(plan.commands[phase].files.map(ref => [ref.path, ref.sha256]));
    for (const ref of [result.manifest_ref, manifest.authorization_ref, f.request.test_evidence_ref]) assert.equal(pins.get(ref.path), ref.sha256);
    for (const ref of Object.values(f.request.previous_packet)) assert.equal(pins.has(ref.path), false);
    for (const file of manifest.files) assert.equal(pins.get(file.source), file.sha256);
  }
  f.env.NOKIY_DEPLOYMENT_ACTION_ID = f.request.action_id;
  f.env.NOKIY_DEPLOYMENT_PLAN_SHA256 = result.plan_sha256;
  assert.deepEqual(await f.api.boundManifest(result.manifest_ref.path), manifest);
  const receipts = await fs.lstat(plan.artifact_root);
  assert.equal(plan.artifact_root, path.join(f.request.output, "native-artifacts"));
  assert.equal(receipts.isDirectory(), true);
  assert.equal(receipts.isSymbolicLink(), false);
  assert.equal(receipts.uid, process.getuid());
  assert.equal(receipts.mode & 0o777, 0o700);
  assert.deepEqual(await fs.readdir(plan.artifact_root), []);
  assert.deepEqual(await fs.readdir(f.request.output), ["authorization.json", "manifest.json", "native-artifacts", "native-plan.json"]);
  assert.deepEqual(f.effects, [["mkdir", f.request.output], ["mkdir", plan.artifact_root],
    ...["authorization.json", "manifest.json", "native-plan.json"].map(name => ["open", path.join(f.request.output, name)])]);
  for (const [name, bytes] of f.originals) assert.deepEqual(await fs.readFile(name), bytes, `Input changed: ${name}`);
  const effects = [...f.effects];
  await assert.rejects(f.run(), /output already exists/);
  assert.deepEqual(f.effects, effects);
});

test("prepare preserves a system executable pin and rejects digest drift before publication", async t => {
  const executable = { path: await fs.realpath("/bin/echo") };
  executable.sha256 = hash(await fs.readFile(executable.path));
  assert.equal((await fs.lstat(executable.path)).uid, 0);
  assert.equal((await fs.lstat(path.dirname(executable.path))).uid, 0);
  const f = await fixture(t, { commandExecutable: executable });
  const result = await f.run(), plan = await f.read(result.plan_ref.path);
  assert.equal(result.evidence_only, true);
  for (const phase of ["preflight", "apply", "verify"]) {
    assert.equal(plan.commands[phase].argv[0], executable.path);
    assert.deepEqual(plan.commands[phase].files.find(ref => ref.path === executable.path), executable);
  }
  assert.equal(hash(await fs.readFile(executable.path)), executable.sha256);

  // Model a stale verified pin without modifying the installed system binary.
  const stale = { ...executable, sha256: hash("previous executable bytes\n") };
  assert.notEqual(stale.sha256, executable.sha256);
  const drifted = await fixture(t, { commandExecutable: stale });
  await assert.rejects(drifted.run(), /previous command pin drift/);
  assert.deepEqual(drifted.effects, []);
  await assert.rejects(fs.lstat(drifted.request.output), { code: "ENOENT" });
});

test("prepare preserves unset CODEX_HOME and uses the legitimate HOME default", async t => {
  const f = await fixture(t);
  delete f.env.CODEX_HOME;
  f.request.identity.CODEX_HOME = null;
  const env = { ...f.env }, request = structuredClone(f.request);
  const result = await f.run();
  assert.equal(result.evidence_only, true);
  assert.deepEqual(f.env, env);
  assert.equal(Object.hasOwn(f.env, "CODEX_HOME"), false);
  assert.deepEqual(f.request, request);
});

test("prepare preserves an explicit CODEX_HOME outside the default directory", async t => {
  const f = await fixture(t);
  const explicitHome = path.join(f.request.identity.HOME, "explicit-codex");
  await fs.mkdir(explicitHome);
  f.env.CODEX_HOME = f.request.identity.CODEX_HOME = explicitHome;
  const env = { ...f.env }, request = structuredClone(f.request);
  const result = await f.run();
  assert.equal(result.evidence_only, true);
  assert.deepEqual(f.env, env);
  assert.deepEqual(f.request, request);
});

test("prepare reuses a hash-verified completed predecessor after its execution expiry", async t => {
  const f = await fixture(t, { previousExpiresAt: 1 });
  const result = await f.run(), manifest = await f.read(result.manifest_ref.path), plan = await f.read(result.plan_ref.path);
  assert.equal(f.manifest.expires_at, 1);
  assert.ok(f.request.expires_at > f.manifest.expires_at);
  assert.equal(manifest.expires_at, f.request.expires_at);
  assert.equal(plan.expires_at, f.request.expires_at);
  assert.ok(plan.expires_at <= f.auth.expires_at);
  for (const phase of ["preflight", "apply", "verify"]) {
    const pins = new Map(plan.commands[phase].files.map(ref => [ref.path, ref.sha256]));
    for (const ref of Object.values(f.request.previous_packet)) {
      assert.equal(pins.has(ref.path), false);
      assert.equal(hash(await fs.readFile(ref.path)), ref.sha256);
    }
  }
  f.env.NOKIY_DEPLOYMENT_ACTION_ID = f.request.action_id;
  f.env.NOKIY_DEPLOYMENT_PLAN_SHA256 = result.plan_sha256;
  assert.deepEqual(await f.api.boundManifest(result.manifest_ref.path), manifest);
});

test("prepare rejects expired new authorization despite a completed expired predecessor", async t => {
  const f = await fixture(t, { previousExpiresAt: 1 });
  f.auth.expires_at = 1;
  await f.sealAuth();
  await assert.rejects(f.run(), /authorization.expires_at/);
  assert.deepEqual(f.effects, []);
});

const guards = [
  ["missing candidate", f => { delete f.request.candidate; }, /candidate/],
  ["candidate drift", f => f.put(f.request.files[0].source, "changed"), /Candidate changed/],
  ["installed preimage drift", f => f.put(path.join(f.target, "src/widget.mjs"), "changed"), /installed source drift|Preimage drift/],
  ["installed invariant drift", f => f.put(path.join(f.target, "src/server.mjs"), "changed"), /invariant changed/],
  ["previous receipt drift", f => f.put(f.request.previous_packet.verification_ref.path, "{}"), /verification_ref hash drift/],
  ["expired authorization", async f => { f.auth.expires_at = 1; await f.sealAuth(); }, /authorization.expires_at/],
  ["expired requested plan", f => { f.request.expires_at = 1; }, /expired expires_at/],
  ["extended expiry", f => { f.request.expires_at += 1; }, /extends authorization/],
  ["authorization scope mismatch", async f => { f.auth.source_update.files = []; await f.sealAuth(); }, /scope/],
  ["unbound test candidate", async f => { f.evidence.tested_policy_sha256 = "a".repeat(64); f.request.test_evidence_ref = await f.json(f.request.test_evidence_ref.path, f.evidence); }, /candidate hash/],
  ["malformed test evidence", async f => { f.request.test_evidence_ref = await f.put(f.request.test_evidence_ref.path, "invalid JSON"); }, /Invalid test_evidence_ref/],
  ["test context drift", f => f.put(f.context.path, "changed"), /test context drift/],
  ["owner mismatch", f => { f.request.identity.uid += 1; }, /identity.uid/],
  ["thread mismatch", f => { f.env.CODEX_THREAD_ID = "other-thread"; }, /native_thread_id/],
  ["HOME drift", f => { f.env.HOME += "-other"; }, /identity.HOME/],
  ["CODEX_HOME drift", f => { f.env.CODEX_HOME += "-other"; }, /identity.CODEX_HOME/],
  ["CODEX_HOME becoming unset", f => { delete f.env.CODEX_HOME; }, /identity.CODEX_HOME environment drift/],
  ["CODEX_HOME becoming explicit", f => { f.request.identity.CODEX_HOME = null; }, /identity.CODEX_HOME environment drift/],
  ["unset CODEX_HOME workspace overlap", f => {
    delete f.env.CODEX_HOME;
    f.request.identity.CODEX_HOME = null;
    f.request.workspace = path.join(f.request.identity.HOME, ".codex");
    f.request.output = path.join(f.request.workspace, f.request.action_id);
  }, /protected\/candidate overlap/],
  ["unsafe output", f => { f.request.output = path.join(f.request.candidate, f.request.action_id); }, /output/],
  ["existing action output", f => f.put(path.join(f.request.output, "apply-state.json"), "uncertain"), /output already exists/],
];
for (const [name, change, message] of guards) test(`prepare rejects ${name} before effects`, async t => {
  const f = await fixture(t);
  await change(f);
  await assert.rejects(f.run(), message);
  assert.deepEqual(f.effects, []);
});
