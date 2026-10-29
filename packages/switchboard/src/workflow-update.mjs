import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { constants } from "node:fs";
import { copyFile, lstat, mkdir, open, readFile, realpath, rename, unlink } from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";
import { ADMISSION_PATH, CATALOG_PATH, RUNTIME_PATH, WORKFLOW_PATH, verifyAdmission } from "./catalog.mjs";
import { withDirectCatalog } from "./nokiy-direct-route.mjs";
import { withUpdateLock } from "./update-lock.mjs";
import { verifyNativeInterpreter } from "./nokiy-native-tool.mjs";
import { composePickerCatalog, publishPickerCatalog, PICKER_CATALOG_PATH } from "./picker-catalog.mjs";
import { loadClaudeBridge } from "./claude-bridge-route.mjs";

const HOME = "/Users/nokiy";
const TARGET = `${HOME}/.codex-multi/switchboard`;
const LABEL = "com.nokiy.codex-inference-switchboard";
const SERVICE = `gui/${process.getuid()}/${LABEL}`;
const PLIST = `${HOME}/Library/LaunchAgents/${LABEL}.plist`;
const ALLOWED = new Set(["src/server.mjs", "src/catalog.mjs", "src/admission.mjs", "src/activation.mjs",
  "src/nokiy-workflow-profiles.mjs", "src/nokiy-direct-route.mjs", "src/nokiy-native-tool.mjs",
  "src/nokiy-native-host.py", "src/nokiy-native-contract.json", "src/workflow-update.mjs", "src/update-lock.mjs", "config/nokiy-workflows.json"]);
