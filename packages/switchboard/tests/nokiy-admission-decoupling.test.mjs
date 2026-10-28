import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { createSwitchboardServer } from "../src/server.mjs";

const hash = (value) => createHash("sha256").update(value).digest("hex");

// The admission watcher demotes status/external_batch_admitted when the app
// installation cannot be inspected. On 2026-10-17 that returned 503
// CAPABILITY_NOT_ADMITTED to every Nokiy request until the next watcher pass.
test("watcher demotion keeps Nokiy routable; policy and Nokiy admission still gate it", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "nokiy-admission-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const script = path.join(root, "host.py");
  await writeFile(script, "# test host\n");
  let upstreamRequests = 0;
  const upstream = http.createServer(async (req, res) => {
    for await (const _chunk of req) { /* consume */ }
    upstreamRequests += 1;
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ id: "resp_test", output: [] }));
  });
  await new Promise((resolve) => upstream.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => upstream.close(resolve)));
  const fakeWorker = { close() {} };
  const admission = {
    status: "VERIFICATION_REQUIRED", external_batch_admitted: false,
    watcher_false_predicates: ["installation_not_stable", "installation_inspection_failed"],
    official_models: ["gpt-6-astra", "gpt-6.1-sol"],
    protocol_compatibility: { external_model_orchestration: "responses-tool-loop-v2" },
    policy: { external_models: "allow", effect_scope: "local_inference_routing_only" },
    authority_effect: "local_inference_routing_only",
    local_nokiy_direct: { admitted: true, host: { python: process.execPath, script, sha256: hash("# test host\n") } },
  };
  const board = await createSwitchboardServer({
    runtime: { installation_capability: "test-capability-abcdefghijklmnopqrstuvwxyz" },
    admission,
    workflowProfiles: { schema_version: "nokiy_workflow_profiles_v1", profiles: [{
      slug: "nokiy-direct-test", display_name: "Test", workflow: "nokiy-direct",
      coordinator: { model: "gpt-6-astra", reasoning_effort: "ultra" },
      worker: { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" },
    }] },
    modelCatalog: { models: ["gpt-6-astra", "gpt-6.1-sol"].map((slug) => ({ slug,
      supported_reasoning_levels: [{ effort: "max" }, { effort: "ultra" }] })) },
    xaiWorker: fakeWorker, agyWorker: fakeWorker, webWorker: fakeWorker,
    openrouterWorker: fakeWorker, skipFileGate: true,
    officialOrigin: `http://127.0.0.1:${upstream.address().port}`,
  });
  const address = await board.listen({ host: "127.0.0.1", port: 0 });
  t.after(() => board.close());
  const send = (value) => fetch(
    `http://127.0.0.1:${address.port}/test-capability-abcdefghijklmnopqrstuvwxyz/backend-api/codex/v1/responses/compact`,
    { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(value) });
  const body = { model: "nokiy-direct-test", reasoning: { effort: "max" }, input: [] };

  const demoted = await send(body);
  assert.equal(demoted.status, 200);
  await demoted.arrayBuffer();
  assert.equal(upstreamRequests, 1);

  admission.policy.external_models = "deny";
  const denied = await send(body);
  assert.equal(denied.status, 403);
  assert.equal((await denied.json()).error.code, "EXTERNAL_MODELS_POLICY_DENIED");
  admission.policy.external_models = "allow";

  admission.local_nokiy_direct.admitted = false;
  const unadmitted = await send(body);
  assert.equal(unadmitted.status, 503);
  assert.equal((await unadmitted.json()).error.code, "NOKIY_DIRECT_NOT_ADMITTED");
  assert.equal(upstreamRequests, 1);
});
