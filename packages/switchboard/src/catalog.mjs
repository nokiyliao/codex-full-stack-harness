import { createHash, randomBytes } from "node:crypto";
import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import { chmod, lstat, mkdir, open, readFile, rename, stat, writeFile } from "node:fs/promises";
import { promisify } from "node:util";

import {
  AGY_MODELS,
  EXTERNAL_MODELS,
  EXTERNAL_MULTI_AGENT_MODELS,
  WEB_MODELS,
  XAI_MODEL,
} from "./shared.mjs";
import {
  EXTERNAL_EFFECT_SCOPE,
  EXTERNAL_ORCHESTRATION_PROTOCOL,
} from "./external-admission.mjs";

const execFileAsync = promisify(execFile);

export const APP_PATH = "/Applications/ChatGPT.app";
// Consumers need the native payload, including process-identity comparisons.
export let CLI_PATH = existsSync(`${APP_PATH}/Contents/Resources/codex-cli`)
  ? `${APP_PATH}/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex`
  : `${APP_PATH}/Contents/Resources/codex`;
export const LEGACY_CATALOG_PATH = "/Users/nokiy/.codex-multi/models.json";
export const MODELS_DIR = "/Users/nokiy/.codex-multi/models";
export const CATALOG_PATH = `${MODELS_DIR}/admitted-models.json`;
export const ADMISSION_PATH = `${MODELS_DIR}/admission.json`;
export const RUNTIME_PATH = "/Users/nokiy/.codex-multi/switchboard/runtime/installation.json";
export const WORKFLOW_PATH = "/Users/nokiy/.codex-multi/switchboard/config/nokiy-workflows.json";

export async function configuredWorkflows() {
  try { return JSON.parse(await readFile(WORKFLOW_PATH, "utf8")); }
  catch (error) { if (error.code === "ENOENT") return null; throw error; }
}
export const EXPECTED_TEAM_ID = "2DC432GLL2";
export const AGY_GEMINI_CONTEXT_MODEL = "gemini-3.8-flash-high";
export const AGY_ARGON_CONTEXT_MODEL = "gemini-4-argon-high";
export const AGY_MILLION_CONTEXT_MODELS = Object.freeze([
  AGY_GEMINI_CONTEXT_MODEL,
  AGY_ARGON_CONTEXT_MODEL,
]);
export const AGY_GEMINI_CONTEXT_WINDOW = 1_000_000;
export const AGY_GEMINI_AUTO_COMPACT_LIMIT = 900_000;
export const SOL_CONTEXT_MODEL = "gpt-5.6-sol";
export const SOL_MAX_CONTEXT_WINDOW = 1_050_000;
export const SOL_CONFIGURED_CONTEXT_WINDOW = 391_045;
export const SOL_EFFECTIVE_CONTEXT_WINDOW = 371_492;
export const SOL_AUTO_COMPACT_LIMIT = 300_000;
export const CURRENT_SOL_CONTEXT_MODEL = "gpt-6.1-sol";
export const CURRENT_SOL_DEFAULT_CONTEXT_WINDOW = 500_000;
export const ASTRA_MODEL = "gpt-6-astra";
export const ASTRA_PICKER_VISIBILITY = "list";
export const WEB_AUTO_COMPACT_LIMIT = 250_000;
const WEB_MODEL_PROFILES = Object.freeze({
  "chatgpt-web/light": {
    display_name: "ChatGPT Web - Instant",
    description: "ChatGPT Web Instant through the isolated downstream provider route.",
    default_reasoning_level: "low",
    supported_reasoning_levels: [{ effort: "low", description: "ChatGPT Web - Instant" }],
    context_window: 333_579,
    max_context_window: 333_579,
  },
  "chatgpt-web/medium": {
    display_name: "ChatGPT Web - Medium",
    description: "ChatGPT Web Medium through the isolated downstream provider route.",
    default_reasoning_level: "medium",
    supported_reasoning_levels: [{ effort: "medium", description: "ChatGPT Web - Medium" }],
    context_window: 333_579,
    max_context_window: 333_579,
  },
  "chatgpt-web/high": {
    display_name: "ChatGPT Web - High",
    description: "ChatGPT Web High through the isolated downstream provider route.",
    default_reasoning_level: "high",
    supported_reasoning_levels: [{ effort: "high", description: "ChatGPT Web - High" }],
    context_window: 333_579,
    max_context_window: 333_579,
  },
  "chatgpt-web/extra-high": {
    display_name: "ChatGPT Web - Extra High",
    description: "Account-gated ChatGPT Web Extra High through the isolated downstream provider route.",
    default_reasoning_level: "xhigh",
    supported_reasoning_levels: [{ effort: "xhigh", description: "ChatGPT Web - Extra High" }],
    context_window: 333_579,
    max_context_window: 333_579,
  },
  "chatgpt-web/pro": {
    display_name: "ChatGPT Web - Pro",
    description: "Account-gated ChatGPT Pro through the isolated downstream provider route.",
    default_reasoning_level: "ultra",
    supported_reasoning_levels: [{ effort: "ultra", description: "ChatGPT Web - Pro" }],
    context_window: 336_579,
    max_context_window: 336_579,
  },
});
export const CLIENT_TOOL_CONTRACT_FIELDS = Object.freeze([
  "apply_patch_tool_type",
  "include_apps_usage_instructions",
  "include_plugin_usage_instructions",
  "include_skills_usage_instructions",
  "node_repl_auto_review_required",
  "node_repl_disabled",
  "shell_type",
  "tool_mode",
  "use_responses_lite",
  "web_search_tool_type",
]);

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

