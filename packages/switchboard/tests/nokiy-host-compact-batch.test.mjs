// Native host compact batch expansion: empty shared write lists are normalized, not rejected.

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const HOST = fileURLToPath(new URL("../src/nokiy-native-host.py", import.meta.url));
const PROBE = [
  "import importlib.util, json, sys",
  "spec = importlib.util.spec_from_file_location('nokiy_native_host', sys.argv[1])",
  "host = importlib.util.module_from_spec(spec); spec.loader.exec_module(host)",
  "try: print(json.dumps({'ok': host.expand_verifier_references(host.expand_batch_input(json.load(sys.stdin)))}))",
  "except ValueError as error: print(json.dumps({'error': str(error)}))",
].join("\n");
const expand = (shared, deltas, extra = {}) => JSON.parse(execFileSync(process.env.PYTHON || "python3", ["-I", "-B", "-c", PROBE, HOST], {
  input: JSON.stringify({ schema_version: "nokiy_native_tool_v2", worker: "w", reasoning_effort: "max", service_tier: "default",
    payload: { operation: "run_batch", artifact_root: "/a", shared, member_deltas: deltas, ...extra } }),
  maxBuffer: 8 * 1024 * 1024,
}).toString());
const mission = { mission_id: "m", mode: "DELIVERY", objective: "o", current_predicate: "p" };
const shared = (action) => ({ schema_version: "nokiy_batch_shared_v1", draft: { workspace: "/w", prompt: "task" },
  action: { context_summary: "s", mission, ...action } });
const member = (taskId, action = {}) => ({ draft: { artifact_root: "/a/" + taskId, prompt: " " + taskId },
  action: { mission: { task_id: taskId }, ...action } });

test("empty shared write lists are handed to members instead of rejecting the batch", () => {
  const result = expand(shared({ write_scopes: [], target_paths: [] }), [member("one"), member("two")]);
  assert.ok(result.ok, result.error);
  for (const item of result.ok.payload.members) {
    assert.deepEqual(item.action.write_scopes, []);
    assert.deepEqual(item.action.target_paths, []);
  }
});

test("a member's own write list survives an empty shared list", () => {
  const result = expand(shared({ write_scopes: [] }), [member("one", { write_scopes: ["src/x.py"] }), member("two")]);
  assert.ok(result.ok, result.error);
  assert.deepEqual(result.ok.payload.members.map((item) => item.action.write_scopes), [["src/x.py"], []]);
  assert.equal("target_paths" in result.ok.payload.members[1].action, false);
});

test("inspect actions expand in compact batches, shared or per member", () => {
  const fromShared = expand(shared({ inspect: {}, operations: ["read", "command"], write_scopes: [] }),
    [member("one"), member("two")]);
  assert.ok(fromShared.ok, fromShared.error);
  for (const item of fromShared.ok.payload.members) {
    assert.deepEqual(item.action.inspect, {});
    assert.deepEqual(item.action.write_scopes, []);
  }
  const perMember = expand(shared({ operations: ["read", "command"] }), [member("one", { inspect: {} }), member("two")]);
  assert.ok(perMember.ok, perMember.error);
  assert.deepEqual(perMember.ok.payload.members.map((item) => "inspect" in item.action), [true, false]);
});

test("non-empty shared writes stay rejected with an actionable message", () => {
  const result = expand(shared({ write_scopes: ["src/x.py"] }), [member("one")]);
  assert.match(result.error, /non-empty write_scopes\/target_paths stay per member/);
});

// I-12 (2026-10-22): verifier_commands were refused in compact members only, with no field named.
test("compact members carry verifier grants, shared or per member", () => {
  const verifier = [{ argv: ["/usr/bin/python3", "-B", "-m", "unittest", "test_x"] }];
  const perMember = expand(shared({ operations: ["read", "modify", "command"] }),
    [member("one", { verifier_commands: verifier }), member("two")]);
  assert.ok(perMember.ok, perMember.error);
  assert.deepEqual(perMember.ok.payload.members.map((item) => item.action.verifier_commands), [verifier, undefined]);
  const fromShared = expand(shared({ verifier_commands: verifier }), [member("one"), member("two")]);
  assert.ok(fromShared.ok, fromShared.error);
  for (const item of fromShared.ok.payload.members) assert.deepEqual(item.action.verifier_commands, verifier);
});

test("an unknown compact field is named together with the allowed fields", () => {
  const result = expand(shared({}), [member("one", { verifier_command: [], "bad key!": 1 })]);
  assert.match(result.error, /^Unknown compact draft\/action field: <invalid>, verifier_command; allowed: /);
  assert.match(result.error, /verifier_commands/);
  const draft = expand(shared({}), [{ ...member("one"), draft: { artifact_root: "/a/one", prompt: " one", model: "x" } }]);
  assert.match(draft.error, /^Unknown compact draft\/action field: model; allowed: .*workspace/);
});

