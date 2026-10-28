// Configuration API: resolveWorkflowProfiles(config, catalog) returns a detached,
// frozen array of profiles. Pass that array (or the original config) to route APIs.
// This is derived from the router's validated profile, never from prompt text or
// the projected model name (which no longer identifies the requested alias).
export function efficientNativeLoop(profile) {
  return profile?.slug === "nokiy-efficient" && profile.workflow === "nokiy-direct"
    && profile.coordinator?.model === "gpt-6.1-sol" && profile.coordinator.reasoning_effort === "max";
}

export const EFFICIENT_NATIVE_GUIDANCE = "Efficient may complete one coherent branch in the SAME native Codex loop with its already-advertised native capabilities and real schemas, without a mandatory Sol coordinator -> Sol worker -> coordinator handoff. Fused native execution is not independent Nokiy runtime acceleration and makes no universal performance claim. Cost rule: every native step re-sends this whole conversation, while a worker starts from a compact packet. Once the conversation is long (roughly 100K or more tokens of history, the normal state of an ongoing session), delegate each bounded execution chunk that needs three or more tool calls with known scope (an investigation, running tests or backtests and analysing them, multi-file edits with their verification) to one worker through nokiy_execute and keep only its compact result, even when there is only one branch. Keep in the native loop: one- or two-call checks, steps whose judgment needs the full conversation, live owner/CAS/account or deployment decisions, and acceptance. Use installed full-runtime workers in parallel for genuinely independent useful branches; honor explicit independent-runtime or selected-worker requests even for one branch. Native permissions, session/Goal, owner/CAS and deployment authority remain controlling.";

export const ULTRA_DISPATCH_GUIDANCE = "Ultra dispatch rule: the Astra coordinator is the most expensive model in this topology (about five times Sol 6.1 per token) and every native step re-sends the whole conversation, so it plans, splits, decides and accepts while workers do the execution. Delegate any work that needs more than one native call, including single-branch implementation, investigation, analysis runs and their tests (as worker verifiers): split it into independent chunks and dispatch them together in one run_batch, then dispatch dependent chunks as soon as their inputs exist. Use native calls only for one batched read-only call per decision that gathers facts a dispatch needs, App or cross-session operations, owner/CAS/account or deployment decisions, and a final acceptance gate that is not already a post_batch_verifier. Workers start from a compact packet: give them paths, locations and acceptance criteria instead of reading their source first.";

// Advisory native-run reminder. A step is one completed coordinator response that called
// a tool other than nokiy_execute; every step re-sends the whole thread. Past the alias
// threshold on a long conversation, the next request gets one appended developer reminder
// and the count restarts. An operator turn, a final answer or a nokiy_execute call resets it;
// Already-bound parent coordination controls leave it unchanged. Opaque code cells
// still count: poll words in strings or mixed work do not prove control-only work.
export const NATIVE_RUN_REMINDER = Object.freeze({ "nokiy-efficient": 5, "nokiy-direct": 2 });
export const NATIVE_RUN_REMINDER_MIN_INPUT_TOKENS = 100_000;
const RUN_CALL_TYPES = new Set(["function_call", "custom_tool_call", "local_shell_call"]);
function coordinationOnlyCall(item, bindings) {
  if (bindings.length) {
    const matches = bindings.filter((binding) => binding.type === item.type && binding.name === item.name
      && (item.namespace == null || (binding.namespace ?? null) === item.namespace));
    return matches.length === 1 && matches[0].name !== "nokiy_native";
  }
  return item.name === "wait" && (item.namespace == null || item.namespace === "functions");
}

export function latestInputIsOperatorTurn(input) {
  if (!Array.isArray(input)) return false;
  for (let index = input.length - 1; index >= 0; index--) {
    const item = input[index];
    if (item?.role === "developer" || item?.type === "reasoning") continue;
    return item?.role === "user" && (item.type === undefined || item.type === "message");
  }
  return false;
}

export function nativeRunAfterResponse(state, alias, output, usage, coordinatorBindings = []) {
  if (!Object.hasOwn(NATIVE_RUN_REMINDER, alias)) return null;
  const same = state?.alias === alias;
  const steps = same ? state.steps : 0;
  const inputTokens = Number.isFinite(usage?.input_tokens) ? usage.input_tokens : same ? state.inputTokens : 0;
  const calls = (Array.isArray(output) ? output : []).filter((item) => RUN_CALL_TYPES.has(item?.type));
  let next = steps + 1;
  if (calls.length === 0 || calls.some((item) => item.name === "nokiy_execute")) next = 0;
  else if (calls.every((item) => coordinationOnlyCall(item, coordinatorBindings ?? []))) next = steps;
  return Object.freeze({ alias, steps: next, inputTokens });
}