// Current ChatGPT/Codex CLI deserializes model_catalog_json with a required
// `supports_parallel_tool_calls` bool. `codex debug models --bundled` still
// omits it on official rows. Fill only when absent; do not override a
// bundled boolean.
export function withCliRequiredModelFields(model) {
  if (typeof model?.supports_parallel_tool_calls === "boolean") {
    return model;
  }
  return { ...model, supports_parallel_tool_calls: true };
}

// Codex's announced default can lead the installed binary's bundled metadata.
// Keep this per-model default in generation; preserve every other model field.
export function withOfficialDefaultContext(model) {
  return model.slug === CURRENT_SOL_CONTEXT_MODEL
    ? { ...model, context_window: CURRENT_SOL_DEFAULT_CONTEXT_WINDOW }
    : model;
}

function clientToolContract(model) {
  return Object.fromEntries(
    CLIENT_TOOL_CONTRACT_FIELDS.map((field) => [field, model?.[field] ?? null]),
  );
}

function officialToolContractAnchor(models) {
  const anchor = models.find((model) => model.slug === SOL_CONTEXT_MODEL);
  if (!anchor) throw new Error(`official catalog is missing tool-contract anchor ${SOL_CONTEXT_MODEL}`);
  return withCliRequiredModelFields(anchor);
}

function withCurrentClientToolContract(model, anchor) {
  return {
    ...model,
    ...clientToolContract(anchor),
  };
}

