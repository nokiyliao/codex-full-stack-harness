import { createHash } from "node:crypto";
import { ASTRA_MODEL, withCliRequiredModelFields } from "./catalog.mjs";
import { SwitchboardError } from "./shared.mjs";
import { nativeToolProjection } from "./nokiy-native-tool.mjs";
import { DEFAULT_PROFILES, efficientNativeLoop, resolveWorkflowProfiles } from "./nokiy-workflow-profiles.mjs";
import { coordinatorOnly, coordinatorContract } from "./nokiy-coordinator-policy.mjs";

const selectedProfiles = (profiles, catalog) => {
  try { return resolveWorkflowProfiles(profiles ?? DEFAULT_PROFILES, catalog); }
  catch (error) {
    throw new SwitchboardError("NOKIY_WORKFLOW_PROFILE_INVALID", error.message, 400);
  }
};

const SUBAGENT_NAMES = new Set(["spawn_agent", "send_input", "close_agent", "resume_agent"]);
const EXEC_NAMES = new Set(["exec", "exec_command"]);
const isNokiyAlias = (alias) => typeof alias === "string"
  && (alias === "nokiy" || alias.startsWith("nokiy-") || alias.startsWith("nokiy/"));

function members(tools) {
  return (Array.isArray(tools) ? tools : []).flatMap((tool) =>
    tool?.type === "namespace" ? members(tool.tools) : [tool]);
}

function advertisedTools(body) {
  return [
    ...members(body.tools),
    ...(Array.isArray(body.input) ? body.input : [])
      .filter((item) => item?.type === "additional_tools" || item?.type === "tool_search_output")
      .flatMap((item) => members(item.tools)),
  ];
}

function withoutSubagents(tools) {
  return tools.flatMap((tool) => {
    if (tool?.type === "namespace" && ["collaboration", "agents"].includes(tool.name)) return [];
    if (SUBAGENT_NAMES.has(tool?.name)) return [];
    if (tool?.type !== "namespace" || !Array.isArray(tool.tools)) return [tool];
    const filtered = withoutSubagents(tool.tools);
    return filtered.length ? [{ ...tool, tools: filtered }] : [];
  });
}

const withoutSubagentItems = (input) => input.map((item) =>
  (item?.type === "additional_tools" || item?.type === "tool_search_output") && Array.isArray(item.tools)
    ? { ...item, tools: withoutSubagents(item.tools) } : item);

function missingExecDiagnostic(body, reason = "missing_native_exec") {
  const advertised = advertisedTools(body);
  return { reason, advertised_tool_count: advertised.length,
    advertised_tools: advertised.slice(0, 64).map((tool) => String(tool?.type ?? "?") + ":" + String(tool?.name ?? "?")),
    tools_present: Array.isArray(body.tools),
    chained: typeof body.previous_response_id === "string" && body.previous_response_id !== "", lite: isLiteRequest(body) };
}

function coordinatorFallback(body, profile, wireEffort, diagnostic, answerOnly = false) {
  const projected = { ...body, model: profile.coordinator.model,
    ...(Array.isArray(body.tools) ? { tools: withoutSubagents(body.tools) } : {}),
    ...(Array.isArray(body.input) ? { input: withoutSubagentItems(body.input) } : {}),
    ...(wireEffort === null ? {} : { reasoning: { ...body.reasoning, effort: wireEffort } }) };
  if (SUBAGENT_NAMES.has(body.tool_choice?.name) || SUBAGENT_NAMES.has(body.tool_choice)
      || (body.tool_choice === "required" && advertisedTools(projected).length === 0)) projected.tool_choice = "auto";
  if (answerOnly) projected.tool_choice = "none";
  return { body: projected, nativeTool: null, requestedAlias: body.model, profile, worker: null,
    wireEffort, nativeEffortObserved: null, dispatch: "none", coordinatorFallback: true,
    fallbackAnswerOnly: answerOnly, nativeToolMissing: diagnostic };
}

// Responses Lite (the Codex default for these models) sends no top-level instructions or
// tools: both travel as conversation prefix items. A remote compaction renders top-level
// instructions/tools unlike an executing turn, so a top-level Nokiy contract made every
// compaction miss the prompt cache (0% of 300-400K tokens) while native compactions hit
// 96-99%. Lite requests therefore carry the Nokiy tools inside the leading additional_tools
// item and the contract as a developer message after the native prefix. Only full requests
// receive them; chained deltas inherit both from the stored upstream context.
const LITE_PREFIX_TYPES = new Set(["additional_tools", "tool_search_output"]);
export const isLiteRequest = (body) => body?.tools === undefined && body?.instructions === undefined;

