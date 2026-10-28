import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

import {
  CATALOG_PATH, CLI_PATH, CLIENT_TOOL_CONTRACT_FIELDS, MODELS_DIR,
  atomicWriteJson, catalogCompatibility,
} from "./catalog.mjs";
import { loadClaudeBridge } from "./claude-bridge-route.mjs";

export const PICKER_CATALOG_PATH = `${MODELS_DIR}/unified-models.json`;
const run = promisify(execFile);
const serialized = (value) => `${JSON.stringify(value, null, 2)}\n`;
const digest = (bytes) => createHash("sha256").update(bytes).digest("hex");

function catalogError(code, message) {
  return Object.assign(new Error(message), { code });
}

export const WORKFLOW_CONTEXT_FIELDS = Object.freeze([
  "context_window", "max_context_window", "effective_context_window_percent", "auto_compact_token_limit",
]);

// Aliases own workflow policy, but their native conversation belongs to the
// admitted coordinator. Do not retain a stale copy of its context limits.
export function alignWorkflowContext(catalog, workflows) {
  if (workflows == null) return catalog;
  if (!Array.isArray(workflows.profiles)) {
    throw catalogError("PICKER_WORKFLOW_CONTEXT_INVALID", "Admitted workflow profiles are invalid");
  }
  const profiles = new Map(workflows.profiles.map(profile => [profile.slug, profile]));
  if (profiles.size !== workflows.profiles.length) {
    throw catalogError("PICKER_WORKFLOW_CONTEXT_INVALID", "Duplicate workflow identity");
  }
  return { ...catalog, models: catalog.models.map(model => {
    const profile = profiles.get(model.slug);
    if (!profile) return model;
    const anchor = catalog.models.find(row => row.slug === profile.coordinator?.model);
    if (!anchor || anchor.slug === model.slug || profiles.has(anchor.slug)
      || !Number.isSafeInteger(anchor.context_window) || anchor.context_window <= 0
      || (anchor.max_context_window != null && (!Number.isSafeInteger(anchor.max_context_window)
        || anchor.max_context_window < anchor.context_window))
      || (anchor.effective_context_window_percent != null && (!Number.isFinite(anchor.effective_context_window_percent)
        || anchor.effective_context_window_percent <= 0 || anchor.effective_context_window_percent > 100))
      || (anchor.auto_compact_token_limit != null && (!Number.isSafeInteger(anchor.auto_compact_token_limit)
        || anchor.auto_compact_token_limit <= 0))) {
      throw catalogError("PICKER_WORKFLOW_CONTEXT_INVALID", `Invalid coordinator context for ${model.slug}`);
    }
    const aligned = { ...model };
    for (const field of WORKFLOW_CONTEXT_FIELDS) {
      if (Object.hasOwn(anchor, field)) aligned[field] = anchor[field];
      else delete aligned[field];
    }
    // An explicit profile compaction limit is workflow policy: an alias may compact earlier
    // than its coordinator, never at or beyond the coordinator's context window.
    const compact = profile.auto_compact_token_limit;
    if (compact !== undefined) {
      if (!Number.isSafeInteger(compact) || compact < 100_000 || !(compact < anchor.context_window)) {
        throw catalogError("PICKER_WORKFLOW_CONTEXT_INVALID", `Invalid compaction limit for ${model.slug}`);
      }
      aligned.auto_compact_token_limit = compact;
    }
    return aligned;
  }) };
}

function assertCatalog(catalog, label, { nativeReadback = false } = {}) {
  if (!Array.isArray(catalog?.models) || !catalog.models.length) {
    throw catalogError("PICKER_CATALOG_INVALID", `${label} catalog is empty or invalid`);
  }
  const seen = new Set();
  for (const model of catalog.models) {
    if (typeof model?.slug !== "string" || !model.slug.trim() || seen.has(model.slug)
      || (typeof model.supports_parallel_tool_calls !== "boolean"
        && !(nativeReadback && model.supports_parallel_tool_calls === undefined))) {
      throw catalogError("PICKER_CATALOG_INVALID", `${label} catalog has an invalid identity or native tool field`);
    }
    seen.add(model.slug);
  }
  return seen;
}

