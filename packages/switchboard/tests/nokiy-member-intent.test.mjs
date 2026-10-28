// 2026-10-25 (I26 V418): the Efficient coordinator spent 170 s before dispatch reading worker-owned
// source, hashing the catalog and writing a 1,186-token draft/action plan; native Sol spawned at ~30 s.
// Member intents let the coordinator dispatch at once while the adapter derives mechanical fields.
import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { NOKIY_TOOL, applyBatchIntentDefaults, lowerMemberIntents, pinCatalogReferences, nativeToolProjection, nativeToolItem } from "../src/nokiy-native-tool.mjs";

const root = mkdtempSync(join(tmpdir(), "nokiy-intent-"));
const workspace = join(root, "ws");
mkdirSync(join(workspace, "pkg"), { recursive: true });
writeFileSync(join(workspace, "pkg/existing.py"), "x = 1\n");
const catalogBytes = JSON.stringify({ commands: { focused: { argv: ["/bin/true"] } } });
const catalog = join(root, "catalog.json");
writeFileSync(catalog, catalogBytes);
const digest = createHash("sha256").update(catalogBytes).digest("hex");
const batchRoot = join(root, "batch");
const projection = (input = [{ type: "message", role: "user", content: "Operator task text." }]) => nativeToolProjection(
  { tools: [{ type: "namespace", name: "functions", tools: [{ type: "function", name: "exec_command", parameters: { type: "object" } }] }], input },
  { host: { python: "/never/python", script: "/never/host.py" }, worker: "gpt-6.1-sol", workerEffort: "xhigh",
    workerServiceTier: "default", workflow: "nokiy-direct" });
const dispatch = (payload, projected = projection()) => {
  const item = nativeToolItem({ type: "function_call", name: NOKIY_TOOL, call_id: "c", arguments: JSON.stringify(payload) }, projected);
  const cmd = JSON.parse(item.arguments).cmd;
  return JSON.parse(Buffer.from(cmd.match(/'--payload' '([A-Za-z0-9_-]+)'/)[1], "base64url").toString("utf8")).payload;
};

test("a writer intent derives grants from file existence and keeps reads explicit", () => {
  const [member] = lowerMemberIntents([{ intent: { workspace, task: "Branch A only.",
    write: ["pkg/existing.py", "pkg/new_test.py"], read: ["pkg/**", "/abs/frozen_test.py"],
    verifier: { catalog_ref: { path: catalog, command: "focused" } } } }], batchRoot);
  assert.equal(member.draft.artifact_root, join(batchRoot, "a"));
  assert.equal(member.draft.authority_effect, "workspace");
  assert.equal(member.draft.timeout_seconds, null);
  assert.equal(member.draft.prompt, "Branch A only.\n\nOperator request (context; change only your assigned scope):\n{{nokiy:user_message}}\n");
  assert.deepEqual(member.action.operations, ["read", "command", "modify", "create"]);
  assert.deepEqual(member.action.read_scopes, ["pkg/existing.py", "pkg/new_test.py", "pkg/**", "/abs/frozen_test.py"]);
  assert.deepEqual(member.action.read_directories, ["pkg"]);
  assert.deepEqual(member.action.write_scopes, ["pkg/existing.py", "pkg/new_test.py"]);
  assert.deepEqual(member.action.target_paths, member.action.write_scopes);
  assert.equal(member.action.source_read, true);
  assert.deepEqual(member.action.command_templates, []);
  assert.deepEqual(member.action.verifier_commands, [{ catalog_ref: { path: catalog, command: "focused" } }]);
  assert.equal(member.action.terminal_delivery, "evidence_only");
  assert.deepEqual(member.action.forbidden_effects, ["deployment", "broker", "order"]);
  assert.deepEqual(member.action.mission, { mission_id: "batch", task_id: "a", mode: "DELIVERY",
    objective: "Branch A only.", current_predicate: "Assigned scope is not yet implemented and verified." });
  assert.equal(member.action.context_summary, "Branch A only.");
});

