import assert from "node:assert/strict";
import test from "node:test";

import { canRecoverWatcherDemotion, inspectStableInstallation, inspectionFailure } from "../src/admission-recovery.mjs";

function demoted(overrides = {}) {
  return {
    stable: true,
    verified: {
      predicates: { status_admitted: false, notarized: true, protocol_compatible: true },
      admission: {
        status: "RESTART_REQUIRED",
        external_batch_admitted: false,
        watcher_checked_at: "2026-08-14T22:26:59.452Z",
        live_tests: { ok: true, checks: [{ name: "external_batch", ok: true }] },
      },
    },
    ...overrides,
  };
}

test("watcher demotion recovers when only its status predicate is false", () => {
  assert.equal(canRecoverWatcherDemotion(demoted()), true);
});

test("watcher demotion stays closed when an installation predicate fails", () => {
  const input = demoted();
  input.verified.predicates.notarized = false;
  assert.equal(canRecoverWatcherDemotion(input), false);
});

test("watcher demotion stays closed without a complete prior live receipt", () => {
  const input = demoted();
  input.verified.admission.live_tests.checks[0].ok = false;
  assert.equal(canRecoverWatcherDemotion(input), false);
});

const snapshot = (id = "a") => Object.fromEntries([
  "app_version", "app_build", "team_id", "cli_team_id", "bundle_id",
  "cli_sha256", "info_plist_sha256", "bundled_catalog_sha256",
].map((key) => [key, id]));
const noWait = async () => {};

test("transient inspection failure is fully reverified before any demotion", async () => {
  let calls = 0;
  const result = await inspectStableInstallation(async () => {
    if (++calls === 1) throw Object.assign(new Error("timeout"), { killed: true, signal: "SIGTERM", inspection_phase: "spctl --assess" });
    return snapshot();
  }, { wait: noWait });
  assert.equal(result.stable, true);
  assert.equal(result.attempts, 2);
  assert.equal(calls, 3);
  assert.equal(result.failures[0].phase, "spctl --assess");
});

test("a second-read transient failure cannot reuse the first snapshot", async () => {
  let calls = 0;
  const result = await inspectStableInstallation(async () => {
    if (++calls === 2) throw Object.assign(new Error("update"), { code: "ENOENT" });
    return snapshot(calls < 3 ? "old" : "new");
  }, { wait: noWait });
  assert.equal(result.stable, true);
  assert.equal(calls, 4);
  assert.deepEqual(result.first, snapshot("new"));
  assert.deepEqual(result.second, snapshot("new"));
});

test("an update-time signature failure is rechecked against two stable reads", async () => {
  let calls = 0;
  const result = await inspectStableInstallation(async () => {
    if (++calls === 1) throw Object.assign(new Error("signature unavailable"), {
      code: 1, inspection_phase: "codesign --verify --deep --strict --verbose=2",
    });
    return snapshot("updated");
  }, { wait: noWait });
  assert.equal(result.stable, true);
  assert.equal(result.attempts, 2);
  assert.equal(calls, 3);
  assert.equal(result.failures[0].transient, true);
});

test("persistent signature rejection exhausts the fixed budget and stays closed", async () => {
  let calls = 0;
  const result = await inspectStableInstallation(async () => {
    calls++;
    throw Object.assign(new Error("signature invalid"), { code: 1, inspection_phase: "codesign --verify" });
  }, { wait: noWait });
  assert.equal(result.stable, false);
  assert.equal(calls, 3);
  assert.equal(result.failures.length, 3);
  assert.equal(result.failures[0].exit_code, 1);
});

test("other installation failures are not retried as signature churn", async () => {
  let calls = 0;
  const result = await inspectStableInstallation(async () => {
    calls++;
    throw Object.assign(new Error("invalid installation"), { code: 1, inspection_phase: "spctl --assess" });
  }, { wait: noWait });
  assert.equal(result.stable, false);
  assert.equal(calls, 1);
});

test("persistent transient failure exhausts the fixed budget and stays closed", async () => {
  let calls = 0;
  const result = await inspectStableInstallation(async () => {
    calls++;
    throw Object.assign(new Error("busy"), { code: "EBUSY" });
  }, { wait: noWait });
  assert.equal(result.stable, false);
  assert.equal(calls, 3);
  assert.equal(result.failures.length, 3);
});

test("a one-off in-process read failure is retried before any demotion", async () => {
  // 2026-10-18T20:49Z: one uncoded installation_read failure demoted external providers for 5 minutes.
  let calls = 0;
  const result = await inspectStableInstallation(async () => {
    if (++calls === 1) throw new SyntaxError("Unexpected token W in JSON at position 0");
    return snapshot();
  }, { wait: noWait });
  assert.equal(result.stable, true);
  assert.equal(result.attempts, 2);
  assert.equal(calls, 3);
  assert.deepEqual(result.failures, [{ phase: "installation_read", code: "INSTALLATION_CHECK_FAILED", exit_code: null,
    signal: null, timed_out: false, transient: true, error_name: "SyntaxError" }]);
});

test("a persistent in-process read failure still exhausts the fixed budget and stays closed", async () => {
  let calls = 0;
  const result = await inspectStableInstallation(async () => {
    calls++;
    throw new Error("bundled model catalog is empty or invalid");
  }, { wait: noWait });
  assert.equal(result.stable, false);
  assert.equal(calls, 3);
  assert.equal(result.error_code, "INSTALLATION_CHECK_FAILED");
});

test("changing installation is never accepted until two complete identities agree", async () => {
  let calls = 0;
  const result = await inspectStableInstallation(async () => snapshot(String(++calls)), { wait: noWait });
  assert.equal(result.stable, false);
  assert.equal(calls, 6);
});

test("missing identity fields do not compare equal as undefined", async () => {
  const result = await inspectStableInstallation(async () => ({}), { wait: noWait });
  assert.equal(result.stable, false);
});

test("verification-required state automatically recovers only after full verification", () => {
  const input = demoted();
  input.verified.admission.status = "VERIFICATION_REQUIRED";
  assert.equal(canRecoverWatcherDemotion(input), true);
  input.verified.predicates.protocol_compatible = false;
  assert.equal(canRecoverWatcherDemotion(input), false);
});

test("inspection diagnostics do not expose raw command output or credentials", () => {
  const result = inspectionFailure(Object.assign(new Error("secret"), { code: 1, stderr: "secret", stdout: "secret" }));
  assert.equal(JSON.stringify(result).includes("secret"), false);
  const unclassified = inspectionFailure(new SyntaxError("secret catalog text"));
  assert.equal(JSON.stringify(unclassified).includes("secret"), false);
  assert.equal(unclassified.error_name, "SyntaxError");
});