export function nativeRunReminder(state, alias, operatorTurn) {
  if (!Object.hasOwn(NATIVE_RUN_REMINDER, alias) || state?.alias !== alias) return { state, text: null, diagnostic: null };
  if (operatorTurn) return { state: Object.freeze({ ...state, steps: 0 }), text: null, diagnostic: null };
  if (state.steps < NATIVE_RUN_REMINDER[alias] || state.inputTokens < NATIVE_RUN_REMINDER_MIN_INPUT_TOKENS) {
    return { state, text: null, diagnostic: null };
  }
  const size = Math.round(state.inputTokens / 1000) + "K";
  const text = alias === "nokiy-direct"
    ? "Ultra dispatch reminder: " + state.steps + " consecutive native coordinator steps at about " + size
      + " tokens of context; every Astra step re-sends this whole thread. Hand the remaining execution of this chunk"
      + " (reads, edits, tests, analysis) to workers now through nokiy_execute run or run_batch, one member per"
      + " independent chunk. Keep planning, App/cross-session operations, original-handle observation, Goal controls,"
      + " live owner/CAS/account or deployment decisions and acceptance native; do not delegate those controls."
    : "Cost reminder: " + state.steps + " consecutive native steps at about " + size
      + " tokens of context; every step re-sends this whole thread. If the current chunk still needs three or more"
      + " tool calls, delegate it to one worker through nokiy_execute; keep short checks, full-context judgment,"
      + " App/cross-session operations, original-handle observation, Goal controls, live owner/CAS/account or deployment"
      + " decisions and acceptance native; do not delegate those controls.";
  return { state: Object.freeze({ ...state, steps: 0 }), text,
    diagnostic: { steps: state.steps, input_tokens: state.inputTokens } };
}

export const DEFAULT_PROFILES = Object.freeze(["luna", "sol", "astra"].map((family) => Object.freeze({
  slug: `nokiy-direct-${family}`, display_name: `Nokiy Ultra - ${family === "sol" ? "gpt-6.1-sol" : `gpt-6-${family}`}`,
  workflow: "nokiy-direct",
  coordinator: Object.freeze({ model: "gpt-6-astra", reasoning_effort: "ultra" }),
  worker: Object.freeze({ model: family === "sol" ? "gpt-6.1-sol" : `gpt-6-${family}`, reasoning_effort: "max", service_tier: "default" }),
})));

const record = (value, keys, label, optional = []) => {
  if (!value || typeof value !== "object" || Array.isArray(value)
      || Object.keys(value).some((key) => ![...keys, ...optional].includes(key))
      || keys.some((key) => !Object.hasOwn(value, key))) {
    throw new TypeError(`Invalid ${label} fields`);
  }
};

