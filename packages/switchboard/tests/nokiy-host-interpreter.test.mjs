import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { chmod, mkdtemp, readFile, realpath, symlink, writeFile, rm } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { createSwitchboardServer } from "../src/server.mjs";
import { verifyNativeInterpreter } from "../src/nokiy-native-tool.mjs";
import { nativeHostInterpreter } from "../src/workflow-update.mjs";

const hash = (value) => createHash("sha256").update(value).digest("hex");

test("deployment binds the actual Python payload, surviving retirement of its caller alias", async (t) => {
  const python = execFileSync("python3", ["-c", "import sys; print(sys.executable)"], { encoding: "utf8" }).trim();
  const root = await mkdtemp(path.join(os.tmpdir(), "nokiy-interpreter-release-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const alias = path.join(root, "old-release-python");
  await symlink(python, alias);
  const binding = await nativeHostInterpreter(alias);
  assert.equal(binding.python, await realpath(python));
  assert.equal(binding.python_sha256, hash(await readFile(binding.python)));
  await rm(alias);
  assert.doesNotThrow(() => verifyNativeInterpreter(binding));
  await assert.rejects(nativeHostInterpreter(alias));
  await assert.rejects(nativeHostInterpreter(process.execPath));
});

test("interpreter admission checks missing, nonexecutable, changed and broken-link entries", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "nokiy-interpreter-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const unavailable = { code: "NOKIY_NATIVE_INTERPRETER_UNAVAILABLE", status: 503 };
  for (const python of [undefined, "relative/python", root, path.join(root, "missing")]) {
    assert.throws(() => verifyNativeInterpreter({ python }), unavailable);
  }
  const nonexec = path.join(root, "nonexec");
  await writeFile(nonexec, "not executable", { mode: 0o600 });
  assert.throws(() => verifyNativeInterpreter({ python: nonexec }), unavailable);
  const broken = path.join(root, "broken");
  await symlink(path.join(root, "missing"), broken);
  assert.throws(() => verifyNativeInterpreter({ python: broken }), unavailable);
  const host = { python: process.execPath, python_sha256: hash(await readFile(process.execPath)) };
  assert.doesNotThrow(() => verifyNativeInterpreter(host));
  assert.throws(() => verifyNativeInterpreter({ ...host, python_sha256: "0".repeat(64) }), unavailable);
  await chmod(nonexec, 0o700);
  assert.doesNotThrow(() => verifyNativeInterpreter({ python: nonexec }));
});

test("missing worker interpreter is rejected before inference, without blocking compact/prewarm", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "nokiy-host-interpreter-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const script = path.join(root, "host.py");
  await writeFile(script, "# test host\n");
  let upstreamRequests = 0;
  const upstream = http.createServer(async (req, res) => {
    for await (const _chunk of req) { /* consume the bounded synthetic request */ }
    upstreamRequests += 1;
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ id: "resp_test", output: [] }));
  });
  await new Promise((resolve) => upstream.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => upstream.close(resolve)));
  const profiles = { schema_version: "nokiy_workflow_profiles_v1", profiles: [{
    slug: "nokiy-direct-test", display_name: "Test", workflow: "nokiy-direct",
    coordinator: { model: "gpt-6-astra", reasoning_effort: "ultra" },
    worker: { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" },
  }] };
  const fakeWorker = { close() {} };
  const host = { python: path.join(root, "removed-release/python"), script,
    sha256: hash("# test host\n") };
  const board = await createSwitchboardServer({
    runtime: { installation_capability: "test-capability-abcdefghijklmnopqrstuvwxyz" },
    admission: { status: "ADMITTED", external_batch_admitted: true,
      official_models: ["gpt-6-astra", "gpt-6.1-sol"],
      protocol_compatibility: { external_model_orchestration: "responses-tool-loop-v2" },
      policy: { external_models: "allow", effect_scope: "local_inference_routing_only" },
      authority_effect: "local_inference_routing_only",
      local_nokiy_direct: { admitted: true, host } },
    workflowProfiles: profiles,
    modelCatalog: { models: ["gpt-6-astra", "gpt-6.1-sol"].map((slug) => ({ slug,
      supported_reasoning_levels: [{ effort: "max" }, { effort: "ultra" }] })) },
    xaiWorker: fakeWorker, agyWorker: fakeWorker, webWorker: fakeWorker,
    openrouterWorker: fakeWorker, skipFileGate: true,
    officialOrigin: `http://127.0.0.1:${upstream.address().port}`,
  });
  const address = await board.listen({ host: "127.0.0.1", port: 0 });
  t.after(() => board.close());
  const body = { model: "nokiy-direct-test", reasoning: { effort: "max" }, input: [],
    tools: [{ type: "function", name: "exec_command", parameters: {
      type: "object", properties: { cmd: { type: "string" } }, required: ["cmd"],
    } }] };
  const send = (endpoint, value) => fetch(
    `http://127.0.0.1:${address.port}/test-capability-abcdefghijklmnopqrstuvwxyz/backend-api/codex/v1${endpoint}`,
    { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(value) });
  const denied = await send("/responses", body);
  assert.equal(denied.status, 503);
  assert.equal((await denied.json()).error.code, "NOKIY_NATIVE_INTERPRETER_UNAVAILABLE");
  assert.equal(upstreamRequests, 0);
  for (const [endpoint, value] of [["/responses", { ...body, generate: false }],
    ["/responses/compact", body]]) {
    const response = await send(endpoint, value);
    assert.equal(response.status, 200);
    await response.arrayBuffer();
  }
  assert.equal(upstreamRequests, 2);
  host.python = process.execPath;
  host.python_sha256 = hash(await readFile(process.execPath));
  const admitted = await send("/responses", body);
  assert.equal(admitted.status, 200);
  await admitted.arrayBuffer();
  assert.equal(upstreamRequests, 3);
});
