import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { SOL_CONTEXT_MODEL } from "../src/catalog.mjs";
import { createClaudeBridge } from "../src/claude-bridge-route.mjs";
import {
  composePickerCatalog, preserveAdmittedCatalog, publishPickerCatalog,
  validateNativePickerCatalog,
} from "../src/picker-catalog.mjs";
import { AGY_MODELS, EXTERNAL_MODELS, EXTERNAL_MULTI_AGENT_MODELS } from "../src/shared.mjs";

const encode = (value) => `${JSON.stringify(value, null, 2)}\n`;
const hash = (value) => createHash("sha256").update(value).digest("hex");
const row = (slug, extra = {}) => ({ slug, visibility: "list", supports_parallel_tool_calls: true, ...extra });
const admitted = () => ({ models: [row("gpt-6-sol", { operator_field: 42 }), row("nokiy-direct"), row("memory4-dream", { visibility: "hide" })] });
const bridge = () => createClaudeBridge({ port: 1234, key: "test-key", token: "test-token", slugs: ["claude-opus", "claude-sonnet"],
  catalogRows: [row("claude-opus"), row("claude-sonnet")], prepare() {} });

async function files(t) {
  const dir = await mkdtemp(join(tmpdir(), "picker-test-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const catalogPath = join(dir, "admitted.json"), pickerCatalogPath = join(dir, "unified.json");
  await writeFile(catalogPath, encode(admitted()));
  return { catalogPath, pickerCatalogPath };
}

test("composition retains all admitted overlays exactly and appends only Claude identities", () => {
  const original = admitted();
  const snapshot = structuredClone(original);
  const catalog = composePickerCatalog(original, bridge());
  assert.deepEqual(catalog.models.slice(0, original.models.length), original.models);
  assert.deepEqual(catalog.models.slice(original.models.length).map((model) => model.slug), ["claude-opus", "claude-sonnet"]);
  assert.deepEqual(original, snapshot);
  catalog.models[0].operator_field = 0;
  assert.equal(original.models[0].operator_field, 42);
  assert.deepEqual(composePickerCatalog(original, null), original);
});

test("composition rejects duplicate, missing, conflicting and modified identities", () => {
  const base = admitted();
  assert.throws(() => composePickerCatalog({ models: [row("x"), row("x")] }), { code: "PICKER_CATALOG_INVALID" });
  assert.throws(() => composePickerCatalog({ models: [{ slug: "x" }] }), { code: "PICKER_CATALOG_INVALID" });
  assert.throws(() => composePickerCatalog({ models: [row("claude-opus")] }, bridge()), { code: "PICKER_CLAUDE_IDENTITY_CONFLICT" });
  const incomplete = { models: new Set(["claude-opus"]), withCatalogRows: (catalog) => catalog };
  assert.throws(() => composePickerCatalog(base, incomplete), { code: "PICKER_CLAUDE_CATALOG_INVALID" });
  const replaces = { models: new Set(["claude-opus"]), withCatalogRows: (catalog) => ({ models: [row("replacement"), ...catalog.models.slice(1), row("claude-opus")] }) };
  assert.throws(() => composePickerCatalog(base, replaces), { code: "PICKER_CLAUDE_CATALOG_INVALID" });
});

test("publication is atomic, does not alter admitted bytes and skips identical rewrites", async (t) => {
  const paths = await files(t);
  const original = await readFile(paths.catalogPath, "utf8");
  let validations = 0;
  const options = { ...paths, claudeBridge: bridge(), validateNative: async (catalog) => {
    validations++;
    assert.equal(catalog.models.length, 5);
    return { ok: true };
  } };
  const first = await publishPickerCatalog(options);
  const before = await stat(paths.pickerCatalogPath);
  const second = await publishPickerCatalog(options);
  const after = await stat(paths.pickerCatalogPath);
  assert.equal(first.changed, true);
  assert.equal(second.changed, false);
  assert.equal(first.sha256, second.sha256);
  assert.equal(before.ino, after.ino);
  assert.equal(before.mtimeMs, after.mtimeMs);
  assert.equal(after.mode & 0o777, 0o600);
  assert.equal(validations, 2);
  assert.equal(await readFile(paths.catalogPath, "utf8"), original);
  assert.equal(first.admitted_catalog_sha256, hash(original));
  assert.deepEqual(JSON.parse(await readFile(paths.pickerCatalogPath, "utf8")).models.slice(0, 3), admitted().models);
});

test("explicitly disabling Claude does not load bridge files or remove admitted overlays", async (t) => {
  const paths = await files(t);
  await publishPickerCatalog({ ...paths, claudeBridge: bridge(), validateNative: async () => ({ ok: true }) });
  const result = await publishPickerCatalog({ ...paths, claudeBridge: null,
    loadBridge() { throw new Error("disabled Claude loader must not run"); }, validateNative: async () => ({ ok: true }) });
  assert.equal(result.changed, true);
  assert.deepEqual(result.claude_models, []);
  assert.deepEqual(JSON.parse(await readFile(paths.pickerCatalogPath, "utf8")), admitted());
});

test("publication retains destination on failed validation or a changed source/destination preimage", async (t) => {
  for (const mutation of ["native", "source", "destination"]) {
    const paths = await files(t);
    await writeFile(paths.pickerCatalogPath, "previous-picker\n");
    const validateNative = async () => {
      if (mutation === "source") await writeFile(paths.catalogPath, encode({ models: [row("new-source")] }));
      if (mutation === "destination") await writeFile(paths.pickerCatalogPath, "concurrent-picker\n");
      return { ok: mutation !== "native" };
    };
    await assert.rejects(publishPickerCatalog({ ...paths, claudeBridge: null, validateNative }), {
      code: mutation === "native" ? "PICKER_NATIVE_VALIDATION_FAILED" : "PICKER_CATALOG_PREIMAGE_CHANGED",
    });
    assert.equal(await readFile(paths.pickerCatalogPath, "utf8"), mutation === "destination" ? "concurrent-picker\n" : "previous-picker\n");
  }
});

test("publisher cannot overwrite the admission catalog", async (t) => {
  const paths = await files(t);
  await assert.rejects(publishPickerCatalog({ ...paths, pickerCatalogPath: paths.catalogPath }), { code: "PICKER_CATALOG_PATH_CONFLICT" });
});

test("native validation checks exact identities and required tool fields and removes its preview", async () => {
  const catalog = { models: [row("gpt-6-sol", { tool_mode: "code_mode_only", shell_type: "shell_command" })] };
  let preview;
  const execute = async (_path, args) => {
    preview = JSON.parse(args[1].slice("model_catalog_json=".length));
    assert.deepEqual(JSON.parse(await readFile(preview, "utf8")), catalog);
    return { stdout: JSON.stringify(catalog) };
  };
  assert.deepEqual(await validateNativePickerCatalog(catalog, { cliPath: "/test/native", execute }), {
    ok: true, model_count: 1, native_reference_compared: true, native_shell_projections: [],
  });
  await assert.rejects(readFile(preview), { code: "ENOENT" });
  const nativeSerialized = structuredClone(catalog);
  delete nativeSerialized.models[0].supports_parallel_tool_calls;
  assert.equal((await validateNativePickerCatalog(catalog, { execute: async () => ({ stdout: JSON.stringify(nativeSerialized) }) })).ok, true);
  await assert.rejects(validateNativePickerCatalog(catalog, { execute: async () => ({
    stdout: JSON.stringify({ models: [row("gpt-6-sol", { tool_mode: "shell_command", shell_type: "shell_command" })] }),
  }) }), { code: "PICKER_NATIVE_READBACK_MISMATCH" });
  await assert.rejects(validateNativePickerCatalog(catalog, { execute: async () => ({
    stdout: JSON.stringify({ models: [...catalog.models, row("unexpected")] }),
  }) }), { code: "PICKER_NATIVE_READBACK_MISMATCH" });
});

test("native shell projection is accepted only with identical original native readbacks", async () => {
  const referenceCatalog = { models: [row("gpt-6-sol", { shell_type: "shell_command" })] };
  const catalog = { models: [...referenceCatalog.models, row("claude-opus", { shell_type: "unified_exec" })] };
  const execute = async (_path, args) => {
    const path = JSON.parse(args[1].slice("model_catalog_json=".length));
    const source = JSON.parse(await readFile(path, "utf8"));
    return { stdout: JSON.stringify({ models: source.models.map((model) => ({ ...model, shell_type: "unified_exec" })) }) };
  };
  const result = await validateNativePickerCatalog(catalog, { referenceCatalog, execute });
  assert.deepEqual(result.native_shell_projections, [{ slug: "gpt-6-sol", configured: "shell_command", effective: "unified_exec" }]);
  let reads = 0;
  await assert.rejects(validateNativePickerCatalog(catalog, { referenceCatalog, execute: async (...args) => {
    const value = await execute(...args);
    const loaded = JSON.parse(value.stdout);
    if (++reads === 2) loaded.models[0].base_instructions = "unexpected-effective-drift";
    return { stdout: JSON.stringify(loaded) };
  } }), { code: "PICKER_NATIVE_REFERENCE_MISMATCH" });
  const newUnreferencedProjection = { models: [...referenceCatalog.models, row("claude-opus", { shell_type: "shell_command" })] };
  await assert.rejects(validateNativePickerCatalog(newUnreferencedProjection, { referenceCatalog, execute }), { code: "PICKER_NATIVE_READBACK_MISMATCH" });
});

function priorAdmissionFixture() {
  const official = { cli_sha256: "native-hash", bundled_catalog_sha256: "bundle-hash", bundled_catalog: { models: [row(SOL_CONTEXT_MODEL)] } };
  const catalog = { models: [row(SOL_CONTEXT_MODEL), ...EXTERNAL_MODELS.map((slug) => row(slug, {
    supported_in_api: true, supports_search_tool: true,
    supports_parallel_tool_calls: !AGY_MODELS.includes(slug),
    experimental_supported_tools: [], input_modalities: ["text"], context_window: 1000, max_context_window: 1000,
    ...(EXTERNAL_MULTI_AGENT_MODELS.includes(slug) ? { multi_agent_version: "v2" } : {}),
  })), row("nokiy-direct", { operator_field: { exact: true } }), row("gpt-newer-than-bundled")] };
  const catalogRaw = encode(catalog);
  const admissionRaw = JSON.stringify({ status: "ADMITTED", catalog_sha256: hash(catalogRaw),
    cli_sha256: official.cli_sha256, bundled_catalog_sha256: official.bundled_catalog_sha256,
    official_models: [SOL_CONTEXT_MODEL, "gpt-newer-than-bundled"] });
  return { catalog, catalogRaw, admissionRaw, official };
}

test("same-source admission keeps overlays and previously refreshed official rows exactly", () => {
  const prior = priorAdmissionFixture();
  const candidate = { models: [row(SOL_CONTEXT_MODEL, { visibility: "hide" }), row("gpt-added")] };
  const kept = preserveAdmittedCatalog(candidate, prior);
  assert.deepEqual(kept.catalog.models.slice(0, prior.catalog.models.length), prior.catalog.models);
  assert.equal(kept.catalog.models.at(-1).slug, "gpt-added");
  assert.deepEqual(kept.officialModels, [SOL_CONTEXT_MODEL, "gpt-newer-than-bundled"]);
  assert.deepEqual(kept.sourceIdentityObservations.matches, { app_version: null, app_build: null, cli_sha256: true, bundled_catalog_sha256: true });
  assert.deepEqual(kept.sourceIdentityObservations.drifted_fields, []);
  assert.deepEqual(preserveAdmittedCatalog(candidate, { catalogRaw: null, admissionRaw: null }), { catalog: candidate, officialModels: [] });
});

test("compatible native and app identity drift is observed without replacing admitted overlays", () => {
  const prior = priorAdmissionFixture();
  const admission = JSON.parse(prior.admissionRaw);
  admission.app_version = "1.0";
  admission.app_build = "100";
  const official = { ...prior.official, cli_sha256: "new-native-hash", bundled_catalog_sha256: "new-bundle-hash", app_version: "1.1", app_build: "101" };
  const candidate = { models: [row(SOL_CONTEXT_MODEL, { operator_field: "candidate-must-not-replace" }), row("gpt-added")] };
  const kept = preserveAdmittedCatalog(candidate, { ...prior, admissionRaw: JSON.stringify(admission), official });
  assert.deepEqual(kept.catalog.models.slice(0, prior.catalog.models.length), prior.catalog.models);
  assert.equal(kept.catalog.models.at(-1).slug, "gpt-added");
  assert.deepEqual(kept.sourceIdentityObservations, {
    previous: { app_version: "1.0", app_build: "100", cli_sha256: "native-hash", bundled_catalog_sha256: "bundle-hash" },
    current: { app_version: "1.1", app_build: "101", cli_sha256: "new-native-hash", bundled_catalog_sha256: "new-bundle-hash" },
    matches: { app_version: false, app_build: false, cli_sha256: false, bundled_catalog_sha256: false },
    drifted_fields: ["app_version", "app_build", "cli_sha256", "bundled_catalog_sha256"],
  });
});

test("admission fails closed on unknown or incompatible source identity", () => {
  const prior = priorAdmissionFixture();
  const candidate = { models: [row("candidate")] };
  assert.throws(() => preserveAdmittedCatalog(candidate, { ...prior, admissionRaw: null }), { code: "PICKER_READMISSION_IDENTITY_UNKNOWN" });
  assert.throws(() => preserveAdmittedCatalog(candidate, { ...prior, catalogRaw: prior.catalogRaw + " " }), { code: "PICKER_READMISSION_IDENTITY_UNKNOWN" });
  assert.throws(() => preserveAdmittedCatalog(candidate, { ...prior, official: { ...prior.official, cli_sha256: null } }), { code: "PICKER_READMISSION_IDENTITY_UNKNOWN" });
  const changed = structuredClone(prior.official);
  changed.bundled_catalog.models[0].tool_mode = "different-contract";
  assert.throws(() => preserveAdmittedCatalog(candidate, { ...prior, official: changed }), { code: "PICKER_READMISSION_CONTRACT_CHANGED" });
});

test("verification-required incompatible tool contracts remain typed and preserve the source catalog", () => {
  const prior = priorAdmissionFixture();
  const originalCatalog = structuredClone(prior.catalog);
  const admission = { ...JSON.parse(prior.admissionRaw), status: "VERIFICATION_REQUIRED" };
  const changed = structuredClone(prior.official);
  changed.cli_sha256 = "updated-native";
  changed.bundled_catalog_sha256 = "updated-bundle";
  changed.bundled_catalog.models[0].tool_mode = "changed-tool-contract";
  assert.throws(() => preserveAdmittedCatalog({ models: [row("candidate")] }, {
    ...prior, admissionRaw: JSON.stringify(admission), official: changed,
  }), { code: "PICKER_READMISSION_CONTRACT_CHANGED" });
  assert.deepEqual(prior.catalog, originalCatalog);
  assert.equal(prior.catalogRaw, encode(originalCatalog));
});