export function catalogCompatibility(models, currentOfficialModels) {
  if (!Array.isArray(models) || !Array.isArray(currentOfficialModels)) {
    return {
      official_anchor_contract_compatible: false,
      external_client_contract_compatible: false,
    };
  }
  const currentAnchorSource = currentOfficialModels.find((model) => model.slug === SOL_CONTEXT_MODEL);
  if (!currentAnchorSource) {
    return {
      official_anchor_contract_compatible: false,
      external_client_contract_compatible: false,
    };
  }
  const currentAnchor = withCliRequiredModelFields(currentAnchorSource);
  const admittedAnchor = models.find((model) => model.slug === SOL_CONTEXT_MODEL);
  const expectedContract = clientToolContract(currentAnchor);
  const officialAnchorCompatible = admittedAnchor
    && JSON.stringify(clientToolContract(admittedAnchor)) === JSON.stringify(expectedContract);
  const webSol = models.find(model => model.slug === "gpt-6-sol");
  const externalCompatible = externalBatchComplete(models)
    && EXTERNAL_MODELS.every((slug) => {
      const model = models.find((candidate) => candidate.slug === slug);
      const webOnSol = ["chatgpt-web/extra-high", "chatgpt-web/pro"].includes(slug)
        && model?.default_reasoning_level === "max";
      const expected = webOnSol ? webSol && clientToolContract(webSol) : expectedContract;
      if (!model || !expected || JSON.stringify(clientToolContract(model)) !== JSON.stringify(expected)
          || (webOnSol && (model.model_messages?.instructions_template !== webSol.model_messages?.instructions_template
            || model.base_instructions !== webSol.base_instructions
            || model.supported_reasoning_levels?.[0]?.effort !== "max"))) {
        return false;
      }
      const serialProvider = AGY_MODELS.includes(slug);
      return model.supported_in_api === true
        && model.supports_search_tool === true
        && model.supports_parallel_tool_calls === !serialProvider
        && Array.isArray(model.experimental_supported_tools)
        && Array.isArray(model.input_modalities)
        && model.input_modalities.includes("text")
        && Number.isFinite(model.context_window)
        && model.context_window > 0
        && Number.isFinite(model.max_context_window)
        && model.max_context_window > 0;
    });
  return {
    official_anchor_contract_compatible: officialAnchorCompatible === true,
    external_client_contract_compatible: externalCompatible,
  };
}

async function execText(command, args) {
  try {
    const { stdout, stderr } = await execFileAsync(command, args, {
      timeout: 60_000,
      maxBuffer: 64 * 1024 * 1024,
    });
    return `${stdout}${stderr}`;
  } catch (error) {
    error.inspection_phase = `${command.split("/").at(-1)} ${args.filter((arg) => !arg.includes("/")).join(" ")}`;
    throw error;
  }
}

export async function atomicWriteJson(filePath, value, mode = 0o600) {
  await mkdir(new URL(".", `file://${filePath}`).pathname, { recursive: true, mode: 0o700 });
  const temporary = `${filePath}.tmp-${process.pid}-${Date.now()}`;
  const bytes = Buffer.from(`${JSON.stringify(value, null, 2)}\n`);
  await writeFile(temporary, bytes, { mode });
  await rename(temporary, filePath);
  await chmod(filePath, mode);
  return { bytes: bytes.length, sha256: sha256(bytes) };
}

async function inspectionBatch(checks) {
  // Drain the batch before retrying, and never hide a verification rejection behind a timeout.
  const results = await Promise.allSettled(checks);
  const failures = results.filter((result) => result.status === "rejected").map((result) => result.reason);
  if (failures.length) throw failures.find((error) => Number.isInteger(error.code) && !error.killed) || failures[0];
  return results.map((result) => result.value);
}

function cliLayoutError(code, message, cause) {
  return Object.assign(new Error(message, { cause }), { code });
}

