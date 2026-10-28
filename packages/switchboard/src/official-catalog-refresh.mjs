import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import { ADMISSION_PATH, CATALOG_PATH, CLI_PATH, CLIENT_TOOL_CONTRACT_FIELDS, atomicWriteJson, withCliRequiredModelFields } from "./catalog.mjs";
import { alignWorkflowContext, publishPickerCatalog, validateNativePickerCatalog } from "./picker-catalog.mjs";
import { EXTERNAL_MODELS, XAI_MODEL, routeForModel } from "./shared.mjs";

const run = promisify(execFile);
const digest = bytes => createHash("sha256").update(bytes).digest("hex");
const WEB_CODEX_MODEL = "gpt-6-sol";

function webWithSolTemplate(model, template) {
  return {
    ...model,
    ...Object.fromEntries(CLIENT_TOOL_CONTRACT_FIELDS.map(field => [field, template[field] ?? null])),
    base_instructions: template.base_instructions,
    model_messages: structuredClone(template.model_messages),
    supports_parallel_tool_calls: template.supports_parallel_tool_calls,
    default_reasoning_level: "max",
    supported_reasoning_levels: [{ effort: "max", description: model.display_name }],
  };
}

export function mergeOfficialAdditions(catalog, admission, remote) {
  if (!Array.isArray(remote?.models) || !remote.models.length) throw new Error("Official model catalog is empty or invalid");
  const seen = new Set();
  for (const model of remote.models) {
    if (typeof model.slug !== "string" || !model.slug || seen.has(model.slug)
      || EXTERNAL_MODELS.includes(model.slug)) throw new Error("Invalid or conflicting official model identity");
    seen.add(model.slug);
  }
  const available = new Set([...seen, ...catalog.models.map(model => model.slug)]);
  const replacements = { "gpt-5.6-sol": "gpt-6-sol", "gpt-5.6-luna": "gpt-6-luna" };
  const template = catalog.models.find(model => model.slug === WEB_CODEX_MODEL)
    || remote.models.find(model => model.slug === WEB_CODEX_MODEL);
  if (!template?.model_messages?.instructions_template) throw new Error("Official GPT-6 Sol template is unavailable");
  const models = catalog.models.map(model => {
    if (["chatgpt-web/extra-high", "chatgpt-web/pro"].includes(model.slug)) {
      return webWithSolTemplate(model, withCliRequiredModelFields(template));
    }
    if (replacements[model.slug] && available.has(replacements[model.slug])) {
      return { ...model, visibility: "hide" };
    }
    if (["chatgpt-web/light", "chatgpt-web/medium", "chatgpt-web/high", "grok-4.6"].includes(model.slug)) {
      return { ...model, visibility: "hide" };
    }
    return model;
  });
  if (!models.some(model => model.slug === XAI_MODEL)) {
    const previous = models.find(model => model.slug === "grok-4.6");
    if (!previous) throw new Error("Existing Grok tool contract is unavailable");
    models.push({ ...previous, slug: XAI_MODEL, display_name: "Grok 4.7", visibility: "list",
      description: "xAI Grok 4.7 through the official Grok CLI OAuth session." });
  }
  const existing = new Set(models.map(model => model.slug));
  const added = remote.models.filter(model => !existing.has(model.slug)).map(withCliRequiredModelFields);
  const aligned = alignWorkflowContext({ ...catalog, models: [...models, ...added] }, admission.local_nokiy_direct?.profiles);
  // Existing rows are operator-owned overlays, not disposable upstream copies.
  return {
    catalog: aligned,
    admission: { ...admission, external_models: [...EXTERNAL_MODELS], official_models: [...new Set([...admission.official_models, ...added.map(model => model.slug)])] },
    added: added.map(model => model.slug),
    changed: JSON.stringify(aligned.models) !== JSON.stringify(catalog.models),
  };
}

export async function routeWithCurrentOfficialModels(model, initialModels, readAdmission) {
  try { return routeForModel(model, initialModels); } catch (error) {
    if (error.code !== "MODEL_ROUTE_UNAVAILABLE") throw error;
    const current = await readAdmission();
    if (current?.status !== "ADMITTED" || !Array.isArray(current.official_models)) throw error;
    return routeForModel(model, new Set(current.official_models));
  }
}

async function fetchCurrentOfficialCatalog() {
  const auth = JSON.parse(await readFile(join(process.env.CODEX_HOME || join(homedir(), ".codex"), "auth.json"), "utf8"));
  if (!auth.tokens?.access_token || !auth.tokens?.account_id) throw new Error("Current ChatGPT authentication unavailable");
  const { stdout: version } = await run(CLI_PATH, ["--version"], { timeout: 10000 });
  const response = await fetch("https://chatgpt.com/backend-api/codex/models?client_version=" + encodeURIComponent(version.trim().split(" ").at(-1)), {
    headers: { Authorization: "Bearer " + auth.tokens.access_token, "ChatGPT-Account-Id": auth.tokens.account_id },
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error(`Official model refresh returned HTTP ${response.status}`);
  return response.json();
}

export async function refreshOfficialCatalog({
  catalogPath = CATALOG_PATH,
  admissionPath = ADMISSION_PATH,
  fetchRemote = fetchCurrentOfficialCatalog,
  validateNative = validateNativePickerCatalog,
  publishPicker = publishPickerCatalog,
} = {}) {
  const [catalogRaw, admissionRaw] = await Promise.all([
    readFile(catalogPath, "utf8"), readFile(admissionPath, "utf8"),
  ]);
  const admission = JSON.parse(admissionRaw);
  if (admission.status !== "ADMITTED") throw new Error("Model refresh requires the existing admitted installation");
  if (admission.catalog_sha256 !== digest(catalogRaw)) {
    throw Object.assign(new Error("Model refresh requires the exact admitted catalog bytes"), { code: "CATALOG_PREIMAGE_UNADMITTED" });
  }
  let remote;
  try { remote = await fetchRemote(); }
  catch (error) { error.refresh_phase = "official_catalog_fetch"; throw error; }
  const merged = mergeOfficialAdditions(JSON.parse(catalogRaw), admission, remote);
  if (!merged.changed) {
    const picker = await publishPicker({ catalogPath });
    return { changed: picker.changed, official_catalog_changed: false, added: [], picker_catalog: picker };
  }
  const native = await validateNative(merged.catalog);
  if (native?.ok !== true) throw new Error("Native Codex did not load the candidate model catalog");
  if (await readFile(catalogPath, "utf8") !== catalogRaw || await readFile(admissionPath, "utf8") !== admissionRaw) {
    throw new Error("Model catalog changed during refresh; no update applied");
  }
  merged.admission.catalog_sha256 = digest(Buffer.from(JSON.stringify(merged.catalog, null, 2) + "\n"));
  merged.admission.official_refresh_at = new Date().toISOString();
  merged.admission.official_refresh_added = merged.added;
  await atomicWriteJson(catalogPath, merged.catalog);
  await atomicWriteJson(admissionPath, merged.admission);
  const picker = await publishPicker({ catalogPath });
  return { changed: true, official_catalog_changed: true, added: merged.added, picker_catalog: picker };
}