export function resolveWorkflowProfiles(value, catalog) {
  // Arrays are the resolved form returned by this function; revalidate against
  // the current catalog when reused, not against a previous catalog snapshot.
  const config = Array.isArray(value) ? { schema_version: "nokiy_workflow_profiles_v1", profiles: value } : value;
  record(config, ["schema_version", "profiles"], "workflow configuration");
  if (config.schema_version !== "nokiy_workflow_profiles_v1" || !Array.isArray(config.profiles)
      || config.profiles.length < 1 || config.profiles.length > 12) throw new TypeError("Invalid workflow profile schema or count");
  const slugs = new Set();
  return Object.freeze(config.profiles.map((profile) => {
    record(profile, ["slug", "display_name", "workflow", "coordinator", "worker"], "profile",
      ["worker_selector", "auto_compact_token_limit", "tool_output_token_limit"]);
    const plain = profile.workflow === "nokiy";
    const direct = profile.workflow === "nokiy-direct";
    const efficient = profile.slug === "nokiy-efficient" && direct;
    if (!plain && !direct) throw new TypeError("Unimplemented Nokiy workflow");
    if (typeof profile.slug !== "string" || slugs.has(profile.slug)
        || (plain && (!/^nokiy(?:-[a-z0-9]+)*$/.test(profile.slug) || profile.slug.startsWith("nokiy-direct")))
        || (direct && !efficient && !/^nokiy-direct(?:-[a-z0-9]+)*$/.test(profile.slug))) throw new TypeError("Invalid or duplicate Nokiy slug");
    slugs.add(profile.slug);
    if (typeof profile.display_name !== "string" || !profile.display_name.trim()
        || profile.display_name.length > 100) throw new TypeError("Invalid display name");
    record(profile.coordinator, ["model", "reasoning_effort"], "coordinator");
    if (profile.coordinator.model !== (efficient ? "gpt-6.1-sol" : "gpt-6-astra")) {
      throw new TypeError("Efficient requires Sol; other workflows require Astra");
    }
    record(profile.worker, ["model", "reasoning_effort", "service_tier"], "worker");
    const { model, reasoning_effort, service_tier } = profile.worker;
    if (typeof model !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(model) || model.startsWith("nokiy")
        || !Array.isArray(catalog?.models)) throw new TypeError("Invalid or recursive worker model");
    const matches = catalog.models.filter((item) => item?.slug === model);
    if (matches.length !== 1 || !Array.isArray(matches[0].supported_reasoning_levels)
        || !matches[0].supported_reasoning_levels.some((level) => level?.effort === reasoning_effort)) {
      throw new TypeError("Worker model or reasoning effort is not uniquely supported by catalog");
    }
    const coordinatorMatches = catalog.models.filter((item) => item?.slug === profile.coordinator.model);
    if (coordinatorMatches.length !== 1 || !Array.isArray(coordinatorMatches[0].supported_reasoning_levels)
        || !coordinatorMatches[0].supported_reasoning_levels.some((level) => level?.effort === profile.coordinator.reasoning_effort)) {
      throw new TypeError("Coordinator model or reasoning effort is not uniquely supported by catalog");
    }
    const compact = profile.auto_compact_token_limit;
    // Profiles resolve against several catalog projections: the official bundled candidate at
    // admission (Astra 272K) and the operator-overlaid served catalog (500K). Only the model's
    // hard ceiling is projection-independent; alignWorkflowContext enforces the served window.
    const ceiling = coordinatorMatches[0].max_context_window ?? coordinatorMatches[0].context_window ?? 0;
    if (compact !== undefined && (!Number.isSafeInteger(compact) || compact < 100_000
        || !(compact < ceiling))) {
      throw new TypeError("Coordinator auto-compact limit must be an integer below its maximum context window");
    }
    // Codex shows a tool output up to min(requested max_output_tokens, catalog truncation_policy);
    // the request default stays 10000, so only explicit large requests (Nokiy results) use more.
    const outputLimit = profile.tool_output_token_limit;
    if (outputLimit !== undefined && (!Number.isSafeInteger(outputLimit) || outputLimit < 10_000
        || outputLimit > 262_144)) {
      throw new TypeError("Tool output token limit must be an integer from 10000 to 262144");
    }
    if (!["default", "priority", "ultrafast"].includes(service_tier)) throw new TypeError("Unsupported worker service tier");
    let worker_selector;
    if (Object.hasOwn(profile, "worker_selector")) {
      record(profile.worker_selector, ["default_effort", "models"], "worker selector", ["reasoning_efforts"]);
      const { default_effort, models, reasoning_efforts } = profile.worker_selector;
      if (default_effort !== "medium") throw new TypeError("Worker selector default must be medium");
      const levels = Object.hasOwn(models, "xhigh") ? ["low", "medium", "high", "xhigh"] : ["low", "medium", "high"];
      record(models, levels, "worker selector models");
      if (reasoning_efforts !== undefined) {
        record(reasoning_efforts, levels, "worker selector reasoning efforts");
        if (Object.values(reasoning_efforts).some((effort) => typeof effort !== "string")) {
          throw new TypeError("Worker selector reasoning efforts must be explicit strings");
        }
      }
      const expected = levels.length === 4
        ? { low: "gpt-6-luna", medium: "gpt-6.1-terra", high: "gpt-6.1-sol", xhigh: "gpt-6-astra" }
        : { low: "gpt-6-luna", medium: "gpt-6.1-sol", high: "gpt-6-astra" };
      for (const effort of levels) {
        const selected = models[effort];
        if (selected !== expected[effort] || selected.startsWith("nokiy")) {
          throw new TypeError("Unknown or recursive worker selector model");
        }
        const found = catalog.models.filter((item) => item?.slug === selected);
        if (found.length !== 1 || !Array.isArray(found[0].supported_reasoning_levels)
            || !found[0].supported_reasoning_levels.some((level) => level?.effort === (reasoning_efforts?.[effort] ?? "max"))) {
          throw new TypeError("Selector worker reasoning effort is not uniquely supported by catalog");
        }
      }
      if (model !== models[default_effort] || reasoning_effort !== (reasoning_efforts?.[default_effort] ?? "max")) {
        throw new TypeError("Configured worker must match selector default and reasoning effort");
      }
      worker_selector = Object.freeze({ default_effort, models: Object.freeze({ ...models }),
        ...(reasoning_efforts !== undefined ? { reasoning_efforts: Object.freeze({ ...reasoning_efforts }) } : {}) });
    }
    return Object.freeze({ slug: profile.slug, display_name: profile.display_name, workflow: profile.workflow,
      coordinator: Object.freeze({ model: profile.coordinator.model, reasoning_effort: profile.coordinator.reasoning_effort }),
      worker: Object.freeze({ model, reasoning_effort, service_tier }),
      ...(worker_selector ? { worker_selector } : {}),
      ...(compact !== undefined ? { auto_compact_token_limit: compact } : {}),
      ...(outputLimit !== undefined ? { tool_output_token_limit: outputLimit } : {}) });
  }));
}
