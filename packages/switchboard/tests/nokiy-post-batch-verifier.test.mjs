// Projection tests only: no host, caller, model or verifier command is launched.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { NOKIY_TOOL, nativeToolItem, nativeToolProjection } from "../src/nokiy-native-tool.mjs";

let directory, previousCodexHome;
before(() => {
  directory = mkdtempSync(join(tmpdir(), "nokiy-post-batch-"));
  writeFileSync(join(directory, "config.toml"), "[agents]\nmax_threads = 2\n");
  previousCodexHome = process.env.CODEX_HOME;
  process.env.CODEX_HOME = directory;
});
after(() => {
  if (previousCodexHome === undefined) delete process.env.CODEX_HOME;
  else process.env.CODEX_HOME = previousCodexHome;
  rmSync(directory, { recursive: true, force: true });
});

function projection(workflow = "nokiy-direct", type = "function") {
  const target = type === "function"
    ? { type, name: "exec_command", parameters: { type: "object" } }
    : { type, name: "exec", format: { type: "text" } };
  return nativeToolProjection({ tools: [{ type: "namespace", name: "functions", tools: [target] }] }, {
    host: { python: "/stub/python3", script: "/stub/host.py", batch_reference: "direct_v1" },
    worker: "gpt-6-sol", workerEffort: "max", workerServiceTier: "priority", workflow,
  });
}

const verifier = {
  draft: { workspace: "/workspace", artifact_root: "/task/final-artifacts", prompt: "Combined tests",
    authority_effect: "none" },
  action: { operations: ["read", "command"], read_scopes: ["tests/combined.py"], write_scopes: [],
    target_paths: [], verifier_commands: [{ argv: ["/stub/python3", "-m", "unittest"] }] },
  surface_id: "combined-tests",
};
const ordinary = { operation: "run_batch", artifact_root: "/task/batch", members: [
  { draft: { workspace: "/workspace", artifact_root: "/task/batch/one", prompt: "bounded" },
    action: { context_summary: "scope" } },
] };
const compact = { operation: "run_batch", artifact_root: "/task/batch",
  shared: { schema_version: "nokiy_batch_shared_v1", draft: { workspace: "/workspace", prompt: "bounded" }, action: {} },
  member_deltas: [{ draft: { artifact_root: "/task/batch/one" }, action: { mission: { task_id: "one" } } }],
};

function call(payload, projected = projection(), type = "function") {
  return nativeToolItem({ type: type === "function" ? "function_call" : "custom_tool_call", name: NOKIY_TOOL,
    id: "original-item", call_id: "original-call", status: "completed",
    [type === "function" ? "arguments" : "input"]: JSON.stringify(payload) }, projected);
}

function decoded(item) {
  const command = item.type === "function_call" ? JSON.parse(item.arguments).cmd : item.input;
  const encoded = command.match(/'--payload' '([A-Za-z0-9_-]+)'/)?.[1];
  assert.ok(encoded, "projected native command retains its quoted envelope after --payload");
  return JSON.parse(Buffer.from(encoded, "base64url").toString("utf8"));
}

test("catalog references survive function and custom projection for members and final verifier", () => {
  const reference = { catalog_ref: { path: "/task/catalog.json", sha256: "a".repeat(64), command: "check" } };
  for (const type of ["function", "custom"]) {
    const payload = structuredClone(ordinary);
    payload.members[0].action.verifier_commands = [reference];
    payload.post_batch_verifier = structuredClone(verifier);
    payload.post_batch_verifier.action.verifier_commands = [reference];
    const projected = projection("nokiy-direct", type);
    assert.match(projected.body.tools.at(-1).description, /catalog_ref/);
    const result = decoded(call(payload, projected, type)).payload;
    assert.deepEqual(result.members[0].action.verifier_commands, [reference]);
    assert.deepEqual(result.post_batch_verifier.action.verifier_commands, [reference]);
    assert.deepEqual(result.post_batch_verifier.action.write_scopes, []);
  }
});