// The desktop composer forks each finished thread to predict the user's next message
// (client_metadata.prediction_source_thread_id). The request carries a response schema,
// which never shares the prompt cache with the thread, so on a Nokiy thread every
// prediction re-read the whole 120-360K-token conversation uncached. A next-message guess
// needs the recent turns: keep the native prefix and a tail starting at a user message.
const PREDICTION_TAIL_BYTES = Number(process.env.SWITCHBOARD_PREDICTION_TAIL_BYTES ?? 256 * 1024);
// App-server metadata reaches the request inside the x-codex-turn-metadata JSON blob, not as
// top-level client_metadata keys.
export function isPredictionRequest(body) {
  const metadata = body?.client_metadata;
  if (!metadata || typeof metadata !== "object") return false;
  if (typeof metadata.prediction_source_thread_id === "string") return true;
  const blob = metadata["x-codex-turn-metadata"];
  if (typeof blob !== "string" || !blob.includes("prediction")) return false;
  try {
    const turn = JSON.parse(blob);
    return typeof turn?.prediction_source_thread_id === "string" || turn?.thread_source === "composer_predictions";
  } catch {
    return false;
  }
}

export function trimPredictionHistory(input, maxBytes = PREDICTION_TAIL_BYTES) {
  if (!Array.isArray(input) || !(maxBytes > 0)) return null;
  let prefix = 0;
  while (prefix < input.length && LITE_PREFIX_TYPES.has(input[prefix]?.type)) prefix += 1;
  if (input[prefix]?.type === "message" && input[prefix].role === "developer") prefix += 1;
  const sizes = input.map((item) => Buffer.byteLength(JSON.stringify(item) ?? ""));
  const total = sizes.reduce((sum, size) => sum + size, 0);
  let start = input.length - 1, bytes = sizes[start] ?? 0;
  while (start - 1 >= prefix && bytes + sizes[start - 1] <= maxBytes) bytes += sizes[--start];
  // Begin at a user message so no tool output, call or reasoning item loses its context.
  while (start < input.length - 1 && !(input[start]?.type === "message" && input[start].role === "user")) start += 1;
  if (start <= prefix) return null;
  const trimmed = [...input.slice(0, prefix), ...input.slice(start)];
  return { input: trimmed, stats: { before_items: input.length, after_items: trimmed.length,
    before_bytes: total, after_bytes: sizes.slice(0, prefix).reduce((sum, size) => sum + size, 0)
      + sizes.slice(start).reduce((sum, size) => sum + size, 0) } };
}

// Native threads' composer predictions have the same shape and cost. Nokiy aliases trim
// inside projectNokiyDirect; a chained delta carries no history to trim.
export function trimNativePrediction(body) {
  if (typeof body?.model !== "string" || /^nokiy(?:$|[-/])/.test(body.model) || body.previous_response_id
      || !isPredictionRequest(body)) return null;
  return trimPredictionHistory(body.input);
}