test("write intent alone never grants a directory read", () => {
  const [member] = lowerMemberIntents([{ intent: { workspace, task: "Edit one file.", write: ["pkg/existing.py"] } }], batchRoot);
  assert.deepEqual(member.action.read_scopes, ["pkg/existing.py"]);
  assert.equal(Object.hasOwn(member.action, "read_directories"), false);
  assert.deepEqual(member.action.operations, ["read", "command", "modify"]);
  assert.equal(Object.hasOwn(member.action, "verifier_commands"), false);
});

test("a read-only intent stays read-only, keeps findings and may omit the operator message", () => {
  const [member] = lowerMemberIntents([{ intent: { workspace, task: "Review the parser.", read: ["pkg/existing.py"],
    operator_message: false, id: "review", context: "parse() at line 4 owns the branch.", surface_id: "surface-x" } }], batchRoot);
  assert.equal(member.draft.authority_effect, "none");
  assert.equal(member.draft.prompt, "Review the parser.");
  assert.equal(member.draft.artifact_root, join(batchRoot, "review"));
  assert.deepEqual(member.action.operations, ["read", "command"]);
  assert.deepEqual(member.action.write_scopes, []);
  assert.equal(member.action.terminal_delivery, "assistant_reply");
  assert.equal(member.action.context_summary, "parse() at line 4 owns the branch.");
  assert.equal(member.surface_id, "surface-x");
});

test("intents reject overrides and unsafe or ambiguous shapes before any worker starts", () => {
  for (const intent of [
    { workspace, task: "x", write: ["/abs.py"] }, { workspace, task: "x", write: ["../up.py"] },
    { workspace, task: "x", write: ["a/*.py"] }, { workspace, task: "x", write: ["a/.git/config"] },
    { workspace, task: " ", write: ["a.py"] }, { workspace, task: "x" },
    { workspace: "relative", task: "x", write: ["a.py"] }, { workspace, task: "x", write: ["a.py", "a.py"] },
    { workspace, task: "x", write: ["a.py"], model: "other" }, { workspace, task: "x", write: ["a.py"], artifact_root: "/x" },
    { workspace, task: "x", write: ["a.py"], operations: ["delete"] }, { workspace, task: "x", write: ["a.py"], id: "Upper" },
    { workspace, task: "x", write: ["a.py"], verifier: [] }, { workspace, task: "x", write: ["a.py"], operator_message: "no" },
  ]) assert.throws(() => lowerMemberIntents([{ intent }], batchRoot), /Member intent requires/);
  assert.throws(() => lowerMemberIntents([{ intent: { workspace, task: "x", write: ["a.py"] }, surface_id: "s" }], batchRoot),
    /Member intent requires/);
  assert.throws(() => lowerMemberIntents([{ intent: { workspace, task: "x", write: ["a.py"], id: "a" } },
    { intent: { workspace, task: "y", write: ["b.py"], id: "a" } }], batchRoot), /repeats/);
  assert.throws(() => lowerMemberIntents([{ intent: { workspace, task: "x", write: ["a.py"] } }], "relative"),
    /absolute run_batch artifact_root/);
});

test("ordinary members pass through unchanged beside intents", () => {
  const ordinary = { draft: { workspace, artifact_root: join(batchRoot, "z") }, action: { context_summary: "bounded" } };
  const [kept, lowered] = lowerMemberIntents([ordinary, { intent: { workspace, task: "x", write: ["a.py"] } }], batchRoot);
  assert.equal(kept, ordinary);
  assert.equal(lowered.draft.artifact_root, join(batchRoot, "b"));
  const plain = [ordinary];
  assert.equal(lowerMemberIntents(plain, undefined), plain);
});

test("an omitted catalog digest is pinned from current bytes; an explicit one is left for the host to verify", () => {
  const pinned = pinCatalogReferences({ members: [{ action: { verifier_commands: [
    { catalog_ref: { path: catalog, command: "focused" } }, { argv: ["/bin/true"] }] } }] });
  assert.deepEqual(pinned.members[0].action.verifier_commands, [
    { catalog_ref: { path: catalog, command: "focused", sha256: digest } }, { argv: ["/bin/true"] }]);
  const explicit = { verifier_commands: [{ catalog_ref: { path: catalog, sha256: "0".repeat(64), command: "focused" } }] };
  assert.deepEqual(pinCatalogReferences(explicit), explicit);
  assert.throws(() => pinCatalogReferences({ verifier_commands: [{ catalog_ref: { path: "relative.json", command: "k" } }] }),
    /absolute catalog file/);
  assert.throws(() => pinCatalogReferences({ verifier_commands: [{ catalog_ref: { path: join(root, "missing.json"), command: "k" } }] }),
    /not a readable regular catalog file/);
  assert.throws(() => pinCatalogReferences({ verifier_commands: [{ catalog_ref: { path: root, command: "k" } }] }),
    /not a readable regular catalog file/);
});