// Only the two known layouts inside this app are candidates. An incomplete new
// package is not a reason to use a possibly stale legacy payload.
export async function resolveOfficialCliPaths(appPath = APP_PATH) {
  const resources = `${appPath}/Contents/Resources`;
  const packageRoot = `${resources}/codex-cli`;
  const launcher = `${packageRoot}/bin/codex`;
  const packaged = `${packageRoot}/CodexCLI.app/Contents/MacOS/codex`;
  const legacy = `${resources}/codex`;
  async function fileStatus(path) {
    try {
      return await lstat(path);
    } catch (error) {
      if (error.code === "ENOENT") return null;
      throw cliLayoutError("CLI_LAYOUT_INVALID", `Cannot inspect packaged CLI: ${path}`, error);
    }
  }
  const [launcherStat, packagedStat, packageStat] = await Promise.all([
    fileStatus(launcher), fileStatus(packaged), fileStatus(packageRoot),
  ]);
  const isExecutable = (file) => file?.isFile() && (file.mode & 0o111) !== 0;
  const isMachO = async (path) => {
    let handle;
    try {
      handle = await open(path, "r");
      const header = Buffer.alloc(4);
      const { bytesRead } = await handle.read(header, 0, 4, 0);
      return bytesRead === 4 && ["feedface", "feedfacf", "cefaedfe", "cffaedfe", "cafebabe", "bebafeca", "cafebabf", "bfbafeca"].includes(header.toString("hex"));
    } catch (error) {
      throw cliLayoutError("CLI_LAYOUT_INVALID", `Cannot read packaged CLI: ${path}`, error);
    } finally {
      await handle?.close();
    }
  };
  if (launcherStat || packagedStat || packageStat) {
    if (!packageStat?.isDirectory() || !isExecutable(launcherStat) || !isExecutable(packagedStat)) {
      throw cliLayoutError("CLI_LAYOUT_INVALID", "Incomplete or non-executable packaged CLI layout");
    }
    let script;
    try {
      script = await readFile(launcher, "utf8");
    } catch (error) {
      throw cliLayoutError("CLI_LAYOUT_INVALID", "Cannot read packaged CLI launcher", error);
    }
    if (!script.startsWith("#!")
      || !/^\s*exec\s+[^\n#]*\.\.\/CodexCLI\.app\/Contents\/MacOS\/codex(?:["']|\s|$)/m.test(script)
      || !await isMachO(packaged)) {
      throw cliLayoutError("CLI_LAYOUT_INVALID", "Malformed packaged CLI launcher or payload");
    }
    return { launch_path: launcher, cli_path: packaged };
  }
  const legacyStat = await fileStatus(legacy);
  if (!legacyStat) throw cliLayoutError("CLI_LAYOUT_MISSING", "Official CLI package is missing");
  if (!isExecutable(legacyStat) || !await isMachO(legacy)) {
    throw cliLayoutError("CLI_LAYOUT_INVALID", "Malformed legacy CLI payload");
  }
  return { launch_path: legacy, cli_path: legacy };
}

export async function inspectOfficialInstallation() {
  const { cli_path: cliPath } = await resolveOfficialCliPaths();
  CLI_PATH = cliPath;
  const [
    infoRaw,
    signatureRaw,
    cliSignatureRaw,
    cliBytes,
    catalogRaw,
    cliStat,
    infoStat,
    cliVersionRaw,
  ] = await inspectionBatch([
    readFile(`${APP_PATH}/Contents/Info.plist`),
    execText("/usr/bin/codesign", ["-dv", "--verbose=4", APP_PATH]),
    execText("/usr/bin/codesign", ["-dv", "--verbose=4", cliPath]),
    readFile(cliPath),
    execText(cliPath, ["debug", "models", "--bundled"]),
    stat(cliPath),
    stat(`${APP_PATH}/Contents/Info.plist`),
    execText(cliPath, ["--version"]),
  ]);
  await inspectionBatch([
    execText("/usr/bin/codesign", ["--verify", "--deep", "--strict", "--verbose=2", APP_PATH]),
    execText("/usr/sbin/spctl", ["--assess", "--type", "execute", "--verbose=4", APP_PATH]),
    execText("/usr/bin/codesign", ["--verify", "--strict", "--verbose=2", cliPath]),
  ]);
  const version = await execText("/usr/bin/defaults", [
    "read",
    `${APP_PATH}/Contents/Info`,
    "CFBundleShortVersionString",
  ]);
  const build = await execText("/usr/bin/defaults", [
    "read",
    `${APP_PATH}/Contents/Info`,
    "CFBundleVersion",
  ]);
  const teamId = /TeamIdentifier=([^\s]+)/.exec(signatureRaw)?.[1] || null;
  const cliTeamId = /TeamIdentifier=([^\s]+)/.exec(cliSignatureRaw)?.[1] || null;
  const notarized = /Notarization Ticket=stapled/.test(signatureRaw);
  const catalog = JSON.parse(catalogRaw);
  if (!Array.isArray(catalog.models) || !catalog.models.length) {
    throw new Error("bundled model catalog is empty or invalid");
  }
  return {
    cli_path: cliPath,
    app_version: version.trim(),
    app_build: build.trim(),
    team_id: teamId,
    cli_team_id: cliTeamId,
    notarized,
    bundle_id: /Identifier=([^\s]+)/.exec(signatureRaw)?.[1] || null,
    cli_version: cliVersionRaw.trim(),
    cli_sha256: sha256(cliBytes),
    cli_size: cliStat.size,
    cli_mtime_ms: Math.trunc(cliStat.mtimeMs),
    info_plist_sha256: sha256(infoRaw),
    info_plist_size: infoStat.size,
    info_plist_mtime_ms: Math.trunc(infoStat.mtimeMs),
    bundled_catalog: catalog,
    bundled_catalog_sha256: sha256(Buffer.from(`${JSON.stringify(catalog)}\n`)),
  };
}

function externalCatalogEntries(legacyCatalog, officialCatalog) {
  const bySlug = new Map(legacyCatalog.models.map((model) => [model.slug, model]));
  const toolContractAnchor = officialToolContractAnchor(officialCatalog.models);
  return EXTERNAL_MODELS.map((slug, priority) => {
    const webProfile = WEB_MODEL_PROFILES[slug];
    let source = webProfile
      ? {
          ...structuredClone(toolContractAnchor),
          ...webProfile,
          slug,
          effective_context_window_percent: 85,
          auto_compact_token_limit: WEB_AUTO_COMPACT_LIMIT,
        }
      : bySlug.get(slug) || bySlug.get(`grok-agy-${slug}`)
        || (slug === XAI_MODEL ? bySlug.get("grok-4.6") : undefined);
    if (!source) throw new Error(`legacy catalog is missing required external model ${slug}`);
    if (slug === XAI_MODEL) source = {
      ...source, slug, display_name: "Grok 4.7",
      description: "xAI Grok 4.7 through the official Grok CLI OAuth session.",
    };
    const isAgy = AGY_MODELS.includes(slug);
    if (isAgy) {
      source = {
        ...structuredClone(source),
        slug,
        display_name: slug,
        description: slug,
      };
    }
    const isWeb = WEB_MODELS.includes(slug);
    const serialProvider = isAgy;
    return {
      ...withCurrentClientToolContract(structuredClone(source), toolContractAnchor),
      priority: priority + 1,
      // In Codex this gates harness-side deferred tool discovery, not provider-hosted web search.
      supports_search_tool: true,
      supports_parallel_tool_calls: !serialProvider,
      experimental_supported_tools: [],
      ...(EXTERNAL_MULTI_AGENT_MODELS.includes(slug) ? { multi_agent_version: "v2" } : {}),
      ...(isWeb ? { multi_agent_version: "v1" } : {}),
      ...(isWeb ? { visibility: ["chatgpt-web/extra-high", "chatgpt-web/pro"].includes(slug) ? "list" : "hide" } : {}),
      ...(slug === XAI_MODEL ? { tool_mode: "code_mode_only" } : {}),
      service_tiers: [],
      additional_speed_tiers: [],
      input_modalities: isAgy || isWeb ? ["text", "image"] : ["text"],
      ...(isAgy ? { supports_image_detail_original: false } : {}),
      ...(AGY_MILLION_CONTEXT_MODELS.includes(slug) ? {
        context_window: AGY_GEMINI_CONTEXT_WINDOW,
        max_context_window: AGY_GEMINI_CONTEXT_WINDOW,
        effective_context_window_percent: 95,
        auto_compact_token_limit: AGY_GEMINI_AUTO_COMPACT_LIMIT,
        truncation_policy: { mode: "tokens", limit: 10_000 },
      } : {}),
    };
  });
}

// Official models added by the admitted remote refresh (absent from the app bundle, such as
// gpt-6.1-terra) stay admitted across re-admission, so workflows that use them still resolve.
export async function admittedOfficialAdditions(bundledCatalog) {
  let catalog, admission;
  try {
    [catalog, admission] = await Promise.all([CATALOG_PATH, ADMISSION_PATH]
      .map(async (path) => JSON.parse(await readFile(path, "utf8"))));
  } catch (error) {
    if (error.code === "ENOENT") return [];
    throw error;
  }
  if (admission?.status !== "ADMITTED" || !Array.isArray(admission.official_models)
    || !Array.isArray(catalog?.models)) return [];
  const bundled = new Set(bundledCatalog.models.map((model) => model.slug));
  const added = new Set(admission.official_models.filter((slug) => !bundled.has(slug)
    && !EXTERNAL_MODELS.includes(slug)));
  return catalog.models.filter((model) => added.has(model.slug));
}

export async function buildCandidateCatalog(official, additions) {
  additions ??= await admittedOfficialAdditions(official.bundled_catalog);
  const legacy = JSON.parse(await readFile(LEGACY_CATALOG_PATH, "utf8"));
  const external = externalCatalogEntries(legacy, official.bundled_catalog);
  const officialModels = structuredClone(official.bundled_catalog.models)
    .map(withCliRequiredModelFields).map(withOfficialDefaultContext);
  const sol = officialModels.find((model) => model.slug === SOL_CONTEXT_MODEL);
  if (!sol) throw new Error(`official catalog is missing required model ${SOL_CONTEXT_MODEL}`);
  sol.max_context_window = SOL_MAX_CONTEXT_WINDOW;
  const astra = officialModels.find((model) => model.slug === ASTRA_MODEL);
  if (astra) astra.visibility = ASTRA_PICKER_VISIBILITY;
  const candidate = { models: [...external, ...officialModels, ...structuredClone(additions)] };
  const copiedOfficial = candidate.models.slice(external.length, external.length + officialModels.length);
  if (JSON.stringify(copiedOfficial) !== JSON.stringify(officialModels)) {
    throw new Error("official model objects do not match the admitted context override projection");
  }
  const profiles = await configuredWorkflows();
  if (!profiles) return candidate;
  const { withDirectCatalog } = await import("./nokiy-direct-route.mjs");
  return withDirectCatalog(candidate, profiles);
}

export function externalBatchComplete(models) {
  return EXTERNAL_MODELS.every((slug) => models.some((model) => model.slug === slug))
    && EXTERNAL_MULTI_AGENT_MODELS.every(
      (slug) => models.some((model) => model.slug === slug && model.multi_agent_version === "v2"),
    );
}

export async function ensureRuntimeManifest() {
  try {
    const existing = JSON.parse(await readFile(RUNTIME_PATH, "utf8"));
    if (/^[A-Za-z0-9_-]{32,}$/.test(existing.installation_capability || "")) {
      await chmod(RUNTIME_PATH, 0o600);
      return existing;
    }
  } catch {
    // First installation creates a new capability below.
  }
  const runtime = {
    schema_version: "codex_inference_switchboard_runtime_v1",
    installation_capability: randomBytes(32).toString("base64url"),
    host: "127.0.0.1",
    port: 43130,
  };
  await atomicWriteJson(RUNTIME_PATH, runtime, 0o600);
  return runtime;
}

export async function admitCurrentInstallation({ write = true, liveTests = null } = {}) {
  const official = await inspectOfficialInstallation();
  if (official.team_id !== EXPECTED_TEAM_ID
    || official.cli_team_id !== EXPECTED_TEAM_ID
    || official.bundle_id !== "com.openai.codex"
    || !official.notarized) {
    throw new Error("official ChatGPT.app signature identity or notarization did not pass admission");
  }
  const runtime = await ensureRuntimeManifest();
  const additions = await admittedOfficialAdditions(official.bundled_catalog);
  const candidate = await buildCandidateCatalog(official, additions);
  const catalogBytes = Buffer.from(`${JSON.stringify(candidate, null, 2)}\n`);
  const testsPassed = liveTests?.ok === true;
  const admission = {
    schema_version: "codex_inference_switchboard_admission_v2",
    status: testsPassed ? "ADMITTED" : "STAGED",
    admitted_at: new Date().toISOString(),
    authority_effect: "local_inference_routing_only",
    protocol_compatibility: {
      external_model_orchestration: EXTERNAL_ORCHESTRATION_PROTOCOL,
    },
    policy: {
      external_models: "allow",
      effect_scope: EXTERNAL_EFFECT_SCOPE,
    },
    app_path: APP_PATH,
    app_version: official.app_version,
    app_build: official.app_build,
    team_id: official.team_id,
    cli_team_id: official.cli_team_id,
    bundle_id: official.bundle_id,
    notarized: official.notarized,
    cli_version: official.cli_version,
    cli_path: official.cli_path,
    cli_sha256: official.cli_sha256,
    cli_size: official.cli_size,
    cli_mtime_ms: official.cli_mtime_ms,
    info_plist_sha256: official.info_plist_sha256,
    info_plist_size: official.info_plist_size,
    info_plist_mtime_ms: official.info_plist_mtime_ms,
    bundled_catalog_sha256: official.bundled_catalog_sha256,
    catalog_path: CATALOG_PATH,
    catalog_sha256: sha256(catalogBytes),
    official_models: [...official.bundled_catalog.models, ...additions].map((model) => model.slug),
    external_models: [...EXTERNAL_MODELS],
    local_context_overrides: [{
      model: SOL_CONTEXT_MODEL,
      max_context_window: SOL_MAX_CONTEXT_WINDOW,
      configured_context_window: SOL_CONFIGURED_CONTEXT_WINDOW,
      effective_context_window_percent: 95,
      effective_context_window: SOL_EFFECTIVE_CONTEXT_WINDOW,
      auto_compact_token_limit: SOL_AUTO_COMPACT_LIMIT,
    }],
    local_presentation_overrides: official.bundled_catalog.models.some((model) => model.slug === ASTRA_MODEL)
      ? [{ model: ASTRA_MODEL, visibility: ASTRA_PICKER_VISIBILITY }]
      : [],
    external_batch_admitted: testsPassed,
    live_tests: liveTests,
    capability_path: `/${runtime.installation_capability}/backend-api/codex/v1`,
  };
  const profiles = await configuredWorkflows();
  if (profiles) {
    // Re-admission must retain the exact installed workflow, not silently drop
    // its picker aliases or accept a changed native execution host.
    const previous = JSON.parse(await readFile(ADMISSION_PATH, "utf8"));
    const installed = previous.local_nokiy_direct;
    if (!installed?.admitted || JSON.stringify(profiles) !== JSON.stringify(installed.profiles)
      || sha256(await readFile(installed.host.script)) !== installed.host.sha256) {
      throw new Error("Installed Nokiy workflow changed; use a source-bound workflow update");
    }
    admission.local_nokiy_direct = installed;
    if (installed.host.contract && sha256(await readFile(installed.host.contract.path)) !== installed.host.contract.sha256) {
      throw new Error("Installed Nokiy intake contract changed; use a source-bound workflow update");
    }
  }
  if (write) {
    await atomicWriteJson(CATALOG_PATH, candidate, 0o600);
    await atomicWriteJson(ADMISSION_PATH, admission, 0o600);
  }
  return { official, runtime, candidate, admission };
}

export async function verifyAdmission({ current: providedCurrent = null } = {}) {
  const current = providedCurrent || await inspectOfficialInstallation();
  let admissionRaw;
  let catalog;
  try {
    admissionRaw = await readFile(ADMISSION_PATH, "utf8");
  } catch (error) {
    throw Object.assign(new Error("admission file could not be read"), {
      code: "ADMISSION_FILE_UNREADABLE",
      cause: error,
    });
  }
  try {
    catalog = await readFile(CATALOG_PATH);
  } catch (error) {
    throw Object.assign(new Error("admitted catalog could not be read"), {
      code: "CATALOG_FILE_UNREADABLE",
      cause: error,
    });
  }
  let admission;
  let parsedCatalog;
  try {
    admission = JSON.parse(admissionRaw);
  } catch (error) {
    throw Object.assign(new Error("admission file is not valid JSON"), {
      code: "ADMISSION_JSON_INVALID",
      cause: error,
    });
  }
  try {
    parsedCatalog = JSON.parse(catalog);
  } catch (error) {
    throw Object.assign(new Error("admitted catalog is not valid JSON"), {
      code: "CATALOG_JSON_INVALID",
      cause: error,
    });
  }
  const predicates = {
    schema_supported: admission.schema_version === "codex_inference_switchboard_admission_v2",
    status_admitted: admission.status === "ADMITTED",
    team_id_matches: current.team_id === admission.team_id && current.team_id === EXPECTED_TEAM_ID,
    cli_team_id_matches: current.cli_team_id === admission.cli_team_id
      && current.cli_team_id === EXPECTED_TEAM_ID,
    bundle_id_matches: current.bundle_id === admission.bundle_id && current.bundle_id === "com.openai.codex",
    notarized: current.notarized === true,
    protocol_compatible: admission.protocol_compatibility?.external_model_orchestration
      === EXTERNAL_ORCHESTRATION_PROTOCOL,
    policy_allows_external: admission.policy?.external_models === "allow",
    effect_scope_bounded: admission.policy?.effect_scope === EXTERNAL_EFFECT_SCOPE
      && admission.authority_effect === EXTERNAL_EFFECT_SCOPE,
    catalog_sha256_matches: sha256(catalog) === admission.catalog_sha256,
    official_anchor_contract_compatible: false,
    external_client_contract_compatible: false,
    local_context_override_matches: false,
    local_presentation_override_matches: false,
    external_batch_complete: false,
  };
  const officialRows = parsedCatalog.models.filter((model) => admission.official_models.includes(model.slug));
  const expectedOfficialRows = structuredClone(current.bundled_catalog.models)
    .map(withCliRequiredModelFields).map(withOfficialDefaultContext);
  const expectedSol = expectedOfficialRows.find((model) => model.slug === SOL_CONTEXT_MODEL);
  if (expectedSol) expectedSol.max_context_window = SOL_MAX_CONTEXT_WINDOW;
  const expectedAstra = expectedOfficialRows.find((model) => model.slug === ASTRA_MODEL);
  if (expectedAstra) expectedAstra.visibility = ASTRA_PICKER_VISIBILITY;
  const compatibility = catalogCompatibility(parsedCatalog.models, current.bundled_catalog.models);
  predicates.official_anchor_contract_compatible = compatibility.official_anchor_contract_compatible;
  predicates.external_client_contract_compatible = compatibility.external_client_contract_compatible;
  predicates.local_context_override_matches = JSON.stringify(admission.local_context_overrides) === JSON.stringify([{
    model: SOL_CONTEXT_MODEL,
    max_context_window: SOL_MAX_CONTEXT_WINDOW,
    configured_context_window: SOL_CONFIGURED_CONTEXT_WINDOW,
    effective_context_window_percent: 95,
    effective_context_window: SOL_EFFECTIVE_CONTEXT_WINDOW,
    auto_compact_token_limit: SOL_AUTO_COMPACT_LIMIT,
  }]);
  predicates.local_presentation_override_matches = JSON.stringify(admission.local_presentation_overrides)
    === JSON.stringify(expectedAstra ? [{ model: ASTRA_MODEL, visibility: ASTRA_PICKER_VISIBILITY }] : []);
  predicates.external_batch_complete = externalBatchComplete(parsedCatalog.models);
  const false_predicates = Object.entries(predicates)
    .filter(([, value]) => !value)
    .map(([key]) => key);
  const installation_observations = {
    app_version_matches: current.app_version === admission.app_version,
    app_build_matches: current.app_build === admission.app_build,
    cli_sha256_matches: current.cli_sha256 === admission.cli_sha256,
    cli_stat_matches: current.cli_size === admission.cli_size
      && current.cli_mtime_ms === admission.cli_mtime_ms,
    info_plist_stat_matches: current.info_plist_size === admission.info_plist_size
      && current.info_plist_mtime_ms === admission.info_plist_mtime_ms,
  };
  const catalog_observations = {
    bundled_catalog_matches: current.bundled_catalog_sha256 === admission.bundled_catalog_sha256,
    official_models_deep_equal: JSON.stringify(officialRows) === JSON.stringify(expectedOfficialRows),
  };
  return {
    ok: false_predicates.length === 0,
    status: false_predicates.length === 0 ? "ADMITTED" : "RESTART_REQUIRED",
    predicates,
    installation_observations,
    catalog_observations,
    false_predicates,
    admission,
    current: {
      app_version: current.app_version,
      app_build: current.app_build,
      team_id: current.team_id,
      cli_sha256: current.cli_sha256,
      bundled_catalog_sha256: current.bundled_catalog_sha256,
    },
  };
}