const run = promisify(execFile);
const digest = bytes => createHash("sha256").update(bytes).digest("hex");
const read = async file => JSON.parse((await fileBytes(file)).toString("utf8"));
const SOURCE_SCOPE = "switchboard_exact_source_update_v1";
const SHA256 = /^[0-9a-f]{64}$/;
const sourceOnly = m => m.update_scope === SOURCE_SCOPE;
const manifestHashes = new WeakMap();
const canonical = value => JSON.stringify(sort(value));
function sort(value) {
  return Array.isArray(value) ? value.map(sort) : value && typeof value === "object"
    ? Object.fromEntries(Object.keys(value).sort().map(k => [k, sort(value[k])])) : value;
}
async function ownedDirectory(dir) {
  assert.equal(path.resolve(dir), dir, "Expected absolute canonical directory");
  const st = await lstat(dir);
  assert.ok(st.isDirectory() && !st.isSymbolicLink() && st.uid === process.getuid(), `Unsafe directory: ${dir}`);
  assert.equal(await realpath(dir), dir);
}
async function fileBytes(file, missing = false) {
  assert.equal(path.resolve(file), file, "Expected absolute canonical file");
  // Check the parent even for an absent preimage; ENOENT must not admit a
  // missing directory or a dangling/symlinked path into another tree.
  await ownedDirectory(path.dirname(file));
  let fd;
  try {
    const st = await lstat(file);
    assert.ok(st.isFile() && !st.isSymbolicLink() && st.uid === process.getuid(), `Unsafe file identity: ${file}`);
    fd = await open(file, constants.O_RDONLY | constants.O_NOFOLLOW);
    const opened = await fd.stat();
    assert.ok(opened.isFile() && opened.uid === process.getuid(), `Unsafe open file: ${file}`);
    assert.equal(opened.dev, st.dev);
    assert.equal(opened.ino, st.ino);
    return await fd.readFile();
  } catch (error) { if (missing && error.code === "ENOENT") return null; throw error; }
  finally { if (fd) await fd.close(); }
}
async function identity(file, missing = false) {
  const bytes = await fileBytes(file, missing);
  return bytes === null ? null : digest(bytes);
}
async function checkPinnedExecutable(file, expected) {
  // A verified command may use a system-owned executable. Read only its exact
  // pinned identity; writable packet and source paths still use fileBytes.
  assert.match(expected || "", SHA256, `Missing previous command pin: ${file}`);
  assert.equal(path.resolve(file), file, "Expected absolute canonical executable");
  assert.equal(await realpath(file), file, "Expected canonical executable path");
  const st = await lstat(file);
  assert.ok(st.isFile() && !st.isSymbolicLink(), `Unsafe executable identity: ${file}`);
  const fd = await open(file, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const opened = await fd.stat();
    assert.ok(opened.isFile(), `Unsafe open executable: ${file}`);
    assert.equal(opened.dev, st.dev);
    assert.equal(opened.ino, st.ino);
    assert.equal(digest(await fd.readFile()), expected, `previous command pin drift: ${file}`);
  } finally { await fd.close(); }
}
async function atomic(file, bytes, mode = 0o600) {
  await ownedDirectory(path.dirname(file));
  const temp = `${file}.workflow-${process.pid}`;
  const fd = await open(temp, "wx", mode);
  try { await fd.writeFile(bytes); await fd.sync(); }
  finally { await fd.close(); }
  try { await rename(temp, file); }
  catch (error) { await unlink(temp); throw error; }
}
async function json(file, value) { await atomic(file, Buffer.from(JSON.stringify(value, null, 2) + "\n")); }
async function servicePid() {
  const { stdout } = await run("/bin/launchctl", ["print", SERVICE], { timeout: 10000 });
  const pid = Number(/\bpid = (\d+)/.exec(stdout)?.[1]);
  assert.ok(pid > 0 && /state = running/.test(stdout), "Expected running switchboard service");
  return pid;
}
async function serviceActivity() {
  const runtime = await read(RUNTIME_PATH);
  // GET /health is not itself counted as an active request.
  const response = await fetch(`http://${runtime.host}:${runtime.port}/health`, { signal: AbortSignal.timeout(2000) });
  if (!response.ok) throw new Error(`health ${response.status}`);
  return (await response.json()).activity;
}
// A restart reaches every Codex turn on this port: SIGTERM closes all connections, and the SIGUSR2
// drain closes the listener for as long as the longest turn runs. Coordinator turns are mostly tool
// time between model calls, so restart in a moment with no POST or WebSocket turn in flight; after
// the bound, refuse. An unreadable health endpoint proves nothing about in-flight turns, so it
// refuses too: only a measured zero HTTP/WS moment admits a reload.
export async function waitForIdleService({ fetchActivity = serviceActivity, timeoutMs = 120000, pollMs = 200,
  now = Date.now, sleep = ms => new Promise(resolve => setTimeout(resolve, ms)) } = {}) {
  const started = now();
  for (;;) {
    let activity;
    try { activity = await fetchActivity(); }
    catch { return { idle: false, health: "unknown", waited_ms: now() - started }; }
    if (activity?.active_http_requests === 0 && activity?.active_ws_turns === 0) {
      return { idle: true, health: "idle", waited_ms: now() - started };
    }
    if (now() - started >= timeoutMs) return { idle: false, health: "busy", waited_ms: now() - started, activity };
    await sleep(pollMs);
  }
}
export function reloadRefused(window) {
  return Object.assign(new Error(`Switchboard reload refused: no measured idle moment (${window?.health ?? "unknown"})`),
    { code: "RELOAD_REFUSED_NOT_IDLE", window });
}
// A supervised switchboard (cluster primary owning the listener) hot-reloads on SIGHUP: the
// predecessor worker drains its own turns, so no idle moment is needed and nothing is cut.
async function supervisedService(pid) {
  try {
    const runtime = await read(RUNTIME_PATH);
    const response = await fetch(`http://127.0.0.1:${runtime.port}/${runtime.installation_capability}/ready`,
      { signal: AbortSignal.timeout(2000) });
    const ready = await response.json();
    if (!Number.isInteger(ready?.worker_pid) || ready.supervisor_pid !== pid) return null;
    return { supervisor_pid: pid, worker_pid: ready.worker_pid, adapter_source_sha256: ready.adapter_source_sha256 ?? null };
  } catch { return null; }
}
export async function restartRegisteredService(command = run, wait = waitForIdleService, hot = null,
  kill = (pid, signal) => process.kill(pid, signal)) {
  // Keep launchd registration and KeepAlive even if reload fails. Removing the
  // registration first can leave every client disconnected after a later error.
  await command("/bin/launchctl", ["print", SERVICE], { timeout: 10000 });
  if (hot) {
    kill(hot.supervisor_pid, "SIGHUP");
    return { mode: "hot_reload", supervisor_pid: hot.supervisor_pid, prior_worker_pid: hot.worker_pid };
  }
  const window = await wait();
  if (window?.idle !== true) throw reloadRefused(window);
  await command("/bin/launchctl", ["kickstart", "-k", SERVICE], { timeout: 30000 });
  return window;
}
export async function nativeHostInterpreter(python) {
  // The adapter uses stdlib only; do not retain a dependency on a disposable caller venv.
  const resolved = await realpath(python);
  const binding = { python: resolved, python_sha256: digest(await readFile(resolved)) };
  verifyNativeInterpreter(binding);
  await run(resolved, ["-I", "-c", "import hashlib, tomllib; assert callable(hashlib.file_digest)"],
    { timeout: 10000, maxBuffer: 1024 });
  return binding;
}
async function boundManifest(file) {
  const bytes = await fileBytes(file);
  const m = JSON.parse(bytes.toString("utf8"));
  manifestHashes.set(m, digest(bytes));
  assert.equal(m.schema_version, "switchboard_workflow_update_v1");
  assert.ok(m.update_scope === undefined || sourceOnly(m), "Unknown update scope");
  assert.equal(m.target, TARGET);
  assert.ok(typeof m.native_thread_id === "string" && m.native_thread_id.length > 0);
  assert.ok(typeof m.action_id === "string" && m.action_id.length > 0);
  assert.equal(m.native_thread_id, process.env.CODEX_THREAD_ID);
  assert.equal(m.action_id, process.env.NOKIY_DEPLOYMENT_ACTION_ID);
  assert.match(process.env.NOKIY_DEPLOYMENT_PLAN_SHA256 || "", /^[0-9a-f]{64}$/);
  assert.ok(m.expires_at > Date.now() / 1000, "Deployment authorization expired");
  assert.equal(await realpath(m.candidate), m.candidate);
  assert.equal(await realpath(m.artifact_root), m.artifact_root);
  assert.match(m.authorization_ref.sha256, SHA256);
  const authBytes = await fileBytes(m.authorization_ref.path);
  assert.equal(digest(authBytes), m.authorization_ref.sha256);
  const auth = JSON.parse(authBytes.toString("utf8"));
  assert.equal(auth.native_thread_id, m.native_thread_id);
  assert.equal(auth.target, TARGET);
  assert.equal(auth.action_id, m.action_id);
  assert.equal(auth.operator_request, "直接部署");
  assert.equal(auth.restart_existing_switchboard, true);
  assert.equal(auth.modify_signed_app, false);
  if (sourceOnly(m)) {
    assert.equal(auth.schema_version, "operator_deployment_authorization_record_v1");
    assert.equal(auth.new_service, false);
    assert.equal(auth.source_update?.schema_version, SOURCE_SCOPE, "Exact source file authorization required");
    exactSourceFiles(m.files);
    exactSourceFiles(auth.source_update.files);
    const ordered = files => [...files].sort((a, b) => a.target.localeCompare(b.target));
    assert.equal(canonical(ordered(auth.source_update.files)), canonical(ordered(m.files)), "Unbound source files");
    for (const dir of [TARGET, m.candidate, m.artifact_root]) await ownedDirectory(dir);
    for (const dir of [m.candidate, m.artifact_root]) {
      assert.ok(!within(TARGET, dir) && !within(dir, TARGET), "Candidate/artifacts overlap installed tree");
    }
    assert.equal(file, path.join(m.artifact_root, "manifest.json"));
    assert.equal(m.authorization_ref.path, path.join(m.artifact_root, "authorization.json"));
    assert.ok(typeof m.release === "string" && m.release.length > 0);
    assert.match(m.catalog_preimage, SHA256);
    assert.match(m.admission_preimage, SHA256);
    for (const protectedFile of [WORKFLOW_PATH, PICKER_CATALOG_PATH, m.native]) {
      assert.match(m.invariants?.[protectedFile], SHA256, `Missing protected invariant: ${protectedFile}`);
    }
    assert.match(expectedServer(m), SHA256, "Bind the installed server hash when not updating it");
    for (const f of m.files) {
      assert.equal(f.source, path.join(m.candidate, f.target), "Source must match exact candidate-relative target");
      assert.ok(!Object.hasOwn(m.invariants, path.join(TARGET, f.target)), "Invariant overlaps update target");
    }
  } else {
    assert.equal(m.files.length, ALLOWED.size);
    assert.deepEqual(new Set(m.files.map(f => f.target)), ALLOWED);
  }
  for (const f of m.files) {
    assert.equal(await realpath(f.source), f.source);
    assert.ok(f.source.startsWith(m.candidate + "/"));
    assert.equal(await identity(f.source), f.sha256, `Candidate changed: ${f.target}`);
  }
  await checkInvariants(m);
  return m;
}
const within = (root, file) => file === root || file.startsWith(root + "/");
function exactSourceFiles(files) {
  assert.ok(Array.isArray(files) && files.length > 0, "Nonempty exact source file scope required");
  const targets = new Set();
  for (const f of files) {
    assert.deepEqual(Object.keys(f).sort(), ["preimage", "sha256", "source", "target"]);
    assert.match(f.target, /^(?:src\/(?:[A-Za-z0-9_-][A-Za-z0-9_.-]*\/)*[A-Za-z0-9_-][A-Za-z0-9_.-]*\.mjs|src\/nokiy-native-host\.py|tests\/(?:[A-Za-z0-9_-][A-Za-z0-9_.-]*\/)*[A-Za-z0-9_-][A-Za-z0-9_.-]*\.test\.mjs)$/);
    assert.ok(!targets.has(f.target), `Duplicate source target: ${f.target}`);
    targets.add(f.target);
    assert.equal(typeof f.source, "string");
    assert.match(f.sha256, SHA256);
    if (f.preimage !== null) assert.match(f.preimage, SHA256);
  }
}
const expectedServer = m => m.files.find(f => f.target === "src/server.mjs")?.sha256
  ?? m.invariants?.[`${TARGET}/src/server.mjs`];
