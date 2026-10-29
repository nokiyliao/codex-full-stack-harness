import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { constants } from "node:fs";
import { mkdtemp, readFile, realpath, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

const updaterSource = await readFile(new URL("../src/workflow-update.mjs", import.meta.url), "utf8");
const lockSource = await readFile(new URL("../src/update-lock.mjs", import.meta.url), "utf8");
const TARGET = "/Users/nokiy/.codex-multi/switchboard";
const CATALOG_PATH = "/Users/nokiy/.codex-multi/models/admitted-models.json";
const ADMISSION_PATH = "/Users/nokiy/.codex-multi/models/admission.json";
const PICKER_CATALOG_PATH = "/Users/nokiy/.codex-multi/models/unified-models.json";
const WORKFLOW_PATH = `${TARGET}/config/nokiy-workflows.json`;
const RUNTIME_PATH = `${TARGET}/runtime/installation.json`;
const LOCK = `${TARGET}/runtime/workflow-update.lock`;
const SCOPE = "switchboard_exact_source_update_v1";
const THREAD = "019fd4f0-1079-77e2-8be7-cbf75ca28df5";
const legacyTargets = ["src/server.mjs", "src/catalog.mjs", "src/admission.mjs", "src/activation.mjs",
  "src/nokiy-workflow-profiles.mjs", "src/nokiy-direct-route.mjs", "src/nokiy-native-tool.mjs",
  "src/nokiy-native-host.py", "src/nokiy-native-contract.json", "src/workflow-update.mjs", "src/update-lock.mjs", "config/nokiy-workflows.json"];
const hash = bytes => createHash("sha256").update(bytes).digest("hex");
const encoded = value => JSON.stringify(value, null, 2) + "\n";

// Execute the unchanged module bodies with lexical import substitutions. This
// needs no experimental Node mocking flag, real filesystem writes, subprocess,
// network, production injection API, environment override, or live installation.
function load(source, dependencies, exports) {
  const body = source.replace(/^import [^\n]+;\n/gm, "").replace(/^export /gm, "");
  assert.doesNotMatch(body, /^import |^export /m, "Update the fixture linker for new ESM syntax");
  return new Function(...Object.keys(dependencies), `"use strict";\n${body}\nreturn { ${exports.join(", ")} };`)(...Object.values(dependencies));
}

function fixture(legacy = false) {
  const nodes = new Map();
  const effects = [], reads = [], commands = [];
  const control = { pid: 41, restarts: 0, publish: 0, generation: 0, fetches: 0 };
  let ino = 0;
  const error = code => Object.assign(new Error(code), { code });
  function directory(file) {
    if (nodes.has(file)) return;
    if (file !== "/") directory(path.dirname(file));
    nodes.set(file, { kind: "dir", uid: 501, dev: 1, ino: ++ino });
  }
  function put(file, bytes) {
    directory(path.dirname(file));
    nodes.set(file, { kind: "file", uid: 501, dev: 1, ino: ++ino, bytes: Buffer.from(bytes) });
  }
  const node = file => { const n = nodes.get(file); if (!n) throw error("ENOENT"); return n; };
  const bytes = file => Buffer.from(node(file).bytes);
  const object = file => JSON.parse(bytes(file));
  const stat = n => ({ ...n, isFile: () => n.kind === "file", isDirectory: () => n.kind === "dir",
    isSymbolicLink: () => n.kind === "link" });
  async function realpath(file) {
    let current = "/";
    for (const part of file.split("/").filter(Boolean)) {
      current = path.join(current, part);
      const n = node(current);
      if (n.kind === "link") current = await realpath(n.to);
    }
    return current;
  }
  const fs = {
    lstat: async file => { reads.push(file); return stat(node(file)); },
    realpath,
    readFile: async (file, encoding) => { reads.push(file); return encoding ? bytes(file).toString(encoding) : bytes(file); },
    async open(file, flags) {
      if (flags === "wx") {
        effects.push(["open", file]);
        if (nodes.has(file)) throw error("EEXIST");
        assert.equal(node(path.dirname(file)).kind, "dir");
        put(file, "");
      } else {
        assert.equal(flags, constants.O_RDONLY | constants.O_NOFOLLOW);
        if (node(file).kind === "link") throw error("ELOOP");
        control.beforeOpen?.(file);
      }
      const opened = node(file);
      return {
        stat: async () => stat(opened),
        readFile: async () => { reads.push(file); return Buffer.from(opened.bytes); },
        writeFile: async data => { effects.push(["write", file]); opened.bytes = Buffer.from(data); },
        sync: async () => {}, close: async () => {},
      };
    },
    async mkdir(file, options) {
      effects.push(["mkdir", file]);
      if (nodes.has(file) && !options?.recursive) throw error("EEXIST");
      directory(file);
    },
    async copyFile(from, to) { effects.push(["copy", to]); put(to, bytes(from)); },
    async rename(from, to) {
      effects.push(["rename", to]);
      nodes.set(to, node(from)); nodes.delete(from);
      control.afterRename?.(to);
    },
    async unlink(file) { effects.push(["unlink", file]); node(file); nodes.delete(file); },
  };
  const process = { getuid: () => 501, pid: 1234,
    env: { CODEX_THREAD_ID: THREAD, NOKIY_DEPLOYMENT_ACTION_ID: "fixture-new-action", NOKIY_DEPLOYMENT_PLAN_SHA256: "a".repeat(64) } };
  const profiles = { schema_version: "nokiy_workflow_profiles_v1", profiles: ["nokiy", "nokiy-direct"].map(slug => ({
    slug, coordinator: { model: "gpt-6-astra", reasoning_effort: "ultra" },
    worker: { model: "gpt-6.1-sol", reasoning_effort: "max" },
  })) };
  const catalog = { models: ["gpt-6-astra", "nokiy", "nokiy-direct"].map(slug => ({ slug, visibility: "list" })) };
  for (const dir of [TARGET, `${TARGET}/src`, `${TARGET}/tests`, "/candidate/src", "/candidate/tests", "/deployment"]) directory(dir);
  put(CATALOG_PATH, encoded(catalog)); put(PICKER_CATALOG_PATH, encoded(catalog));
  put(WORKFLOW_PATH, encoded(profiles));
  put("/native/codex", "native binary fixture"); put("/native/python", "python binary fixture");
  for (const file of legacyTargets.filter(f => f.startsWith("src/"))) put(`${TARGET}/${file}`, `old ${file}\n`);
  put(`${TARGET}/src/widget.mjs`, "export const value = 1;\n");
  put(RUNTIME_PATH, encoded({ port: 43130, installation_capability: "synthetic-fixture-only" }));
  put(ADMISSION_PATH, encoded({ status: "ADMITTED", catalog_sha256: hash(bytes(CATALOG_PATH)), policy: { preserved: true },
    local_nokiy_direct: { admitted: true, release: "old-release", profiles, preserved: { nested: "yes" },
      host: { python: "/native/python", python_sha256: hash(bytes("/native/python")), script: `${TARGET}/src/nokiy-native-host.py`,
        sha256: hash(bytes(`${TARGET}/src/nokiy-native-host.py`)),
        coordinator_mode: "dispatch_only_v1", parent_result_delivery: "accept_v1", parent_closeout: "direct_v1",
        batch_reference: "direct_v1", parent_deployment: "direct_v1", future_extension: { preserved: true },
        contract: { path: `${TARGET}/src/nokiy-native-contract.json`, sha256: hash(bytes(`${TARGET}/src/nokiy-native-contract.json`)) } },
      source_manifest_sha256: "b".repeat(64) } }));
  const files = [];
  for (const target of legacy ? legacyTargets : ["src/widget.mjs", "tests/widget.test.mjs"]) {
    const source = target.startsWith("config/") ? "/candidate/nokiy-workflows.json" : `/candidate/${target}`;
    put(source, target.startsWith("config/") ? encoded(profiles) : `// new ${target}\n`);
    files.push({ target, source, sha256: hash(bytes(source)), preimage: nodes.has(`${TARGET}/${target}`) ? hash(bytes(`${TARGET}/${target}`)) : null });
  }
  const auth = { schema_version: "operator_deployment_authorization_record_v1", native_thread_id: THREAD,
    action_id: process.env.NOKIY_DEPLOYMENT_ACTION_ID, target: TARGET, operator_request: "直接部署",
    restart_existing_switchboard: true, modify_signed_app: false, new_service: false };
  const manifest = { schema_version: "switchboard_workflow_update_v1", ...(legacy ? {} : { update_scope: SCOPE }),
    native_thread_id: THREAD, action_id: auth.action_id, target: TARGET, candidate: "/candidate", artifact_root: "/deployment",
    release: "fixture-release", expires_at: Math.floor(Date.now() / 1000) + 3600,
    native: "/native/codex", ...(legacy ? { python: "/native/python" } : {}),
    authorization_ref: { path: "/deployment/authorization.json", sha256: "" }, files,
    catalog_preimage: hash(bytes(CATALOG_PATH)), admission_preimage: hash(bytes(ADMISSION_PATH)),
    invariants: Object.fromEntries((legacy ? ["/native/codex"] : [WORKFLOW_PATH, PICKER_CATALOG_PATH, "/native/codex", `${TARGET}/src/server.mjs`])
      .map(file => [file, hash(bytes(file))])) };
  function seal(scope = true) {
    if (!legacy && scope) auth.source_update = { schema_version: SCOPE, files: structuredClone(manifest.files) };
    put("/deployment/authorization.json", encoded(auth));
    manifest.authorization_ref.sha256 = hash(bytes("/deployment/authorization.json"));
    put("/deployment/manifest.json", encoded(manifest));
  }
  seal();
  const command = async (file, args) => {
    commands.push([file, args]);
    await control.beforeCommand?.(file, args);
    if (file === "/bin/launchctl") {
      if (args[0] === "print") return { stdout: `state = running\npid = ${control.pid}\n` };
      assert.equal(args[0], "kickstart"); assert.equal(args[1], "-k");
      control.restarts++; control.pid++;
      if (control.reloadFailure) throw error("UNCERTAIN_RELOAD");
      return { stdout: "" };
    }
    if (file === "/usr/sbin/lsof") return { stdout: `p${control.owner ?? control.pid}\n` };
    if (file === "/native/python") return { stdout: "" };
    assert.equal(file, "/native/codex");
    const preview = args[0] === "-c" ? JSON.parse(args[1].slice("model_catalog_json=".length)) : CATALOG_PATH;
    return { stdout: JSON.stringify(control.nativeModels ?? object(preview)) };
  };
  const verifyNativeInterpreter = host => {
    assert.equal(host.python, "/native/python");
    assert.equal(host.python_sha256, hash(bytes(host.python)));
  };
  const { withUpdateLock } = load(lockSource, { ...fs, process }, ["withUpdateLock"]);
  const { workflowUpdate, exactSourceFiles } = load(updaterSource, {
    ...fs, assert, createHash, constants, path, process, execFile: command, promisify: fn => fn,
    ADMISSION_PATH, CATALOG_PATH, RUNTIME_PATH, WORKFLOW_PATH, PICKER_CATALOG_PATH, withUpdateLock, verifyNativeInterpreter,
    verifyAdmission: async () => ({ ok: control.admitted ?? true, false_predicates: ["fixture"], admission: object(ADMISSION_PATH) }),
    withDirectCatalog: (original, selected) => { control.generation++; return { ...original,
      models: [...original.models.filter(row => !row.slug.startsWith("nokiy")), ...selected.profiles.map(p => ({ slug: p.slug, visibility: "list" }))] }; },
    composePickerCatalog: disk => disk, loadClaudeBridge: async () => null,
    publishPickerCatalog: async () => { control.publish++; effects.push(["publish", PICKER_CATALOG_PATH]); put(PICKER_CATALOG_PATH, encoded(object(CATALOG_PATH))); },
    fetch: async url => {
      if (String(url).endsWith("/health")) {
        // Idle-only reload reads the canonical health activity; a queue lets a test vary it per poll.
        const next = Array.isArray(control.health) ? control.health.shift() : control.health;
        if (next instanceof Error) throw next;
        return { ok: true, status: 200, json: async () => ({ status: "ok",
          activity: next ?? { active_http_requests: 0, active_ws_turns: 0 } }) };
      }
      control.fetches++;
      const admission = object(ADMISSION_PATH);
      const ready = { status: "ADMITTED", adapter_source_sha256: hash(bytes(`${TARGET}/src/server.mjs`)),
        workflow_release: admission.local_nokiy_direct.release, catalog_sha256: hash(bytes(CATALOG_PATH)), ...control.ready };
      return { status: 200, json: async () => url.endsWith("/ready") ? ready : (control.served ?? object(CATALOG_PATH)) };
    },
    AbortSignal: { timeout: () => undefined }, setTimeout: fn => fn(),
  }, ["workflowUpdate", "exactSourceFiles"]);
  return { nodes, effects, reads, commands, control, process, manifest, auth, seal, put, bytes, object, exactSourceFiles,
    run: phase => workflowUpdate(phase, "/deployment/manifest.json"),
    noEffects() { assert.deepEqual(effects.filter(([, file]) => file !== LOCK), []); assert.equal(control.restarts, 0); },
    link(file, to) { nodes.set(file, { kind: "link", uid: 501, dev: 1, ino: ++ino, to }); },
  };
}

function nativeHostFixture(unchanged = false) {
  const f = fixture(), target = "src/nokiy-native-host.py", source = `/candidate/${target}`;
  f.put(source, unchanged ? f.bytes(`${TARGET}/${target}`) : "# updated native host\n");
  f.manifest.files = [{ target, source, sha256: hash(f.bytes(source)), preimage: hash(f.bytes(`${TARGET}/${target}`)) }];
  f.seal();
  return f;
}

test("exact source scope admits only the named native host in addition to modules", async t => {
  const root = await realpath(await mkdtemp(path.join(os.tmpdir(), "native-source-scope-")));
  t.after(() => rm(root, { recursive: true, force: true }));
  const f = fixture(), file = { target: "src/nokiy-native-host.py", source: path.join(root, "src/nokiy-native-host.py"),
    sha256: hash("verified native host\n"), preimage: hash("installed native host\n") };
  assert.doesNotThrow(() => f.exactSourceFiles([file]));
  for (const target of ["src/host.py", "src/nested/nokiy-native-host.py", "src/nokiy-native-host.pyc",
    "tests/nokiy-native-host.py", "src/nokiy-native-contract.json", "config/nokiy-workflows.json",
    "src/../src/nokiy-native-host.py"]) {
    assert.throws(() => f.exactSourceFiles([{ ...file, target, source: path.join(root, target) }]), undefined, target);
  }
  f.noEffects();
});

test("a busy service refuses the reload before any installed file changes", async () => {
  const f = fixture();
  const before = f.manifest.files.map(file => [file.target, f.nodes.has(`${TARGET}/${file.target}`) ? f.bytes(`${TARGET}/${file.target}`) : null]);
  f.control.health = new Error("health unreadable");
  await assert.rejects(f.run("apply"), error => error.code === "RELOAD_REFUSED_NOT_IDLE");
  for (const [target, bytes] of before) {
    if (bytes === null) assert.equal(f.nodes.has(`${TARGET}/${target}`), false);
    else assert.deepEqual(f.bytes(`${TARGET}/${target}`), bytes);
  }
  assert.equal(f.control.restarts, 0);
  assert.equal(f.object("/deployment/apply-state.json").state, "RELOAD_REFUSED_BEFORE_INSTALL");
  await assert.rejects(f.run("apply"), /already attempted/);
});

test("a reload refused after install restores the preimage and never kickstarts", async () => {
  const f = fixture();
  const before = f.manifest.files.map(file => [file.target, f.nodes.has(`${TARGET}/${file.target}`) ? f.bytes(`${TARGET}/${file.target}`) : null]);
  const admission = f.bytes(ADMISSION_PATH), catalog = f.bytes(CATALOG_PATH);
  f.control.health = [{ active_http_requests: 0, active_ws_turns: 0 }, new Error("health unreadable")];
  await assert.rejects(f.run("apply"), error => error.code === "RELOAD_REFUSED_NOT_IDLE");
  for (const [target, bytes] of before) {
    if (bytes === null) assert.equal(f.nodes.has(`${TARGET}/${target}`), false);
    else assert.deepEqual(f.bytes(`${TARGET}/${target}`), bytes);
  }
  assert.deepEqual(f.bytes(ADMISSION_PATH), admission);
  assert.deepEqual(f.bytes(CATALOG_PATH), catalog);
  assert.equal(f.control.restarts, 0);
  assert.equal(f.commands.some(([file, args]) => file === "/bin/launchctl" && args[0] === "kickstart"), false);
  assert.equal(f.object("/deployment/apply-state.json").state, "RELOAD_REFUSED_PREIMAGE_RESTORED");
});

test("exact scoped source install/verify uses shared lock, CAS, restart and real readiness fields without regenerating config", async () => {
  const f = fixture();
  const protectedBytes = [WORKFLOW_PATH, CATALOG_PATH, PICKER_CATALOG_PATH, RUNTIME_PATH].map(file => [file, f.bytes(file)]);
  const prior = f.object(ADMISSION_PATH);
  const preflight = await f.run("preflight");
  assert.equal(preflight.native_catalog_accepts_profiles, true); f.noEffects();
  assert.equal(f.reads.includes("/candidate/nokiy-workflows.json"), false);
  assert.equal((await f.run("apply")).applied, true);
  for (const file of f.manifest.files) assert.equal(hash(f.bytes(`${TARGET}/${file.target}`)), file.sha256);
  for (const [file, bytes] of protectedBytes) assert.deepEqual(f.bytes(file), bytes);
  const after = f.object(ADMISSION_PATH);
  prior.local_nokiy_direct.release = f.manifest.release;
  prior.local_nokiy_direct.source_manifest_sha256 = after.local_nokiy_direct.source_manifest_sha256;
  assert.deepEqual(after, prior);
  const receipt = await f.run("verify");
  assert.equal(receipt.verified, true); assert.equal(receipt.observed_release, f.manifest.release);
  assert.equal(receipt.manifest_sha256, hash(f.bytes("/deployment/manifest.json")));
  assert.equal(receipt.native_thread_id, THREAD);
  assert.equal(receipt.native_catalog_loaded, true); assert.equal(receipt.served_catalog_matches, true);
  assert.equal(f.control.restarts, 1); assert.equal(f.control.publish, 0); assert.equal(f.control.generation, 0);
  assert.equal(f.commands.some(([file]) => file === "/native/python"), false);
  assert.equal(f.commands.some(([, args]) => args[0] === "-c"), false);
  assert.equal(f.nodes.has(LOCK), false);
  await f.run("verify");
  await assert.rejects(f.run("apply"), /already attempted/);
  assert.equal(f.control.restarts, 1);
});

test("authorized exact host publication refreshes only its admitted digest and source bookkeeping", async () => {
  const f = nativeHostFixture(), prior = f.object(ADMISSION_PATH);
  const protectedBytes = [WORKFLOW_PATH, CATALOG_PATH, PICKER_CATALOG_PATH, RUNTIME_PATH].map(file => [file, f.bytes(file)]);
  await f.run("preflight"); f.noEffects();
  await f.run("apply");
  const after = f.object(ADMISSION_PATH);
  assert.deepEqual(after, { ...prior, local_nokiy_direct: { ...prior.local_nokiy_direct,
    release: f.manifest.release, source_manifest_sha256: after.local_nokiy_direct.source_manifest_sha256,
    host: { ...prior.local_nokiy_direct.host, sha256: f.manifest.files[0].sha256 } } });
  assert.equal(hash(f.bytes(`${TARGET}/src/nokiy-native-host.py`)), after.local_nokiy_direct.host.sha256);
  for (const [file, bytes] of protectedBytes) assert.deepEqual(f.bytes(file), bytes);
  assert.equal((await f.run("verify")).verified, true);
  assert.equal(f.control.restarts, 1); assert.equal(f.control.publish, 0); assert.equal(f.control.generation, 0);
});

test("byte-unchanged exact host publication repairs stale admission under independent preimages", async () => {
  const f = nativeHostFixture(true), prior = f.object(ADMISSION_PATH);
  prior.local_nokiy_direct.host.sha256 = hash("# older admitted host\n");
  f.put(ADMISSION_PATH, encoded(prior));
  f.manifest.admission_preimage = hash(f.bytes(ADMISSION_PATH)); f.seal();
  const source = f.manifest.files[0], installed = f.bytes(`${TARGET}/${source.target}`);
  assert.equal(source.preimage, source.sha256);
  assert.notEqual(prior.local_nokiy_direct.host.sha256, source.preimage);
  await f.run("apply");
  assert.deepEqual(f.bytes(`${TARGET}/${source.target}`), installed);
  assert.deepEqual(f.object(ADMISSION_PATH).local_nokiy_direct.host, { ...prior.local_nokiy_direct.host, sha256: source.sha256 });
  assert.equal((await f.run("verify")).verified, true);
});

for (const [name, change] of [
  ["different script", (f, admission) => { admission.local_nokiy_direct.host.script = "/other/nokiy-native-host.py"; }],
  ["missing script", (f, admission) => { delete admission.local_nokiy_direct.host.script; }],
  ["invalid digest", (f, admission) => { admission.local_nokiy_direct.host.sha256 = "invalid"; }],
  ["missing source", f => { f.nodes.delete(`${TARGET}/src/nokiy-native-host.py`); f.manifest.files[0].preimage = null; }],
]) test(`exact host publication requires an existing component binding before effects: ${name}`, async () => {
  const f = nativeHostFixture(), admission = f.object(ADMISSION_PATH);
  change(f, admission); f.put(ADMISSION_PATH, encoded(admission));
  f.manifest.admission_preimage = hash(f.bytes(ADMISSION_PATH)); f.seal();
  await assert.rejects(f.run("apply")); f.noEffects();
  assert.equal(f.nodes.has("/deployment/apply-state.json"), false);
});

for (const file of [ADMISSION_PATH, `${TARGET}/src/nokiy-native-host.py`]) {
  test(`exact host publication rejects preimage drift after earlier preflight before effects: ${file}`, async () => {
    const f = nativeHostFixture(true);
    await f.run("preflight"); f.put(file, f.bytes(file).toString() + "\n");
    await assert.rejects(f.run("apply")); f.noEffects();
    assert.equal(f.nodes.has("/deployment/apply-state.json"), false);
  });
}

for (const field of ["sha256", "script"]) test(`verify rejects a pinned but wrong requested host binding: ${field}`, async () => {
  const f = nativeHostFixture(), prior = f.object(ADMISSION_PATH).local_nokiy_direct.host;
  await f.run("apply");
  // Model the old publisher's internally consistent receipt, not external drift.
  const admission = f.object(ADMISSION_PATH), state = f.object("/deployment/apply-state.json");
  admission.local_nokiy_direct.host[field] = field === "sha256" ? prior.sha256 : "/other/nokiy-native-host.py";
  f.put(ADMISSION_PATH, encoded(admission)); state.admission_sha256 = hash(f.bytes(ADMISSION_PATH));
  f.put("/deployment/apply-state.json", encoded(state));
  const count = f.effects.length;
  await assert.rejects(f.run("verify"), field === "sha256" ? /Requested native host source postimage/ : /Requested native host component identity/);
  assert.equal(f.nodes.has("/deployment/verification.json"), false);
  assert.equal(f.effects.slice(count).some(([, file]) => file !== LOCK), false);
  assert.equal(f.control.restarts, 1);
});

test("explicit server source updates verify the new server hash, not an absent fixed-set entry", async () => {
  const f = fixture();
  f.put("/candidate/src/server.mjs", "// updated server\n");
  f.manifest.files.push({ target: "src/server.mjs", source: "/candidate/src/server.mjs", sha256: hash(f.bytes("/candidate/src/server.mjs")),
    preimage: hash(f.bytes(`${TARGET}/src/server.mjs`)) });
  delete f.manifest.invariants[`${TARGET}/src/server.mjs`]; f.seal();
  await f.run("apply"); assert.equal((await f.run("verify")).verified, true);
});

test("exact nested/dotted module names need no recurring filename allowlist", async () => {
  const f = fixture();
  const target = "src/components.v1/helper.v2.mjs", source = `/candidate/${target}`;
  f.put(`${TARGET}/${target}`, "// prior helper\n"); f.put(source, "// new helper\n");
  f.manifest.files = [{ target, source, sha256: hash(f.bytes(source)), preimage: hash(f.bytes(`${TARGET}/${target}`)) }];
  f.seal(); await f.run("apply"); assert.equal((await f.run("verify")).verified, true);
});

test("legacy twelve-target workflow/catalog behavior remains on its existing generation/publication path", async () => {
  const f = fixture(true);
  await f.run("preflight"); await f.run("apply");
  assert.equal((await f.run("verify")).verified, true);
  assert.equal(f.control.generation, 2); assert.equal(f.control.publish, 1); assert.equal(f.control.restarts, 1);
  assert.ok(f.reads.includes("/candidate/nokiy-workflows.json"));
  const local = f.object(ADMISSION_PATH).local_nokiy_direct;
  assert.equal(local.host.sha256, f.manifest.files.find(file => file.target === "src/nokiy-native-host.py").sha256);
  assert.equal(local.host.contract.sha256, f.manifest.files.find(file => file.target === "src/nokiy-native-contract.json").sha256);
});

test("normal workflow updates preserve native controls and extensions while rebinding verified executable identities", async () => {
  const f = fixture(true), prior = f.object(ADMISSION_PATH);
  const controls = { coordinator_mode: "dispatch_only_v1", parent_result_delivery: "accept_v1",
    parent_deployment: "direct_v1", parent_closeout: "direct_v1", batch_reference: "direct_v1" };
  Object.assign(prior.local_nokiy_direct.host, controls, { future_extension: { preserved: true },
    python: "/old/python", python_sha256: "c".repeat(64), script: "/old/immutable-host.py", sha256: "d".repeat(64),
    contract: { path: "/old/contract.json", sha256: "e".repeat(64) } });
  prior.local_nokiy_direct.future_extension = { preserved: "parent-owned" };
  f.put(ADMISSION_PATH, encoded(prior));
  f.manifest.admission_preimage = hash(f.bytes(ADMISSION_PATH));
  f.seal();
  await f.run("apply");
  const after = f.object(ADMISSION_PATH);
  assert.deepEqual(after.local_nokiy_direct, { ...prior.local_nokiy_direct,
    admitted: true, release: f.manifest.release, profiles: f.object("/candidate/nokiy-workflows.json"),
    source_manifest_sha256: after.local_nokiy_direct.source_manifest_sha256,
    host: { ...prior.local_nokiy_direct.host, python: "/native/python", python_sha256: hash(f.bytes("/native/python")),
      script: `${TARGET}/src/nokiy-native-host.py`,
      sha256: f.manifest.files.find(file => file.target === "src/nokiy-native-host.py").sha256,
      contract: { path: `${TARGET}/src/nokiy-native-contract.json`,
        sha256: f.manifest.files.find(file => file.target === "src/nokiy-native-contract.json").sha256 } },
  });
  assert.deepEqual(after.policy, prior.policy);
  assert.equal((await f.run("verify")).verified, true);
  assert.equal(f.control.restarts, 1);
  assert.equal(f.control.publish, 1);
});

const deniedContracts = [
  ["missing authorization file scope", f => { delete f.auth.source_update; f.seal(false); }],
  ["historical authorization under a new action", f => { f.auth.action_id = "historical-action"; f.seal(false); }],
  ["authorization does not cover extra manifest file", f => { f.auth.source_update.files.pop(); f.seal(false); }],
  ["authorization has unexpected file", f => { f.auth.source_update.files.push({ ...f.auth.source_update.files[0], target: "src/extra.mjs" }); f.seal(false); }],
  ["empty exact scope", f => { f.manifest.files = []; f.seal(); }],
  ["duplicate manifest targets", f => { f.manifest.files.push({ ...f.manifest.files[0] }); f.seal(); }],
  ["duplicate authorization targets", f => { f.auth.source_update.files.push({ ...f.auth.source_update.files[0] }); f.seal(false); }],
  ["unbound source hash", f => { f.auth.source_update.files[0].sha256 = "f".repeat(64); f.seal(false); }],
  ["unbound preimage", f => { f.auth.source_update.files[0].preimage = null; f.seal(false); }],
  ["unexpected file property", f => { f.manifest.files[0].recursive = true; f.seal(); }],
  ["missing preimage", f => { delete f.manifest.files[0].preimage; f.seal(); }],
  ["missing opt in", f => { delete f.manifest.update_scope; f.seal(); }],
  ["unknown opt in", f => { f.manifest.update_scope = "all_sources"; f.seal(); }],
  ["wrong thread", f => { f.manifest.native_thread_id = "other-thread"; f.seal(); }],
  ["wrong authorization thread", f => { f.auth.native_thread_id = "other-thread"; f.seal(); }],
  ["wrong action", f => { f.manifest.action_id = "other-action"; f.seal(); }],
  ["absent plan binding", f => { delete f.process.env.NOKIY_DEPLOYMENT_PLAN_SHA256; }],
  ["wrong target", f => { f.manifest.target = "/other-installation"; f.seal(); }],
  ["expired authorization", f => { f.manifest.expires_at = 1; f.seal(); }],
  ["authorization hash drift", f => { f.put("/deployment/authorization.json", "{}\n"); }],
  ["restart not authorized", f => { f.auth.restart_existing_switchboard = false; f.seal(); }],
  ["operator deployment request absent", f => { delete f.auth.operator_request; f.seal(); }],
  ["signed app change requested", f => { f.auth.modify_signed_app = true; f.seal(); }],
  ["new service requested", f => { f.auth.new_service = true; f.seal(); }],
  ["candidate overlaps installed tree", f => { f.manifest.candidate = TARGET; f.seal(); }],
  ["artifacts overlap installed tree", f => { f.manifest.artifact_root = TARGET; f.seal(); }],
  ["source outside candidate", f => { f.manifest.files[0].source = `${TARGET}/src/widget.mjs`; f.seal(); }],
  ["source not target-relative", f => { f.manifest.files[0].source = "/candidate/src/other.mjs"; f.seal(); }],
  ["missing unchanged server identity", f => { delete f.manifest.invariants[`${TARGET}/src/server.mjs`]; f.seal(); }],
  ["invariant overlaps source target", f => { f.manifest.invariants[`${TARGET}/src/widget.mjs`] = f.manifest.files[0].preimage; f.seal(); }],
  ...[WORKFLOW_PATH, PICKER_CATALOG_PATH, "/native/codex"].map(file => [`missing protected invariant ${file}`, f => { delete f.manifest.invariants[file]; f.seal(); }]),
];
for (const [name, change] of deniedContracts) test(`contract denial before effects: ${name}`, async () => {
  const f = fixture(); change(f); await assert.rejects(f.run("apply")); f.noEffects(); assert.equal(f.nodes.has(LOCK), false);
});

for (const target of ["config/nokiy-workflows.json", "runtime/installation.json", "credentials.json", "../escape.mjs",
  "src/../escape.mjs", "src/../../escape.mjs", "/src/absolute.mjs", "src/*.mjs", "src/%2e%2e/escape.mjs",
  "src/sub\\escape.mjs", "src/.hidden.mjs", "src/host.py", "tests/not-a-unit-test.mjs", "tests/*.test.mjs"]) {
  test(`unsafe/protected source target denied without effects: ${target}`, async () => {
    const f = fixture(); f.manifest.files[0].target = target; f.seal();
    await assert.rejects(f.run("preflight")); f.noEffects();
  });
}

const driftCases = [
  ["candidate hash", f => f.put("/candidate/src/widget.mjs", "changed source")],
  ["installed preimage", f => f.put(`${TARGET}/src/widget.mjs`, "changed installed source")],
  ["unexpected existing new target", f => f.put(`${TARGET}/tests/widget.test.mjs`, "unexpected preimage")],
  ...[CATALOG_PATH, ADMISSION_PATH, WORKFLOW_PATH, PICKER_CATALOG_PATH, "/native/codex", `${TARGET}/src/server.mjs`]
    .map(file => [`protected identity ${file}`, f => f.put(file, "drift")]),
  ...["/candidate/src/widget.mjs", `${TARGET}/src/widget.mjs`, "/candidate/src", `${TARGET}/src`, "/deployment", "/deployment/authorization.json", "/deployment/manifest.json"]
    .map(file => [`ownership ${file}`, f => { f.nodes.get(file).uid = 999; }]),
  ["source symlink", f => f.link("/candidate/src/widget.mjs", `${TARGET}/src/widget.mjs`)],
  ["target symlink", f => f.link(`${TARGET}/src/widget.mjs`, "/candidate/src/widget.mjs")],
  ["source directory symlink", f => f.link("/candidate/src", `${TARGET}/src`)],
  ["absent target via directory symlink", f => f.link(`${TARGET}/tests`, "/candidate/tests")],
  ["source traversal", f => { f.manifest.files[0].source = "/candidate/src/../src/widget.mjs"; f.seal(); }],
  ["open identity replaced after lstat", f => { f.control.beforeOpen = file => { if (file === "/candidate/src/widget.mjs") f.put(file, "replacement"); }; }],
  ["service port owner", f => { f.control.owner = 999; }],
  ["official admission", f => { f.control.admitted = false; }],
  ["native model readback", f => { f.control.nativeModels = { models: [] }; }],
];
for (const [name, change] of driftCases) test(`preflight drift/identity failure has no install effects: ${name}`, async () => {
  const f = fixture(); change(f); await assert.rejects(f.run("apply")); f.noEffects();
  assert.equal(f.nodes.has("/deployment/apply-state.json"), false);
});

test("the real shared update lock rejects conflict without deleting the other owner's lock", async () => {
  const f = fixture(); f.put(LOCK, "other-owner");
  const previous = process.env.SWITCHBOARD_UPDATE_LOCK_WAIT_MS;
  process.env.SWITCHBOARD_UPDATE_LOCK_WAIT_MS = "0";
  try { await assert.rejects(f.run("apply"), { code: "EEXIST" }); }
  finally {
    if (previous === undefined) delete process.env.SWITCHBOARD_UPDATE_LOCK_WAIT_MS;
    else process.env.SWITCHBOARD_UPDATE_LOCK_WAIT_MS = previous;
  }
  assert.equal(f.bytes(LOCK).toString(), "other-owner"); f.noEffects();
});

test("legacy still requires the fixed set and legacy root workflow input", async () => {
  const incomplete = fixture(true); incomplete.manifest.files.pop(); incomplete.seal();
  await assert.rejects(incomplete.run("preflight")); incomplete.noEffects();
  const missing = fixture(true); missing.nodes.delete("/candidate/nokiy-workflows.json");
  await assert.rejects(missing.run("preflight")); missing.noEffects();
});

for (const file of ["/candidate/src/widget.mjs", `${TARGET}/src/widget.mjs`]) test(`apply rechecks identity after a successful earlier preflight: ${file}`, async () => {
  const f = fixture(); await f.run("preflight"); f.put(file, "later drift");
  await assert.rejects(f.run("apply")); f.noEffects();
});

test("candidate changes after admission cannot install unpinned bytes; attempt remains non-replayable", async () => {
  const f = fixture(); const source = "/candidate/src/widget.mjs", original = f.bytes(source);
  f.control.beforeCommand = file => { if (file === "/native/codex") f.put(source, "late candidate drift"); };
  await assert.rejects(f.run("apply"), /Candidate changed before install/);
  assert.equal(f.bytes(`${TARGET}/src/widget.mjs`).toString(), "export const value = 1;\n");
  assert.equal(f.control.restarts, 0);
  f.put(source, original); delete f.control.beforeCommand;
  await assert.rejects(f.run("apply"), /already attempted/);
  await assert.rejects(f.run("verify"), /outcome uncertain/);
});

test("uncertain install publication is never replayed or verified as complete", async () => {
  const f = fixture(); let injected = false;
  f.control.afterRename = file => { if (file === `${TARGET}/src/widget.mjs` && !injected) { injected = true; throw new Error("uncertain rename"); } };
  await assert.rejects(f.run("apply"));
  assert.equal(hash(f.bytes(`${TARGET}/src/widget.mjs`)), f.manifest.files[0].sha256);
  const count = f.effects.length;
  await assert.rejects(f.run("apply"), /already attempted/);
  await assert.rejects(f.run("verify"), /outcome uncertain/);
  assert.equal(f.effects.slice(count).some(([, file]) => file !== LOCK), false);
  assert.equal(f.control.restarts, 0);
});

test("uncertain registered-service reload retains its receipt and cannot cause another restart", async () => {
  const f = fixture(); f.control.reloadFailure = true;
  await assert.rejects(f.run("apply"), { code: "UNCERTAIN_RELOAD" });
  assert.equal(f.object("/deployment/apply-state.json").state, "RELOAD_FAILED_SERVICE_REGISTERED");
  await assert.rejects(f.run("apply"), /already attempted/);
  await assert.rejects(f.run("verify"), /outcome uncertain/);
  assert.equal(f.control.restarts, 1);
  assert.equal(f.commands.some(([, args]) => ["bootout", "bootstrap"].includes(args[0])), false);
});

const badReadbacks = [
  ["readiness admission status", f => { f.control.ready = { status: "RESTART_REQUIRED" }; }],
  ["running server hash", f => { f.control.ready = { adapter_source_sha256: "0".repeat(64) }; }],
  ["workflow release", f => { f.control.ready = { workflow_release: "old-release" }; }],
  ["ready catalog hash", f => { f.control.ready = { catalog_sha256: "0".repeat(64) }; }],
  ["served catalog", f => { f.control.served = { models: [] }; }],
  ["native models", f => { f.control.nativeModels = { models: [] }; }],
  ["official admission", f => { f.control.admitted = false; }],
  ["service did not restart", f => { f.control.pid = 41; }],
  ["new port owner", f => { f.control.owner = 999; }],
  ["installed source changed", f => f.put(`${TARGET}/src/widget.mjs`, "wrong installed bytes")],
  ["admission changed", f => f.put(ADMISSION_PATH, encoded({ ...f.object(ADMISSION_PATH), tampered: true }))],
  ["picker bytes changed", f => f.put(PICKER_CATALOG_PATH, "drift")],
  ["manifest changed under same plan", f => { f.manifest.expires_at++; f.seal(); }],
  ...["action_id", "target", "release", "plan_sha256", "native_thread_id", "manifest_sha256", "state", "written", "admission_sha256", "server_sha256"]
    .map(key => [`apply-state ${key}`, f => { const state = f.object("/deployment/apply-state.json"); state[key] = key === "written" ? [] : "wrong"; f.put("/deployment/apply-state.json", encoded(state)); }]),
];
for (const [name, change] of badReadbacks) test(`verify fails closed without replay: ${name}`, async () => {
  const f = fixture(); await f.run("apply"); change(f); const count = f.effects.length;
  await assert.rejects(f.run("verify"));
  assert.equal(f.nodes.has("/deployment/verification.json"), false);
  assert.equal(f.effects.slice(count).some(([, file]) => file !== LOCK), false);
  assert.equal(f.control.restarts, 1);
});

test("a supervised service installs while busy, hot-reloads and verifies the successor worker", async () => {
  const f = fixture();
  const kills = [];
  f.control.ready = { supervisor_pid: f.control.pid, worker_pid: 77 };
  f.control.health = { active_http_requests: 2, active_ws_turns: 3 };
  f.process.kill = (pid, signal) => { kills.push([pid, signal]); f.control.ready = { supervisor_pid: f.control.pid, worker_pid: 78 }; };
  assert.equal((await f.run("apply")).applied, true);
  for (const file of f.manifest.files) assert.equal(hash(f.bytes(`${TARGET}/${file.target}`)), file.sha256);
  assert.deepEqual(kills, [[41, "SIGHUP"]]);
  assert.equal(f.control.restarts, 0);
  assert.equal(f.commands.some(([file, args]) => file === "/bin/launchctl" && args[0] === "kickstart"), false);
  const state = f.object("/deployment/apply-state.json");
  assert.equal(state.state, "APPLIED_PENDING_VERIFY");
  assert.deepEqual(state.restart_window, { mode: "hot_reload", supervisor_pid: 41, prior_worker_pid: 77 });
  const receipt = await f.run("verify");
  assert.equal(receipt.verified, true);
  assert.equal(receipt.reload_mode, "hot_reload");
  assert.equal(receipt.worker_pid, 78);
  assert.equal(receipt.service_pid, 41);
});

test("a hot reload whose successor never answers does not verify", async () => {
  const f = fixture();
  f.control.ready = { supervisor_pid: f.control.pid, worker_pid: 77 };
  f.process.kill = () => {};
  assert.equal((await f.run("apply")).applied, true);
  await assert.rejects(f.run("verify"));
  assert.equal(f.nodes.has("/deployment/verification.json"), false);
});

test("a service whose ready supervisor is not the launchd pid keeps the idle-only restart", async () => {
  const f = fixture();
  f.control.ready = { supervisor_pid: 9999, worker_pid: 77 };
  f.control.health = new Error("health unreadable");
  await assert.rejects(f.run("apply"), error => error.code === "RELOAD_REFUSED_NOT_IDLE");
  assert.equal(f.object("/deployment/apply-state.json").state, "RELOAD_REFUSED_BEFORE_INSTALL");
});
