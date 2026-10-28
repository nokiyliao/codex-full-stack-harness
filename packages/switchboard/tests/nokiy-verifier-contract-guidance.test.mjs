import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("../src/nokiy-native-tool.mjs", import.meta.url), "utf8");
const guidance = source.split("\n").find((line) => line.includes("tool.description += ' Worker tests:"));
const parallelGuidance = source.split("\n").find((line) => line.includes("tool.description += ' Parallel branch tests:"));

test("rejected requested verifiers remain visible without silent worker downgrade", () => {
  assert.ok(guidance);
  assert.match(guidance, /REJECTED_PREPARATION with its original diagnostics/);
  assert.match(guidance, /no verifier is dropped and no worker starts/);
  assert.doesNotMatch(guidance, /prepares that worker once more without verifiers/);
  assert.match(guidance, /Correct the rejected grant once/);
  assert.match(guidance, /explicitly assign the required checks to the existing parent nokiy_native executor/);
  assert.match(guidance, /same authorized task, then continue worker edits with that parent-owned check assignment/);
  assert.match(guidance, /Required checks remain pending, not skipped, until they pass/);
  assert.match(guidance, /grants no authority or automatic verification ownership transfer/);
});

test("external verifier-only entries require actual compiler admission and retain pin boundaries", () => {
  assert.match(guidance, /"scope":"verifier_only"/);
  assert.match(guidance, /accepted only when the actual workspace compiler accepts that grant/);
  assert.match(guidance, /host fills its SHA-256/);
  assert.match(guidance, /appear once in exact argv/);
  assert.match(guidance, /outside Git metadata and every verifier scratch root, without symlink ancestry/);
  assert.match(guidance, /grants no source_read or write access/);
  assert.match(guidance, /Leave executable_sha256 and sha256 values out: the host computes them from current bytes/);
  assert.match(guidance, /Runtime support does not imply global DCF adoption/);
  assert.match(guidance, /preserve scopes, pins and permissions/);
  assert.doesNotMatch(guidance, /never run a harness script from outside the workspace/);
});

test("parallel branch checks use existing argv without replacing complete acceptance", () => {
  assert.ok(parallelGuidance);
  assert.match(parallelGuidance, /when pytest-xdist is installed and the selected tests are independent/);
  assert.match(parallelGuidance, /explicit "-p","xdist","-n","N" arguments/);
  assert.match(parallelGuidance, /available CPU\/memory after accounting for concurrent checks/);
  assert.match(parallelGuidance, /never default to auto or multiply every worker by a fixed count/);
  assert.match(parallelGuidance, /not the unchanged complete acceptance harness in post_batch_verifier, its integrity checks or required coverage/);
  assert.match(parallelGuidance, /Do not append pytest flags to custom validator scripts, inject PYTEST_ADDOPTS, drop required checks or create wrappers/);
  assert.match(parallelGuidance, /If independence, plugin availability or resources are unknown, preserve the original verifier argv/);
  assert.match(parallelGuidance, /Scopes, pins, permissions and parent acceptance remain unchanged/);
});