const requestedHost = m => sourceOnly(m) ? m.files.find(f => f.target === "src/nokiy-native-host.py") : undefined;
async function checkInvariants(m) {
  for (const [file, hash] of Object.entries(m.invariants)) {
    assert.match(hash, SHA256);
    assert.equal(await identity(file), hash, `Protected deployment invariant changed: ${file}`);
  }
}
// Verified dependency closure of the existing activation/publisher executor.
const EXECUTOR_FILES = ["package-lock.json", "package.json", "src/activation.mjs", "src/catalog.mjs",
  "src/claude-bridge-route.mjs", "src/config-state.mjs", "src/external-admission.mjs",
  "src/nokiy-coordinator-policy.mjs", "src/nokiy-direct-route.mjs", "src/nokiy-native-contract.json",
  "src/nokiy-native-observation.mjs", "src/nokiy-native-tool.mjs", "src/nokiy-result-delivery.mjs",
  "src/nokiy-workflow-profiles.mjs", "src/picker-catalog.mjs", "src/route-ownership.mjs",
  "src/shared.mjs", "src/update-lock.mjs", "src/workflow-update.mjs"];
const NATIVE_COMMAND_FILE_LIMIT = 64;
// Data-only request for activation.mjs --workflow-update prepare <request.json>.
// Supply identity {uid, HOME, CODEX_HOME}, native_thread_id, action_id, release,
// expires_at, candidate, exact files, workspace, output = workspace/action_id,
// authorization_ref, test_evidence_ref and previous_packet refs for plan,
// manifest, apply_state and verification. Refs bind existing bytes; preparation
// never issues authorization, executes tests, takes a deployment lock or runs a phase.
// CODEX_HOME is null/omitted when unset; resolve HOME/.codex without changing env.
// Optional executor_bindings_ref {path, sha256} pins existing JSON with status
// EXACT_SAME_BYTE_EXECUTOR_VERIFIED, installed_root, executor_root, bindings
// [{relative_path, installed, executor, sha256}] for all EXECUTOR_FILES, and
// node_modules_same_physical_directory (canonical shared realpath). Required for
// execution-pin target overlap or an immutable predecessor. Reuse unchanged
// bindings after self-update, or rotate to current installed bytes; the completed
// packet and predecessor closure justify installed identities independently.
async function preparePacket(file) {
  let r;
  try { r = await read(file); }
  catch (error) { throw new Error(`Invalid prepare request: ${error.message}`); }
  assert.ok(r && typeof r === "object" && !Array.isArray(r), "Invalid prepare request object");
  const text = (value, field) => assert.ok(typeof value === "string" && value.length > 0, `Missing/invalid ${field}`);
  const absolute = (value, field) => { text(value, field); assert.equal(path.resolve(value), value, `Invalid ${field}: canonical absolute path required`); };
  const future = (value, field) => assert.ok(Number.isFinite(value) && value > Date.now() / 1000, `Missing/invalid or expired ${field}`);
  const scope = (files, field) => {
    try { exactSourceFiles(files); }
    catch (error) { assert.fail(`Invalid ${field}: ${error.message}`); }
  };
  const refs = new Map();
  async function reference(ref, field, pinForExecution = true) {
    assert.ok(ref && typeof ref === "object", `Missing/invalid ${field}`);
    absolute(ref.path, `${field}.path`);
    assert.match(ref.sha256 || "", SHA256, `Missing/invalid ${field}.sha256`);
    let bytes, value;
    try { bytes = await fileBytes(ref.path); value = JSON.parse(bytes.toString("utf8")); }
    catch (error) { throw new Error(`Invalid ${field}: ${error.message}`); }
    assert.equal(digest(bytes), ref.sha256, `${field} hash drift`);
    assert.ok(value && typeof value === "object", `Invalid ${field} JSON object`);
    if (pinForExecution) refs.set(ref.path, ref.sha256);
    return { bytes, value };
  }
  assert.equal(r.schema_version, "switchboard_workflow_update_prepare_v1", "Invalid schema_version");
  assert.equal(r.identity?.uid, process.getuid(), "Invalid identity.uid");
  assert.equal(r.identity?.HOME, HOME, "Invalid identity.HOME");
  assert.equal(process.env.HOME, r.identity.HOME, "identity.HOME environment drift");
  const codexHomeIdentity = r.identity.CODEX_HOME ?? null;
  assert.equal(process.env.CODEX_HOME ?? null, codexHomeIdentity, "identity.CODEX_HOME environment drift");
  const codexHome = codexHomeIdentity ?? path.join(HOME, ".codex");
  absolute(codexHome, "identity.CODEX_HOME");
  await ownedDirectory(codexHome);
  for (const field of ["native_thread_id", "action_id", "release"]) text(r[field], field);
  assert.equal(r.native_thread_id, process.env.CODEX_THREAD_ID, "Invalid native_thread_id");
  assert.match(r.action_id, /^[A-Za-z0-9][A-Za-z0-9_.-]*$/, "Invalid action_id");
  future(r.expires_at, "expires_at");
  for (const field of ["candidate", "workspace", "output"]) absolute(r[field], field);
  assert.equal(r.output, path.join(r.workspace, r.action_id), "Invalid output: expected workspace/action_id");
  for (const dir of [TARGET, r.candidate, r.workspace]) await ownedDirectory(dir);
  assert.ok(!within(TARGET, r.candidate) && !within(r.candidate, TARGET), "Invalid candidate: installed tree overlap");
  for (const protectedDir of [TARGET, r.candidate, codexHome, path.dirname(CATALOG_PATH)]) {
    assert.ok(!within(protectedDir, r.workspace) && !within(r.workspace, protectedDir), "Invalid workspace: protected/candidate overlap");
  }
  try { await lstat(r.output); assert.fail("output already exists; do not replay action_id"); }
  catch (error) { if (error.code !== "ENOENT") throw error; }
  scope(r.files, "files");
  const prior = r.previous_packet;
  assert.ok(prior && typeof prior === "object", "Missing/invalid previous_packet");
  // Hash-verify completed preparation evidence without making it an input to
  // the new execution. The current manifest carries its validated bindings.
  const { value: p } = await reference(prior.plan_ref, "previous_packet.plan_ref", false);
  const { value: m } = await reference(prior.manifest_ref, "previous_packet.manifest_ref", false);
  const { value: state } = await reference(prior.apply_state_ref, "previous_packet.apply_state_ref", false);
  const { value: verified } = await reference(prior.verification_ref, "previous_packet.verification_ref", false);
  assert.equal(p.schema_version, "nokiy_deployment_plan_v1", "Invalid previous plan schema_version");
  assert.equal(m.schema_version, "switchboard_workflow_update_v1", "Invalid previous manifest schema_version");
  assert.equal(m.update_scope, SOURCE_SCOPE, "Invalid previous manifest update_scope");
  scope(m.files, "previous manifest files");
  for (const key of ["action_id", "native_thread_id", "target", "release", "expires_at"]) {
    assert.equal(p[key], m[key], `previous packet ${key} mismatch`);
  }
  assert.equal(m.target, TARGET, "Invalid previous target");
  assert.equal(m.native_thread_id, r.native_thread_id, "previous packet native_thread_id mismatch");
  assert.notEqual(m.action_id, r.action_id, "action_id already used by previous packet");
  // Completed receipts bind historical evidence; only the new authorization grants time.
  assert.ok(Number.isFinite(m.expires_at) && m.expires_at > 0, "Invalid previous packet expires_at");
  absolute(m.candidate, "previous candidate");
  absolute(m.artifact_root, "previous artifact_root");
  assert.equal(p.workspace, m.artifact_root, "previous workspace mismatch");
  assert.equal(canonical(p.authorization_ref), canonical(m.authorization_ref), "previous authorization_ref mismatch");
  for (const [ref, name] of [[prior.manifest_ref, "manifest"], [prior.apply_state_ref, "apply-state"], [prior.verification_ref, "verification"]]) {
    assert.equal(ref.path, path.join(m.artifact_root, `${name}.json`), `previous ${name} path mismatch`);
  }
  const previousBindings = { action_id: m.action_id, native_thread_id: m.native_thread_id,
    target: TARGET, release: m.release, plan_sha256: digest(Buffer.from(canonical(p))), manifest_sha256: prior.manifest_ref.sha256 };
  for (const record of [state, verified]) for (const [key, value] of Object.entries(previousBindings)) {
    assert.equal(record[key], value, `previous receipt ${key} mismatch`);
  }
  assert.equal(state.state, "APPLIED_PENDING_VERIFY", "Invalid previous apply state");
  assert.deepEqual(state.written, m.files.map(f => f.target), "Invalid previous written scope");
  for (const key of ["admitted", "verified", "healthy"]) assert.equal(verified[key], true, `previous verification.${key} required`);
  assert.equal(verified.observed_release, m.release, "previous observed_release mismatch");
  assert.match(m.catalog_preimage || "", SHA256, "Invalid previous catalog_preimage");
  assert.match(state.admission_sha256 || "", SHA256, "Invalid previous admission_sha256");
  assert.equal(await identity(CATALOG_PATH), m.catalog_preimage, "catalog_preimage drift");
  assert.equal(await identity(ADMISSION_PATH), state.admission_sha256, "admission_preimage drift");
  for (const protectedFile of [WORKFLOW_PATH, PICKER_CATALOG_PATH, m.native]) {
    assert.match(m.invariants?.[protectedFile] || "", SHA256, `Missing previous invariant: ${protectedFile}`);
  }
  assert.match(expectedServer(m) || "", SHA256, "Missing previous server binding");
  assert.equal(state.server_sha256, expectedServer(m), "previous server binding mismatch");
  await checkInvariants(m);
  const invariants = { ...m.invariants };
  for (const f of m.files) {
    const installed = path.join(TARGET, f.target);
    assert.equal(await identity(installed), f.sha256, `previous installed source drift: ${f.target}`);
    invariants[installed] = f.sha256;
  }
  const { bytes: authBytes, value: auth } = await reference(r.authorization_ref, "authorization_ref");
  assert.equal(auth.schema_version, "operator_deployment_authorization_record_v1", "Invalid authorization schema_version");
  for (const [key, value] of Object.entries({ action_id: r.action_id, native_thread_id: r.native_thread_id,
    target: TARGET, operator_request: "直接部署", restart_existing_switchboard: true, modify_signed_app: false, new_service: false })) {
    assert.equal(auth[key], value, `Invalid authorization.${key}`);
  }
  future(auth.expires_at, "authorization.expires_at");
  assert.ok(r.expires_at <= auth.expires_at, "expires_at extends authorization");
  assert.equal(auth.source_update?.schema_version, SOURCE_SCOPE, "Invalid authorization.source_update scope");
  scope(auth.source_update.files, "authorization.source_update.files scope");
  const ordered = files => [...files].sort((a, b) => a.target.localeCompare(b.target));
  assert.equal(canonical(ordered(auth.source_update.files)), canonical(ordered(r.files)), "authorization.source_update.files mismatch");
  for (const f of r.files) {
    assert.equal(f.source, path.join(r.candidate, f.target), `Invalid files.source: ${f.target}`);
    assert.equal(await realpath(f.source), f.source, `Invalid candidate path: ${f.target}`);
    assert.equal(await identity(f.source), f.sha256, `Candidate changed: ${f.target}`);
    const installed = path.join(TARGET, f.target);
    assert.equal(await identity(installed, true), f.preimage, `Preimage drift: ${f.target}`);
    delete invariants[installed];
  }
  const { value: evidence } = await reference(r.test_evidence_ref, "test_evidence_ref");
  assert.equal(evidence.parent_thread_id, r.native_thread_id, "Invalid test evidence parent_thread_id");
  assert.equal(evidence.test_exit_code, 0, "Invalid test evidence test_exit_code");
  assert.equal(evidence.test_command?.cwd, r.candidate, "Invalid test evidence candidate cwd");
  assert.ok(Array.isArray(evidence.test_command?.argv) && evidence.test_command.argv.length > 0
    && evidence.test_command.argv.every(x => typeof x === "string" && x.length), "Invalid test evidence argv");
  const summary = evidence.tap_summary;
  assert.ok(Number.isInteger(summary?.tests) && summary.tests > 0 && summary.pass === summary.tests, "Invalid test evidence tap_summary");
  for (const key of ["fail", "cancelled", "skipped", "todo"]) assert.equal(summary[key], 0, `Invalid test evidence ${key}`);
  const testedHashes = new Set(Object.entries(evidence).filter(([key]) => /^tested_.+_sha256$/.test(key)).map(([, value]) => value));
  for (const f of r.files) assert.ok(testedHashes.has(f.sha256), `Missing test evidence candidate hash: ${f.target}`);
  await reference(evidence.source_binding_ref, "test evidence source_binding_ref");
  absolute(evidence.original_test_log, "test evidence original_test_log");
  assert.match(evidence.test_log_sha256 || "", SHA256, "Invalid test evidence test_log_sha256");
  assert.equal(await identity(evidence.original_test_log), evidence.test_log_sha256, "test evidence log drift");
  refs.set(evidence.original_test_log, evidence.test_log_sha256);
  assert.ok(Array.isArray(evidence.unchanged_test_context), "Missing test evidence unchanged_test_context");
  for (const ref of evidence.unchanged_test_context) {
    assert.ok(ref && typeof ref === "object", "Invalid test context entry");
    absolute(ref.path, "test context path");
    assert.ok(within(r.candidate, ref.path), "Invalid test context candidate scope");
    assert.match(ref.sha256 || "", SHA256, "Invalid test context sha256");
    assert.equal(await identity(ref.path), ref.sha256, `test context drift: ${ref.path}`);
    refs.set(ref.path, ref.sha256);
  }
  const installedEntry = `${TARGET}/src/activation.mjs`, previousEntry = p.commands?.preflight?.argv?.[1];
  absolute(previousEntry, "previous publisher executor");
  const previousRoot = path.dirname(path.dirname(previousEntry));
  assert.equal(previousEntry, path.join(previousRoot, "src/activation.mjs"), "Unsupported previous publisher executor");
  if (previousEntry !== installedEntry) {
    await ownedDirectory(previousRoot);
    for (const dir of [TARGET, r.candidate, r.workspace, codexHome]) {
      assert.ok(!within(dir, previousRoot) && !within(previousRoot, dir), "Unsafe previous executor root overlap");
    }
  }
  let executorEntry = installedEntry;
  const executorMappings = new Map(), executorPins = new Map();
  for (const f of r.files) if (EXECUTOR_FILES.includes(f.target)) {
    assert.ok(r.executor_bindings_ref, `Unsafe execution pin target overlap: ${f.target}; same-byte executor_bindings_ref required`);
  }
  if (r.executor_bindings_ref !== undefined) {
    const { value: executor } = await reference(r.executor_bindings_ref, "executor_bindings_ref");
    assert.equal(executor.status, "EXACT_SAME_BYTE_EXECUTOR_VERIFIED", "Invalid executor bindings status");
    assert.equal(executor.installed_root, TARGET, "Invalid executor installed_root");
    absolute(executor.executor_root, "executor_root");
    await ownedDirectory(executor.executor_root);
    const protectedRoots = [TARGET, r.candidate, r.workspace, codexHome];
    if (previousEntry !== installedEntry && previousRoot !== executor.executor_root) protectedRoots.push(previousRoot);
    for (const dir of protectedRoots) {
      assert.ok(!within(dir, executor.executor_root) && !within(executor.executor_root, dir), "Unsafe executor root overlap");
    }
    executorEntry = path.join(executor.executor_root, "src/activation.mjs");
    assert.ok(Array.isArray(executor.bindings), "Invalid executor bindings");
    assert.equal(executor.bindings.length, EXECUTOR_FILES.length, "Incomplete executor bindings");
    assert.deepEqual(new Set(executor.bindings.map(b => b.relative_path)), new Set(EXECUTOR_FILES), "Invalid executor dependency closure");
    absolute(executor.node_modules_same_physical_directory, "executor node_modules");
    await ownedDirectory(executor.node_modules_same_physical_directory);
    for (const root of new Set([TARGET, executor.executor_root, previousRoot])) {
      assert.equal(await realpath(path.join(root, "node_modules")), executor.node_modules_same_physical_directory, "Executor node_modules identity mismatch");
    }
    for (const b of executor.bindings) {
      assert.deepEqual(Object.keys(b).sort(), ["executor", "installed", "relative_path", "sha256"], "Invalid executor binding fields");
      assert.equal(b.installed, path.join(TARGET, b.relative_path), "Invalid executor installed mapping");
      assert.equal(b.executor, path.join(executor.executor_root, b.relative_path), "Invalid executor path mapping");
      assert.match(b.sha256 || "", SHA256, "Invalid executor binding sha256");
      assert.equal(await identity(b.executor), b.sha256, `Executor digest drift: ${b.relative_path}`);
      // Only reuse may retain pre-update executor bytes. A fresh executor must
      // match the current installation, then be justified by predecessor pins.
      const installedHash = previousEntry === executorEntry
        ? m.files.find(f => f.target === b.relative_path)?.sha256 ?? m.invariants[b.installed] ?? b.sha256 : b.sha256;
      assert.equal(await identity(b.installed), installedHash, `Executor installed binding drift: ${b.relative_path}`);
      executorMappings.set(b.installed, b);
      executorPins.set(b.executor, b.sha256);
    }
  }
  assert.ok(previousEntry === installedEntry || executorPins.size > 0,
    "Unsupported previous publisher executor: executor_bindings_ref required");
  const manifestPath = path.join(r.output, "manifest.json"), authorizationPath = path.join(r.output, "authorization.json");
  const manifest = { schema_version: m.schema_version, update_scope: SOURCE_SCOPE, target: TARGET,
    action_id: r.action_id, native_thread_id: r.native_thread_id, expires_at: r.expires_at, release: r.release,
    candidate: r.candidate, artifact_root: r.output, authorization_ref: { path: authorizationPath, sha256: digest(authBytes) },
    catalog_preimage: m.catalog_preimage, admission_preimage: state.admission_sha256, files: ordered(r.files),
    invariants: sort(invariants), native: m.native };
  const encode = value => Buffer.from(JSON.stringify(value, null, 2) + "\n");
  const manifestBytes = encode(manifest);
  refs.delete(r.authorization_ref.path);
  refs.set(authorizationPath, digest(authBytes)); refs.set(manifestPath, digest(manifestBytes));
  for (const f of r.files) refs.set(f.source, f.sha256);
  for (const [name, sha256] of Object.entries(invariants)) refs.set(name, sha256);
  const commands = {};
  for (const phase of ["preflight", "apply", "verify"]) {
    const c = p.commands?.[phase];
    assert.ok(c && Array.isArray(c.argv), `Missing previous commands.${phase}`);
    assert.deepEqual(c.argv.slice(1), [previousEntry, "--workflow-update", phase, prior.manifest_ref.path], `Invalid previous ${phase} argv`);
    absolute(c.argv[0], `previous ${phase} executable`);
    assert.ok(Number.isInteger(c.timeout_seconds) && c.timeout_seconds > 0, `Invalid previous ${phase} timeout_seconds`);
    assert.ok(Array.isArray(c.files), `Invalid previous ${phase} files`);
    const pins = new Map();
    for (const ref of c.files) {
      absolute(ref.path, `previous ${phase} pin path`);
      assert.match(ref.sha256 || "", SHA256, `Invalid previous ${phase} pin sha256`);
      assert.ok(!pins.has(ref.path), `Duplicate previous pin: ${ref.path}`);
      pins.set(ref.path, ref.sha256);
    }
    assert.equal(pins.get(prior.manifest_ref.path), prior.manifest_ref.sha256, "previous manifest command pin mismatch");
    assert.equal(pins.get(m.authorization_ref.path), m.authorization_ref.sha256, "previous authorization command pin mismatch");
    await checkPinnedExecutable(c.argv[0], pins.get(c.argv[0]));
    const currentPins = new Map([[c.argv[0], pins.get(c.argv[0])]]);
    for (const relative of executorPins.size && previousEntry === installedEntry
      ? ["src/activation.mjs", "src/workflow-update.mjs", "src/update-lock.mjs"] : EXECUTOR_FILES) {
      const name = path.join(previousRoot, relative);
      assert.match(pins.get(name) || "", SHA256, `Missing previous command pin: ${name}`);
      assert.equal(await identity(name), pins.get(name), `previous command pin drift: ${name}`);
      if (!executorPins.size) currentPins.set(name, pins.get(name));
    }
    for (const b of executorMappings.values()) {
      if (previousEntry !== installedEntry) {
        const previousExecutor = path.join(previousRoot, b.relative_path);
        if (previousEntry === executorEntry) {
          assert.equal(pins.get(previousExecutor), b.sha256, `Missing/mismatched previous executor pin: ${previousExecutor}`);
        } else {
          const installedHash = m.files.find(f => f.target === b.relative_path)?.sha256
            ?? m.invariants[b.installed] ?? pins.get(previousExecutor);
          assert.equal(b.sha256, installedHash, `Executor installed binding drift: ${b.relative_path}`);
        }
      } else if (pins.has(b.installed)) {
        assert.equal(pins.get(b.installed), b.sha256, `previous executor binding mismatch: ${b.installed}`);
      }
    }
    // Rebuild the current closure, including every protected invariant and
    // exact source/test input. Historical command evidence is not inherited;
    // a reused predecessor candidate remains pinned by its executor bindings.
    for (const [name, sha256] of [...refs, ...executorPins]) {
      assert.ok(!currentPins.has(name) || currentPins.get(name) === sha256, `Conflicting packet pin: ${name}`);
      currentPins.set(name, sha256);
    }
    for (const f of r.files) assert.ok(!currentPins.has(path.join(TARGET, f.target)), `Unsafe mutable target pin: ${f.target}`);
    assert.ok(currentPins.size <= NATIVE_COMMAND_FILE_LIMIT,
      `commands.${phase}.files count ${currentPins.size} exceeds native admission limit ${NATIVE_COMMAND_FILE_LIMIT}`);
    commands[phase] = { ...c, argv: [c.argv[0], executorEntry, "--workflow-update", phase, manifestPath],
      files: [...currentPins].sort(([a], [b]) => a.localeCompare(b)).map(([name, sha256]) => ({ path: name, sha256 })) };
  }
  const plan = { ...p, action_id: r.action_id, native_thread_id: r.native_thread_id, release: r.release,
    expires_at: r.expires_at, authorization_ref: manifest.authorization_ref, workspace: r.output,
    artifact_root: path.join(r.output, "native-artifacts"), commands };
  const planBytes = encode(plan), planPath = path.join(r.output, "native-plan.json");
  // All validation precedes the first effect. Exclusive creation leaves a failed
  // publication non-replayable; never overwrite or clean up an uncertain packet.
  await mkdir(r.output, { mode: 0o700 });
  await mkdir(plan.artifact_root, { mode: 0o700 });
  for (const [name, bytes] of [[authorizationPath, authBytes], [manifestPath, manifestBytes], [planPath, planBytes]]) {
    const fd = await open(name, "wx", 0o600);
    try { await fd.writeFile(bytes); await fd.sync(); } finally { await fd.close(); }
  }
  return { evidence_only: true, action_id: r.action_id, manifest_ref: { path: manifestPath, sha256: digest(manifestBytes) },
    plan_ref: { path: planPath, sha256: digest(planBytes) }, plan_sha256: digest(Buffer.from(canonical(plan))) };
}
const bindings = m => ({ action_id: m.action_id, target: m.target, release: m.release,
  plan_sha256: process.env.NOKIY_DEPLOYMENT_PLAN_SHA256,
  ...(sourceOnly(m) ? { native_thread_id: m.native_thread_id, manifest_sha256: manifestHashes.get(m) } : {}) });