test("source queries survive compact expansion without moving member scopes", () => {
  const queries = ["missing_api"];
  const result = expand(shared({}), [member("one", { source_queries: queries, read_scopes: ["a.py"] }), member("two")]);
  assert.ok(result.ok, result.error);
  assert.deepEqual(result.ok.payload.members[0].action.source_queries, queries);
  assert.deepEqual(result.ok.payload.members[0].action.read_scopes, ["a.py"]);
  assert.equal("source_queries" in result.ok.payload.members[1].action, false);
  const common = expand(shared({ source_queries: queries }), [member("one"), member("two")]);
  assert.ok(common.ok, common.error);
  for (const item of common.ok.payload.members) assert.deepEqual(item.action.source_queries, queries);
});

test("explicit shared read discovery reaches every worker without sharing writes", () => {
  const input = shared({ read_directories: ["tests"], operations: ["read", "command", "modify"] });
  const before = JSON.stringify(input);
  const result = expand(input, [member("one", { write_scopes: ["src/a.py"], target_paths: ["src/a.py"] }),
    member("two", { write_scopes: ["src/b.py"], target_paths: ["src/b.py"] })]);
  assert.ok(result.ok, result.error);
  assert.equal(JSON.stringify(input), before);
  const actions = result.ok.payload.members.map(item => item.action);
  assert.deepEqual(actions.map(action => action.read_directories), [["tests"], ["tests"]]);
  assert.deepEqual(actions.map(action => action.write_scopes), [["src/a.py"], ["src/b.py"]]);
  const conflict = expand(input, [member("one", { read_directories: ["private"] })]);
  assert.match(conflict.error, /disjoint; no overrides or scope unions/);
});

test("twenty individually bounded workers may reuse common text beyond the argv-sized aggregate", () => {
  const common = shared({ read_scopes: ["src/x.py"], write_scopes: [], target_paths: [] });
  common.draft.prompt = "x".repeat(5000);
  const deltas = Array.from({ length: 20 }, (_, index) => member(`m${index}`));
  const result = expand(common, deltas);
  assert.ok(result.ok, result.error);
  assert.equal(result.ok.payload.members.length, 20);
  assert.ok(Buffer.byteLength(JSON.stringify(result.ok.payload)) > 96 * 1024);
  for (const [index, item] of result.ok.payload.members.entries()) {
    assert.equal(item.draft.prompt, common.draft.prompt + ` m${index}`);
    assert.deepEqual(item.action.read_scopes, ["src/x.py"]);
    assert.deepEqual(item.action.write_scopes, []);
  }
});

test("the compact transport and member-count bounds are not aggregate capacity exemptions", () => {
  const oversized = shared({});
  oversized.draft.prompt = "x".repeat(96 * 1024);
  assert.match(expand(oversized, [member("one")]).error, /payload byte bound/);
  assert.match(expand(shared({}), Array.from({ length: 65 }, (_, i) => member(`m${i}`))).error,
    /1 through 64/);
});

test("catalog expansion checks every worker and the final verifier without an aggregate argv bound", () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), "nokiy-capacity-"));
  try {
    const path = join(root, "catalog.json");
    const bytes = JSON.stringify({ commands: { large: { argv: ["/usr/bin/printf", "x".repeat(10000)] } } });
    writeFileSync(path, bytes);
    const ref = { catalog_ref: { path, sha256: createHash("sha256").update(bytes).digest("hex"), command: "large" } };
    const common = shared({ verifier_commands: [ref] });
    const good = expand(common, Array.from({ length: 20 }, (_, i) => member(`m${i}`)));
    assert.ok(good.ok, good.error);
    assert.ok(Buffer.byteLength(JSON.stringify(good.ok.payload)) > 96 * 1024);
    assert.equal(good.ok.payload.members[19].action.verifier_commands[0].argv[1].length, 10000);
    common.draft.prompt = "x".repeat(90000);
    assert.match(expand(common, [member("one")]).error, /member.*payload byte bound/);
    const final = { draft: { workspace: "/w", artifact_root: "/a/final", prompt: "x".repeat(90000) },
      action: { mission: { ...mission, task_id: "final" }, verifier_commands: [ref] } };
    assert.match(expand(shared({}), [member("one")], { post_batch_verifier: final }).error,
      /final verifier.*payload byte bound/);
    const wrong = structuredClone(ref);
    wrong.catalog_ref.sha256 = "0".repeat(64);
    assert.match(expand(shared({ verifier_commands: [wrong] }), [member("one")]).error, /SHA-256 mismatch/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

