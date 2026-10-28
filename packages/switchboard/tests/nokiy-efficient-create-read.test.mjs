import test from "node:test";
import assert from "node:assert/strict";
import { coordinatorContract } from "../src/nokiy-coordinator-policy.mjs";
import { nativeToolItem, NOKIY_TOOL } from "../src/nokiy-native-tool.mjs";

const profile = (slug, workflow = "nokiy-direct") => ({
  slug, workflow, display_name: slug,
  coordinator: { model: slug === "nokiy-efficient" ? "gpt-6.1-sol" : "gpt-6-astra", reasoning_effort: "max" },
  worker: { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" },
});
const futureFile = "output/new.txt";
const member = {
  draft: { workspace: "/fixture/workspace", artifact_root: "/fixture/artifacts/fresh/worker",
    prompt: "Create and verify one exact file", authority_effect: "none", timeout_seconds: null },
  action: { mission: { mission_id: "fixture", task_id: "create-read", mode: "DELIVERY",
    objective: "Create and verify one file", current_predicate: "File is absent" },
    context_summary: "Fixture only", operations: ["create", "read", "command"],
    read_scopes: [futureFile], write_scopes: [futureFile], target_paths: [futureFile],
    source_read: true, command_templates: [], forbidden_effects: ["deployment"] },
  surface_id: "fixture-surface",
};

// Inspect the existing native envelope without executing a host or proving admission.
function translated(payload, { workflow = "nokiy-direct", worker = "gpt-6.1-sol", effort = "max", tier = "default" } = {}) {
  const before = structuredClone(payload);
  const result = nativeToolItem({ type: "function_call", name: NOKIY_TOOL,
    call_id: "fixture-call", arguments: JSON.stringify(payload) }, {
    workflow, worker, workerEffort: effort, serviceTier: tier, userTexts: [],
    operations: ["run", "run_ref", "run_batch", "read_result", "read_batch"],
    host: { python: "/fixture/python", script: "/fixture/host.py" },
    target: { type: "function", name: "exec_command", namespace: "functions" },
  });
  assert.deepEqual(payload, before);
  assert.equal(result.name, "exec_command");
  assert.equal(result.namespace, "functions");
  const args = JSON.parse(result.arguments);
  const encoded = args.cmd.match(/ '--payload' '([A-Za-z0-9_-]+)'$/)?.[1];
  assert.ok(encoded, "one native host invocation contains the original payload");
  return JSON.parse(Buffer.from(encoded, "base64url").toString("utf8"));
}

test("Efficient plans narrow future-file reads and pins verification to its own postimage", () => {
  const text = coordinatorContract(profile("nokiy-efficient"));
  assert.match(text, /Before dispatch, match structured grants to the already-authorized assignment: existing-file edits require modify, new files require create;/);
  assert.match(text, /include read\/command and source_read where used, exact writable files in write_scopes and target_paths,/);
  assert.match(text, /and required reads in read_scopes, including planned new files that need verification\./);
  // A complete member diff is the change evidence; source_read postimage checks remain the fallback.
  assert.match(text, /After the worker's own mutation, a batch member whose changes\.status is COMPLETE already delivers its exact unified diff bound to preimage\/postimage SHA-256/);
  assert.match(text, /once native-owned code and test edits are finished and delegated results are available, reconcile integrated acceptance while using that diff as the change evidence/);
  assert.match(text, /do not recompute those hashes or reread terminal receipts with shell scripts/);
  assert.match(text, /Otherwise verify through existing source_read with expected_sha256 set to the returned postimage_sha256/);
  assert.match(text, /Keep grants to exact files; no broad directory read grant/);
  assert.match(text, /no broad directory read grant or automatic escalation from write-only intent/);
  assert.match(text, /Correct omitted fields only within existing parent authority; do not dispatch implementation when its contract denies a required edit\./);
  assert.match(text, /Do not repeat a worker solely for an omitted field or a model-reported unattempted read/);
});

test("Efficient fixes small acceptance defects natively; Ultra keeps corrective delegation", () => {
  const efficient = coordinatorContract(profile("nokiy-efficient"));
  // A fresh corrective worker re-reads its whole context; autonomous pairs on 2026-10-22 spent
  // 168-253 s per delegated one-line fix while native parents fixed the same defects in about a minute.
  assert.match(efficient, /fix it directly in this native loop with your advertised native tools and rerun the failed checks/);
  assert.match(efficient, /Dispatch a corrective worker only for substantial rework of an independent branch, never to re-solve the task/);
  assert.doesNotMatch(efficient, /delegate only that missing verification\/correction/);
  assert.match(efficient, /after one consistency pass over its own edits; do not request iterative semantic or source re-review/);
  assert.match(efficient, /Ask for tests proportional to the change: one focused test per new requirement or main regression path/);
  assert.match(efficient, /do not request exhaustive, adversarial or permutation coverage/);
  // 2026-10-22 i4: a coordinator used the run directory (parent of ./workspace and ./workers) as
  // draft.workspace and lost a round to NOKIY_NATIVE_HOST_REJECTED (artifact overlaps workspace).
  assert.match(efficient, /Set draft\.workspace to the source root the workers edit \(scope paths are relative to it\) and keep every batch and member artifact_root outside that workspace/);
  assert.match(efficient, /Once acceptance passes, deliver one final response directly from the batch summary \(worker identifiers, usage, changes\) and the check output without further commands unless a failure, missing evidence or source drift requires follow-up; do not precede it with a separate success-only progress recap\./);
  const ultra = coordinatorContract(profile("nokiy-direct"));
  assert.match(ultra, /delegate only that missing verification\/correction instead of independently solving the task again/);
  assert.doesNotMatch(ultra, /fix it directly in this native loop/);
});

test("Efficient overlaps only beneficial delegation without universal line-rate assumptions", () => {
  const efficient = coordinatorContract(profile("nokiy-efficient"));
  assert.match(efficient, /Two or more independently implementable and verifiable branches with nonoverlapping write scopes belong to workers even when small or in the same worktree/);
  assert.match(efficient, /dispatch every such branch in one run_batch instead of implementing one yourself/);
  assert.match(efficient, /Set "observe":"later" only when the parent has independent work/);
  assert.match(efficient, /omit observe so the existing executor waits on the original batch/);
  assert.match(efficient, /Implement any retained branch while workers run/);
  assert.match(efficient, /Use current task evidence, not a fixed changed-line threshold/);
  assert.doesNotMatch(efficient, /300 changed lines/);
  assert.doesNotMatch(efficient, /Measured cost evidence|Select the same native loop only/);
  assert.doesNotMatch(coordinatorContract(profile("nokiy-direct")), /Measured cost evidence/);
});

test("Efficient retains delegated fresh-root singleton batching, recovery and authority checks", () => {
  const text = coordinatorContract(profile("nokiy-efficient"));
  assert.match(text, /When an explicit independent-runtime or selected-worker request requires one installed worker with a fresh artifact root/);
  assert.match(text, /dispatch run_batch explicitly with exactly one member/);
  assert.match(text, /artifact_root is the missing task-local plan directory under an existing canonical parent/);
  assert.match(text, /members\[0\]\.draft\.artifact_root is its direct child/);
  assert.match(text, /no preliminary run, mkdir or setup call is needed/);
  assert.match(text, /Keep run for an existing valid artifact root/);
  assert.match(text, /Preserve the original batch plan\/member identities and use read_batch after uncertainty/);
  assert.match(text, /never convert or resubmit a potentially executed run as a batch/);
  assert.match(text, /Recover uncertain outcomes with read_result\/read_batch on the original request\/plan/);
  assert.match(text, /Selecting this workflow grants no new authority/);
  assert.match(text, /Do not use this channel to evade a real native permission denial, owner conflict or CAS failure/);
});

test("Serial and every Ultra model retain their existing dispatch guidance", () => {
  const modes = [["nokiy", "nokiy"],
    ...["luna", "sol", "astra"].map((family) => [`nokiy-direct-${family}`, "nokiy-direct"])];
  for (const [slug, workflow] of modes) {
    const configured = profile(slug, workflow);
    const before = structuredClone(configured);
    const text = coordinatorContract(configured);
    assert.deepEqual(configured, before);
    assert.doesNotMatch(text, /planned new files that need verification|exact writable files in write_scopes and target_paths|postimage_sha256/);
    assert.doesNotMatch(text, /dispatch run_batch explicitly with exactly one member/);
    if (workflow === "nokiy") {
      assert.match(text, /Execute one bounded action at a time; no batch or fan-out/);
    } else {
      assert.match(text, /Retain run for a single coherent deliverable/);
    }
  }
});

test("guidance preserves configured identities, selectors, effort, tier and native packets", () => {
  const configured = profile("nokiy-efficient");
  configured.worker_selector = { default_effort: "medium",
    models: { low: "gpt-6-luna", medium: "gpt-6.1-sol", high: "gpt-6-astra" },
    reasoning_efforts: { low: "max", medium: "max", high: "xhigh" } };
  const before = structuredClone(configured);
  const text = coordinatorContract(configured);
  assert.deepEqual(configured, before);
  assert.ok(text.includes(`${configured.coordinator.model}/${configured.coordinator.reasoning_effort} is the native-capable coordinator for this Efficient task`));
  assert.ok(text.includes(`Worker ${configured.worker.model}/${configured.worker.reasoning_effort} performs the assigned work when delegated through nokiy_execute`));
  for (const [worker, effort, tier] of [
    ["gpt-6-luna", "max", "default"], ["gpt-6.1-sol", "max", "priority"], ["gpt-6-astra", "xhigh", "ultrafast"],
  ]) {
    const batch = { operation: "run_batch", artifact_root: "/fixture/artifacts/fresh", members: [member] };
    assert.deepEqual(translated(batch, { worker, effort, tier }), {
      schema_version: "nokiy_native_tool_v2", worker, reasoning_effort: effort,
      service_tier: tier, payload: batch,
    });
    const single = { operation: "run", ...structuredClone(member) };
    assert.deepEqual(translated(single, { workflow: "nokiy", worker, effort, tier }), {
      schema_version: "nokiy_native_tool_v3", workflow: "nokiy", worker, reasoning_effort: effort,
      service_tier: tier, payload: single,
    });
  }
});