test("direct tool advertises an ordinary optional packet prepared in the same dispatch", () => {
  for (const type of ["function", "custom"]) {
    const tool = projection("nokiy-direct", type).body.tools.at(-1);
    assert.match(tool.description, /post_batch_verifier is off when omitted/);
    assert.match(tool.description, /prepares it in the same dispatch/);
    assert.match(tool.description, /actual parent thread/);
    assert.match(tool.description, /declared combined tests/);
    assert.match(tool.description, /UNKNOWN without independently durable original outcome proof/);
    assert.match(tool.description, /native verifier fence/);
    assert.match(tool.description, /explicit scratch_root outside its receipt artifact root/);
    assert.match(tool.description, /only scratch is writable, not its enclosing directory/);
    assert.match(tool.description, /DCF-managed workspace still requires its canonical compiler/);
    assert.match(tool.description, /Parent acceptance remains required/);
    assert.match(tool.description, /use a JSON parser to extract needed catalog keys/);
    assert.match(tool.description, /Keep the complete original file for the verifier; selective display never narrows validation/);
    assert.match(tool.description, /With exact task paths, read those paths and relevant tests/);
    assert.match(tool.description, /one batched navigate locations query/);
    assert.match(tool.description, /already known and unchanged, do not rebuild the map/);
    assert.match(tool.description, /declare it in run_batch\.post_batch_verifier with the workers/);
    assert.match(tool.description, /never final write grants/);
    assert.match(tool.description, /do not rerun an identical passing gate against unchanged subjects/);
    assert.match(tool.description, /keep that check parent-owned and pending/);
    if (type === "function") {
      const schema = tool.parameters.properties.post_batch_verifier;
      // V411 added the compact acceptance intent beside the ordinary packet.
      assert.deepEqual(Object.keys(schema.properties).sort(), [...Object.keys(verifier), "intent"].sort());
      assert.deepEqual(schema.anyOf, [{ required: ["draft", "action"] }, { required: ["intent"] }]);
      assert.deepEqual(schema.properties.draft, tool.parameters.properties.draft);
      assert.deepEqual(schema.properties.action, tool.parameters.properties.action);
      assert.equal(schema.additionalProperties, false);
      assert.equal(Object.hasOwn(schema, "default"), false);
      assert.equal(tool.parameters.required.includes("post_batch_verifier"), false);
      assert.equal(Object.hasOwn(projection("nokiy").body.tools.at(-1).parameters.properties, "post_batch_verifier"), false);
      assert.doesNotMatch(projection("nokiy").body.tools.at(-1).description, /Coordinator reuse:/);
    }
  }
});

test("inline and compact transport retain the entire packet and host profile", () => {
  for (const type of ["function", "custom"]) {
    const projected = projection("nokiy-direct", type);
    for (const base of [ordinary, compact]) {
      for (const payload of [base, { ...base, post_batch_verifier: verifier }]) {
        const item = call(payload, projected, type);
        const envelope = decoded(item);
        assert.deepEqual(envelope, { schema_version: "nokiy_native_tool_v2", worker: "gpt-6-sol",
          reasoning_effort: "max", service_tier: "priority", payload });
        assert.equal(item.id, "original-item");
        assert.equal(item.call_id, "original-call");
      }
    }
  }
});

test("locators, model-supplied owners and writing packets cannot become native execution calls", () => {
  const invalid = [null, {}, [], { ...verifier, argv: ["unsafe"] },
    { request_path: "/old/request.json", request_file_sha256: "a".repeat(64),
      request_id: "tura_embedded_" + "b".repeat(64), artifact_root: "/task/final-artifacts",
      native_thread_id: "01234567-89ab-cdef-0123-456789abcdef" },
    { ...verifier, operation: "run" }, { ...verifier, run: {} },
    { ...verifier, native_thread_id: "01234567-89ab-cdef-0123-456789abcdef" },
    { ...verifier, draft: [] }, { ...verifier, action: [] },
    { ...verifier, draft: { ...verifier.draft, authority_effect: "workspace" } },
    { ...verifier, draft: { ...verifier.draft, model: "gpt-6-sol" } },
    ...["native_thread_id", "model", "effort", "credentials"].map((key) =>
      ({ ...verifier, action: { ...verifier.action, [key]: "forbidden" } })),
    { ...verifier, action: { ...verifier.action, operations: ["create"] } },
    { ...verifier, action: { ...verifier.action, allowed_operations: ["modify"] } },
    { ...verifier, action: { ...verifier.action, write_scopes: ["source.py"] } },
    { ...verifier, action: { ...verifier.action, target_paths: ["source.py"] } },
    { ...verifier, action: { ...verifier.action, verifier_commands: [] } },
    { ...verifier, action: { ...verifier.action, verifier_commands: [{}, {}] } }];
  for (const type of ["function", "custom"]) {
    const projected = projection("nokiy-direct", type);
    for (const spec of invalid) {
      assert.throws(() => call({ ...ordinary, post_batch_verifier: spec }, projected, type), /post_batch_verifier/);
    }
    assert.throws(() => call({ operation: "run", post_batch_verifier: verifier }, projected, type), /post_batch_verifier/);
  }
});

test("reference and recovery keep their original payload contracts", () => {
  const projected = projection();
  for (const payload of [
    { operation: "run_batch_ref", input_path: "/task/batch.json", input_sha256: "c".repeat(64) },
    { operation: "read_batch", plan_path: "/task/original-plan.json", plan_sha256: "d".repeat(64) },
  ]) {
    assert.deepEqual(decoded(call(payload, projected)).payload, payload);
    assert.throws(() => call({ ...payload, post_batch_verifier: verifier }, projected));
  }
  assert.throws(() => call({ operation: "run", post_batch_verifier: verifier }, projection("nokiy")), /post_batch_verifier/);
});
