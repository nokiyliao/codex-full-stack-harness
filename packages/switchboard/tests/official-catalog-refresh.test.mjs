import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { mergeOfficialAdditions, refreshOfficialCatalog, routeWithCurrentOfficialModels } from "../src/official-catalog-refresh.mjs";

function fixture() {
  return {
    catalog: { models: [
      ...["light", "medium", "high", "extra-high", "pro"].map(mode => ({ slug: `chatgpt-web/${mode}`, visibility: "list", operator_field: 42 })),
      { slug: "grok-4.6", visibility: "list", tool_mode: "code_mode_only" },
      { slug: "gpt-6-astra", visibility: "list", operator_field: 42 },
    ] },
    admission: { status: "ADMITTED", official_models: ["gpt-6-astra"] },
    remote: { models: [{ slug: "gpt-6-astra", operator_field: 0 }, { slug: "gpt-6-sol", visibility: "list", base_instructions: "GPT-6 Sol base", model_messages: { instructions_template: "GPT-6 Sol instructions" }, shell_type: "shell_command", supports_parallel_tool_calls: true }, { slug: "gpt-6-luna", visibility: "list" }] },
  };
}

test("refresh adds official models while preserving overlays and limiting picker choices", () => {
  const f = fixture();
  const result = mergeOfficialAdditions(f.catalog, f.admission, f.remote);
  assert.deepEqual(result.added, ["gpt-6-sol", "gpt-6-luna"]);
  assert.equal(result.catalog.models.find(m => m.slug === "gpt-6-astra").operator_field, 42);
  assert.deepEqual(result.catalog.models.filter(m => m.slug.startsWith("chatgpt-web/") && m.visibility === "list").map(m => m.slug), ["chatgpt-web/extra-high", "chatgpt-web/pro"]);
  assert.equal(result.catalog.models.find(m => m.slug === "grok-4.6").visibility, "hide");
  assert.equal(result.catalog.models.find(m => m.slug === "grok-4.7").tool_mode, "code_mode_only");
  for (const slug of ["chatgpt-web/extra-high", "chatgpt-web/pro"]) {
    const web = result.catalog.models.find(m => m.slug === slug);
    assert.equal(web.model_messages.instructions_template, "GPT-6 Sol instructions");
    assert.equal(web.base_instructions, "GPT-6 Sol base");
    assert.equal(web.shell_type, "shell_command");
    assert.equal(web.default_reasoning_level, "max");
    assert.equal(web.supported_reasoning_levels[0].effort, "max");
  }
  assert.equal(typeof result.catalog.models.find(m => m.slug === "gpt-6-sol").supports_parallel_tool_calls, "boolean");
  assert.equal(mergeOfficialAdditions(result.catalog, result.admission, f.remote).changed, false);
  assert.equal(f.catalog.models.length, 7);
});

test("refresh rejects empty, duplicate and external-colliding official identities", () => {
  const f = fixture();
  for (const models of [[], [{ slug: "x" }, { slug: "x" }], [{ slug: "chatgpt-web/pro" }]]) {
    assert.throws(() => mergeOfficialAdditions(f.catalog, f.admission, { models }));
  }
});

test("refresh prefers the installed GPT-6 Sol tool contract over remote drift", () => {
  const f = fixture();
  f.catalog.models.push({
    slug: "gpt-6-sol",
    base_instructions: "Installed GPT-6 Sol base",
    model_messages: { instructions_template: "Installed GPT-6 Sol instructions" },
    shell_type: "shell_command",
    node_repl_auto_review_required: false,
    supports_parallel_tool_calls: true,
  });
  f.remote.models.find(model => model.slug === "gpt-6-sol").node_repl_auto_review_required = true;
  const result = mergeOfficialAdditions(f.catalog, f.admission, f.remote);
  for (const slug of ["chatgpt-web/extra-high", "chatgpt-web/pro"]) {
    const web = result.catalog.models.find(model => model.slug === slug);
    assert.equal(web.node_repl_auto_review_required, false);
    assert.equal(web.base_instructions, "Installed GPT-6 Sol base");
    assert.equal(web.model_messages.instructions_template, "Installed GPT-6 Sol instructions");
    assert.equal(web.default_reasoning_level, "max");
  }
});

test("GPT-6 replaces matching 5.6 picker entries while preserving explicit legacy routes", () => {
  const f = fixture();
  f.catalog.models.push({ slug: "gpt-5.6-sol", visibility: "list" }, { slug: "gpt-5.6-luna", visibility: "list" }, { slug: "gpt-5.6-terra", visibility: "list" });
  f.admission.official_models.push("gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra");
  const r = mergeOfficialAdditions(f.catalog, f.admission, f.remote);
  for (const slug of ["gpt-5.6-sol", "gpt-5.6-luna"]) {
    assert.equal(r.catalog.models.find(m => m.slug === slug).visibility, "hide");
    assert.ok(r.admission.official_models.includes(slug));
  }
  assert.equal(r.catalog.models.find(m => m.slug === "gpt-5.6-terra").visibility, "list");
  assert.throws(() => mergeOfficialAdditions(f.catalog, f.admission, { models: [{ slug: "gpt-6-astra" }] }), /template is unavailable/);
});