export function composePickerCatalog(admitted, claudeBridge = null) {
  const admittedSlugs = assertCatalog(admitted, "Admitted");
  if (claudeBridge === null) return structuredClone(admitted);
  if (!(claudeBridge.models instanceof Set) || !claudeBridge.models.size
    || typeof claudeBridge.withCatalogRows !== "function") {
    throw catalogError("PICKER_CLAUDE_CATALOG_INVALID", "Claude picker catalog identity is unavailable");
  }
  if ([...claudeBridge.models].some((slug) => admittedSlugs.has(slug))) {
    throw catalogError("PICKER_CLAUDE_IDENTITY_CONFLICT", "Claude picker identity conflicts with an admitted route");
  }
  const composed = claudeBridge.withCatalogRows(structuredClone(admitted));
  const composedSlugs = assertCatalog(composed, "Unified");
  if (composed.models.length !== admitted.models.length + claudeBridge.models.size
    || [...claudeBridge.models].some((slug) => !composedSlugs.has(slug))
    || composed.models.slice(admitted.models.length).some((row) => !claudeBridge.models.has(row.slug))
    || JSON.stringify(composed.models.slice(0, admitted.models.length)) !== JSON.stringify(admitted.models)) {
    throw catalogError("PICKER_CLAUDE_CATALOG_INVALID", "Unified picker must retain every admitted row and exact Claude identities");
  }
  return composed;
}

// Compatible native updates may retain operator overlays. Source identity drift
// is observed separately from the current tool-contract acceptance predicates.
export function preserveAdmittedCatalog(candidate, { catalogRaw, admissionRaw, official }) {
  assertCatalog(candidate, "Candidate");
  if (catalogRaw === null && admissionRaw === null) return { catalog: candidate, officialModels: [] };
  if (catalogRaw === null || admissionRaw === null) {
    throw catalogError("PICKER_READMISSION_IDENTITY_UNKNOWN", "Existing catalog and admission must both be present");
  }
  const previous = JSON.parse(catalogRaw);
  const admission = JSON.parse(admissionRaw);
  const slugs = assertCatalog(previous, "Previous admitted");
  if (admission.catalog_sha256 !== digest(catalogRaw)
    || !Array.isArray(admission.official_models)
    || admission.official_models.some((slug) => !slugs.has(slug))
    || !admission.cli_sha256 || !admission.bundled_catalog_sha256
    || !official?.cli_sha256 || !official?.bundled_catalog_sha256) {
    throw catalogError("PICKER_READMISSION_IDENTITY_UNKNOWN", "Existing admitted catalog identity did not verify");
  }
  const compatibility = catalogCompatibility(previous.models, official.bundled_catalog?.models);
  if (!compatibility.official_anchor_contract_compatible || !compatibility.external_client_contract_compatible) {
    throw catalogError("PICKER_READMISSION_CONTRACT_CHANGED", "Existing operator overlays do not match the current native tool contract");
  }
  if (!["ADMITTED", "STAGED"].includes(admission.status)) {
    throw catalogError("PICKER_READMISSION_IDENTITY_UNKNOWN", "Existing admission status does not permit retaining operator overlays");
  }
  const identityFields = ["app_version", "app_build", "cli_sha256", "bundled_catalog_sha256"];
  const sourceIdentityObservations = {
    previous: Object.fromEntries(identityFields.map((field) => [field, admission[field] ?? null])),
    current: Object.fromEntries(identityFields.map((field) => [field, official[field] ?? null])),
    matches: Object.fromEntries(identityFields.map((field) => [field,
      admission[field] == null || official[field] == null ? null : admission[field] === official[field],
    ])),
  };
  sourceIdentityObservations.drifted_fields = identityFields.filter((field) => sourceIdentityObservations.matches[field] === false);
  return {
    catalog: alignWorkflowContext({ ...candidate, ...previous,
      models: [...previous.models, ...candidate.models.filter((model) => !slugs.has(model.slug))] },
    admission.local_nokiy_direct?.profiles),
    officialModels: admission.official_models,
    sourceIdentityObservations,
  };
}