// Codex keeps user messages verbatim across compactions, including the <appshot> accessibility
// dumps the desktop app attaches when the user mentions an app. Once such a message precedes a
// compaction its UI snapshot is stale, yet every later request re-read it: one live thread
// carried about 170K characters (~43K tokens) of them through all 26 compactions of a day.
// History before the latest compaction is rewritten by that compaction anyway, so omitting the
// dumps there adds no prompt-cache miss, provided every request type applies the same rule.
// The newest user messages before the boundary keep theirs: a compaction may land mid-turn.
// Screenshots attached to those older user messages are just as stale and dominate the bytes:
// four live threads re-sent 2-7 of them (3.0-5.0 MB of base64) with every full request, which the
// note below cuts to 0.35-0.48 MB per request (SWITCHBOARD_STALE_IMAGE_ELISION=0 keeps them).
const COMPACTION_ITEM_TYPES = new Set(["compaction", "context_compaction", "compaction_summary"]);
const APPSHOT_BLOCK = /(<appshot\b(?:[^>"']|"[^"]*"|'[^']*')*>)([\s\S]*?)(<\/appshot>)/g;
export const STALE_APPSHOT_KEEP_USER_MESSAGES = 2;
const STALE_APPSHOT_MIN_CHARS = 1000;

export function elideStaleAppshots(input, keep = STALE_APPSHOT_KEEP_USER_MESSAGES) {
  if (!Array.isArray(input)) return null;
  let boundary = -1;
  for (let index = input.length - 1; index >= 0 && boundary < 0; index -= 1) {
    if (COMPACTION_ITEM_TYPES.has(input[index]?.type)) boundary = index;
  }
  if (boundary <= 0) return null;
  const users = [];
  for (let index = 0; index < boundary; index += 1) {
    if (input[index]?.type === "message" && input[index].role === "user") users.push(index);
  }
  let appshots = 0;
  let chars = 0;
  let images = 0;
  const imageElision = process.env.SWITCHBOARD_STALE_IMAGE_ELISION !== "0";
  const elide = (text) => text.includes("<appshot") ? text.replace(APPSHOT_BLOCK, (block, open, inner, close) => {
    if (inner.length < STALE_APPSHOT_MIN_CHARS) return block;
    appshots += 1;
    chars += inner.length;
    return `${open}[Earlier UI snapshot omitted after context compaction: ${inner.length} characters]${close}`;
  }) : text;
  const elidePart = (part) => {
    if (part?.type === "input_text" && typeof part.text === "string") {
      const text = elide(part.text);
      return text === part.text ? part : { ...part, text };
    }
    if (imageElision && part?.type === "input_image") {
      images += 1;
      return { type: "input_text", text: "[Earlier image omitted after context compaction]" };
    }
    return part;
  };
  const output = [...input];
  for (const index of users.slice(0, Math.max(0, users.length - keep))) {
    const item = output[index];
    if (typeof item.content === "string") {
      const text = elide(item.content);
      if (text !== item.content) output[index] = { ...item, content: text };
    } else if (Array.isArray(item.content)) {
      const content = item.content.map(elidePart);
      if (content.some((part, position) => part !== item.content[position])) output[index] = { ...item, content };
    }
  }
  return appshots || images ? { input: output, stats: { appshots, chars, images } } : null;
}

// Native threads retain the same snapshots across compactions (a live Astra thread re-read about
// 43K tokens of them per request); Nokiy aliases omit them inside projectNokiyDirect. Only full
// requests to official models are rewritten, by the same rule; a chained delta has no history.
export function elideNativeStaleAppshots(body, isOfficialModel = () => true) {
  if (typeof body?.model !== "string" || /^nokiy(?:$|[-/])/.test(body.model) || body.previous_response_id
      || !isOfficialModel(body.model) || process.env.SWITCHBOARD_STALE_APPSHOT_ELISION === "0") return null;
  return elideStaleAppshots(body.input);
}

// Measured 2026-10-26: Nokiy result summaries stay in the coordinator's context for the rest of the
// session and every later request re-reads them. A live Ultra session carried 1.77M characters of
// them over 228 calls, 169K of which were 2,638 SHA-256 values; an I26 batch summary was 22K
// characters, about a third digests and evidence paths. The digests bind on-disk evidence for the
// caller and for this switchboard's own delivery checks, which read the unprojected history; the
// model cannot recompute them. Forwarded requests therefore carry a fixed projection of each batch
// and run summary: status, blockers, diffs, verifier outcomes, postimages and evidence paths stay,
// audit-only digests, identities and coverage metadata go. Full requests and chained deltas apply
// the same rule, so the upstream conversation and later replays stay identical.
const COMPACT_RESULT_SCHEMAS = new Set(["nokiy_direct_batch_summary_v1", "nokiy_direct_batch_summary_v2",
  "nokiy_terminal_summary_v1"]);
const SOURCE_POSTIMAGE_CONTEXT_SCHEMAS = new Set(["nokiy_source_postimages_v1",
  "nokiy_source_postimage_delta_v1", "nokiy_source_postimage_delta_v2"]);
const RESULT_AUDIT_KEYS = new Set(["sha256", "request_sha256", "terminal_sha256", "terminal_content_sha256",
  "diff_sha256", "preimage_sha256", "request_file_sha256", "executable_sha256", "event_sha256", "command_sha256",
  "stdout_sha256", "stderr_sha256", "evidence_sha256", "receipt_sha256", "result_sha256", "native_thread_id",
  "session_id", "runtime_id", "execution_id", "delivery_binding", "coverage", "model_usage_coverage"]);

function compactResultValue(value, key, depth) {
  if (Array.isArray(value)) return value.map((item) => compactResultValue(item, key, depth + 1));
  if (!value || typeof value !== "object" || key === "source_postimages"
      || (key === "source_postimage_context" && SOURCE_POSTIMAGE_CONTEXT_SCHEMAS.has(value.schema_version))) return value;
  const out = {};
  for (const [name, item] of Object.entries(value)) {
    if (RESULT_AUDIT_KEYS.has(name) || (depth > 0 && name === "schema_version")) continue;
    if (name === "source_event") {
      if (item && typeof item === "object" && Number.isSafeInteger(item.verifier_index)) out.verifier_index = item.verifier_index;
      continue;
    }
    if (name === "binding" && key === "post_batch_verifier") {
      if (Array.isArray(item?.command?.argv)) out.binding = { command: { argv: item.command.argv } };
      continue;
    }
    if (name === "diff_path" && value.diff_inline_truncated === false && value.diff_inline_files === undefined) continue;
    out[name] = compactResultValue(item, name, depth + 1);
  }
  return out;
}

function compactResultText(text, stats) {
  if (typeof text !== "string" || !text.includes("schema_version") || !text.includes("nokiy_")) return text;
  let changed = false;
  const lines = text.split("\n").map((line) => {
    const trimmed = line.trim();
    if (!trimmed.startsWith("{") || !trimmed.endsWith("}")) return line;
    let value;
    try { value = JSON.parse(trimmed); } catch { return line; }
    if (!value || typeof value !== "object" || Array.isArray(value)) return line;
    // Desktop code mode wraps the host's stdout in a command result, {"chunk_id",...,"output":"..."};
    // older cells nested the parsed summary itself under output.
    if (!Object.hasOwn(value, "schema_version") && value.output && typeof value.output === "object"
        && COMPACT_RESULT_SCHEMAS.has(value.output.schema_version)) {
      const compact = JSON.stringify({ ...value, output: compactResultValue(value.output, null, 0) });
      if (compact.length >= trimmed.length) return line;
      stats.results += 1;
      stats.chars_before += trimmed.length;
      stats.chars_after += compact.length;
      changed = true;
      return compact;
    }
    if (!Object.hasOwn(value, "schema_version") && typeof value.output === "string") {
      const inner = compactResultText(value.output, stats);
      if (inner === value.output) return line;
      changed = true;
      return JSON.stringify({ ...value, output: inner });
    }
    if (!COMPACT_RESULT_SCHEMAS.has(value.schema_version)) return line;
    const compact = JSON.stringify(compactResultValue(value, null, 0));
    if (compact.length >= trimmed.length) return line;
    stats.results += 1;
    stats.chars_before += trimmed.length;
    stats.chars_after += compact.length;
    changed = true;
    return compact;
  });
  return changed ? lines.join("\n") : text;
}

export function compactNokiyResults(input) {
  if (!Array.isArray(input)) return null;
  const stats = { results: 0, chars_before: 0, chars_after: 0 };
  const output = input.map((item) => {
    if (item?.type !== "function_call_output" && item?.type !== "custom_tool_call_output") return item;
    if (typeof item.output === "string") {
      const text = compactResultText(item.output, stats);
      return text === item.output ? item : { ...item, output: text };
    }
    if (!Array.isArray(item.output)) return item;
    const parts = item.output.map((part) => {
      if (typeof part?.text !== "string") return part;
      const text = compactResultText(part.text, stats);
      return text === part.text ? part : { ...part, text };
    });
    return parts.some((part, index) => part !== item.output[index]) ? { ...item, output: parts } : item;
  });
  return stats.results ? { input: output, stats } : null;
}

export function withLitePrefix(input, tools, contractText) {
  const items = [...input];
  let index = 0;
  while (index < items.length && LITE_PREFIX_TYPES.has(items[index]?.type)) index += 1;
  const toolsAt = items.findIndex((item, position) => position < index && item?.type === "additional_tools");
  if (toolsAt >= 0) {
    items[toolsAt] = { ...items[toolsAt], tools: [...(Array.isArray(items[toolsAt].tools) ? items[toolsAt].tools : []), ...tools] };
  } else if (tools.length) {
    items.unshift({ type: "additional_tools", role: "developer", tools });
    index += 1;
  }
  // Native base instructions are the developer message that follows the tools prefix.
  if (items[index]?.type === "message" && items[index].role === "developer") index += 1;
  items.splice(index, 0, { type: "message", role: "developer", content: [{ type: "input_text", text: contractText }] });
  return items;
}

const contract = (profile) => [
  profile.workflow === "nokiy"
    ? `Nokiy serial opt-in: native Codex remains the task/Goal, permission and tool owner; ${profile.coordinator.model}/${profile.coordinator.reasoning_effort} manages the native conversation context and acceptance, independently of the execution model. This is not a single-inference-call guarantee.`
    : `${profile.display_name} opt-in: native Codex remains the task/Goal, permission and tool owner; commander is ${profile.coordinator.model}/${profile.coordinator.reasoning_effort}.`,
  `Selected workflow ${profile.slug} applies to this request and subsequent requests carrying this model alias; no @ trigger is needed. Native Codex retains the conversation and context. Supply only bounded task-relevant context via action.context_summary, not a copied rollout.`,
  `For bounded worker work use nokiy_execute. Its native-host adapter binds worker model ${profile.worker.model} with reasoning_effort ${profile.worker.reasoning_effort} and requested service_tier ${profile.worker.service_tier} independently of coordinator tier; requested tier does not prove delivered speed. Read the installed $nokiy skill and invocation details only when task-scoping/DCF guidance is needed.`,
  profile.workflow === "nokiy"
    ? "The nokiy_execute tool invokes the genuine installed runtime through native exec; it is not a native subagent, context owner or permission issuer. Run only one bounded installed action at a time; wait for terminal/read-result before starting the next. Do not form a task delegation tree, spawn native subagents or fan out. The native driver and bounded runtime action still exist. Native Codex owns the session, Goal, permissions, planning, integration and acceptance. Never recursively dispatch Nokiy or treat selecting this mode as permission for effects."
    : "The nokiy_execute tool invokes the genuine installed full runtime through native exec; it is not a native subagent or permission issuer. Delegate bounded worker work through that tool rather than spawning another Codex agent or executing the worker task yourself. The parent owns planning, integration and acceptance. Never recursively dispatch Nokiy or treat selecting this mode as permission for effects.",
  "For the advertised nokiy_execute route, the native-host adapter verifies the installed caller, runtime image and native identity; do not duplicate that binding with shell discovery, manifest reads or manual hash checks. When the task already supplies its workspace, exact DCF surface (or verified non-DCF scope), read/write operations and artifact root, dispatch directly with the advertised tool schema instead of rereading the skill or CLI invocation guide. If a task-scoping or capability detail is missing, inspect only the relevant guidance; do not infer a grant or change routes after denial. Manual CLI use without the verified adapter still requires the installed skill binding checks. Preserve the actual CODEX_THREAD_ID, HOME and CODEX_HOME. Supply the bounded mission/action, J-Space scopes, operations and forbidden effects; the host performs preparation and fresh source/authority validation before execution. Retain the original request ID and await the original handle. Keep repeated inner tools.write_stdin waits in the same functions.exec. Read a Nokiy result JSON in one piece: give the cell that waits for or prints it an output budget (functions.exec max_output_tokens, functions.wait max_tokens) of at least 64000. Run and read-result summaries are bounded at 64 KiB and batch summaries at 128 KiB (about 64000 tokens when fully escaped), so this always returns them in one piece, including any verified parent_review_answer, and costs nothing when they are smaller; a smaller budget truncates the JSON, which disables result delivery and forces read-result/read-batch recovery. The inner tools.write_stdin 300000 ms limit is not an outer observation cap. The adapter cell's first observation yields after at most 600000 ms; if functions.exec yields a cell ID, use functions.wait on that original cell with yield_time_ms 300000, not a 1s/10s polling ladder; any explicitly smaller schema bound still controls. These are observation cadences, neither measured maxima nor universal host guarantees. Use the actual tool schema; never invent an infinite wait. This is an observation wait, not a worker deadline; preserve user interruption, original-ID recovery, unknown-effect reconciliation, cleanup and acceptance. When an operator message interrupts an observation, answer it from current evidence first, then resume waiting on the original cell. Recover uncertain outcomes with read-result on the ORIGINAL ID, never retry uncertain effects. Inspect terminal evidence and owned-process cleanup before parent-owned acceptance. When result_inspection.status is EVIDENCE_VERIFIED and the inspection is bound to the expected request/thread, reuse its proven artifact/command/cleanup checks rather than repeat hashes, key dumps or process probes; still inspect task-specific quality and necessary changed-file postimages, with fresh owner/CAS at effects. Where inputs are known, combine remaining task verification, postimage/quality readback, compact evidence publication and authorized closeout into one dependency-ordered parent acceptance call. This is not a call-count cap, grant or automatic acceptance; failed/incomplete/unknown evidence still requires original-ID recovery. No bare provider inference counts as the Nokiy runtime.",
  ...(profile.workflow === "nokiy" ? [] : ["For nokiy_direct_batch_summary_v1/v2, use each member's native_thread_id, request_sha256, evidence_status, cleanup_pass and result_truncated; v2 member_defaults supplies factored shared fields. An EVIDENCE_VERIFIED member bound to the expected request/thread with successful cleanup and complete result_text already delivers its answer for parent quality review. inspection_summary and terminal_path are recovery references, not instructions to reopen the same artifacts. Missing, mismatched, blocked or truncated evidence still requires original-ID recovery; this does not waive semantic acceptance, changed-file readback or fresh effect authority."]),
  "A valid capability_gap is diagnostic, not a grant: the native parent must first recover the original request terminal and reconcile effects/cleanup; then freshly admit only unfinished work whose exact path/operation is already within original human authorization, with fresh owner/CAS/lease checks, without asking again. Genuine prohibitions, new protected scopes, owner conflicts or unresolved effects stop continuation. A read-only patch proposal must contain a usable diff or exact missing-read evidence, never promises or placeholders. Prefer advertised Nokiy deterministic capabilities such as read_http for exact authorized HTTPS evidence. A coordinator-only parent cannot fall back to raw execution; outside that mode, authorized parent native execution must be labeled native execution. No self-grant, recursive dispatch, replay of consumed/maybe-executed effects, daemon/orchestrator or new controller.",
].join("\n");

export function directCatalogEntries(catalog, profiles) {
  return selectedProfiles(profiles, catalog).map((profile) => {
    const anchor = catalog.models.find((item) => item.slug === profile.coordinator.model);
    const effort = profile.coordinator.reasoning_effort;
    const entry = structuredClone(withCliRequiredModelFields(anchor));
    const selector = profile.worker_selector;
    const selectorEffort = (level) => selector?.reasoning_efforts?.[level] ?? "max";
    const selectorLevels = selector ? Object.keys(selector.models) : [];
    const selectorNames = selectorLevels.map(level => ({ "gpt-6-luna": "Luna", "gpt-6.1-terra": "Terra", "gpt-6.1-sol": "Sol", "gpt-6-astra": "Astra" })[selector.models[level]]);
    const selectorWorkers = selectorLevels.every((level) => selectorEffort(level) === "max")
      ? selectorNames.join("/") + " workers at max"
      : selectorNames.map((name, index) => `${name}/${selectorEffort(selectorLevels[index])}`).join(", ") + " workers";
    return {
      ...entry,
      slug: profile.slug,
      display_name: profile.display_name,
      description: selector
        ? `${profile.workflow === "nokiy" ? "Serial Nokiy" : profile.display_name}: ${anchor.slug}/${effort} native context manager; picker ${selectorLevels.join("/")} dispatches ${selectorWorkers} (requested ${profile.worker.service_tier} worker tier).`
        : profile.workflow === "nokiy"
        ? `Serial Nokiy: ${anchor.slug}/${effort} native context manager with installed ${profile.worker.model}/${profile.worker.reasoning_effort} executor; one action at a time (requested ${profile.worker.service_tier} worker tier).`
        : `${anchor.slug}/${effort} coordinator with native-tool dispatch to the installed Nokiy ${profile.worker.model}/${profile.worker.reasoning_effort} runtime (requested ${profile.worker.service_tier} worker tier).`,
      visibility: "list",
      // Each coordinator step re-reads the whole thread, so a profile may compact earlier than
      // its anchor model (Astra cached input costs ten times Sol 6.1 per token).
      ...(profile.auto_compact_token_limit ? { auto_compact_token_limit: profile.auto_compact_token_limit } : {}),
      // A complete batch result (member diffs plus verifier gate) must reach the coordinator in
      // one call; the anchor's 10000-token ceiling middle-truncates it and forces re-reads.
      ...(profile.tool_output_token_limit
        ? { truncation_policy: { mode: "tokens", limit: profile.tool_output_token_limit } } : {}),
      default_reasoning_level: selector ? selector.default_effort : effort,
      supported_reasoning_levels: selector
        ? selectorLevels.map((level) => ({ effort: level,
          description: `${selector.models[level]} / ${selectorEffort(level)} worker; ${anchor.slug} native context manager` }))
        : entry.supported_reasoning_levels.filter((level) => level.effort === effort),
      // The picker offers only the configured effort; the CLI still needs the
      // real catalog's normalization target when decoding native Ultra.
      ...(effort === "ultra" ? { multi_agent_reasoning_effort: normalizedUltraEffort(anchor) } : {}),
    };
  });
}

// Ultra is a native orchestration setting. Codex normalizes it before the API
// request; preserving the real catalog is necessary for that normalization.
function normalizedUltraEffort(anchor) {
  const supported = new Set(anchor?.supported_reasoning_levels?.map((level) => level.effort));
  if (!supported.has("ultra")) throw new Error("Official coordinator ultra metadata is required");
  const override = anchor.multi_agent_reasoning_effort;
  if (override && override !== "ultra" && supported.has(override)) return override;
  return ["max", "xhigh", "high", "medium", "low", "minimal", "none"].find((effort) => supported.has(effort)) || "medium";
}

export function ultraWireEffort(catalog) {
  return normalizedUltraEffort(catalog?.models?.find((item) => item.slug === ASTRA_MODEL));
}

export function withDirectCatalog(catalog, profiles) {
  const entries = directCatalogEntries(catalog, profiles);
  // Configuration owns the complete workflow namespace, including removals.
  return { ...catalog, models: [...catalog.models.filter((item) => !isNokiyAlias(item.slug)), ...entries] };
}

// Null means ordinary routing remains completely unchanged. This module only projects
// official Responses payloads; the native client, not this HTTP/WS router, invokes tools.
export function projectNokiyDirect(body, { endpoint = "/responses", host = null, catalog = null, continuationTarget = null, continuationCall = null, continuationCoordinatorBindings = null, continuationPendingDelivery = null, continuationFallback = null, operatorTexts = null, profiles, replayRestore = null, recordedNativeCall = null, runReminder = null } = {}) {
  const alias = body?.model;
  // Ordinary routing is not dependent on a workflow profile configuration.
  if (!isNokiyAlias(alias)) return null;
  let profile = selectedProfiles(profiles, catalog).find((item) => item.slug === alias);
  if (!profile) {
    if (typeof alias === "string" && (alias === "nokiy" || alias.startsWith("nokiy-")
      || alias.startsWith("nokiy/"))) {
      throw new SwitchboardError("NOKIY_MODE_UNSUPPORTED", `Nokiy mode ${alias} is not implemented by this route.`, 400);
    }
    return null;
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new SwitchboardError("NOKIY_DIRECT_INVALID_REQUEST", "Direct requires a Responses request object.", 400);
  }
  // Auxiliary capabilities only project the alias, without inference policy.
  if (["/alpha/search", "/memories/trace_summarize", "/realtime/calls",
    "/images/generations", "/images/edits"].includes(endpoint)) {
    return { body: { ...body, model: profile.coordinator.model }, nativeTool: null,
      requestedAlias: alias, profile, worker: null, wireEffort: null,
      nativeEffortObserved: null, dispatch: "none" };
  }
  if (endpoint !== "/responses" && endpoint !== "/responses/compact") {
    throw new SwitchboardError("ENDPOINT_NOT_SUPPORTED", `Unsupported endpoint ${endpoint}.`, 404);
  }
  const expectedWireEffort = profile.coordinator.reasoning_effort === "ultra"
    ? normalizedUltraEffort(catalog.models.find((item) => item.slug === profile.coordinator.model))
    : profile.coordinator.reasoning_effort;
  if (body.reasoning_effort !== undefined || (body.reasoning !== undefined && (body.reasoning === null || typeof body.reasoning !== "object"
      || Array.isArray(body.reasoning) || (profile.worker_selector && Object.getPrototypeOf(body.reasoning) !== Object.prototype)
      || (!profile.worker_selector && body.reasoning.effort !== undefined && body.reasoning.effort !== expectedWireEffort)))) {
    throw new SwitchboardError("NOKIY_DIRECT_DECODER_MISMATCH", `Nokiy expects its configured coordinator wire effort (${expectedWireEffort}); no worker was dispatched.`, 400);
  }
  if (body.instructions !== undefined && typeof body.instructions !== "string") {
    throw new SwitchboardError("NOKIY_DIRECT_DECODER_MISMATCH", "Direct requires string instructions.", 400);
  }
  const prewarm = body.generate === false;
  const compact = endpoint === "/responses/compact" || (Array.isArray(body.input)
    && body.input.some((item) => item?.type === "compaction_trigger"));
  const nonExecution = prewarm || compact;
  const chained = typeof body.previous_response_id === "string" && body.previous_response_id !== "";
  // Only the server's exact same-connection response binding can inherit this mode.
  if (!nonExecution && continuationFallback !== null) {
    return coordinatorFallback(body, profile, continuationFallback.wireEffort,
      { ...continuationFallback.nativeToolMissing, chained, lite: isLiteRequest(body), continuation: true },
      continuationFallback.answerOnly);
  }
  // An unchained /responses request that advertises no tool at all (app title, summary or other
  // helper calls) cannot dispatch anything. Answer it with the coordinator model, as the auxiliary
  // endpoints above do, instead of rejecting it for a missing exec tool. Lite and bound
  // continuations legitimately omit tools and keep the ordinary path.
  if (endpoint === "/responses" && !nonExecution && !chained && continuationTarget === null
      && Array.isArray(body.tools) && advertisedTools(body).length === 0) {
    const coordinator = catalog?.models?.find((item) => item?.slug === profile.coordinator.model);
    const effort = body.reasoning?.effort;
    const supported = effort === undefined
      || coordinator?.supported_reasoning_levels?.some((level) => level?.effort === effort) === true;
    return coordinatorFallback(body, profile, supported ? effort ?? null : expectedWireEffort,
      missingExecDiagnostic(body));
  }
  const hasNativeExec = continuationTarget !== null || advertisedTools(body).some((tool) => EXEC_NAMES.has(tool?.name)
    && (tool?.type === "function" || tool?.type === "custom"));
  if (!nonExecution && !hasNativeExec) {
    const nativeToolMissing = missingExecDiagnostic(body);
    // An unbound chain may inherit projected tools we cannot reconstruct. It can still
    // answer from the supplied results, but cannot make another tool call in that mode.
    return coordinatorFallback(body, profile, expectedWireEffort,
      { ...nativeToolMissing, ...(chained ? { unbound_chain: true } : {}) }, chained);
  }
  const fallbackBody = body;
  const fallbackProfile = profile;
  // A full request replays Codex's history, where each Nokiy call appears as its projected
  // native wrapper; restore the model's own calls so the replay matches the cached chain.
  let replayRestored = 0;
  let replayInserted = 0;
  let predictionTrim = null;
  // The model gets its own restored calls; pending cells and result delivery bind the
  // native calls Codex actually executed, so they keep the unrestored client input.
  let clientInput = null;
  if (!chained && typeof replayRestore === "function" && Array.isArray(body.input)) {
    const result = replayRestore(body.input);
    if ((result?.restored > 0 || result?.inserted > 0) && Array.isArray(result.input)) {
      clientInput = body.input;
      body = { ...body, input: result.input };
      replayRestored = result.restored || 0;
      replayInserted = result.inserted || 0;
    }
  }
  if (!chained && isPredictionRequest(body)) {
    const trimmed = trimPredictionHistory(body.input);
    if (trimmed) {
      body = { ...body, input: trimmed.input };
      predictionTrim = trimmed.stats;
    }
  }
  // Execution, prewarm and remote compaction requests apply the same rule, so they keep one
  // cacheable prefix; a chained delta carries no history to rewrite.
  let staleAppshots = null;
  if (!chained && endpoint === "/responses" && process.env.SWITCHBOARD_STALE_APPSHOT_ELISION !== "0") {
    const elided = elideStaleAppshots(body.input);
    if (elided) {
      body = { ...body, input: elided.input };
      staleAppshots = elided.stats;
    }
  }
  if (profile.worker_selector) {
    const inputEffort = body.reasoning?.effort;
    // Old alias efforts fall back to the configured default only when the
    // selector does not explicitly assign that level to a worker.
    const selection = inputEffort === undefined && nonExecution
      ? profile.worker_selector.default_effort
      : ["max", "ultra", "xhigh"].includes(inputEffort) && !Object.hasOwn(profile.worker_selector.models, inputEffort)
        ? profile.worker_selector.default_effort : inputEffort;
    if (typeof selection !== "string" || !Object.hasOwn(profile.worker_selector.models, selection)) {
      throw new SwitchboardError("NOKIY_DIRECT_DECODER_MISMATCH", "Unknown Nokiy worker picker effort; no worker was dispatched.", 400);
    }
    profile = Object.freeze({ ...profile, worker: Object.freeze({ ...profile.worker,
      model: profile.worker_selector.models[selection],
      reasoning_effort: profile.worker_selector.reasoning_efforts?.[selection] ?? "max" }) });
  } else if (!nonExecution && body.reasoning?.effort !== expectedWireEffort) {
    throw new SwitchboardError("NOKIY_DIRECT_DECODER_MISMATCH", "Nokiy requires the configured coordinator wire effort; the router never invents an effort setting.", 400);
  }
  if (!nonExecution && (SUBAGENT_NAMES.has(body.tool_choice?.name) || SUBAGENT_NAMES.has(body.tool_choice))) {
    throw new SwitchboardError("NOKIY_DIRECT_SUBAGENT_FORBIDDEN", "Direct cannot select a native subagent tool.", 400);
  }
  const projected = { ...body, model: profile.coordinator.model };
  if (profile.worker_selector) projected.reasoning = { ...body.reasoning, effort: expectedWireEffort };
  // A chained execution request inherits the lite prefix only through a bound continuation,
  // whose chain started with an injected full request. An unbound chain that advertises exec
  // in its own delta keeps the top-level form instead of running without the Nokiy tools.
  const lite = isLiteRequest(body) && !(chained && !compact && !prewarm && continuationTarget === null);
  const contractText = (coordinatorOnly(host) || efficientNativeLoop(profile)) ? coordinatorContract(profile) : contract(profile);
  // Prewarm and remote compaction v2 replay the executing conversation, so they take its
  // exact prefix (instructions, tools, parallel flag) and the prompt cache can serve them.
  // Neither dispatches; the legacy /responses/compact endpoint stays unmodified.
  let policyContainerSha256 = null;
  const policyHash = (text) => createHash("sha256").update(text).digest("hex");
  const turnPrefix = (target) => {
    if (profile.workflow === "nokiy") target.parallel_tool_calls = false;
    if (!lite) {
      target.instructions = [body.instructions, contractText].filter(Boolean).join("\n\n");
      policyContainerSha256 = policyHash(target.instructions);
    }
    if (Array.isArray(body.tools)) target.tools = withoutSubagents(body.tools);
    if (Array.isArray(body.input)) target.input = withoutSubagentItems(body.input);
    return target;
  };
  // Lite requests move the projected top-level tools into the conversation prefix.
  const liteBody = (projectedBody) => {
    if (!lite) return projectedBody;
    const { tools = [], instructions: _instructions, ...rest } = projectedBody;
    if (chained || !Array.isArray(rest.input)) { policyContainerSha256 = null; return rest; }
    policyContainerSha256 = policyHash(JSON.stringify([{ type: "input_text", text: contractText }]));
    return { ...rest, input: withLitePrefix(rest.input, Array.isArray(tools) ? tools : [], contractText) };
  };
  const projectionOptions = { worker: profile.worker.model, workerEffort: profile.worker.reasoning_effort,
    workerServiceTier: profile.worker.service_tier, workflow: profile.workflow, host, profile, continuationTarget,
    continuationCall, continuationCoordinatorBindings, continuationPendingDelivery, operatorTexts,
    observationInput: clientInput ? withoutSubagentItems(clientInput) : null, recordedNativeCall, runReminder };
  let nativeTool = null;
  let requestBody = projected;
  let cacheAlignment = null;
  if (!compact && (!prewarm || hasNativeExec)) {
    try { nativeTool = nativeToolProjection(turnPrefix(projected), projectionOptions); }
    catch (error) {
      if (!nonExecution && continuationTarget === null
          && error?.code === "NOKIY_NATIVE_TOOL_INVALID" && error.details?.native_binding_missing === true) {
        return coordinatorFallback(fallbackBody, fallbackProfile, expectedWireEffort,
          missingExecDiagnostic(fallbackBody, "unsupported_native_exec_binding"), chained);
      }
      throw error;
    }
  } else if (compact && endpoint === "/responses" && lite && chained) {
    // The stored upstream context already holds the lite prefix; send the delta unchanged.
    cacheAlignment = "lite_chained";
  } else if (compact && endpoint === "/responses" && hasNativeExec) {
    // An unprojected compaction (300-400K tokens) diverged from every executing request
    // at the instructions and missed the prompt cache almost entirely.
    try {
      const aligned = turnPrefix({ ...projected });
      const shape = nativeToolProjection(aligned, { ...projectionOptions, continuationCall: null, continuationPendingDelivery: null, runReminder: null });
      aligned.tools = shape.body.tools;
      // Keep the conversation itself; drop any result-delivery turn the projection appends.
      if (Array.isArray(aligned.input)) aligned.input = shape.body.input.slice(0, aligned.input.length);
      requestBody = liteBody(aligned);
      cacheAlignment = lite ? "lite_full" : "applied";
    } catch (error) {
      // A cache optimization must never fail a compaction: send it unmodified.
      cacheAlignment = `fallback:${error?.code || error?.name || "error"}`;
    }
  } else if (compact) {
    cacheAlignment = endpoint === "/responses" ? "skipped:no_native_exec" : "skipped:legacy_endpoint";
  }
  let finalBody = nativeTool ? liteBody(nativeTool.body) : requestBody;
  // Applied last: pending cells and result delivery above read the unprojected summaries.
  let resultCompaction = null;
  if (process.env.SWITCHBOARD_NOKIY_RESULT_COMPACTION !== "0") {
    const compacted = compactNokiyResults(finalBody.input);
    if (compacted) {
      finalBody = { ...finalBody, input: compacted.input };
      resultCompaction = compacted.stats;
    }
  }
  return { body: finalBody, nativeTool, requestedAlias: alias, profile, cacheAlignment,
    receiptPolicy: policyContainerSha256 ? { sha256: policyHash(contractText), bytes: Buffer.byteLength(contractText),
      container_sha256: policyContainerSha256 } : null,
    replayRestored, replayInserted, predictionTrim, staleAppshots, resultCompaction, fullRequest: !chained,
    worker: profile.worker.model, wireEffort: projected.reasoning?.effort ?? null,
    nativeEffortObserved: null, dispatch: nonExecution ? "none" : "native-tool" };
}
