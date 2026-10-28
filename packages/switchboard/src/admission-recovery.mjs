export function canRecoverWatcherDemotion({ stable, verified }) {
  const admission = verified?.admission;
  const liveTests = admission?.live_tests;
  const checks = liveTests?.checks;
  const onlyWatcherStatusFailed = Object.entries(verified?.predicates || {})
    .every(([name, value]) => value === true || name === "status_admitted");

  return stable === true
    && ["RESTART_REQUIRED", "VERIFICATION_REQUIRED"].includes(admission?.status)
    && admission?.external_batch_admitted === false
    && typeof admission?.watcher_checked_at === "string"
    && onlyWatcherStatusFailed
    && liveTests?.ok === true
    && Array.isArray(checks)
    && checks.length > 0
    && checks.every((check) => check?.ok === true);
}

const TRANSIENT_CODES = new Set(["ENOENT", "EIO", "ESTALE", "EBUSY", "EMFILE", "ENFILE", "ETIMEDOUT"]);
const IDENTITY_KEYS = ["app_version", "app_build", "team_id", "cli_team_id", "bundle_id",
  "cli_sha256", "info_plist_sha256", "bundled_catalog_sha256"];

export function inspectionFailure(error) {
  const phase = error.inspection_phase || "installation_read";
  const retryableCodesignFailure = phase.startsWith("codesign --verify") && error.code === 1;
  // An in-process read or parse error (no command phase, errno or exit code) is not a
  // verification rejection: give it the same fixed retry budget as transient I/O.
  const unclassified = !error.inspection_phase && typeof error.code !== "string" && !Number.isInteger(error.code);
  return {
    phase,
    code: typeof error.code === "string" ? error.code : "INSTALLATION_CHECK_FAILED",
    exit_code: Number.isInteger(error.code) ? error.code : null,
    signal: error.signal || null,
    timed_out: error.code === "ETIMEDOUT" || (error.killed === true && error.signal === "SIGTERM"),
    transient: TRANSIENT_CODES.has(error.code) || retryableCodesignFailure || unclassified
      || (error.killed === true && error.signal === "SIGTERM"),
    // The error class only (for example SyntaxError), never its message or command output.
    ...(unclassified && /^[A-Za-z]{1,40}$/.test(error.name || "") ? { error_name: error.name } : {}),
  };
}

export async function inspectStableInstallation(inspect, {
  wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  maxAttempts = 3,
} = {}) {
  const failures = [];
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      const first = await inspect();
      await wait(1500);
      const second = await inspect();
      const stable = IDENTITY_KEYS.every((key) => first[key] != null && first[key] === second[key]);
      if (stable || attempt === maxAttempts) return { stable, first, second, attempts: attempt, failures };
      failures.push({ phase: "identity_comparison", code: "INSTALLATION_CHANGED", transient: true });
    } catch (error) {
      const failure = inspectionFailure(error);
      failures.push(failure);
      if (!failure.transient || attempt === maxAttempts) {
        return { stable: false, second: {}, attempts: attempt, failures, error_code: failure.code };
      }
    }
    await wait(1500);
  }
  throw new Error("installation inspection requires at least one attempt");
}
