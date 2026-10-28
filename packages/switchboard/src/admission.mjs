import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";

import {
  ADMISSION_PATH,
  CATALOG_PATH,
  admitCurrentInstallation,
  atomicWriteJson,
  inspectOfficialInstallation,
  verifyAdmission,
} from "./catalog.mjs";
import { prepareOfficialDirectCandidate } from "./config-state.mjs";
import { canRecoverWatcherDemotion, inspectStableInstallation, inspectionFailure } from "./admission-recovery.mjs";
import { refreshOfficialCatalog } from "./official-catalog-refresh.mjs";
import {
  preserveAdmittedCatalog, publishPickerCatalog,
  readOptionalCatalogFile, validateNativePickerCatalog,
} from "./picker-catalog.mjs";
import { withUpdateLock } from "./update-lock.mjs";

function output(value) {
  process.stdout.write(`${JSON.stringify(value, null, 2)}\n`);
}

async function stableInstallation() {
  return inspectStableInstallation(inspectOfficialInstallation);
}

async function persistAdmission(liveTests) {
  const catalogRaw = await readOptionalCatalogFile(CATALOG_PATH);
  const admissionRaw = await readOptionalCatalogFile(ADMISSION_PATH);
  const result = await admitCurrentInstallation({ write: false, liveTests });
  const preserved = preserveAdmittedCatalog(result.candidate, { catalogRaw, admissionRaw, official: result.official });
  result.candidate = preserved.catalog;
  result.admission.official_models = [...new Set([...result.admission.official_models, ...preserved.officialModels])];
  if (preserved.sourceIdentityObservations) result.admission.readmission_source_observations = preserved.sourceIdentityObservations;
  const candidateRaw = `${JSON.stringify(result.candidate, null, 2)}\n`;
  result.admission.catalog_sha256 = createHash("sha256").update(candidateRaw).digest("hex");
  await validateNativePickerCatalog(result.candidate);
  if (await readOptionalCatalogFile(CATALOG_PATH) !== catalogRaw
    || await readOptionalCatalogFile(ADMISSION_PATH) !== admissionRaw) {
    throw Object.assign(new Error("Admission source changed during validation; no update applied"), { code: "ADMISSION_PREIMAGE_CHANGED" });
  }
  if (candidateRaw !== catalogRaw) await atomicWriteJson(CATALOG_PATH, result.candidate, 0o600);
  await atomicWriteJson(ADMISSION_PATH, result.admission, 0o600);
  const picker = await publishPickerCatalog();
  output({ ok: true, status: result.admission.status, admission: result.admission, picker_catalog: picker });
}

async function stage() {
  return persistAdmission(null);
}

async function admit(receiptPath) {
  if (!receiptPath) throw new Error("--admit requires --receipt <path>");
  const receipt = JSON.parse(await readFile(receiptPath, "utf8"));
  if (receipt.ok !== true || !Array.isArray(receipt.checks) || receipt.checks.some((check) => check.ok !== true)) {
    throw new Error("the external batch receipt did not pass every admission check");
  }
  return persistAdmission(receipt);
}

async function check() {
  const result = await verifyAdmission();
  output(result);
  if (!result.ok) process.exitCode = 3;
}

async function watch() {
  const stable = await stableInstallation();
  let verified = null;
  try {
    verified = stable.second.app_version
      ? await verifyAdmission({ current: stable.second })
      : { ok: false, false_predicates: ["installation_inspection_failed"] };
  } catch (error) {
    stable.failures.push(inspectionFailure(error));
    verified = {
      ok: false,
      false_predicates: [(error.code || "ADMISSION_VERIFICATION_FAILED").toLowerCase()],
      error_code: error.code || "ADMISSION_VERIFICATION_FAILED",
    };
  }
  if (stable.stable && verified.ok) {
    let catalogRefresh;
    try { catalogRefresh = await refreshOfficialCatalog(); }
    catch (error) {
      if (error.refresh_phase !== "official_catalog_fetch") throw error;
      const picker = await publishPickerCatalog();
      catalogRefresh = { changed: picker.changed, error: error.message, picker_catalog: picker };
    }
    output({ ok: true, status: "ADMITTED", changed: catalogRefresh.changed, catalog_refresh: catalogRefresh, inspection_attempts: stable.attempts, inspection_failures: stable.failures });
    return;
  }
  if (canRecoverWatcherDemotion({ stable: stable.stable, verified })) {
    const recovered = {
      ...verified.admission,
      status: "ADMITTED",
      external_batch_admitted: true,
      watcher_checked_at: new Date().toISOString(),
      watcher_false_predicates: [],
      watcher_error_code: null,
      watcher_inspection_attempts: stable.attempts,
      watcher_inspection_failures: stable.failures,
      watcher_recovered_at: new Date().toISOString(),
    };
    await atomicWriteJson(ADMISSION_PATH, recovered, 0o600);
    const picker = await publishPickerCatalog();
    output({ ok: true, status: "ADMITTED", changed: true, recovered: true, picker_catalog: picker, inspection_attempts: stable.attempts, inspection_failures: stable.failures });
    return;
  }
  let admission = {};
  try { admission = JSON.parse(await readFile(ADMISSION_PATH, "utf8")); } catch {}
  admission = {
    ...admission,
    status: "VERIFICATION_REQUIRED",
    external_batch_admitted: false,
    watcher_checked_at: new Date().toISOString(),
    watcher_false_predicates: [
      ...(stable.stable ? [] : ["installation_not_stable"]),
      ...(verified.false_predicates || []),
    ],
    watcher_error_code: verified.error_code || stable.error_code || null,
    watcher_inspection_attempts: stable.attempts,
    watcher_inspection_failures: stable.failures,
    observed_current: {
      app_version: stable.second.app_version || null,
      app_build: stable.second.app_build || null,
      cli_sha256: stable.second.cli_sha256 || null,
      bundled_catalog_sha256: stable.second.bundled_catalog_sha256 || null,
    },
  };
  await atomicWriteJson(ADMISSION_PATH, admission, 0o600);
  await prepareOfficialDirectCandidate();
  output({ ok: false, status: admission.status, changed: true, false_predicates: admission.watcher_false_predicates, inspection_attempts: stable.attempts, inspection_failures: stable.failures });
  process.exitCode = 3;
}

async function main() {
  const args = process.argv.slice(2);
  if (args.includes("--stage")) return stage();
  if (args.includes("--check")) return check();
  if (args.includes("--refresh-models")) {
    const verified = await verifyAdmission();
    if (!verified.ok) throw new Error("Existing installation must pass admission before model refresh");
    return output(await refreshOfficialCatalog());
  }
  if (args.includes("--watch")) return watch();
  if (args.includes("--admit")) {
    const index = args.indexOf("--receipt");
    return admit(index >= 0 ? args[index + 1] : null);
  }
  throw new Error("usage: admission.mjs --stage | --admit --receipt <path> | --check | --watch | --refresh-models");
}

withUpdateLock(main).catch((error) => {
  output({ ok: false, status: "ADMISSION_FAILED", error_code: error.code || "ADMISSION_FAILED", message: error.message });
  process.exitCode = 1;
});
