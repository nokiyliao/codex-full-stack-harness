import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { coordinatorContract } from "../src/nokiy-coordinator-policy.mjs";

const post = await readFile(new URL("../src/nokiy-coordinator-policy.mjs", import.meta.url), "utf8");
// Restore only the legacy grant paragraph in memory to isolate that regression
// under the current adaptive policy, without snapshots or temporary fixture trees.
const changeStart = post.indexOf('      + " For already-known authorized source tasks,');
const changeEnd = post.indexOf('\n    : "Retain run for a single coherent deliverable', changeStart);
assert.ok(changeStart >= 0 && changeEnd > changeStart);
const base = post.slice(0, changeStart)
  + '      + " Before dispatch, include exact planned new files that require verification in read_scopes as well as write_scopes and target_paths, with create/read/command operations and source_read enabled. After the worker\'s own mutation, verify through existing source_read with expected_sha256 set to the returned postimage_sha256. Keep grants to exact files; no broad directory read grant or automatic escalation from write-only intent. Correct merely omitted fields in the packet; do not repeat a worker solely for an omitted field or a model-reported unattempted read."'
  + post.slice(changeEnd);
const functionStart = base.indexOf("export function coordinatorContract(profile) {");
assert.ok(functionStart >= 0);
const imports = `import { efficientNativeLoop, EFFICIENT_NATIVE_GUIDANCE } from ${JSON.stringify(new URL("../src/nokiy-workflow-profiles.mjs", import.meta.url).href)};\n`;
const baseModule = `data:text/javascript;base64,${Buffer.from(imports + base.slice(functionStart)).toString("base64")}`;
const before = (await import(baseModule)).coordinatorContract;
const profile = (slug = "nokiy-efficient", workflow = "nokiy-direct") => ({
  slug, workflow, display_name: slug,
  coordinator: { model: slug === "nokiy-efficient" ? "gpt-6.1-sol" : "gpt-6-astra", reasoning_effort: "max" },
  worker: { model: "worker", reasoning_effort: "max", service_tier: "default" },
});
const oldContract = before(profile());
const contract = coordinatorContract(profile());

test("the grant-only legacy fixture retains the current adaptive branch guidance", () => {
  assert.notEqual(oldContract, contract);
  for (const text of [oldContract, contract]) {
    assert.match(text, /one coherent branch in the SAME native Codex loop/);
    assert.match(text, /honor explicit independent-runtime or selected-worker requests even for one branch/);
  }
});

test("Efficient corrects the legacy implementation grant omission before dispatch", () => {
  // The original caller requested implementation with read/create/command but
  // omitted modify. Exercise the policy cause without historical packet fixtures.
  assert.match(oldContract, /create\/read\/command operations/);
  assert.doesNotMatch(oldContract, /existing-file edits require modify/);
  assert.doesNotMatch(contract, /with create\/read\/command operations and source_read enabled/);
  assert.match(contract, /existing-file edits require modify, new files require create/);
  assert.match(contract, /Prose and write_scopes never grant operations/);
  assert.match(contract, /do not dispatch implementation when its contract denies a required edit/);
});

test("Efficient distinguishes existing-file edits from creates and exact required reads", () => {
  assert.match(contract, /existing-file edits require modify, new files require create/);
  assert.match(contract, /read\/command and source_read where used/);
  assert.match(contract, /exact writable files in write_scopes and target_paths/);
  assert.match(contract, /required reads in read_scopes, including planned new files that need verification/);
  assert.match(contract, /do not dispatch implementation when its contract denies a required edit/);
});

test("known source work carries complete acceptance and its required evidence end to end", () => {
  assert.match(contract, /already-known authorized source tasks/);
  assert.match(contract, /complete each assigned output end to end/);
  assert.match(contract, /complete acceptance criteria and required source, fixture and test evidence supplied with provenance or through admitted reads/);
});

test("missing worker templates use an explicit existing parent-native test plan", () => {
  assert.match(contract, /If worker command templates are unavailable, explicitly assign the existing advertised parent-native test route/);
  assert.match(contract, /exact command, cwd and environment in the acceptance plan/);
  assert.match(contract, /retain source changes and semantic completion with the worker/);
  assert.match(contract, /If no admitted verification route exists, report that exact gap without inventing templates or grants/);
});

test("read-only discovery stays read-only and prose does not acquire authority", () => {
  assert.match(contract, /Preserve truly read-only reviews and justified navigation when exact scope or dependencies require discovery/);
  assert.match(contract, /Prose and write_scopes never grant operations/);
  assert.match(contract, /Correct omitted fields only within existing parent authority/);
  assert.match(contract, /no broad directory read grant or automatic escalation from write-only intent/);
});

test("Ultra and Efficient retain read-only branch ownership through apply", () => {
  for (const configured of [profile(), profile("nokiy-direct-sol", "nokiy-direct")]) {
    const text = coordinatorContract(configured);
    const dispatch = text.split("\n").find((line) => line.startsWith("Dispatch-first:"));
    assert.ok(dispatch);
    assert.match(dispatch, /read-only planning sets branch owners, interfaces and missing acceptance facts; defer implementation reconnaissance to owners/);
    assert.match(dispatch, /Honor user scope\/parallelism; preserve necessary dependency discovery/);
    assert.match(dispatch, /On apply, retain owners for navigation, implementation, focused tests and verification; never implement both branches then delegate only tests/);
    assert.match(dispatch, /Ownership grants no authority/);
    assert.match(dispatch, /When authorized to implement two or more known independent branches and files, dispatch them all as member intents in one run_batch/);
  }
  const serial = coordinatorContract(profile("nokiy", "nokiy"));
  assert.match(serial, /Execute one bounded action at a time; no batch or fan-out/);
  assert.doesNotMatch(serial, /Dispatch-first:|read-only planning sets branch owners/);
});

test("postimage verification and original-ID recovery advice remain present", () => {
  assert.match(contract, /expected_sha256 set to the returned postimage_sha256/);
  assert.match(contract, /Do not repeat a worker solely for an omitted field or a model-reported unattempted read/);
  assert.match(contract, /Preserve the original batch plan\/member identities and use read_batch after uncertainty/);
  assert.equal(contract.split("proactively check for at least two useful independent authorized").length - 1, 1);
});

test("Serial and Ultra contracts and configured model, effort and tier remain unchanged", () => {
  const modes = [profile("nokiy", "nokiy"), profile("other", "other"),
    ...["luna", "sol", "astra"].map((family) => profile(`nokiy-direct-${family}`, "nokiy-direct"))];
  for (const configured of modes) {
    const original = structuredClone(configured);
    assert.equal(coordinatorContract(configured), before(configured));
    assert.deepEqual(configured, original);
  }
  for (const [model, effort, tier] of [
    ["gpt-6-luna", "max", "default"], ["gpt-6.1-sol", "max", "priority"], ["gpt-6-astra", "xhigh", "ultrafast"],
  ]) {
    const configured = profile();
    configured.worker = { model, reasoning_effort: effort, service_tier: tier };
    const original = structuredClone(configured);
    const text = coordinatorContract(configured);
    assert.deepEqual(configured, original);
    assert.ok(text.includes(`${configured.coordinator.model}/${configured.coordinator.reasoning_effort} is the native-capable coordinator for this Efficient task`));
    assert.ok(text.includes(`Worker ${model}/${effort} performs the assigned work when delegated through nokiy_execute`));
  }
});