test("new official routing uses fresh admission without changing existing task routes", async () => {
  const initial = new Set(["gpt-6-astra"]);
  let reads = 0;
  const read = async () => { reads++; return { status: "ADMITTED", official_models: ["gpt-6-astra", "gpt-6-sol"] }; };
  assert.equal(await routeWithCurrentOfficialModels("gpt-6-astra", initial, read), "openai");
  assert.equal(reads, 0);
  assert.equal(await routeWithCurrentOfficialModels("gpt-6-sol", initial, read), "openai");
  assert.equal(await routeWithCurrentOfficialModels("grok-4.7", initial, read), "xai");
  assert.equal(await routeWithCurrentOfficialModels("grok-4.6", initial, read), "xai");
  await assert.rejects(routeWithCurrentOfficialModels("unknown", initial, read));
  await assert.rejects(routeWithCurrentOfficialModels("gpt-6-sol", initial, async () => ({ status: "VERIFICATION_REQUIRED", official_models: ["gpt-6-sol"] })));
});

async function installedFixture(t) {
  const directory = await mkdtemp(join(tmpdir(), "official-refresh-test-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const f = fixture();
  const merged = mergeOfficialAdditions(f.catalog, f.admission, f.remote);
  const catalogRaw = JSON.stringify(merged.catalog, null, 2) + "\n";
  merged.admission.catalog_sha256 = createHash("sha256").update(catalogRaw).digest("hex");
  const catalogPath = join(directory, "admitted-models.json"), admissionPath = join(directory, "admission.json");
  await writeFile(catalogPath, catalogRaw);
  await writeFile(admissionPath, JSON.stringify(merged.admission));
  return { ...f, catalog: JSON.parse(catalogRaw), catalogRaw, catalogPath, admissionPath };
}

test("unchanged official refresh still publishes the unified picker without rewriting admission", async (t) => {
  const f = await installedFixture(t);
  const before = await stat(f.catalogPath);
  const admissionRaw = await readFile(f.admissionPath, "utf8");
  let published = 0;
  const result = await refreshOfficialCatalog({ ...f, fetchRemote: async () => f.remote,
    validateNative() { throw new Error("unchanged base must not be revalidated separately"); },
    publishPicker: async ({ catalogPath }) => { published++; assert.equal(catalogPath, f.catalogPath); return { changed: true }; },
  });
  assert.equal(published, 1);
  assert.equal(result.changed, true);
  assert.equal(result.official_catalog_changed, false);
  assert.deepEqual(result.added, []);
  assert.equal((await stat(f.catalogPath)).ino, before.ino);
  assert.equal(await readFile(f.admissionPath, "utf8"), admissionRaw);
});

test("official additions update the admitted digest and publish the resulting picker", async (t) => {
  const f = await installedFixture(t);
  const remote = { models: [...f.remote.models, { slug: "gpt-future", visibility: "list" }] };
  let validated = false, published = false;
  const result = await refreshOfficialCatalog({ ...f, fetchRemote: async () => remote,
    validateNative: async (catalog) => { validated = true; assert.equal(catalog.models.at(-1).slug, "gpt-future"); return { ok: true }; },
    publishPicker: async ({ catalogPath }) => {
      published = true;
      assert.equal(validated, true);
      const updated = JSON.parse(await readFile(catalogPath, "utf8"));
      assert.equal(updated.models.at(-1).slug, "gpt-future");
      assert.deepEqual(updated.models.slice(0, f.catalog.models.length), f.catalog.models);
      return { changed: true };
    },
  });
  assert.equal(published, true);
  assert.deepEqual(result.added, ["gpt-future"]);
  const bytes = await readFile(f.catalogPath);
  const admission = JSON.parse(await readFile(f.admissionPath, "utf8"));
  assert.equal(admission.catalog_sha256, createHash("sha256").update(bytes).digest("hex"));
  assert.ok(admission.official_models.includes("gpt-future"));
});

test("refresh refuses an unadmitted preimage before network or picker publication", async (t) => {
  const f = await installedFixture(t);
  await writeFile(f.catalogPath, f.catalogRaw + " ");
  await assert.rejects(refreshOfficialCatalog({ ...f,
    fetchRemote() { throw new Error("unadmitted preimage must not fetch"); },
    publishPicker() { throw new Error("unadmitted preimage must not publish"); },
  }), { code: "CATALOG_PREIMAGE_UNADMITTED" });
});

test("failed native validation does not update admission or publish a picker", async (t) => {
  const f = await installedFixture(t);
  const admissionRaw = await readFile(f.admissionPath, "utf8");
  await assert.rejects(refreshOfficialCatalog({ ...f,
    fetchRemote: async () => ({ models: [...f.remote.models, { slug: "gpt-future" }] }),
    validateNative: async () => ({ ok: false }),
    publishPicker() { throw new Error("failed base validation must not publish"); },
  }), /Native Codex/);
  assert.equal(await readFile(f.catalogPath, "utf8"), f.catalogRaw);
  assert.equal(await readFile(f.admissionPath, "utf8"), admissionRaw);
});

test("official transport failure is distinguished from local publication failure", async (t) => {
  const f = await installedFixture(t);
  const transportError = new Error("offline");
  await assert.rejects(refreshOfficialCatalog({ ...f, fetchRemote: async () => { throw transportError; } }),
    (error) => error === transportError && error.refresh_phase === "official_catalog_fetch");
  const publicationError = new Error("picker-native-invalid");
  await assert.rejects(refreshOfficialCatalog({ ...f, fetchRemote: async () => f.remote,
    publishPicker: async () => { throw publicationError; },
  }), (error) => error === publicationError && error.refresh_phase === undefined);
});