export async function validateNativePickerCatalog(catalog, {
  cliPath = CLI_PATH, execute = run, referenceCatalog = catalog,
} = {}) {
  assertCatalog(catalog, "Candidate");
  assertCatalog(referenceCatalog, "Reference");
  if (referenceCatalog.models.some((reference) =>
    JSON.stringify(catalog.models.find((model) => model.slug === reference.slug)) !== JSON.stringify(reference))) {
    throw catalogError("PICKER_NATIVE_REFERENCE_CHANGED", "Native picker reference rows must be unchanged in the candidate");
  }
  const directory = await mkdtemp(join(tmpdir(), "switchboard-picker-"));
  const preview = join(directory, "models.json");
  try {
    await atomicWriteJson(preview, catalog);
    const { stdout } = await execute(cliPath, ["-c", `model_catalog_json=${JSON.stringify(preview)}`, "debug", "models"], {
      timeout: 30000, maxBuffer: 16 * 1024 * 1024,
    });
    const loaded = JSON.parse(stdout);
    const nativeSlugs = assertCatalog(loaded, "Native loaded", { nativeReadback: true });
    // The native loader requires this bool in input but omits it in debug
    // serialization. Compare it only when a newer native reader returns it.
    const toolFields = [...CLIENT_TOOL_CONTRACT_FIELDS.filter((field) => field !== "shell_type"), "visibility"];
    if (nativeSlugs.size !== catalog.models.length || catalog.models.some((expected) => {
      const actual = loaded.models.find((model) => model.slug === expected.slug);
      return !actual || (actual.supports_parallel_tool_calls !== undefined
        && actual.supports_parallel_tool_calls !== expected.supports_parallel_tool_calls)
        || toolFields.some((field) => JSON.stringify(actual[field] ?? null) !== JSON.stringify(expected[field] ?? null));
    })) {
      throw catalogError("PICKER_NATIVE_READBACK_MISMATCH", "Native Codex did not retain candidate picker identities and tool fields");
    }
    const referencePreview = join(directory, "reference.json");
    await atomicWriteJson(referencePreview, referenceCatalog);
    const referenceResult = await execute(cliPath, ["-c", `model_catalog_json=${JSON.stringify(referencePreview)}`, "debug", "models"], {
      timeout: 30000, maxBuffer: 16 * 1024 * 1024,
    });
    const nativeReference = JSON.parse(referenceResult.stdout);
    const referenceSlugs = assertCatalog(nativeReference, "Native reference", { nativeReadback: true });
    if (referenceSlugs.size !== referenceCatalog.models.length
      || referenceCatalog.models.some((reference) => !referenceSlugs.has(reference.slug))
      || nativeReference.models.some((reference) =>
      JSON.stringify(loaded.models.find((model) => model.slug === reference.slug)) !== JSON.stringify(reference))) {
      throw catalogError("PICKER_NATIVE_REFERENCE_MISMATCH", "Adding picker models changed the native effective readback of existing rows");
    }
    const referenceIdentities = new Set(referenceCatalog.models.map((model) => model.slug));
    const shellProjections = catalog.models.flatMap((expected) => {
      const actual = loaded.models.find((model) => model.slug === expected.slug);
      if ((actual.shell_type ?? null) === (expected.shell_type ?? null)) return [];
      // The caller's native configuration can project shell_type. Accept only
      // a projection reproduced by loading the unchanged source row alone.
      if (!referenceIdentities.has(expected.slug)) {
        throw catalogError("PICKER_NATIVE_READBACK_MISMATCH", "Native shell projection has no unchanged source reference");
      }
      return [{ slug: expected.slug, configured: expected.shell_type ?? null, effective: actual.shell_type ?? null }];
    });
    return { ok: true, model_count: loaded.models.length, native_reference_compared: true, native_shell_projections: shellProjections };
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

export async function readOptionalCatalogFile(path) {
  try { return await readFile(path, "utf8"); }
  catch (error) { if (error.code === "ENOENT") return null; throw error; }
}

// The caller owns the existing Switchboard update lock. This publisher never
// changes admission bytes, route settings, LaunchAgents, or signed app files.
export async function publishPickerCatalog({
  catalogPath = CATALOG_PATH,
  pickerCatalogPath = PICKER_CATALOG_PATH,
  claudeBridge,
  loadBridge = loadClaudeBridge,
  validateNative = validateNativePickerCatalog,
} = {}) {
  if (catalogPath === pickerCatalogPath) {
    throw catalogError("PICKER_CATALOG_PATH_CONFLICT", "Picker publication cannot overwrite the admitted catalog");
  }
  const sourceRaw = await readFile(catalogPath, "utf8");
  const previousRaw = await readOptionalCatalogFile(pickerCatalogPath);
  const bridge = claudeBridge === undefined ? await loadBridge() : claudeBridge;
  const admitted = JSON.parse(sourceRaw);
  const catalog = composePickerCatalog(admitted, bridge);
  const bytes = serialized(catalog);
  const native = await validateNative(catalog, { referenceCatalog: admitted });
  if (native?.ok !== true) throw catalogError("PICKER_NATIVE_VALIDATION_FAILED", "Native picker catalog validation did not pass");
  if (await readFile(catalogPath, "utf8") !== sourceRaw
    || await readOptionalCatalogFile(pickerCatalogPath) !== previousRaw) {
    throw catalogError("PICKER_CATALOG_PREIMAGE_CHANGED", "Catalog changed during picker validation; no publication applied");
  }
  const changed = bytes !== previousRaw;
  if (changed) await atomicWriteJson(pickerCatalogPath, catalog);
  return {
    ok: true, changed, path: pickerCatalogPath,
    sha256: digest(bytes), admitted_catalog_sha256: digest(sourceRaw),
    model_count: catalog.models.length, claude_models: [...(bridge?.models || [])],
    native_validated: true, native_validation: native,
  };
}
