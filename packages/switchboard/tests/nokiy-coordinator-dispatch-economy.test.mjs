import test from "node:test";
import assert from "node:assert/strict";
import { coordinatorContract } from "../src/nokiy-coordinator-policy.mjs";

// 2026-10-22 i6: an Ultra (Astra) coordinator spent about 28K output tokens, nearly all on ten worker
// dispatches; three were rejected, several were one-defect corrections, and one only wrote the final report.
const profile = (slug, workflow, model) => ({ slug, workflow, display_name: slug,
  coordinator: { model, reasoning_effort: "max" }, worker: { model: "gpt-6.1-sol", reasoning_effort: "max" } });

test("existing parent findings accompany assignments without pre-reading worker work", () => {
  for (const configured of [profile("nokiy", "nokiy", "gpt-6-astra"),
    profile("nokiy-direct", "nokiy-direct", "gpt-6-astra"),
    profile("nokiy-efficient", "nokiy-direct", "gpt-6.1-sol")]) {
    const text = coordinatorContract(configured);
    assert.match(text, /In action.context_summary \(intent context\) carry already-observed branch-relevant findings/);
    assert.match(text, /remaining unknowns/);
    assert.match(text, /Do not copy the conversation, invent evidence or read worker-owned source merely to populate this handoff/);
    assert.match(text, /prior finding is not a fresh postimage or test pass/);
    assert.match(text, /shared context does not promise provider cache reuse/);
  }
});

test("coordination-only profiles keep dispatches short, report directly and batch corrections", () => {
  for (const configured of [profile("nokiy-direct", "nokiy-direct", "gpt-6-astra"), profile("nokiy", "nokiy", "gpt-6-astra")]) {
    const text = coordinatorContract(configured);
    assert.match(text, /Write each worker prompt as an assignment, not a copy: the branch goal, acceptance criteria and exact paths/);
    assert.match(text, /needed files listed in read_scopes instead of pasted \(workers read workspace files; the host inlines named external files\)/);
    assert.match(text, /Write the final user-facing report yourself from the accepted results and check output; never dispatch a worker only to summarize/);
    assert.match(text, /send all concrete failures of one branch, quoting the failing assertions, to one corrective member/);
    // The coordination-only role itself is unchanged.
    assert.match(text, /is a coordination-only parent/);
    assert.match(text, /Delegate navigation, implementation, tests, source review and any required semantic verification to the worker/);
  }
});

test("Efficient shares compact assignments while retaining fused execution and complete branch context", () => {
  const text = coordinatorContract(profile("nokiy-efficient", "nokiy-direct", "gpt-6.1-sol"));
  assert.match(text, /Write each worker prompt as an assignment, not a copy: the branch goal, acceptance criteria and exact paths/);
  assert.match(text, /needed files listed in read_scopes instead of pasted/);
  assert.equal(text.split("Write each worker prompt as an assignment, not a copy").length - 1, 1);
  assert.match(text, /complete acceptance criteria and required source, fixture and test evidence supplied with provenance or through admitted reads/);
  assert.match(text, /do not truncate necessary context to reduce calls/);
  assert.doesNotMatch(text, /is a coordination-only parent/);
  assert.match(text, /fix it directly in this native loop/);
});