async function portOwner(pid) {
  const { stdout: owners } = await run("/usr/sbin/lsof", ["-nP", "-iTCP:43130", "-sTCP:LISTEN", "-Fp"], { timeout: 10000 });
  assert.deepEqual([...new Set(owners.match(/^p\d+$/gm))], [`p${pid}`], "Switchboard port ownership mismatch");
}
async function preflight(m) {
  for (const f of m.files) assert.equal(await identity(path.join(TARGET, f.target), true), f.preimage, `Preimage drift: ${f.target}`);
  assert.equal(await identity(CATALOG_PATH), m.catalog_preimage);
  assert.equal(await identity(ADMISSION_PATH), m.admission_preimage);
  const interpreter = sourceOnly(m) ? null : await nativeHostInterpreter(m.python);
  const current = await verifyAdmission();
  assert.equal(current.ok, true, `Official admission failed: ${current.false_predicates}`);
  const pid = await servicePid();
  await portOwner(pid);
  const original = await read(CATALOG_PATH);
  if (sourceOnly(m)) {
    const local = current.admission.local_nokiy_direct;
    assert.equal(local?.admitted, true, "Existing Nokiy workflow admission required");
    assert.deepEqual(await read(WORKFLOW_PATH), local.profiles, "Installed workflow profiles drifted");
    assert.equal(current.admission.catalog_sha256, m.catalog_preimage);
    const host = requestedHost(m);
    if (host) {
      assert.equal(local.host?.script, path.join(TARGET, host.target), "Existing exact native host binding required");
      assert.match(local.host.sha256 || "", SHA256, "Valid existing native host digest required");
      // Admission and source preimages are independent: a prior source-only
      // publication may have installed these bytes without refreshing admission.
      assert.notEqual(host.preimage, null, "Existing native host source required");
    }
    verifyNativeInterpreter(local.host);
    const { stdout } = await run(m.native, ["debug", "models"], { timeout: 30000, maxBuffer: 8 * 1024 * 1024 });
    const loaded = JSON.parse(stdout);
    for (const p of local.profiles.profiles) assert.ok(loaded.models.some(x => x.slug === p.slug && x.visibility === "list"));
    return { pid, current, candidate: original, profiles: local.profiles, interpreter: local.host };
  }
  const profiles = await read(path.join(m.candidate, "nokiy-workflows.json"));
  const candidate = withDirectCatalog(original, profiles);
  assert.equal(candidate.models.filter(x => x.slug === "nokiy").length, 1);
  assert.equal(profiles.profiles.find(x => x.slug === "nokiy").coordinator.model, "gpt-6-astra");
  const preview = path.join(m.artifact_root, "catalog-preview.json");
  await json(preview, candidate);
  const { stdout } = await run(m.native, ["-c", `model_catalog_json=${JSON.stringify(preview)}`, "debug", "models"],
    { timeout: 30000, maxBuffer: 8 * 1024 * 1024 });
  const loaded = JSON.parse(stdout);
  for (const p of profiles.profiles) assert.ok(loaded.models.some(x => x.slug === p.slug && x.visibility === "list"));
  return { pid, current, candidate, profiles, interpreter };
}
async function apply(m) {
  const statePath = path.join(m.artifact_root, "apply-state.json");
  try { await lstat(statePath); throw new Error("Deployment already attempted; read original receipt, do not repeat effects"); }
  catch (error) { if (error.code !== "ENOENT") throw error; }
  // Recheck every mutable identity while holding the same writer lock as the
  // admission CLI. A successful earlier preflight is not mutation clearance.
  const before = await preflight(m);
  const state = { ...bindings(m), state: "APPLY_STARTED", prior_pid: before.pid, written: [] };
  if (sourceOnly(m)) {
    // Persist an exclusive attempt marker before any backup/install effects.
    // Any error from here is uncertain and cannot authorize another apply.
    const fd = await open(statePath, "wx", 0o600);
    try { await fd.writeFile(JSON.stringify(state, null, 2) + "\n"); await fd.sync(); }
    finally { await fd.close(); }
  }
  const backup = path.join(m.artifact_root, "preimage");
  await mkdir(backup, { mode: 0o700 });
  for (const f of m.files.filter(f => f.preimage !== null)) {
    const dest = path.join(backup, f.target);
    await mkdir(path.dirname(dest), { recursive: true, mode: 0o700 });
    await copyFile(path.join(TARGET, f.target), dest);
    assert.equal(await identity(dest), f.preimage);
  }
  await copyFile(CATALOG_PATH, path.join(backup, "catalog.json"));
  await copyFile(ADMISSION_PATH, path.join(backup, "admission.json"));
  if (!sourceOnly(m)) await json(statePath, state);
  // Idle-only reload: refuse before touching installed files unless a zero HTTP/WS moment is measured.
  // A supervised service hot-reloads without cutting turns, so it needs no idle moment.
  const hot = await supervisedService(before.pid);
  const gate = hot ? { idle: true, health: "supervised" } : await waitForIdleService();
  if (gate.idle !== true) {
    state.state = "RELOAD_REFUSED_BEFORE_INSTALL";
    state.restart_window = gate;
    await json(statePath, state);
    throw reloadRefused(gate);
  }
  // Stage while the registered service is available; reload only after both
  // source and catalog are installed, keeping launchd recovery ownership.
  for (const f of m.files) {
    const dest = path.join(TARGET, f.target);
    assert.equal(await identity(dest, true), f.preimage);
    const bytes = await fileBytes(f.source);
    assert.equal(digest(bytes), f.sha256, `Candidate changed before install: ${f.target}`);
    await atomic(dest, bytes);
    state.written.push(f.target);
    await json(statePath, state);
  }
  assert.equal(await identity(CATALOG_PATH), m.catalog_preimage);
  assert.equal(await identity(ADMISSION_PATH), m.admission_preimage);
  verifyNativeInterpreter(before.interpreter);
  const admission = before.current.admission;
  if (sourceOnly(m)) {
    await checkInvariants(m);
    const host = requestedHost(m);
    if (host) assert.equal(await identity(path.join(TARGET, host.target)), host.sha256, "Installed native host postimage drifted");
    // Only an explicit host source entry refreshes its admitted digest. Keep
    // its script, interpreter, contract and controls, and all other admission fields.
    admission.local_nokiy_direct = { ...admission.local_nokiy_direct, release: m.release,
      ...(host ? { host: { ...admission.local_nokiy_direct.host, sha256: host.sha256 } } : {}),
      source_manifest_sha256: digest(Buffer.from(canonical(m.files))) };
    const bytes = Buffer.from(JSON.stringify(admission, null, 2) + "\n");
    state.admission_sha256 = digest(bytes);
    state.server_sha256 = expectedServer(m);
    await json(statePath, state);
    await atomic(ADMISSION_PATH, bytes);
  } else {
    admission.local_nokiy_direct = { ...admission.local_nokiy_direct, admitted: true, release: m.release, profiles: before.profiles,
      host: { ...admission.local_nokiy_direct?.host, ...before.interpreter, script: `${TARGET}/src/nokiy-native-host.py`,
        sha256: m.files.find(f => f.target === "src/nokiy-native-host.py").sha256,
        contract: { path: `${TARGET}/src/nokiy-native-contract.json`,
          sha256: m.files.find(f => f.target === "src/nokiy-native-contract.json").sha256 } },
      source_manifest_sha256: digest(Buffer.from(canonical(m.files))) };
    const catalogBytes = Buffer.from(JSON.stringify(before.candidate, null, 2) + "\n");
    admission.catalog_sha256 = digest(catalogBytes);
    await atomic(CATALOG_PATH, catalogBytes);
    await json(ADMISSION_PATH, admission);
    await publishPickerCatalog();
  }
  state.state = "INSTALLED_PENDING_RELOAD";
  await json(statePath, state);
  try {
    state.restart_window = await restartRegisteredService(run, waitForIdleService, hot);
  } catch (error) {
    if (error.code === "RELOAD_REFUSED_NOT_IDLE") {
      // No restart happened: the running process still serves the preimage, so put the files back.
      for (const f of m.files) {
        const dest = path.join(TARGET, f.target);
        if (f.preimage === null) await unlink(dest).catch((e) => { if (e.code !== "ENOENT") throw e; });
        else await atomic(dest, await fileBytes(path.join(backup, f.target)));
      }
      await atomic(CATALOG_PATH, await fileBytes(path.join(backup, "catalog.json")));
      await atomic(ADMISSION_PATH, await fileBytes(path.join(backup, "admission.json")));
      if (!sourceOnly(m)) await publishPickerCatalog();
      state.state = "RELOAD_REFUSED_PREIMAGE_RESTORED";
      state.restart_window = error.window ?? null;
      await json(statePath, state);
      throw error;
    }
    state.state = "RELOAD_FAILED_SERVICE_REGISTERED";
    state.reload_failure_code = error.code ?? null;
    await json(statePath, state);
    throw error;
  }
  state.state = "APPLIED_PENDING_VERIFY";
  await json(statePath, state);
  return { ...bindings(m), applied: true };
}
async function verify(m) {
  const state = await read(path.join(m.artifact_root, "apply-state.json"));
  if (sourceOnly(m)) {
    for (const [key, value] of Object.entries(bindings(m))) assert.equal(state[key], value, `Apply state binding changed: ${key}`);
    assert.equal(state.state, "APPLIED_PENDING_VERIFY", "Apply outcome uncertain; reconcile, do not replay");
    assert.deepEqual(state.written, m.files.map(f => f.target));
    assert.match(state.admission_sha256, SHA256);
    assert.equal(await identity(ADMISSION_PATH), state.admission_sha256);
    assert.equal(await identity(CATALOG_PATH), m.catalog_preimage);
    assert.equal(state.server_sha256, expectedServer(m));
    assert.equal(await identity(`${TARGET}/src/server.mjs`), expectedServer(m));
  }
  for (const f of m.files) assert.equal(await identity(path.join(TARGET, f.target)), f.sha256);
  const admission = await read(ADMISSION_PATH);
  const host = requestedHost(m);
  if (host) {
    assert.equal(admission.local_nokiy_direct?.host?.script, path.join(TARGET, host.target), "Requested native host component identity changed");
    assert.equal(admission.local_nokiy_direct.host.sha256, host.sha256, "Requested native host source postimage differs from admission");
  }
  const runtime = await read(RUNTIME_PATH);
  const base = `http://127.0.0.1:${runtime.port}/${runtime.installation_capability}`;
  const hot = state.restart_window?.mode === "hot_reload" ? state.restart_window : null;
  let ready;
  // A hot reload is ready once the successor answers: new worker, new source.
  const attempts = hot ? 300 : 30;
  for (let n = 0; n < attempts; n++) {
    try {
      const response = await fetch(base + "/ready", { signal: AbortSignal.timeout(1500) });
      assert.equal(response.status, 200);
      ready = await response.json();
      if (hot) {
        assert.equal(ready.adapter_source_sha256, expectedServer(m));
        assert.notEqual(ready.worker_pid, hot.prior_worker_pid);
      }
      break;
    } catch (error) { if (n === attempts - 1) throw error; await new Promise(r => setTimeout(r, 200)); }
  }
  if (sourceOnly(m)) assert.equal(ready.status, "ADMITTED");
  assert.equal(ready.adapter_source_sha256, expectedServer(m));
  assert.equal(ready.workflow_release, m.release);
  assert.equal(ready.catalog_sha256, await identity(CATALOG_PATH));
  const response = await fetch(base + "/backend-api/codex/v1/models", { signal: AbortSignal.timeout(5000) });
  assert.equal(response.status, 200);
  const served = await response.json();
  const disk = await read(CATALOG_PATH);
  const expectedServed = composePickerCatalog(disk, await loadClaudeBridge());
  assert.deepEqual(served, expectedServed);
  assert.deepEqual(await read(PICKER_CATALOG_PATH), expectedServed);
  for (const p of admission.local_nokiy_direct.profiles.profiles) assert.ok(served.models.some(x => x.slug === p.slug && x.visibility === "list"));
  assert.equal((await verifyAdmission()).ok, true);
  const { stdout } = await run(m.native, ["debug", "models"], { timeout: 30000, maxBuffer: 8 * 1024 * 1024 });
  const loaded = JSON.parse(stdout);
  for (const p of admission.local_nokiy_direct.profiles.profiles) assert.ok(loaded.models.some(x => x.slug === p.slug && x.visibility === "list"));
  const pid = await servicePid();
  if (hot) {
    assert.equal(pid, state.prior_pid);
    assert.equal(ready.supervisor_pid, pid);
  } else {
    assert.notEqual(pid, state.prior_pid);
  }
  if (sourceOnly(m)) await portOwner(pid);
  const receipt = { ...bindings(m), admitted: true, verified: true, healthy: true,
    observed_release: ready.workflow_release, service_pid: pid,
    reload_mode: hot ? "hot_reload" : "restart", worker_pid: ready.worker_pid ?? null,
    picker_aliases: admission.local_nokiy_direct.profiles.profiles.map(p => p.slug),
    native_catalog_loaded: true, served_catalog_matches: true, signed_app_unchanged: true,
    app_restart_performed: false, verified_at: new Date().toISOString() };
  await json(path.join(m.artifact_root, "verification.json"), receipt);
  return receipt;
}
export async function workflowUpdate(phase, file) {
  if (phase === "prepare") return preparePacket(file);
  assert.ok(["preflight", "apply", "verify"].includes(phase), "Unknown deployment phase");
  return withUpdateLock(async () => {
    const m = await boundManifest(file);
    if (phase === "apply") return apply(m);
    if (phase === "verify") return verify(m);
    const result = await preflight(m);
    return { ...bindings(m), admitted: true, previous_pid: result.pid, native_catalog_accepts_profiles: true };
  });
}