test("dispatch lowers intents, pins catalogs and expands the operator message before the host envelope", () => {
  const payload = { operation: "run_batch", artifact_root: batchRoot, members: [{ intent: { workspace, task: "Branch A only.",
    write: ["pkg/existing.py"], verifier: { catalog_ref: { path: catalog, command: "focused" } } } }] };
  const [member] = dispatch(payload).members;
  assert.equal(Object.hasOwn(member, "intent"), false);
  assert.equal(member.draft.prompt, "Branch A only.\n\nOperator request (context; change only your assigned scope):\nOperator task text.\n");
  assert.equal(member.action.verifier_commands[0].catalog_ref.sha256, digest);
  // Without an operator message the reference is omitted instead of failing the whole dispatch.
  const [silent] = dispatch(payload, projection([])).members;
  assert.equal(silent.draft.prompt, "Branch A only.");
});

test("the advertised schema offers member intents beside full members and an optional catalog digest", () => {
  const properties = projection().body.tools.at(-1).parameters.properties;
  const items = properties.members.items;
  assert.deepEqual(items.anyOf, [{ required: ["draft", "action"] }, { required: ["intent"] }]);
  assert.deepEqual(items.properties.intent.required, ["task"]);
  assert.deepEqual(Object.keys(properties.intent_defaults.properties), ["workspace", "context"]);
  assert.deepEqual(properties.post_batch_verifier.properties.intent.required, ["verifier"]);
  assert.deepEqual(Object.keys(items.properties.intent.properties).sort(),
    ["context", "id", "operator_message", "read", "surface_id", "task", "verifier", "workspace", "write"]);
  assert.deepEqual(properties.action.properties.verifier_commands.items.anyOf[0].properties.catalog_ref.required, ["path", "command"]);
  const description = projection().body.tools.at(-1).description;
  assert.match(description, /Member intent \(preferred dispatch form\)/);
  assert.match(description, /omit sha256 and the adapter pins the catalog's current bytes before preparation/);
  assert.match(description, /run_batch\.intent_defaults:\{"workspace"/);
  assert.match(description, /post_batch_verifier may be \{intent:\{verifier\}\}/);
});

// 2026-10-26 (V436 Ultra): the coordinator repeated one workspace path about ten times and wrote two
// 91%-identical member contexts; intent_defaults and final-verifier defaults let it write them once.
test("intent_defaults writes the shared workspace and context once, equal to explicit members", () => {
  const compact = dispatch({ operation: "run_batch", artifact_root: batchRoot,
    intent_defaults: { workspace, context: "Shared rules." },
    members: [{ intent: { task: "Branch A.", write: ["pkg/existing.py"], context: "A detail." } },
      { intent: { task: "Branch B.", write: ["pkg/new_test.py"] } }] });
  const explicit = dispatch({ operation: "run_batch", artifact_root: batchRoot,
    members: [{ intent: { workspace, task: "Branch A.", write: ["pkg/existing.py"], context: "Shared rules.\nA detail." } },
      { intent: { workspace, task: "Branch B.", write: ["pkg/new_test.py"], context: "Shared rules." } }] });
  assert.deepEqual(compact, explicit);
  assert.equal(Object.hasOwn(compact, "intent_defaults"), false);
  assert.deepEqual(compact.members.map((member) => member.action.context_summary), ["Shared rules.\nA detail.", "Shared rules."]);
});

test("a final verifier intent needs only its verifier; the batch fixes the rest", () => {
  const verifier = { argv: ["/bin/true"] };
  const members = (withWorkspace) => [
    { intent: { ...(withWorkspace ? { workspace } : {}), id: "a", task: "A.", write: ["pkg/existing.py"], read: ["pkg/**"] } },
    { intent: { ...(withWorkspace ? { workspace } : {}), id: "b", task: "B.", write: ["pkg/new_test.py"] } }];
  const explicit = dispatch({ operation: "run_batch", artifact_root: batchRoot, members: members(true),
    post_batch_verifier: { intent: { workspace, artifact_root: join(batchRoot, "final-gate"),
      mission: { mission_id: "batch", task_id: "final-gate", mode: "DELIVERY",
        objective: "Run the declared combined acceptance after every member joins.",
        current_predicate: "Combined acceptance has not passed." },
      read_scopes: ["pkg/**"], read_directories: ["pkg"], verifier } } });
  const compact = dispatch({ operation: "run_batch", artifact_root: batchRoot, intent_defaults: { workspace },
    members: members(false), post_batch_verifier: { intent: { verifier } } });
  assert.deepEqual(compact, explicit);
  // Without intent_defaults the members' common workspace is used.
  const common = dispatch({ operation: "run_batch", artifact_root: batchRoot, members: members(true),
    post_batch_verifier: { intent: { verifier } } });
  assert.deepEqual(common, explicit);
  assert.deepEqual(compact.post_batch_verifier.action.read_scopes, ["pkg/**"]);
  assert.equal(compact.post_batch_verifier.draft.artifact_root, join(batchRoot, "final-gate"));
});

test("final verifier defaults fail closed on a member leaf collision or an ambiguous workspace", () => {
  const verifier = { argv: ["/bin/true"] };
  assert.throws(() => dispatch({ operation: "run_batch", artifact_root: batchRoot,
    members: [{ intent: { workspace, id: "final-gate", task: "A.", write: ["pkg/existing.py"] } }],
    post_batch_verifier: { intent: { verifier } } }), /final-gate, which a member already uses/);
  assert.throws(() => dispatch({ operation: "run_batch", artifact_root: batchRoot,
    members: [{ intent: { workspace, task: "A.", write: ["pkg/existing.py"] } },
      { intent: { workspace: join(root, "other-ws"), task: "B.", write: ["x.py"] } }],
    post_batch_verifier: { intent: { verifier } } }), /Acceptance intent requires explicit workspace/);
});

test("intent_defaults rejects unknown keys, relative workspaces and batches without intents", () => {
  const member = { intent: { task: "A.", write: ["pkg/existing.py"] } };
  for (const intentDefaults of [{ workspace, model: "x" }, { workspace: "relative" }, { context: " " }, []]) {
    assert.throws(() => dispatch({ operation: "run_batch", artifact_root: batchRoot, intent_defaults: intentDefaults,
      members: [member] }), /intent_defaults accepts only/);
  }
  assert.throws(() => dispatch({ operation: "run_batch", artifact_root: batchRoot, intent_defaults: { workspace },
    members: [{ draft: { workspace, artifact_root: join(batchRoot, "a"), prompt: "p", authority_effect: "none" },
      action: {} }] }), /intent_defaults accepts only/);
});

const acceptanceBatch = (members) => ({ operation: "run_batch", artifact_root: batchRoot, members,
  post_batch_verifier: { intent: { verifier: { argv: ["/bin/true"] } } } });
const ordinaryMember = (id, action = {}) => ({
  draft: { workspace, artifact_root: join(batchRoot, id), prompt: "Assigned branch.", authority_effect: "workspace" },
  action: { read_scopes: ["pkg/existing.py"], write_scopes: ["pkg/existing.py"], ...action },
});

test("ordinary and mixed members reuse declared read grants for abbreviated acceptance", () => {
  const ordinary = ordinaryMember("a", { read_scopes: ["pkg/existing.py", "pkg/**"], read_directories: ["pkg"] });
  for (const members of [[ordinary, ordinaryMember("b")], [ordinary,
    { intent: { workspace, task: "New file.", write: ["pkg/new_test.py"] } }]]) {
    const payload = acceptanceBatch(members);
    const before = structuredClone(payload);
    const expanded = applyBatchIntentDefaults(payload);
    assert.deepEqual(payload, before);
    assert.equal(expanded.post_batch_verifier.intent.workspace, workspace);
    assert.deepEqual(expanded.post_batch_verifier.intent.read_scopes, ["pkg/**"]);
    assert.deepEqual(expanded.post_batch_verifier.intent.read_directories, ["pkg"]);
    assert.deepEqual(dispatch(payload), dispatch({ ...payload, post_batch_verifier: expanded.post_batch_verifier }));
    const final = dispatch(payload).post_batch_verifier;
    assert.equal(final.draft.authority_effect, "none");
    assert.deepEqual(final.action.operations, ["read", "command"]);
    assert.deepEqual(final.action.write_scopes, []);
  }
});

const compactAcceptanceBatch = () => ({ operation: "run_batch", artifact_root: batchRoot,
  shared: { schema_version: "nokiy_batch_shared_v1", draft: { workspace, prompt: "Shared task." },
    action: { read_scopes: ["pkg/existing.py"], read_directories: ["pkg"], context_summary: "Shared findings." } },
  member_deltas: ["a", "b"].map((id) => ({ draft: { artifact_root: join(batchRoot, id) },
    action: { write_scopes: ["pkg/" + id + ".py"], target_paths: ["pkg/" + id + ".py"] } })),
  post_batch_verifier: { intent: { verifier: { argv: ["/bin/true"] } } },
});

test("shared/delta acceptance uses existing grants without expanding or mutating the transport", () => {
  const payload = compactAcceptanceBatch();
  const before = structuredClone(payload);
  const expanded = applyBatchIntentDefaults(payload);
  assert.deepEqual(payload, before);
  assert.deepEqual(expanded.shared, before.shared);
  assert.deepEqual(expanded.member_deltas, before.member_deltas);
  assert.equal(Object.hasOwn(expanded, "members"), false);
  assert.equal(expanded.post_batch_verifier.intent.workspace, workspace);
  assert.deepEqual(expanded.post_batch_verifier.intent.read_scopes, ["pkg/**"]);
  assert.deepEqual(expanded.post_batch_verifier.intent.read_directories, ["pkg"]);
  assert.deepEqual(dispatch(payload), dispatch({ ...payload, post_batch_verifier: expanded.post_batch_verifier }));
  // Per-member grants are also valid when the shared block does not provide them.
  delete payload.shared.action.read_scopes;
  delete payload.shared.action.read_directories;
  payload.member_deltas[0].action.read_scopes = ["pkg/existing.py"];
  payload.member_deltas[1].action.read_scopes = ["pkg/other.py"];
  assert.deepEqual(dispatch(payload).post_batch_verifier.action.read_scopes, ["pkg/existing.py", "pkg/other.py"]);
});

test("acceptance defaults reject missing reads, cross-workspace ambiguity and compact scope overrides", () => {
  const missing = ordinaryMember("a"); delete missing.action.read_scopes;
  assert.throws(() => dispatch(acceptanceBatch([missing])), /Missing after batch defaults: read_scopes/);
  const other = ordinaryMember("b"); other.draft.workspace = join(root, "other-ws");
  assert.throws(() => dispatch(acceptanceBatch([ordinaryMember("a"), other])), /Missing after batch defaults: workspace, read_scopes/);
  const ambiguous = compactAcceptanceBatch();
  ambiguous.member_deltas[0].action.read_scopes = ["pkg/existing.py"];
  assert.throws(() => dispatch(ambiguous), /Compact acceptance source fields must be disjoint/);
  const invalid = acceptanceBatch([ordinaryMember("a")]);
  invalid.post_batch_verifier.intent.model = "different-model";
  assert.throws(() => dispatch(invalid), /unsupported override/);
  invalid.post_batch_verifier.intent = { verifier: { argv: ["/bin/true"] }, read_scopes: [] };
  assert.throws(() => dispatch(invalid), /Invalid field type/);
});

test("ordinary and compact member artifacts cannot collide with the derived final gate", () => {
  assert.throws(() => dispatch(acceptanceBatch([ordinaryMember("final-gate")])), /which a member already uses/);
  const payload = compactAcceptanceBatch();
  payload.member_deltas[1].draft.artifact_root = join(batchRoot, "final-gate");
  assert.throws(() => dispatch(payload), /which a member already uses/);
});