// V349: native-only timeline items 8/13/14 preceded the first dispatch at 98.2843 s,
// although implementation later overlapped the worker. This checks guidance, not a speed gain.
test("Efficient dispatches ready independent workers before unrelated native investigation", () => {
  const text = coordinatorContract(profile("nokiy-efficient", "nokiy-direct", "gpt-6.1-sol"));
  assert.match(text, /Once an independent worker's scope, inputs, grants and verifier are ready, dispatch it before investigating coordinator-owned branches that cannot affect that dispatch/);
  assert.match(text, /do that independent native work during the original worker wait/);
  assert.match(text, /Complete any dependency, input or authority discovery needed to make the worker ready before launch/);
  for (const configured of [profile("nokiy-direct", "nokiy-direct", "gpt-6-astra"), profile("nokiy", "nokiy", "gpt-6-astra")]) {
    assert.doesNotMatch(coordinatorContract(configured), /Once an independent worker's scope, inputs, grants and verifier are ready/);
  }
});

// V344: the worker returned before the native branch's test edits; after integration was ready,
// the parent split a branch gate, the combined gate and report-only success-log reads over three rounds.
// V345: the worker returned at 185.864 s, native test edits finished at 234.372 s and the gate passed
// at 234.939 s. The later success recap duplicated the final response; the whole 96.35 s interval
// after the worker returned was not delivery overhead.
test("Efficient waits for native edits and collects final acceptance facts in one command", () => {
  const text = coordinatorContract(profile("nokiy-efficient", "nokiy-direct", "gpt-6.1-sol"));
  assert.match(text, /once native-owned code and test edits are finished and delegated results are available, reconcile integrated acceptance/);
  assert.match(text, /do not run the same gate again merely because parent acceptance remains required/);
  assert.match(text, /recovered UNKNOWN or diagnostic_only result is not fresh verification/);
  assert.doesNotMatch(text, /in your first step after the batch returns, start the acceptance checks/);
  assert.match(text, /When the integrated final gate already runs the required branch checks, do not run those same checks separately once integration is ready/);
  assert.match(text, /Include allowed changed paths and all result-dependent facts needed for the final report \(such as test counts or short success logs\) in that gate command/);
  assert.match(text, /without further commands unless a failure, missing evidence or source drift requires follow-up/);
  assert.match(text, /Once acceptance passes, deliver one final response directly/);
  assert.match(text, /do not precede it with a separate success-only progress recap/);
  assert.match(text, /open changes\.diff_path or surrounding source only for files implicated by a failure or a risky hunk/);
});

// 2026-10-22 i18: asked for "exact validation counts", both Ultra workers held task_status done for a
// separate 45-56 second report round, and the parent split its final gate and scope check over two calls.
test("coordination-only workers deliver evidence-only and the parent accepts in its first check", () => {
  for (const configured of [profile("nokiy-direct", "nokiy-direct", "gpt-6-astra"), profile("nokiy", "nokiy", "gpt-6-astra")]) {
    const text = coordinatorContract(configured);
    assert.match(text, /Give each mutation or artifact worker action\.terminal_delivery="evidence_only" and do not ask it for result-dependent prose such as exact test counts/);
    assert.match(text, /ends with task_status done in the same command array as its final passing verifier/);
    assert.match(text, /make one command that runs the final acceptance gate, compares the changed files with the allowed paths and prints the facts your report needs/);
    assert.match(text, /write the final report from that evidence without additional bookkeeping commands/);
    assert.match(text, /PASSED with evidence_authority=live_return/);
    assert.match(text, /do not run the same gate again merely because parent acceptance remains required/);
    assert.match(text, /recovered UNKNOWN or diagnostic_only result is not fresh verification/);
    assert.match(text, /Failed gates, changed subjects and missing evidence require their specific correction/);
    assert.match(text, /cite that location instead of listing, moving or re-hashing it/);
    assert.match(text, /changes\.diff_inline_files names any partial or omitted file/);
  }
  assert.doesNotMatch(coordinatorContract(profile("nokiy-efficient", "nokiy-direct", "gpt-6.1-sol")),
    /do not ask it for result-dependent prose/);
});

// 2026-10-23 i20: the Ultra coordinator spent 233 s before dispatch (three single-file reads, then a
// 5,041-token plan with copied digests and redundant module runners) and 207 s after the batch.
test("coordination-only plans read once, reference the operator text and omit digests", () => {
  for (const configured of [profile("nokiy-direct", "nokiy-direct", "gpt-6-astra"), profile("nokiy", "nokiy", "gpt-6-astra")]) {
    const text = coordinatorContract(configured);
    assert.match(text, /obtain only missing check commands and applicable instructions, in one native call when independent; member intents need no contract fields, grant bookkeeping or catalog digest/);
    assert.match(text, /identify each branch rather than restating the task/);
    assert.match(text, /Omit host-computed digests: inline verifier grants and an omitted catalog_ref sha256 are pinned from current bytes before preparation/);
    assert.match(text, /Never omit other pins a reference contract requires/);
  }
  assert.doesNotMatch(coordinatorContract(profile("nokiy-efficient", "nokiy-direct", "gpt-6.1-sol")),
    /read every file the task names that the plan needs/);
});

// 2026-10-23 i20: a composed runner for frozen tests that read ENGINEERING_WORKSPACE could not start
// (verifier grants carry no environment); the branch command the task supplied set it up itself.
test("a task-supplied check command is the worker verifier before any composed runner", () => {
  for (const configured of [profile("nokiy-direct", "nokiy-direct", "gpt-6-astra"), profile("nokiy", "nokiy", "gpt-6-astra"),
    profile("nokiy-efficient", "nokiy-direct", "gpt-6.1-sol")]) {
    const text = coordinatorContract(configured);
    assert.match(text, /When the task already supplies an exact check command for a branch \(an acceptance, branch or project test command\), grant that command as the worker's verifier instead of composing module runners/);
    assert.match(text, /such commands set up their own environment, while verifier grants carry no environment variables/);
  }
});

// 2026-10-22 i6/i8: 4 of 4 Ultra runs paid a 3-6 minute corrective round for one worker-written fixture
// that resolved helper paths from the working directory; the workers had no way to run those tests.
test("code-writing workers get a module-name verifier matching acceptance and resolve fixtures from the test file", () => {
  for (const configured of [profile("nokiy-direct", "nokiy-direct", "gpt-6-astra"), profile("nokiy", "nokiy", "gpt-6-astra"),
    profile("nokiy-efficient", "nokiy-direct", "gpt-6.1-sol")]) {
    const text = coordinatorContract(configured);
    assert.match(text, /Give every worker that writes or changes code a verifier in its action that runs both the existing tests its change must keep passing and the tests it adds the way the final acceptance harness loads them: a python -m unittest or pytest module runner/);
    assert.match(text, /require that verifier to pass before the worker reports done/);
    assert.match(text, /list that harness or validator in the worker's read_scopes/);
    assert.match(text, /Do not dispatch a separate discovery action for this\./);
    assert.match(text, /resolve from the test file's own location, never the working directory/);
  }
});

test("native App control stays with the advertising parent and preserves original-send recovery", () => {
  for (const configured of [profile("nokiy-direct", "nokiy-direct", "gpt-6-astra"), profile("nokiy", "nokiy", "gpt-6-astra"),
    profile("nokiy-efficient", "nokiy-direct", "gpt-6.1-sol")]) {
    configured.worker.reasoning_effort = "xhigh";
    const before = structuredClone(configured);
    const text = coordinatorContract(configured);
    assert.equal(text.split("Native App cross-session reads").length - 1, 0);
    assert.equal(text.split("Keep native App cross-session reads").length - 1, 1);
    assert.match(text, /Keep native App cross-session reads, recipient identity and availability checks, sends and delivery recovery with the parent that advertises those tools; workers return work results/);
    assert.match(text, /Match delegation to the worker's advertised capabilities before dispatch/);
    assert.match(text, /never delegate unavailable App operations or invent socket\/CLI fallbacks/);
    assert.match(text, /Preserve the exact authorized recipient and permissions/);
    assert.match(text, /Recover an uncertain send through advertised native recovery controls using the original send identity\/handle/);
    assert.match(text, /never replay an uncertain send or substitute a recipient/);
    assert.match(text, /If the required native App control is unavailable, report the exact capability gap and keep unobserved availability or delivery UNKNOWN/);
    assert.match(text, /Native Codex retains session\/Goal, permissions and acceptance ownership/);
    assert.ok(text.includes(`${configured.coordinator.model}/${configured.coordinator.reasoning_effort}`));
    assert.ok(text.includes(`Worker ${configured.worker.model}/xhigh`));
    assert.deepEqual(configured, before);
  }
});

