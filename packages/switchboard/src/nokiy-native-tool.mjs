import { StringDecoder } from "node:string_decoder";
import { accessSync, constants, existsSync, readFileSync, statSync } from "node:fs";
import { lstat, mkdir, readdir, readFile, readlink, rename, rm, symlink, writeFile } from "node:fs/promises";
import { posix as pathPosix } from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { parse } from "smol-toml";
import Ajv from "ajv";
import { SwitchboardError } from "./shared.mjs";
import { prepareResultDelivery, pendingResultDelivery, resultDeliveryInstructions, resultDeliveryProjector } from "./nokiy-result-delivery.mjs";
import { coordinatorOnly, coordinatorProjection, checkCoordinatorCall, hasNativeGoalControl, projectGoalControl } from "./nokiy-coordinator-policy.mjs";
import { efficientNativeLoop, EFFICIENT_NATIVE_GUIDANCE, ULTRA_DISPATCH_GUIDANCE } from "./nokiy-workflow-profiles.mjs";
import { NOKIY_OBSERVE, cellOriginVisible, hasNativeSessionControl, observationControl, observationTool, projectNativeObservation, retainNativeObservations } from "./nokiy-native-observation.mjs";

export const NOKIY_TOOL = "nokiy_execute";
export const NOKIY_NATIVE = "nokiy_native";
const MAX_PAYLOAD_BYTES = 96 * 1024;
export function lowerAcceptanceIntent(spec) {
  if (!spec || typeof spec !== "object" || !Object.hasOwn(spec, "intent")) return spec;
  const intent = spec.intent;
  const required = ["workspace", "artifact_root", "mission", "read_scopes", "verifier"];
  const optional = ["context_summary", "read_directories", "surface_id", "parent_source_targets"];
  const issues = [];
  const issue = (field, expected) => issues.push({ field: "post_batch_verifier." + field, expected });
  const safeKey = (key) => /^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(key) ? key : "<invalid>";
  for (const key of Object.keys(spec)) if (key !== "intent") issue(safeKey(key), "no sibling fields beside intent");
  if (!intent || typeof intent !== "object" || Array.isArray(intent)) issue("intent", "object");
  else {
    for (const key of Object.keys(intent)) if (![...required, ...optional].includes(key)) {
      issue("intent." + safeKey(key), "unsupported override; remove this field");
    }
    for (const key of ["workspace", "artifact_root"]) {
      if (typeof intent[key] !== "string" || !intent[key].startsWith("/")) issue("intent." + key, "absolute path string");
    }
    if (!intent.mission || typeof intent.mission !== "object" || Array.isArray(intent.mission)) {
      issue("intent.mission", "mission object, not text");
    } else if (typeof intent.mission.objective !== "string" || !intent.mission.objective.trim()) {
      issue("intent.mission.objective", "nonempty string");
    }
    if (!Array.isArray(intent.read_scopes) || !intent.read_scopes.length
        || intent.read_scopes.some((value) => typeof value !== "string" || !value)) {
      issue("intent.read_scopes", "nonempty array of source path strings");
    }
    if (!intent.verifier || typeof intent.verifier !== "object" || Array.isArray(intent.verifier)) {
      issue("intent.verifier", "one typed verifier object, not text or an array");
    }
    if (Object.hasOwn(intent, "context_summary") && (typeof intent.context_summary !== "string" || !intent.context_summary.trim())) {
      issue("intent.context_summary", "nonempty string");
    }
  }
  if (issues.length) {
    const missing = required.filter((key) => !intent || typeof intent !== "object" || !Object.hasOwn(intent, key));
    throw fail("Acceptance intent requires explicit workspace, artifact_root, mission, read_scopes and one verifier; no identity or effect overrides."
      + (missing.length ? " Missing after batch defaults: " + missing.join(", ") + "." : " Invalid field type or unsupported override.")
      + " Correct: " + issues.slice(0, 16).map(({ field, expected }) => field + " (" + expected + ")").join("; ") + ".",
    { native_tool_validation: { stage: "acceptance_intent", issues: issues.slice(0, 16) } });
  }
  const context = intent.context_summary ?? intent.mission.objective;
  return {
    draft: { workspace: intent.workspace, artifact_root: intent.artifact_root,
      authority_effect: "none", prompt: context, timeout_seconds: null },
    action: { mission: structuredClone(intent.mission), context_summary: context,
      operations: ["read", "command"], read_scopes: structuredClone(intent.read_scopes),
      write_scopes: [], target_paths: [], source_read: true, command_templates: [],
      forbidden_effects: ["deployment", "broker", "order"], verifier_commands: [structuredClone(intent.verifier)],
      ...(Object.hasOwn(intent, "read_directories") ? { read_directories: structuredClone(intent.read_directories) } : {}) },
    ...Object.fromEntries(["surface_id", "parent_source_targets"].filter((key) => Object.hasOwn(intent, key))
      .map((key) => [key, structuredClone(intent[key])])),
  };
}
// Measured 2026-10-25 (I26 V418): the Efficient coordinator dispatched at 170 s after three exploration
// rounds and a 1,186-token draft/action plan, while native Sol spawned its child at about 30 s. A member
// intent carries only what the coordinator must decide; the adapter derives the mechanical fields, so a
// dispatch needs no source pre-reading, digest round or grant bookkeeping. Read access stays explicit.
const MEMBER_INTENT_KEYS = ["workspace", "task", "write", "read", "verifier", "id", "context", "operator_message", "surface_id"];
const relativeSourceName = (name) => typeof name === "string" && name.length > 0 && name.length <= 512
  && !name.startsWith("/") && !/[*?\[\]{}\\\x00\r\n]/.test(name)
  && name.split("/").every((part) => part && part !== "." && part !== ".." && part !== ".git");
const memberLeaf = (index) => (index < 26 ? String.fromCharCode(97 + index) : `m${index + 1}`);
const MEMBER_OPERATOR_REFERENCE = "\n\nOperator request (context; change only your assigned scope):\n{{nokiy:user_message}}\n";

export function lowerMemberIntents(members, batchRoot, { operatorMessage = true, exists = existsSync } = {}) {
  if (!Array.isArray(members) || !members.some((member) => member && typeof member === "object"
      && Object.hasOwn(member, "intent"))) return members;
  if (typeof batchRoot !== "string" || !batchRoot.startsWith("/") || batchRoot.startsWith("//")) {
    throw fail("Member intents require an absolute run_batch artifact_root; member artifact roots are derived beneath it.");
  }
  const ids = new Set();
  return members.map((member, index) => {
    if (!member || typeof member !== "object" || !Object.hasOwn(member, "intent")) return member;
    const intent = member.intent;
    const valid = Object.keys(member).length === 1 && intent && typeof intent === "object" && !Array.isArray(intent);
    const list = (key) => (valid && Object.hasOwn(intent, key) ? intent[key] : []);
    const write = list("write");
    const read = list("read");
    if (!valid || Object.keys(intent).some((key) => !MEMBER_INTENT_KEYS.includes(key))
        || typeof intent.workspace !== "string" || !intent.workspace.startsWith("/") || intent.workspace.startsWith("//")
        || typeof intent.task !== "string" || !intent.task.trim()
        || !Array.isArray(write) || write.some((name) => !relativeSourceName(name)) || new Set(write).size !== write.length
        || !Array.isArray(read) || read.some((name) => typeof name !== "string" || !name.trim() || name.length > 4096
          || /[\x00\r\n]/.test(name))
        || write.length + read.length === 0
        || (Object.hasOwn(intent, "verifier") && (!intent.verifier || typeof intent.verifier !== "object"
          || Array.isArray(intent.verifier)))
        || (Object.hasOwn(intent, "id") && (typeof intent.id !== "string" || !/^[a-z0-9][a-z0-9_-]{0,31}$/.test(intent.id)))
        || (Object.hasOwn(intent, "context") && (typeof intent.context !== "string" || !intent.context.trim()))
        || (Object.hasOwn(intent, "operator_message") && typeof intent.operator_message !== "boolean")
        || (Object.hasOwn(intent, "surface_id") && (typeof intent.surface_id !== "string" || !intent.surface_id))) {
      throw fail("Member intent requires {intent:{workspace,task}} with write and/or read; optional verifier, id, context, operator_message and surface_id. write entries are exact workspace-relative files. No artifact, mission, permission, model or effect overrides.");
    }
    const id = intent.id ?? memberLeaf(index);
    if (ids.has(id)) throw fail(`Member intent id ${JSON.stringify(id)} repeats; member artifact roots must be distinct.`);
    ids.add(id);
    const task = intent.task.trim();
    const writer = write.length > 0;
    const existing = write.filter((name) => exists(pathPosix.join(intent.workspace, name)));
    const directories = [...new Set(read.filter((name) => name.endsWith("/**")).map((name) => name.slice(0, -3)))]
      .filter((name) => relativeSourceName(name));
    const summary = task.length > 600 ? `${task.slice(0, 597)}...` : task;
    return {
      draft: { workspace: intent.workspace, artifact_root: pathPosix.join(batchRoot, id),
        authority_effect: writer ? "workspace" : "none",
        prompt: task + (intent.operator_message !== false && operatorMessage ? MEMBER_OPERATOR_REFERENCE : ""),
        timeout_seconds: null },
      action: {
        mission: { mission_id: pathPosix.basename(batchRoot) || "nokiy-batch", task_id: id, mode: "DELIVERY",
          objective: summary, current_predicate: writer ? "Assigned scope is not yet implemented and verified."
            : "Assigned question is not yet answered from evidence." },
        context_summary: intent.context?.trim() ?? summary,
        operations: ["read", "command", ...(existing.length ? ["modify"] : []),
          ...(existing.length < write.length ? ["create"] : [])],
        read_scopes: [...new Set([...write, ...read])],
        ...(directories.length ? { read_directories: directories } : {}),
        write_scopes: [...write], target_paths: [...write], source_read: true, command_templates: [],
        ...(Object.hasOwn(intent, "verifier") ? { verifier_commands: [structuredClone(intent.verifier)] } : {}),
        terminal_delivery: writer ? "evidence_only" : "assistant_reply",
        forbidden_effects: ["deployment", "broker", "order"],
      },
      ...(Object.hasOwn(intent, "surface_id") ? { surface_id: intent.surface_id } : {}),
    };
  });
}

// Measured 2026-10-26 (V436 Ultra, I26): the coordinator's 5,912-character run_batch repeated one long
// workspace prefix about ten times, carried two 91%-identical member contexts and restated a final-gate
// artifact_root, mission and read scope that the batch already fixes. Coordinator output tokens are the
// dispatch call's critical path, so these defaults are filled before any validation; the lowered
// draft/action packets equal what the explicit fields produce, and every host check still applies.
const INTENT_DEFAULT_KEYS = ["workspace", "context"];
export const FINAL_GATE_LEAF = "final-gate";
const intentOf = (entry) => (entry && typeof entry === "object" && !Array.isArray(entry) && Object.hasOwn(entry, "intent")
  && entry.intent && typeof entry.intent === "object" && !Array.isArray(entry.intent) ? entry.intent : null);
const MEMBER_INTENT_SHARED_GUIDE = ' Write what every member shares once: run_batch.intent_defaults:{"workspace":"/absolute source root","context":"shared findings or decision fields"} gives workspace to intents that omit it and puts context, then a newline, before each member\'s own context (or uses it alone), so each member intent states only its differences.';

// Read-only view of grants already declared by supported member forms. Compact
// transport still reaches the host unchanged, including its disjoint-field checks.
function acceptanceMemberSources(payload) {
  if (Object.hasOwn(payload, "shared") || Object.hasOwn(payload, "member_deltas")) {
    const shared = payload.shared;
    if (Object.hasOwn(payload, "members") || shared?.schema_version !== "nokiy_batch_shared_v1"
        || !shared.draft || !shared.action || !Array.isArray(payload.member_deltas)) return [];
    return payload.member_deltas.map((delta) => {
      if (!delta?.draft || !delta.action || Object.hasOwn(delta.draft, "workspace")) return null;
      const action = {};
      for (const key of ["read_scopes", "read_directories"]) {
        if (Object.hasOwn(shared.action, key) && Object.hasOwn(delta.action, key)) {
          throw fail("Compact acceptance source fields must be disjoint; no overrides or scope unions: " + key + ".");
        }
        action[key] = Object.hasOwn(delta.action, key) ? delta.action[key] : shared.action[key];
      }
      return { workspace: shared.draft.workspace, artifact_root: delta.draft.artifact_root, ...action };
    });
  }
  return (Array.isArray(payload.members) ? payload.members : []).map((member, index) => {
    const intent = intentOf(member);
    if (intent) return { workspace: intent.workspace,
      artifact_root: typeof payload.artifact_root === "string"
        ? pathPosix.join(payload.artifact_root, intent.id ?? memberLeaf(index)) : undefined,
      read_scopes: [...(Array.isArray(intent.write) ? intent.write : []), ...(Array.isArray(intent.read) ? intent.read : [])] };
    if (!member?.draft || !member.action) return null;
    return { workspace: member.draft.workspace, artifact_root: member.draft.artifact_root,
      read_scopes: member.action.read_scopes, read_directories: member.action.read_directories };
  });
}

export function applyBatchIntentDefaults(payload) {
  if (!payload || typeof payload !== "object" || payload.operation !== "run_batch") return payload;
  const members = Array.isArray(payload.members) ? payload.members : [];
  const verifierIntent = intentOf(payload.post_batch_verifier);
  const hasDefaults = Object.hasOwn(payload, "intent_defaults");
  if (!hasDefaults && !verifierIntent) return payload;
  const defaults = hasDefaults ? payload.intent_defaults : {};
  if (hasDefaults && (!defaults || typeof defaults !== "object" || Array.isArray(defaults)
      || !members.some((member) => intentOf(member))
      || Object.keys(defaults).some((key) => !INTENT_DEFAULT_KEYS.includes(key))
      || (Object.hasOwn(defaults, "workspace") && (typeof defaults.workspace !== "string"
        || !defaults.workspace.startsWith("/") || defaults.workspace.startsWith("//")))
      || (Object.hasOwn(defaults, "context") && (typeof defaults.context !== "string" || !defaults.context.trim())))) {
    throw fail('run_batch.intent_defaults accepts only {"workspace":"/absolute","context":"nonempty shared context"} and needs at least one intent member.');
  }
  const shared = typeof defaults.context === "string" ? defaults.context.trim() : "";
  const { intent_defaults: _defaults, ...out } = payload;
  if (Array.isArray(payload.members)) {
    out.members = members.map((member) => {
      const intent = intentOf(member);
      if (!intent) return member;
      const filled = { ...intent };
      if (!Object.hasOwn(filled, "workspace") && Object.hasOwn(defaults, "workspace")) filled.workspace = defaults.workspace;
      if (shared && !Object.hasOwn(filled, "context")) filled.context = shared;
      else if (shared && typeof filled.context === "string" && filled.context.trim()) {
        filled.context = shared + "\n" + filled.context.trim();
      }
      return { ...member, intent: filled };
    });
  }
  if (verifierIntent) {
    const filled = { ...verifierIntent };
    const sources = acceptanceMemberSources(out);
    const workspaces = new Set(sources.map((source) => source?.workspace));
    if (!Object.hasOwn(filled, "workspace")) {
      const workspace = defaults.workspace ?? (workspaces.size === 1 ? [...workspaces][0] : undefined);
      if (typeof workspace === "string") filled.workspace = workspace;
    }
    const root = out.artifact_root;
    const rootValid = typeof root === "string" && root.startsWith("/") && !root.startsWith("//");
    if (!Object.hasOwn(filled, "artifact_root") && rootValid) {
      const gate = pathPosix.join(root, FINAL_GATE_LEAF);
      const taken = sources.map((source) => source?.artifact_root);
      if (taken.includes(gate)) {
        throw fail("post_batch_verifier.intent.artifact_root defaults to <artifact_root>/" + FINAL_GATE_LEAF
          + ", which a member already uses; supply artifact_root or rename that member.");
      }
      filled.artifact_root = gate;
    }
    if (!Object.hasOwn(filled, "mission")) {
      filled.mission = { mission_id: (rootValid && pathPosix.basename(root)) || "nokiy-batch", task_id: FINAL_GATE_LEAF,
        mode: "DELIVERY", objective: "Run the declared combined acceptance after every member joins.",
        current_predicate: "Combined acceptance has not passed." };
    }
    // Every member must already declare read grants in the same workspace. An
    // ordinary write_scopes/target_paths entry alone never creates a read grant.
    if (!Object.hasOwn(filled, "read_scopes") && typeof filled.workspace === "string" && sources.length
        && sources.every((source) => source?.workspace === filled.workspace
          && Array.isArray(source.read_scopes) && source.read_scopes.length
          && source.read_scopes.every((name) => typeof name === "string" && name))) {
      const declaredDirectories = sources.flatMap((source) => Array.isArray(source.read_directories)
        ? source.read_directories.filter(relativeSourceName) : []);
      const names = [...sources.flatMap((source) => source.read_scopes),
      ...declaredDirectories.map((directory) => directory + "/**"),
      ...(Array.isArray(filled.parent_source_targets) ? filled.parent_source_targets : [])]
        .filter((name) => typeof name === "string" && name && !name.startsWith("/"));
      const directories = [...new Set(names.filter((name) => name.endsWith("/**")).map((name) => name.slice(0, -3)))]
        .filter((name) => relativeSourceName(name));
      const scopes = [...new Set(names)].filter((name) => name.endsWith("/**")
        || !directories.some((directory) => name.startsWith(directory + "/")));
      if (scopes.length) {
        filled.read_scopes = scopes;
        if (directories.length && !Object.hasOwn(filled, "read_directories")) filled.read_directories = directories;
      }
    }
    out.post_batch_verifier = { ...out.post_batch_verifier, intent: filled };
  }
  return out;
}

// An omitted catalog digest is pinned from current bytes here; the host re-reads and verifies them,
// so a concurrent catalog change still fails closed before any worker starts.
export function pinCatalogReferences(value, cache = new Map()) {
  if (Array.isArray(value)) return value.map((entry) => pinCatalogReferences(entry, cache));
  if (!value || typeof value !== "object") return value;
  const out = Object.fromEntries(Object.entries(value).map(([key, entry]) => [key, pinCatalogReferences(entry, cache)]));
  if (!Array.isArray(out.verifier_commands)) return out;
  out.verifier_commands = out.verifier_commands.map((command) => {
    const ref = command?.catalog_ref;
    if (!ref || typeof ref !== "object" || Array.isArray(ref) || Object.hasOwn(ref, "sha256")
        || typeof ref.command !== "string" || !ref.command) return command;
    if (typeof ref.path !== "string" || !ref.path.startsWith("/")) throw fail("catalog_ref.path must be an absolute catalog file.");
    if (!cache.has(ref.path)) {
      let bytes;
      try {
        const stats = statSync(ref.path);
        if (!stats.isFile() || stats.size > 1024 * 1024) throw new Error("bounded regular file required");
        bytes = readFileSync(ref.path);
      } catch {
        throw fail("catalog_ref.path is not a readable regular catalog file of at most 1 MiB; no worker was started.");
      }
      cache.set(ref.path, createHash("sha256").update(bytes).digest("hex"));
    }
    return { ...command, catalog_ref: { ...ref, sha256: cache.get(ref.path) } };
  });
  return out;
}
const nativeArgumentValidators = new Map();
function nativeArgumentValidator(schema) {
  const key = JSON.stringify(schema);
  let validate = nativeArgumentValidators.get(key);
  if (!validate) {
    validate = new Ajv({ strict: false, allErrors: false, validateFormats: false }).compile(schema);
    nativeArgumentValidators.set(key, validate);
    if (nativeArgumentValidators.size > 32) nativeArgumentValidators.delete(nativeArgumentValidators.keys().next().value);
  }
  return validate;
}
const nativeExecutionDescription = "Execute an already-authorized deterministic project command through this same Codex thread's advertised native exec_command, without a worker, Nokiy preparation or DCF command templates. Use for task establishment, existing project validation, admitted deployment and formal closeout; delegate source navigation, implementation and semantic analysis to the Nokiy worker. Each call is a separate model round trip that re-reads this whole thread, so put every step you can already specify into one cmd: chain dependent steps with && or explicit checks, keep independent checks in the same script, print only what the next decision needs, and include cleanup such as closing a browser session. This is a native command, not a grant: native sandbox, approvals, command interception and project owner/lease/CAS controls still apply. Preserve HOME/CODEX_HOME and the assigned model; do not impersonate a thread or disable checks. Prefer existing admitted deployment/closeout plans when available. Code mode observes only the original process handle with empty stdin until terminal or an explicit uncertain result, without a task deadline. Short terminal output remains unchanged; longer output retains its head, tail and original recovery handle. Recover only the original handle after uncertainty, never replay the command. Success of a command is not business acceptance. REJECTED_LOCAL_TOOL_CALL means the proposed operation never started: correct only already-authorized unfinished input and never repeat the same input.";

function nativeExecutionSchema(tool) {
  if (tool.type === "function") {
    if (tool.parameters?.type !== "object" || tool.parameters.properties?.cmd?.type !== "string"
        || !tool.parameters.required?.includes("cmd")) return null;
    return structuredClone(tool.parameters);
  }
  const declaration = tool.description?.match(/declare const tools: \{ exec_command\(args: \{([\s\S]*?)\}\): Promise</)?.[1];
  if (!declaration) {
    // Codex omits deferred nested tools from a large code-mode description while keeping them
    // callable on the global tools object; bind only the stable core exec_command fields then.
    return /deferred nested tools may be omitted/i.test(tool.description ?? "")
      ? { type: "object", properties: { cmd: { type: "string" }, yield_time_ms: { type: "number" },
        max_output_tokens: { type: "number" } }, required: ["cmd"], additionalProperties: false }
      : null;
  }
  const fields = declaration.replace(/\/\/[^\n]*/g, "").split(";").map((field) => field.trim()).filter(Boolean);
  const supported = { cmd: "string", workdir: "string", shell: "string", login: "boolean", tty: "boolean",
    max_output_tokens: "number", yield_time_ms: "number" };
  const properties = {}, required = [];
  for (const field of fields) {
    const match = field.match(/^(\w+)(\?)?:\s*([\s\S]+)$/);
    if (!match) return null;
    const [, name, optional, type] = match;
    if (supported[name] === type) properties[name] = { type };
    // Optional unfamiliar fields remain owned by the native tool, not invented here.
    else if (!optional) return null;
    if (!optional) required.push(name);
  }
  return properties.cmd?.type === "string" && required.includes("cmd")
    ? { type: "object", properties, required, additionalProperties: false } : null;
}

function nativeExecutionControl(body, target) {
  if (![null, "functions"].includes(target.namespace ?? null)) return null;
  function visit(tools, namespace = null, owners = []) {
    for (const tool of tools || []) {
      if (tool?.type === "namespace" && tool.name === "functions" && namespace === null) visit(tool.tools, tool.name, owners);
      else if (namespace === (target.namespace ?? null) && tool?.name === target.name && tool.type === target.type) owners.push(tool);
    }
    return owners;
  }
  // Historical advertisements can update the same binding. Keep ambiguity within
  // one advertisement, but do not count superseded declarations as new owners.
  let owners = [];
  for (const item of body.input || []) {
    if (!["additional_tools", "tool_search_output"].includes(item?.type)) continue;
    const update = visit(item.tools);
    if (update.length) owners = update;
  }
  const current = visit(body.tools);
  if (current.length) owners = current;
  // Only an already bound same-connection continuation can carry an omitted capability.
  const schema = owners.length === 1 ? nativeExecutionSchema(owners[0])
    : owners.length === 0 ? target.executionSchema ?? null : null;
  if (!schema) return null;
  try {
    return Object.freeze({ schema, validate: nativeArgumentValidator(schema) });
  }
  catch { return null; }
}

function nativeExecutionTool(target, control) {
  return target.type === "custom"
    ? { type: "custom", name: NOKIY_NATIVE, description: `${nativeExecutionDescription} Input is JSON with the native arguments: ${JSON.stringify(control.schema)}`, format: { type: "text" } }
    : { type: "function", name: NOKIY_NATIVE, description: nativeExecutionDescription, parameters: structuredClone(control.schema), strict: false };
}

function projectNativeExecution(item, projection) {
  const control = projection.nativeExecution;
  const expectedType = projection.target.type === "custom" ? "custom_tool_call" : "function_call";
  if (!control || item.type !== expectedType) throw fail("An advertised same-thread native execution capability is required.");
  const raw = item.type === "custom_tool_call" ? item.input : item.arguments;
  let args;
  try { if (typeof raw === "string" && Buffer.byteLength(raw) <= MAX_PAYLOAD_BYTES) args = JSON.parse(raw); }
  catch { /* The typed failure below contains no command text. */ }
  if (!args || typeof args !== "object" || Array.isArray(args) || !control.validate(args)
      || Object.keys(args).some((name) => !Object.hasOwn(control.schema.properties, name))) {
    throw fail("Native execution arguments must match the advertised exec_command schema; no command was started.");
  }
  const value = JSON.stringify(args);
  return { ...item, name: projection.target.name,
    ...(projection.target.namespace ? { namespace: projection.target.namespace } : {}),
    [item.type === "custom_tool_call" ? "input" : "arguments"]: item.type === "custom_tool_call"
      ? renderNativeExecution(args, true) : value };
}
const BATCH_ARTIFACT_LAYOUT = "Batch member draft.artifact_root directories must be pairwise disjoint: neither identical nor ancestors of one another. The batch artifact_root is a canonical absolute plan directory and may be their common parent; if that one leaf is missing but its parent exists, the host creates it together with the member directories. For example, use /task/batch for the plan and /task/batch/a and /task/batch/b for members. Missing direct member directories are created by the host after whole-batch input validation, without a separate setup tool call; no parent or workspace directory is created. Other member directories must already exist. Original-plan recovery never creates directories or dispatches missing members. Ordinary members may use different draft.workspace directories when no artifact_root lies inside any member workspace.";
const fail = (message, details) => new SwitchboardError("NOKIY_NATIVE_TOOL_INVALID", message, 422, details);
const MEMBER_INTENT_GUIDE = ' Member intent (preferred dispatch form): a members item may be {"intent":{"workspace":"/absolute source root this member works in","task":"assignment: branch goal, acceptance and boundaries","write":["exact workspace-relative files to edit or create"],"read":["other exact relative files, named absolute external files, or subdir/** for discovery inside an authorized subtree"],"verifier":{"catalog_ref":{"path":"/absolute catalog.json","command":"key"}}}} or with one inline {"argv":[...]} verifier, plus optional id (artifact leaf; default a, b, c...), context (already-observed findings), operator_message:false (otherwise the operator\'s latest message is appended to the prompt by reference) and surface_id. The adapter derives artifact_root beneath the batch artifact_root, mission, authority_effect, modify/create from current file existence, read_scopes (write files plus read), read_directories (relative subdir/** entries), source_read, evidence-only completion for writers and forbidden_effects. Preparation, DCF/J-Space admission, cross-member overlap and owner checks are unchanged. Use full draft/action members only for settings an intent cannot express.';

// A coordinator may reference the user's own task text instead of re-generating it
// token by token. Only prompt/context_summary strings are expanded, deterministically,
// before the host computes request identity; nothing else is read or granted.
const USER_REFERENCE = /\{\{nokiy:user_message(?:_after:([^{}\r\n]{1,200}))?\}\}/g;
const USER_REFERENCE_FIELDS = new Set(["prompt", "context_summary"]);
const unresolvedReference = (message) => new SwitchboardError("NOKIY_USER_REFERENCE_UNRESOLVED", message, 422);

export function userMessageTexts(body) {
  const texts = [];
  for (const item of Array.isArray(body?.input) ? body.input : []) {
    if (item?.role !== "user" || (item.type !== undefined && item.type !== "message")) continue;
    const parts = typeof item.content === "string" ? [item.content]
      : Array.isArray(item.content) ? item.content.filter((part) => part?.type === "input_text"
        && typeof part.text === "string").map((part) => part.text) : [];
    const text = parts.join("\n");
    // Skip host-injected context blocks; they are not the operator's task text.
    if (!text.trim() || /^\s*</.test(text) || text.startsWith("# AGENTS.md instructions")) continue;
    texts.push(text);
  }
  return texts;
}

function resolveUserReference(texts, marker) {
  if (marker === undefined) {
    if (!texts.length) throw unresolvedReference("No operator message is available for {{nokiy:user_message}}.");
    return texts.at(-1);
  }
  for (let index = texts.length - 1; index >= 0; index -= 1) {
    const at = texts[index].indexOf(marker);
    if (at >= 0) return texts[index].slice(at + marker.length).replace(/^[ \t]*\r?\n/, "");
  }
  throw unresolvedReference(`No operator message contains the exact marker for {{nokiy:user_message_after:...}}: ${JSON.stringify(marker.slice(0, 80))}.`);
}

function expandUserReferences(value, texts, key = null) {
  if (typeof value === "string") {
    if (!USER_REFERENCE_FIELDS.has(key) || !value.includes("{{nokiy:user_message")) return value;
    return value.replace(USER_REFERENCE, (_match, marker) => resolveUserReference(texts, marker));
  }
  if (Array.isArray(value)) return value.map((entry) => expandUserReferences(entry, texts));
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value).map(([name, entry]) => [name, expandUserReferences(entry, texts, name)]));
}
const quote = (value) => `'${value.replaceAll("'", "'\\''")}'`;
const contractBytes = readFileSync(new URL("./nokiy-native-contract.json", import.meta.url));
const contract = JSON.parse(contractBytes);
function actionSchema() {
  const schema = structuredClone(contract.action);
  schema.properties.source_queries = {
    type: "array", minItems: 1, maxItems: 16,
    items: { type: "string", minLength: 1, maxLength: 256 },
    description: "Optional literal reference queries for unconfigured local workspaces, only within exact read_scopes. Resolves to source-bound partial excerpts; does not grant access or replace dependency reads. Do not combine with source_sections. No match or excessive match volume retains ordinary reads.",
  };
  schema.properties.verifier_commands = { type: "array", items: { anyOf: [
    { type: "object", required: ["catalog_ref"], additionalProperties: false,
      properties: { catalog_ref: { type: "object", required: ["path", "command"],
        additionalProperties: false, properties: {
          path: { type: "string", minLength: 1 },
          sha256: { type: "string", pattern: "^[0-9a-f]{64}$" },
          command: { type: "string", minLength: 1 },
        } } },
      description: "Exact catalog reference only. No sibling scratch_root, argv or other override; use the pinned catalog command unchanged. Omit sha256 to let the adapter pin the catalog's current bytes." },
    { type: "object", required: ["argv"], additionalProperties: true,
      not: { required: ["catalog_ref"] },
      properties: { argv: { type: "array", minItems: 1, items: { type: "string" } } },
      description: "Inline verifier grant, without catalog_ref; ordinary host validation and admission still apply." },
  ] } };
  return schema;
}
function memberIntentSchema() {
  return { type: "object", required: ["task"], additionalProperties: false,
    description: "Preferred compact member; workspace may come from run_batch.intent_defaults. The adapter derives artifact_root, mission, operations, scopes, source_read, delivery and forbidden effects.",
    properties: {
      workspace: { type: "string" }, task: { type: "string" },
      write: { type: "array", uniqueItems: true, items: { type: "string" } },
      read: { type: "array", items: { type: "string" } },
      verifier: actionSchema().properties.verifier_commands.items,
      id: { type: "string", pattern: "^[a-z0-9][a-z0-9_-]{0,31}$" },
      context: { type: "string" }, operator_message: { type: "boolean" }, surface_id: { type: "string" },
    } };
}
const contractSha256 = createHash("sha256").update(contractBytes).digest("hex");
export function verifyNativeContract(host) {
  if (!host.contract) return; // Historical adapter admission predates this dependency.
  const actual = createHash("sha256").update(readFileSync(host.contract.path)).digest("hex");
  if (actual !== host.contract.sha256 || actual !== contractSha256) {
    throw new SwitchboardError("NOKIY_NATIVE_CONTRACT_CHANGED", "Native intake contract changed after admission.", 503);
  }
}

export function verifyNativeInterpreter(host) {
  try {
    if (typeof host?.python !== "string" || !host.python.startsWith("/")
        || !statSync(host.python).isFile()) throw new Error("Interpreter is not a regular file");
    accessSync(host.python, constants.R_OK | constants.X_OK);
    if (host.python_sha256 !== undefined
        && createHash("sha256").update(readFileSync(host.python)).digest("hex") !== host.python_sha256) {
      throw new Error("Interpreter identity differs from admission");
    }
  } catch {
    throw new SwitchboardError("NOKIY_NATIVE_INTERPRETER_UNAVAILABLE",
      "The admitted Nokiy host interpreter is missing, not executable, or changed; no inference was started.", 503);
  }
}

// The coordinator's native exec runs the host once per nokiy_execute call. Running it from the git
// Each process pins admitted host bytes to a physical generation. The current link is discovery,
// not an execution binding: replacing it must not change code already dispatched by a predecessor.
// The host loads its intake contract and HTTP helper beside itself, so publish them together.
export const NOKIY_HTTP_HELPER = "nokiy-http-evidence.py";
export const NOKIY_HOST_SNAPSHOT_ROOT = "/Users/nokiy/.codex-multi/models/nokiy-host";
const PRODUCTION_HOST_SOURCE = "/Users/nokiy/.codex-multi/switchboard/src";
const SNAPSHOT_CURRENT = "current";
// Builds left behind by a crashed publisher are removed once they are this old.
const STALE_SNAPSHOT_BUILD_MS = 10 * 60_000;
const snapshotPublications = new Map();
const snapshotBindings = new Map();
const SNAPSHOT_VERSION = /^([a-f0-9]{64})-([1-9][0-9]*)-([a-f0-9]{8})$/;
const execFileAsync = promisify(execFile);
const hostChanged = () => new SwitchboardError("NOKIY_NATIVE_HOST_CHANGED", "Nokiy native host bytes changed after admission.", 503);
const sha256Hex = (bytes) => createHash("sha256").update(bytes).digest("hex");
const snapshotDigest = (files) => sha256Hex(JSON.stringify(files.map(([name, bytes]) => [name, sha256Hex(bytes)])
  .sort(([left], [right]) => left.localeCompare(right))));

function publisherAlive(pid) {
  try { process.kill(pid, 0); return true; }
  catch (error) { return error.code !== "ESRCH"; }
}

async function snapshotInUse(directory) {
  try {
    const result = await execFileAsync("/usr/sbin/lsof", ["-t", "+D", directory]);
    return result.stdout.trim() !== "" || result.stderr.trim() !== "";
  } catch (error) {
    // Exit 1 without output means no handles. Unavailable/incomplete evidence preserves it.
    return error.code !== 1 || String(error.stdout ?? "").trim() !== "" || String(error.stderr ?? "").trim() !== "";
  }
}

async function currentSnapshotDirectory(root) {
  const name = await readlink(pathPosix.join(root, SNAPSHOT_CURRENT)).catch(() => null);
  if (!name || name !== pathPosix.basename(name)) return null;
  const directory = pathPosix.join(root, name);
  const info = await lstat(directory).catch(() => null);
  return info?.isDirectory() && !info.isSymbolicLink() ? directory : null;
}

async function readHostFileOrNull(file) {
  try { return await readFile(file); } catch (error) {
    if (error.code === "ENOENT" || error.code === "ENOTDIR") return null;
    throw error;
  }
}

// The bytes of every file the host loads from a directory, or null unless each has its admitted digest.
async function admittedHostFiles(directory, host) {
  const take = async (name, digest) => {
    const bytes = await readHostFileOrNull(pathPosix.join(directory, name));
    return bytes !== null && sha256Hex(bytes) === digest ? [name, bytes] : null;
  };
  const script = await take(pathPosix.basename(host.script), host.sha256);
  if (!script || !host.contract) return script && [script];
  const contractFile = await take(pathPosix.basename(host.contract.path), host.contract.sha256);
  if (!contractFile) return null;
  // Parsed only after its digest matched; the contract binds the helper digest.
  const helperDigest = JSON.parse(contractFile[1].toString("utf8")).read_http_helper_sha256;
  if (typeof helperDigest !== "string") return [script, contractFile];
  const helper = await take(NOKIY_HTTP_HELPER, helperDigest);
  return helper && [script, contractFile, helper];
}

/**
 * The admitted host as requests run it. Other hosts are unchanged unless a root is given.
 * Reuse only verified bytes; the process-local binding never follows a later current-link swap.
 */
export async function admittedHostSnapshot(host, { root, keep = 2 } = {}) {
  if (typeof host?.script !== "string" || typeof host.sha256 !== "string") return host;
  root ??= pathPosix.dirname(host.script) === PRODUCTION_HOST_SOURCE ? NOKIY_HOST_SNAPSHOT_ROOT : null;
  if (!root) return host;
  const effective = (directory) => ({ ...host, script: pathPosix.join(directory, pathPosix.basename(host.script)),
    ...(host.contract ? { contract: { ...host.contract, path: pathPosix.join(directory, pathPosix.basename(host.contract.path)) } } : {}) });
  // One publication per admitted version at a time: the start-up publication and the first request may race.
  const key = `${root}\0${pathPosix.basename(host.script)}\0${host.sha256}\0${host.contract?.path ?? ""}\0${host.contract?.sha256 ?? ""}`;
  const pinned = snapshotBindings.get(key);
  if (pinned && await admittedHostFiles(pinned, host)) return effective(pinned);
  if (!snapshotPublications.has(key)) {
    snapshotPublications.set(key, (async () => {
      const current = await currentSnapshotDirectory(root);
      const files = current && await admittedHostFiles(current, host)
        || await admittedHostFiles(pathPosix.dirname(host.script), host);
      if (!files) throw hostChanged();
      const directory = await publishHostSnapshot(files, root, keep);
      snapshotBindings.set(key, directory);
      return directory;
    })().finally(() => snapshotPublications.delete(key)));
  }
  return effective(await snapshotPublications.get(key));
}

async function publishHostSnapshot(files, root, keep) {
  await mkdir(root, { recursive: true, mode: 0o700 });
  // Each publication gets a new directory, so a file that a running host may read is never replaced.
  const name = `${snapshotDigest(files)}-${process.pid}-${randomUUID().slice(0, 8)}`;
  const building = pathPosix.join(root, `${name}.tmp-${process.pid}`);
  const link = pathPosix.join(root, `.${SNAPSHOT_CURRENT}.tmp-${process.pid}-${randomUUID()}`);
  try {
    await mkdir(building, { mode: 0o700 });
    for (const [file, bytes] of files) await writeFile(pathPosix.join(building, file), bytes, { mode: 0o400 });
    await rename(building, pathPosix.join(root, name));
    await symlink(name, link);
    // rename(2) swaps the link atomically: a reader sees the previous or the new version, never neither.
    await rename(link, pathPosix.join(root, SNAPSHOT_CURRENT));
  } catch (error) {
    await rm(building, { recursive: true, force: true });
    await rm(link, { force: true });
    throw error;
  }
  await pruneHostSnapshots(root, keep, name).catch(() => {});
  return pathPosix.join(root, name);
}

// Active publishers/handles are dependencies, not rollback copies. Unknown/changed entries survive.
async function pruneHostSnapshots(root, keep, published) {
  const live = await readlink(pathPosix.join(root, SNAPSHOT_CURRENT)).catch(() => null);
  const versions = [];
  for (const name of await readdir(root)) {
    if (name === SNAPSHOT_CURRENT || name === published || name === live) continue;
    const info = await lstat(pathPosix.join(root, name)).catch(() => null);
    if (!info) continue;
    const version = name.match(SNAPSHOT_VERSION);
    if (version && info.isDirectory() && !publisherAlive(Number(version[2]))) {
      versions.push({ name, mtime: info.mtimeMs, inode: info.ino, device: info.dev, digest: version[1], pid: Number(version[2]) });
    } else {
      const build = name.match(/^([a-f0-9]{64})-([1-9][0-9]*)-([a-f0-9]{8})\.tmp-([1-9][0-9]*)$/);
      if (build && !publisherAlive(Number(build[4])) && info.isDirectory()
          && Date.now() - info.mtimeMs > STALE_SNAPSHOT_BUILD_MS && !await snapshotInUse(pathPosix.join(root, name))) {
        await rm(pathPosix.join(root, name), { recursive: true, force: true });
      }
    }
  }
  versions.sort((left, right) => right.mtime - left.mtime);
  for (const { name, inode, device, digest, pid } of versions.slice(Math.max(0, keep - 1))) {
    const directory = pathPosix.join(root, name);
    const entries = await readdir(directory, { withFileTypes: true });
    if (entries.length > 3 || entries.some((entry) => !entry.isFile())) continue;
    const files = [];
    for (const entry of entries) {
      const file = pathPosix.join(directory, entry.name);
      const info = await lstat(file);
      if (!info.isFile() || info.size > 1024 * 1024) break;
      files.push([entry.name, await readFile(file)]);
    }
    if (files.length !== entries.length) continue;
    if (snapshotDigest(files) !== digest || publisherAlive(pid) || await snapshotInUse(directory)) continue;
    const fresh = await lstat(directory);
    if (!fresh.isDirectory() || fresh.ino !== inode || fresh.dev !== device) continue;
    const finalEntries = await readdir(directory);
    if (finalEntries.length !== files.length || finalEntries.some((name) => !files.some(([file]) => file === name))) continue;
    const finalFiles = [];
    for (const [file] of files) {
      const info = await lstat(pathPosix.join(directory, file));
      if (!info.isFile() || info.size > 1024 * 1024) break;
      finalFiles.push([file, await readFile(pathPosix.join(directory, file))]);
    }
    if (finalFiles.length !== files.length || snapshotDigest(finalFiles) !== digest || publisherAlive(pid)) continue;
    await rm(directory, { recursive: true, force: true });
  }
}
const draftDefaults = Object.fromEntries(Object.entries(contract.draft.properties)
  .filter(([, spec]) => Object.hasOwn(spec, "default")).map(([key, spec]) => [key, spec.default]));
const draftBounds = Object.entries(contract.draft.properties).flatMap(([name, spec]) => {
  const types = [spec.type].flat();
  if (types.includes("integer")) return [
    `${name}: integer ${spec.minimum}..${spec.maximum}${types.includes("null") ? " or null" : ""}`,
  ];
  return Object.hasOwn(spec, "x-maxUtf8Bytes") ? [`${name}: <=${spec["x-maxUtf8Bytes"]} UTF-8 bytes`] : [];
}).join("; ");

export function workerCapacity(configPath = `${process.env.CODEX_HOME || `${process.env.HOME}/.codex`}/config.toml`) {
  let agents;
  try { const config = parse(readFileSync(configPath, "utf8")); agents = Object.hasOwn(config, "agents") ? config.agents : {}; }
  catch (error) { if (error.code !== "ENOENT") throw fail("Worker capacity configuration unreadable."); agents = {}; }
  if (!agents || typeof agents !== "object" || Array.isArray(agents)) throw fail("Worker capacity agents configuration must be a table.");
  if (agents.enabled === false) return 1;
  const limit = agents.max_concurrent_threads_per_session ?? agents.max_threads ?? 20;
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 64) throw fail("Worker capacity must be an integer from 1 through 64.");
  return Math.min(limit, 20);
}

function execTarget(tools, namespace = null) {
  for (const tool of tools || []) {
    if (tool?.type === "namespace") {
      const found = execTarget(tool.tools, tool.name);
      if (found) return found;
    } else if (tool?.type === "custom" && tool.name === "exec") {
      return { namespace, name: tool.name, type: "custom", ...(hasNativeGoalControl(tool) ? { goalControl: true } : {}),
        ...(hasNativeSessionControl(tool) ? { sessionControl: true } : {}) };
    } else if (tool?.type === "function" && tool.name === "exec_command") {
      return { namespace, name: tool.name, type: "function" };
    }
  }
  return null;
}

export function nativeToolProjection(body, { worker, workerEffort = "max", workerServiceTier = "default", workflow = "nokiy-direct", host, profile = null, continuationTarget = null, continuationCall = null, continuationCoordinatorBindings = null, continuationPendingDelivery = null, operatorTexts = null, observationInput = null, recordedNativeCall = null, runReminder = null }) {
  if (!["nokiy", "nokiy-direct"].includes(workflow)) throw fail("Unknown Nokiy execution workflow.");
  // Only the router's validated profile option can select the fused path. Neither
  // a Sol model name nor client-supplied instructions/metadata identify the alias.
  const adaptiveNative = efficientNativeLoop(profile);
  if (adaptiveNative && (workflow !== profile.workflow || body.model !== profile.coordinator.model
      || body.reasoning?.effort !== profile.coordinator.reasoning_effort)) {
    throw fail("Efficient native execution must match the bound profile and native model/effort.");
  }
  if (!host || !host.python?.startsWith("/") || !host.script?.startsWith("/")) {
    throw new SwitchboardError("NOKIY_NATIVE_HOST_NOT_INSTALLED", "The native Nokiy tool adapter is not installed.", 503);
  }
  const discovered = (Array.isArray(body.input) ? body.input : [])
    .filter((item) => ["additional_tools", "tool_search_output"].includes(item?.type))
    .flatMap((item) => item.tools || []);
  const target = execTarget(body.tools) || execTarget(discovered) || continuationTarget;
  if (!target) throw fail("A native exec or exec_command tool must be advertised; no local execution was attempted.",
    { native_binding_missing: true });
  if ((body.tools || []).some((tool) => tool?.name === NOKIY_TOOL)) throw fail("Nokiy tool name collision.");
  const capacity = workflow === "nokiy-direct" ? workerCapacity() : 1;
  const closeoutEnabled = host.parent_closeout === "direct_v1";
  const deploymentEnabled = host.parent_deployment === "direct_v1";
  const batchReferenceEnabled = workflow === "nokiy-direct" && host.batch_reference === "direct_v1";
  const operations = ["run", "run_ref", "read_result", "read_preparation", "read_http", "query_surfaces", "navigate",
    ...(workflow === "nokiy-direct" ? ["run_batch", "read_batch"] : []),
    ...(batchReferenceEnabled ? ["run_batch_ref"] : []),
    ...(closeoutEnabled ? ["check_closeout", "closeout", "read_closeout"] : []),
    ...(deploymentEnabled ? ["check_deployment", "deploy", "read_deployment"] : [])];
  const tool = {
    type: "custom", name: NOKIY_TOOL,
    description: `Execute a bounded action through the actual installed Nokiy full runtime, worker ${worker}/${workerEffort}, requested tier ${workerServiceTier}. Input must be a JSON object, not code. For a new action: {"operation":"run","draft":{"workspace":"absolute existing workspace","artifact_root":"absolute existing task-local artifact directory outside the workspace","prompt":"bounded worker objective","authority_effect":"none or workspace","timeout_seconds":null},"action":{"mission":{"mission_id":"parent mission","task_id":"bounded action id","mode":"DELIVERY","objective":"objective","current_predicate":"first false predicate"},"context_summary":"bounded instructions and context","operations":["read","command"],"read_scopes":["exact relative files"],"write_scopes":[],"target_paths":[],"source_read":true,"command_templates":[],"forbidden_effects":["deployment","broker","order"]},"surface_id":"optional exact DCF surface ID"}. If surface_id is omitted in a DCF-managed workspace, the host selects the surface shared by most of the first four exact read_scopes files; preparation still fails closed when that is tied, empty or not admitted, so supply surface_id only to override that choice. Existing Nokiy scope/DCF/authority checks remain controlling. Native host binds its own thread, runtime image, model, effort and tier; do not supply credentials or native_thread_id. Tool returns native execution output or an exec session ID: wait on that same session using native tools. For completed-result recovery: {"operation":"read_result","artifact_root":"same absolute artifact directory","request_id":"original tura_embedded_... ID"}. To give a worker the operator's own task text, write {{nokiy:user_message}} (latest operator message) or {{nokiy:user_message_after:EXACT MARKER TEXT}} (text after that exact marker) inside draft.prompt or action.context_summary instead of copying it; the exact text is substituted before preparation and an unresolved reference is rejected without starting a worker. Never create a new action after uncertain execution. This tool does not grant permissions, accept work for the parent, or bypass the native execution policy. It is the native interface of the installed nokiy skill and already carries that skill's execution and recovery contract; do not open the nokiy SKILL.md to use it. Calling this tool is the parent's own first-level delegation, not recursive Nokiy invocation; recursion would be a Nokiy worker invoking Nokiy again, which workers cannot do.`,
    format: { type: "text" },
  };
  if (workflow === "nokiy-direct") tool.description += ` ${ULTRA_DISPATCH_GUIDANCE}`;
  if (workflow === "nokiy-direct") tool.description += ' For declared combined acceptance, post_batch_verifier may be {intent:{verifier}} instead of a full draft/action packet. verifier is one existing inline grant or catalog_ref. An omitted workspace comes from intent_defaults.workspace or the members\' common workspace, artifact_root from <artifact_root>/final-gate, and mission from a combined-acceptance default. For intent members, read_scopes/read_directories derive from their relative write/read entries; ordinary and shared/member_deltas members reuse only their explicit read grants, never writes alone. All members must share the verifier workspace; parent_source_targets are also included. Write fields explicitly only to differ. Optional context_summary, surface_id and Efficient parent_source_targets stay explicit. The adapter fills only fixed read/command, no-source-write settings and uses mission.objective as context when omitted; ordinary host validation, plan identity and parent completion acknowledgement remain required. Never combine intent with draft/action or put model, owner or permission overrides inside intent.';
  if (adaptiveNative) tool.description += ` ${EFFICIENT_NATIVE_GUIDANCE} Worker dispatch and inspect guidance below applies only when delegating a branch; a coherent native branch needs no worker preparation or artifact root.`;
  tool.description += ` For byte-exact file intake: {"operation":"run_ref","input_path":"canonical absolute regular UTF-8 JSON file","input_sha256":"64 lowercase hexadecimal characters"}. The file must contain an ordinary operation=run payload with draft, action and optional surface_id, not an envelope, reference, batch or recovery request. The host reads at most ${MAX_PAYLOAD_BYTES} bytes once, rejects symlinks including parents, verifies the exact bytes before strict JSON parsing, and applies the unchanged inline limits and authority checks. File location and digest do not change action identity; thread, HOME/CODEX_HOME, model, effort and tier remain host-bound. After an uncertain outcome recover the original action, never dispatch another reference.`;
  if (batchReferenceEnabled) tool.description += ' For an already prepared batch input file, prefer {"operation":"run_batch_ref","input_path":"canonical absolute regular UTF-8 JSON file","input_sha256":"64 lowercase hexadecimal characters"} instead of re-emitting the full batch. The file must contain exactly an ordinary run_batch payload, never a nested reference, envelope or recovery operation. The same byte bounds, strict parsing, scopes, member validation, capacity and host identity checks apply. File location and digest do not change batch or member identity. Execution prints the original plan_path/plan_sha256; recover uncertain execution with read_batch, never create a replacement batch.';
  tool.description += ` Draft contract: required ${contract.draft.required.join(", ")}; optional defaults ${JSON.stringify(draftDefaults)}. Draft fields not listed in this contract are rejected. Legacy model_acceleration:false is accepted as omitted; true is rejected because Fast is retired. Validation errors list field paths without starting a model. REJECTED_PREPARATION means no model was started: correct the input rather than retrying the identical failed preparation. Unknown or in-flight execution still requires original-ID recovery.`;
  tool.description += ` Draft bounds (inclusive; null only where listed): ${draftBounds}.`;
  tool.description += ' Recover a rejected preparation without another worker using {"operation":"read_preparation","artifact_root":"original task artifact directory","input_id":"original native-input-... ID"}, as returned in preparation_ref. This thread-bound deterministic reader verifies original diagnostic hashes, starts no model and never replays preparation or effects. ORIGINAL_DETAIL_UNAVAILABLE means the old producer discarded its cause; do not guess it or retry the same input. Correct only known unfinished authorized input against the current compiler.';
  tool.description += ' Action adaptation: source_read:false becomes omission without adding a grant; exact scoped cat/pwd strings become typed templates automatically before preparation. Prefer command_templates objects with argv/effects/targets. Other shell strings return NEEDS_REPREPARATION with the missing capability, not a terminal mission failure. Correct only unfinished already-authorized work and continue; unknown prior execution must be recovered, never replayed.';
  tool.description += ' Read routing: source_read:true supports existing exact regular files and same-action create/readback of explicitly planned absent canonical non-hidden exact targets, without symlink ancestry. This planned route requires read/command/create operations and the same exact workspace-relative path in read_scopes, write_scopes and target_paths; command_templates may be empty. This is explicit read authorization, never write-implies-read; path/absence checks and current writer ownership/CAS still apply. It is not directory discovery. For unknown filenames, declare read_directories:["exact non-hidden subdirectory"], matching read_scopes:["subdirectory/**"] and read/command operations; never use workspace root "." or "**". For an explicitly authorized linked/materialized metadata file, the existing exact typed cat route may be prepared under the same read_scopes, omitting source_read; the DCF compiler still admits or rejects it. Never auto-follow arbitrary links or retry a failed/uncertain execution. REJECTED_PREPARATION diagnostics.child_error_code reports a validated DCF cause, and issues list safe corrective field names; do not run another worker merely to diagnose preparation. exact_file_too_large means an exact file exceeds the whole-file snapshot bound (256 KiB, 1 MiB with source_read): reach it through read_directories discovery or an inspect action. A rejection with surface_resolution reports the automatic surface choice, or, with rejected_surface, the surfaces covering the existing write targets after an explicit surface was refused; correct once with an exact surface_id from its selected, tied or ranked IDs instead of another query_surfaces call. JSPACE_MODULE_UNAVAILABLE means the workspace DCF predates J-Space: use an inspect action there for read-only work, or a J-Space-capable checkout for scoped changes.';
  tool.description += ' External evidence: an exact regular UTF-8 file outside draft.workspace that the task names (frozen tests, rubric, contract) may be listed in read_scopes by absolute path; the host inlines its exact bytes with SHA-256 into the worker prompt while the prompt stays within 64 KiB and removes it from read_scopes, so do not hand-copy such evidence into the draft. Workers cannot read those paths with tools; directories, globs, binary or oversized files keep the typed refusal.';
  // V312 (2026-10-22): workers run programs only through verifier grants, which coordinators were never told about.
  tool.description += ' Worker tests: a worker runs programs only through action.verifier_commands, a list of {"argv":[...]} grants it starts by index with focused_verifier. A verifier defaults to cwd=draft.workspace, reads anywhere except credential stores, writes only its own scratch directory (its TMPDIR: never redirect TMPDIR or write elsewhere, including from a bootstrap module), may serve and reach its own loopback ports but no service that was already listening and no outside network, and its child processes inherit those limits. Give argv and optional python_import_roots and cwd: the host resolves a symlinked or virtualenv interpreter (adding its site-packages), fills digests, pins, scratch, timeout, network:false and the command operation. Leave executable_sha256 and sha256 values out: the host computes them from current bytes, and copying 64-character digests only lengthens the plan. To run tests the way the acceptance harness loads them, mirror its test loading with a module runner: the interpreter the task names with ["-B","-m","unittest","discover","-s","<start directory>"] (discover puts its start directory on sys.path, and its default pattern test*.py needs no -p), ["-B","-m","unittest","-v","<test module names>"] or ["-B","-m","pytest","-q","-p","no:cacheprovider","<test files>"], plus python_import_roots for every other absolute directory the harness puts on sys.path, including PYTHONPATH entries and paths it reads from config files (inside or outside the workspace, at most four); these module runners pin no file. argv runs without a shell and may not contain * ? [ ] { } ( ) or quotes, so name exact files or modules, never patterns. Other programs need a pinned entry: inside draft.workspace it must be in read_scopes; an external canonical entry needs explicit pinned_files with {"path":"/absolute/check.py","scope":"verifier_only"} (the host fills its SHA-256), accepted only when the actual workspace compiler accepts that grant. The external pin must appear once in exact argv, outside Git metadata and every verifier scratch root, without symlink ancestry; it grants no source_read or write access. Runtime support does not imply global DCF adoption. Never write wrappers to evade admission. When the harness runs those tests from a subdirectory, make that subdirectory the worker\'s draft.workspace; when it runs them from a directory the worker does not edit, set that verifier\'s optional cwd to it (an absolute directory: it moves only where the verifier starts and grants nothing). Tell the worker which verifier_index runs what and that it must pass before the worker reports done. A rejected verifier grant stays REJECTED_PREPARATION with its original diagnostics; no verifier is dropped and no worker starts. Correct the rejected grant once, or explicitly assign the required checks to the existing parent nokiy_native executor under this same authorized task, then continue worker edits with that parent-owned check assignment. Required checks remain pending, not skipped, until they pass; preserve scopes, pins and permissions. This recovery grants no authority or automatic verification ownership transfer.';
  tool.description += ' Parallel branch tests: when pytest-xdist is installed and the selected tests are independent, prefer an equivalent module pytest verifier with explicit "-p","xdist","-n","N" arguments. Choose a positive N within available CPU/memory after accounting for concurrent checks; never default to auto or multiply every worker by a fixed count. This can replace only the branch test invocation, not the unchanged complete acceptance harness in post_batch_verifier, its integrity checks or required coverage. Do not append pytest flags to custom validator scripts, inject PYTEST_ADDOPTS, drop required checks or create wrappers. If independence, plugin availability or resources are unknown, preserve the original verifier argv. Scopes, pins, permissions and parent acceptance remain unchanged.';
  tool.description += ' Inspect (read-and-record chains): when the next step is three or more dependent read-only checks whose only writes are evidence files (logs, git history, SQLite, localhost health, JSON receipts, headless browser pages via playwright-cli), dispatch ONE action with operations ["read","command"] and inspect:{} plus mission and context_summary, and no read_scopes, write_scopes, target_paths, command_templates or surface_id; it needs no DCF surface. The worker runs any shell line inside an OS fence: the repository and everything outside artifact_root/inspect/<id>/evidence and temporary directories are read-only; reads and network are those of the worker runtime. Open live SQLite with file:<path>?mode=ro and PRAGMA query_only=ON so committed WAL data remains visible. Use immutable=1 only for a verified immutable snapshot, never a live database. If the sandbox cannot read live WAL state, report unavailable evidence rather than switching to immutable=1 or treating missing rows as zero. Scripts and helper modules the worker writes there are task evidence, not repository source: let it write shared setup once and import it in later steps rather than forbid that. Keep single reads and every repository change on their existing routes.';
  if (workflow === "nokiy-direct") tool.description += ' Independent inspect actions may share one run_batch, also as compact members; a member that writes the repository cannot join them.';
  tool.description += ' For fresh HTTP evidence use {"operation":"read_http","artifact_root":"existing canonical task-local directory","urls":["exact authorized HTTPS URL"],"observation_id":"bounded current observation id"}. This native deterministic GET collects at most four exact URLs without a model, headers, credentials, cookies or automatic redirects. It inherits native permissions. The compact receipt binds timestamp/status/body hashes and source_context for a subsequent read-only worker. Use complete evidence only; HTTP errors/partial/UNKNOWN are not success. Same observation recovers cached evidence without another GET; use a new observation only for an authorized fresh read. Never guess URLs from wildcards.';
  tool.description += ' Optional DCF surface discovery, only to inspect candidates or after a tied or rejected automatic surface: use exactly {"operation":"query_surfaces","workspace":"existing canonical absolute workspace","targets":["exact normalized workspace-relative file"]}. Supply 1 through 4 distinct physical regular files, at most 512 characters each, without wildcards, .git, traversal or symlink ancestry. This queries fresh complete canonical DCF metadata only, starts no preparation or worker, returns SURFACE_CANDIDATES with per-path candidates and common_surface_ids, and does not read source contents or grant read/write access. Empty or ambiguous candidates are diagnostic results, never grants. Do not include draft, action, surface_id, input_id or credentials.';
  tool.description += ' Coordinator source discovery: when task scope or dependencies are unknown, use {"operation":"navigate","workspace":"/project","read_paths":["src","tests"],"queries":["symbol","path"],"projection":"locations"}. Batch known lookups in one call: queries accepts 1 through 32 nonempty strings, each at most 256 UTF-8 bytes; the installed caller lowercases, deduplicates and sorts them for OR literal substring matching. Legacy scalar query remains supported; queries allows only an omitted or empty query. Use locations for assignment and full (default) for relationships. This automatically builds/extends a Python DCF task map without a model call. The map is syntax evidence, not authority or implementation verification; use source-SHA-bound locations for worker scopes and targeted behavior reads. Do not add this call when supplied task metadata already fixes the assignments. To change query/projection under the same source snapshot, use returned source_snapshot_sha256 as expected_source_snapshot_sha256 at offset zero, not the previous map_sha256. A null source snapshot cannot support reuse. For same-view pagination keep query/projection unchanged and use returned next_offset as offset plus expected_map_sha256; the source snapshot alone is not a cursor. Both identities are checked when provided. Unsupported languages, excluded paths and parse failures stay explicit. Filesystem reads follow the same native execution boundary; no new credentials or grants.';
  if (closeoutEnabled) tool.description += ' Parent source-task closeout is deterministic, not worker source analysis: {"operation":"check_closeout","plan_path":"canonical absolute existing JSON plan","plan_sha256":"approved canonical JSON SHA-256"} validates without effects; operation="closeout" runs the exact already-authorized project finish command once. Plan schema nokiy_task_closeout_plan_v1 requires action_id, native_thread_id, workspace, artifact_root, expires_at, authorization_ref and task_closeout (source mode); reuse the existing task_closeout owner/lease/command/expected_receipt contract. Native CODEX_THREAD_ID must match the lease owner. Do not fill missing owners, invent authorization, finish paused tasks, or use draft/action/DCF preparation for closeout. This is not deployment or business acceptance. Recover with {"operation":"read_closeout","artifact_root":"original task artifact directory","action_id":"original action id"}; historical receipt reading ignores execution expiry and never replays effects. An uncertain prior attempt is not retry permission.';
  if (deploymentEnabled) tool.description += ' Authorized parent deployment uses no worker or DCF source preparation: {"operation":"check_deployment","plan_path":"canonical absolute existing JSON plan","plan_sha256":"approved canonical JSON SHA-256"} checks bindings only; operation="deploy" executes the exact admitted nokiy_deployment_plan_v1 preflight/apply/verify sequence. The plan binds native_thread_id, workspace, artifact_root, target, release, expires_at, authorization_ref and pinned commands. Installer admission, owner/CAS and native permissions remain controlling; neither this tool nor a source scope grants service or trading authority. Do not manufacture approval or replace an uncertain action. Recover without replay via {"operation":"read_deployment","artifact_root":"original task artifact directory","action_id":"original action id"}. READY is not deployment success; require VERIFIED plus target-specific functional readback.';
  tool.description += " Omit timeout_seconds or use null for worker-completion-owned lifetime with no total worker deadline. An explicit integer within its advertised bounds opts into a compatibility total deadline. Add \"observe\":\"later\" to a run, run_ref, run_batch or run_batch_ref input only when you have independent work of your own to do meanwhile: the call then returns its cell handle within seconds instead of blocking until the worker finishes; do that work, then wait on that same cell "
    + (adaptiveNative ? "(native observation duration is preserved; finish independent work before a blocking wait)"
      : "(the one-hour observation floor still applies)")
    + " and never resubmit the action.";
  if (workflow === "nokiy-direct") tool.description += ` Nokiy Ultra batch: {"operation":"run_batch","artifact_root":"absolute directory for original plan; one missing leaf is created","members":[{"draft":{...},"action":{...},"surface_id":"optional exact DCF ID"}]} (1 through ${capacity} independent members, bounded by the current configured capacity). max_parallel_workers is optional and must be an integer from 1 through the member count, never above ${capacity}. Omit max_parallel_workers to use all members. For at least two independent members, adding "max_parallel_workers":2 limits concurrency to two; queued members start as slots finish. Choose concurrency separately from member count; 1 serializes execution and is not the default. Use one member per genuinely independent chunk, since each worker step costs a fraction of an Astra step; do not pad the batch with artificial splits. Wait for the current batch terminal before starting another batch. The host validates all members, prepares all before any model starts, then prints the original plan_path and plan_sha256 before execution. Recover only with {"operation":"read_batch","plan_path":"original absolute plan path","plan_sha256":"original 64-character digest"}; never send a new batch on uncertain execution. Serial Nokiy rejects batch.`;
  if (workflow === "nokiy-direct") tool.description += ` ${BATCH_ARTIFACT_LAYOUT}`;
  if (workflow === "nokiy-direct") tool.description += MEMBER_INTENT_GUIDE + MEMBER_INTENT_SHARED_GUIDE;
  if (workflow === "nokiy-direct") tool.description += ' Coordinator reuse: when scope is unknown, use one batched navigate locations query and carry its exact paths/locations into the worker assignments; when those locations are already known and unchanged, do not rebuild the map or reread worker-owned source just to restate the plan. When the task already specifies a deterministic combined acceptance command, declare it in run_batch.post_batch_verifier with the workers instead of generating the same command in a later native round. For files that workers will create, use an explicitly authorized containing read_directories grant for the final verifier, not an exact read of a nonexistent file and never final write grants. The returned J-Space binding and source_postimages are verification evidence for the parent to reconcile; do not rerun an identical passing gate against unchanged subjects. After a passing bound post_batch_verifier, accept from that proof and the returned changes; reread worker-owned source only for a specific concern the evidence leaves open. If the command depends on worker results, its scope is not authorized, evidence is missing, or source identity changed, keep that check parent-owned and pending rather than inventing a grant or accepting stale evidence.';
  if (workflow === "nokiy-direct") tool.description += ' Optional run_batch.post_batch_verifier is off when omitted. With matching caller support, prefer the compact {intent:{verifier}} form; supply a full {draft,action,optional surface_id} packet only for a setting the intent cannot express. A full packet is the same ordinary bounded preparation packet as a member, with draft.authority_effect="none", read/command operations only, no source writes and exactly one existing typed verifier grant covering all member source targets. Its artifact root must be separate from workspaces and member artifacts. In an unconfigured workspace, the final verifier may name an explicit scratch_root outside its receipt artifact root so a task-specified output location need not move. That scratch directory must be disjoint from sources, every member artifact and immutable inputs; only scratch is writable, not its enclosing directory. Ordinary worker scratch restrictions are unchanged, and a DCF-managed workspace still requires its canonical compiler. The host prepares it in the same dispatch, binds the actual parent thread and direct profile, and lowers it to the internal caller locator; never supply operation/run, owner, model, effort or credentials. It starts no child model and consumes no worker capacity. Existing preparation and permission checks remain controlling; unsupported typed prerequisites remain explicit. The caller admits the gate before workers start and runs the declared combined tests after successful member join through the native verifier fence. Inline, compact and run_batch_ref inputs bind the entire declaration into the original plan. Recover only with read_batch and the original plan, without reprepare or replay; recovery remains UNKNOWN without independently durable original outcome proof. Parent acceptance remains required.';
  // Each serial setup call costs a full coordinator round trip before any worker starts.
  if (workflow === "nokiy-direct") tool.description += " Dispatch run_batch directly: the host creates one missing batch artifact_root leaf and resolves omitted DCF surfaces, so no mkdir or query_surfaces setup call is needed. Combine any other independent setup into one call; every call is a separate coordinator round trip.";
  if (workflow === "nokiy-direct") tool.description += ' Compact batch input: for run_batch, replace members with shared:{schema_version:"nokiy_batch_shared_v1",draft:{workspace:...,prompt:"rules the operator text lacks...\\n\\n{{nokiy:user_message}}\\n"},action:{...}} and member_deltas:[{draft:{artifact_root:...,prompt:"Assigned scope for this member"},action:{mission:{task_id:...,...}},surface_id:"optional exact DCF ID"},...]. When workers need the operator\'s task text, write {{nokiy:user_message}} (or {{nokiy:user_message_after:EXACT MARKER TEXT}}) in shared.draft.prompt instead of copying it: the host substitutes the exact operator text before preparation, so the coordinator never re-generates it. That text already carries the operator\'s constraints, evidence limits and output format: do not restate them in shared or member prompts; add only what it lacks, such as member assignments. Emit common material once directly, not a full batch file. shared.draft.workspace is required and shared-only; artifact_root, write_scopes, target_paths, task_id and surface_id are member-only. At least one shared prompt/context_summary must be nonempty. Only prompt/context_summary concatenate shared prefix+delta suffix exactly, without an inserted separator; include needed newlines. Mission keys must be disjoint, combining to mission_id/task_id/mode/objective/current_predicate. Other duplicate fields fail even if equal; no list/scope union or override. Unknown fields and nested run_ref/run_batch_ref references are rejected; apart from {{nokiy:user_message...}} in prompt/context_summary, no templates are evaluated. Model/effort/tier/thread stay host-bound, forbidden in both parts. Expansion precedes unchanged ordinary validation/preparation, full-request identity, original-ID recovery and execution; ordinary payload/envelope byte bounds apply. Ordinary members remain valid. If advertised, run_batch_ref transports the same compact payload; references grant no permission.';
  tool.description += '\n\nRead machine evidence selectively: use a JSON parser to extract needed catalog keys, command definitions or manifest entries, not cat an entire integrity manifest into model context. Keep the complete original file for the verifier; selective display never narrows validation. With exact task paths, read those paths and relevant tests rather than listing the whole repository. When an existing JSON catalog has commands keyed by name, a verifier_commands item may be {"catalog_ref":{"path":"absolute catalog path","command":"key"}} instead of copying its command; omit sha256 and the adapter pins the catalog\'s current bytes before preparation (an explicit sha256 is still verified). No overrides or recursive references. The host expands exact bytes before ordinary admission; references grant no authority. Prefer inline commands when no suitable catalog exists; do not create a catalog merely to shorten a small request.';
  // Prewarm (generate:false) must advertise byte-identical tools; otherwise its cached prefix is unusable.
  if (adaptiveNative) tool.description += '\n\nEfficient dispatch decision: when you will implement an independent parent branch, the combined verifier command is already known, and its existing read grants cover exact parent targets disjoint from worker writes, prefer binding the combined gate at dispatch rather than waiting for worker delivery to generate another verification call. With verified installed complete-batch-parent support, use inline run_batch with observe:"later" and post_batch_verifier.parent_source_targets=["exact relative parent-owned file"]. Complete your branch and its necessary checks while the worker runs. After your last edit, acknowledge once through the native executor: ~/.local/bin/nokiy-embedded-run complete-batch-parent --plan <original plan_path> --plan-sha256 <original plan_sha256>, from PLAN_AVAILABLE_EXECUTION_PENDING. Do not edit acknowledged files afterward. The original batch joins both branches and runs the combined gate once; reconcile its bound proof and worker changes before delivery. No new read/write grants. Changed source, wrong owner or cancellation cannot pass. Unknown targets, a result-dependent verifier, unavailable support, inability to acknowledge, retain ordinary parent-owned final validation; do not force this shape. For an existing pinned run_batch_ref containing parent_source_targets, use parent_completion:true with observe:"later"; the same original-plan acknowledgement is required. The host rejects a reference whose completion mode differs from its contents.';
  tool.description += `\n\nOnly after verifying installed caller support, optionally preload already-known exact authorized local non-DCF UTF-8 source ranges with action.source_sections=[{"path":"exact authorized path","start_line":1,"end_line":20}]. Limits: max 16 ranges, 400 lines each, 12000 UTF-8 excerpt bytes total; the complete serialized summary including metadata and escaping must also fit 12000 characters. Preparation SHA-binds partial preimages, not dependency proof, a new grant, post-edit evidence or DCF bypass. Avoid rereading unchanged supplied ranges; missing context, tests and fresh postimages remain required. This is an action field, not a draft field.`;
  tool.description += `\n\nOnly with a verified matching V132+ installed caller/runtime and an already-known task type, prefer action.initial_task_state={task_group:<bounded objective>,task_type:["debug"]} for debugging (example only, not a default for all tasks). Other IDs require a known supported task type; omit initial_task_state when type is unknown. This is an action field, not a draft field. Omission preserves legacy startup; resumed state and permissions remain unchanged.`;
  tool.description += `\n\nThe installed caller/runtime supports action.terminal_delivery="evidence_only": prefer it for bounded execution/artifact deliverables the parent will inspect, so the worker ends with task_status done in the same command array as its final passing check instead of a separate report turn. Reviews, questions and narrative conclusions retain action.terminal_delivery="assistant_reply"; omission remains assistant_reply. terminal_delivery is an action field, not a draft field. Exact persisted same-request terminal_evidence, observed_terminal_delivery and cleanup are required; never infer success from absent prose.`;
  if (target.type === "function") {
    tool.type = "function";
    delete tool.format;
    tool.parameters = { type: "object", properties: {
      operation: { type: "string", enum: operations },
      workspace: { type: "string", minLength: 1, maxLength: 4096, description: "query_surfaces/navigate: existing canonical absolute workspace; no symlink ancestry" },
      read_paths: { type: "array", minItems: 1, maxItems: 32, uniqueItems: true, items: { type: "string", minLength: 1, maxLength: 512 }, description: "navigate only: explicitly authorized source files/directories relative to workspace; dot selects its source scope, hidden/build/dependency paths excluded" },
      query: { type: "string", maxLength: 512, description: "navigate only: optional symbol/path substring; empty returns a paginated map" },
      queries: { type: "array", minItems: 1, maxItems: 32, items: { type: "string", minLength: 1, maxLength: 256 }, description: "navigate only: batch OR literal substrings of symbol/path; ref:Name matches indexed references (for example ref:CaptureError finds referring symbols, not just its definition). Each at most 256 UTF-8 bytes; caller lowercases, deduplicates and sorts; query must be omitted or empty. Pass needed returned locations and source SHA through existing worker context; do not repeat discovery or assume workers expose navigate. Syntax references are not complete dynamic behavior evidence." },
      projection: { type: "string", enum: ["full", "locations"], default: "full", description: "navigate only: locations for assignment, full for relationships" },
      offset: { type: "integer", minimum: 0, description: "navigate continuation offset; requires expected_map_sha256" },
      expected_map_sha256: { type: "string", pattern: "^[0-9a-f]{64}$", description: "navigate continuation identity from original map" },
      expected_source_snapshot_sha256: { type: "string", pattern: "^[0-9a-f]{64}$", description: "navigate source identity for changing query/projection at offset zero; does not replace expected_map_sha256 for pagination" },
      targets: { type: "array", minItems: 1, maxItems: 4, uniqueItems: true, items: { type: "string", minLength: 1, maxLength: 512 }, description: "query_surfaces only: 1 through 4 distinct physical regular normalized workspace-relative files; metadata discovery, never read/write grants" },
      input_id: { type: "string", pattern: "^native-input-[0-9a-f]{64}$", description: "Exact original rejected preparation directory name" },
      urls: { type: "array", minItems: 1, maxItems: 4, items: { type: "string" }, description: "Exact authorized HTTPS GET URLs, without credentials, fragments or wildcards" },
      observation_id: { type: "string", pattern: "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", description: "Current observation identity; same identity recovers original evidence without re-fetch" },
      input_path: { type: "string", minLength: 1, description: "Canonical absolute regular UTF-8 JSON file; no symlink components" },
      input_sha256: { type: "string", minLength: 64, maxLength: 64, pattern: "^[0-9a-f]{64}$" },
      ...(adaptiveNative && batchReferenceEnabled ? { parent_completion: { type: "boolean", const: true,
        description: "run_batch_ref only, with observe:later. The pinned ordinary batch must declare parent_source_targets. Acknowledge the returned original plan after the final parent edit." } } : {}),
      draft: structuredClone(contract.draft),
      action: actionSchema(),
      surface_id: { type: "string" }, artifact_root: { type: "string" }, request_id: { type: "string" },
      ...(closeoutEnabled || deploymentEnabled ? { action_id: { type: "string", pattern: "^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$" },
        plan_path: { type: "string" }, plan_sha256: { type: "string", pattern: "^[0-9a-f]{64}$" } } : {}),
      ...(workflow === "nokiy-direct" ? {
        shared: { type: "object", properties: { schema_version: { type: "string", const: "nokiy_batch_shared_v1" },
          draft: { type: "object", required: ["workspace"] }, action: { type: "object" } },
          required: ["schema_version", "draft", "action"], additionalProperties: false },
        member_deltas: { type: "array", minItems: 1, maxItems: capacity, items: { type: "object", properties: {
          draft: { type: "object", required: ["artifact_root"] }, action: { type: "object" }, surface_id: { type: "string" },
        }, required: ["draft", "action"], additionalProperties: false } },
        intent_defaults: { type: "object", additionalProperties: false, properties: {
          workspace: { type: "string" }, context: { type: "string" } },
          description: "run_batch with intent members only: workspace for intents and the final verifier that omit it; context goes, then a newline, before each member intent's own context." },
        members: { type: "array", description: BATCH_ARTIFACT_LAYOUT, minItems: 1, maxItems: capacity, items: { type: "object", properties: {
        draft: structuredClone(contract.draft), action: actionSchema(),
        surface_id: { type: "string" },
        intent: memberIntentSchema(),
      }, anyOf: [{ required: ["draft", "action"] }, { required: ["intent"] }], additionalProperties: false } },
        max_parallel_workers: { type: "integer", minimum: 1, maximum: capacity,
          description: "Optional run_batch concurrency, at most the member count; omission uses all members." },
        post_batch_verifier: { type: "object", properties: {
          intent: { type: "object", required: ["verifier"],
            additionalProperties: false, properties: {
              workspace: { type: "string" }, artifact_root: { type: "string" },
              mission: actionSchema().properties.mission, context_summary: { type: "string" },
              read_scopes: { type: "array", minItems: 1, items: { type: "string" } },
              read_directories: { type: "array", items: { type: "string" } },
              verifier: actionSchema().properties.verifier_commands.items,
              surface_id: { type: "string" },
              ...(adaptiveNative ? { parent_source_targets: { type: "array", minItems: 1,
                maxItems: 128, uniqueItems: true, items: { type: "string", minLength: 1 } } } : {}),
            } },
          draft: structuredClone(contract.draft), action: actionSchema(),
          surface_id: { type: "string" },
          ...(adaptiveNative ? { parent_source_targets: {
            type: "array", minItems: 1, maxItems: 128, uniqueItems: true,
            items: { type: "string", minLength: 1 },
            description: "Exact parent-owned relative files, disjoint from worker writes. Requires observe:later and complete-batch-parent after the final parent edit.",
          } } : {}),
        }, anyOf: [{ required: ["draft", "action"] }, { required: ["intent"] }],
        additionalProperties: false, description: "Optional run_batch preparation packet for declared combined tests; authority_effect none, read/command only, exactly one typed verifier. Prepared without a model in this dispatch; owner/profile are host-bound. Parent acceptance remains required." },
        plan_path: { type: "string" }, plan_sha256: { type: "string" } } : {}),
    }, required: ["operation"], additionalProperties: false };
    tool.strict = false;
  }
  const strictCoordinator = coordinatorOnly(host) && !adaptiveNative;
  // A restored replay shows the model its own calls; native bindings use what Codex executed.
  const observationBody = Array.isArray(observationInput) ? { ...body, input: observationInput } : body;
  const nativeObservation = strictCoordinator || adaptiveNative ? observationControl(body, target) : null;
  const nativeBinding = strictCoordinator || adaptiveNative ? nativeExecutionControl(body, target) : null;
  if (adaptiveNative && !nativeBinding) {
    throw fail("Efficient native execution requires a unique supported advertised native exec or exec_command binding; policy text enables no execution.",
      { native_binding_missing: true });
  }
  const nativeExecution = strictCoordinator ? nativeBinding : null;
  if (strictCoordinator) tool.description += ' Native handles are distinct: exec cell_id is a string for native wait; exec_command session_id is a numeric native process handle for write_stdin or advertised nokiy_observe. Never convert between them. Observation is not a worker command template: do not prepare or dispatch a model to poll. Missing native observation requires original-ID recovery, not replay.';
  const restricted = strictCoordinator ? coordinatorProjection(body, tool, continuationCoordinatorBindings,
    target.goalControl === true, nativeObservation ? observationTool(nativeObservation) : null,
    nativeExecution ? nativeExecutionTool(target, nativeExecution) : null) : null;
  const projection = { body: restricted?.body ?? { ...body, tools: [...(body.tools || []), tool] },
    target: Object.freeze({ ...target, ...(nativeBinding ? { executionSchema: nativeBinding.schema } : {}) }),
    worker, workerEffort, workflow, serviceTier: workerServiceTier, host: Object.freeze({ python: host.python, script: host.script }),
    coordinatorBindings: restricted?.bindings ?? null, adaptiveNative,
    nativeObservation, nativeExecution, observationBody, continuationCall,
    resultDeliveryEnabled: host.parent_result_delivery === "accept_v1", operations: Object.freeze(operations),
    // WS deltas omit earlier turns; the server carries this connection's operator texts.
    userTexts: Object.freeze(Array.isArray(operatorTexts) ? [...operatorTexts] : userMessageTexts(body)) };
  if (host.parent_result_delivery === "accept_v1") {
    const matches = (call) => matchesProjectedNativeCall(call, projection, recordedNativeCall);
    projection.resultDelivery = prepareResultDelivery(observationBody, matches, continuationCall, continuationPendingDelivery,
      (reason) => { projection.resultDeliveryRejection = reason; });
    projection.pendingResultDelivery = pendingResultDelivery(observationBody, matches, continuationCall, continuationPendingDelivery);
    if (projection.resultDelivery && strictCoordinator) {
      projection.resultDelivery = Object.freeze({ ...projection.resultDelivery, coordinatorOnly: true });
    }
    // Append the transport as the newest developer turn: editing instructions mid-chain
    // changes the prompt prefix and makes the whole returning conversation uncached.
    if (projection.resultDelivery) {
      const before = Array.isArray(projection.body.input) ? projection.body.input : [];
      const transport = { type: "message", role: "developer",
        content: [{ type: "input_text", text: resultDeliveryInstructions(projection.resultDelivery) }] };
      projection.body.input = [...before, transport];
      // Codex never sees this turn; a later replay re-inserts it after the same output.
      projection.deliveryInsertion = Object.freeze({ anchor: before.at(-1) ?? null, items: Object.freeze([transport]) });
    }
  }
  // Same transport as result delivery: an appended developer turn that the replay
  // restorer re-inserts after the same anchor, so the cached prefix stays unchanged.
  if (typeof runReminder === "string" && runReminder && !projection.resultDelivery) {
    const before = Array.isArray(projection.body.input) ? projection.body.input : [];
    const note = { type: "message", role: "developer", content: [{ type: "input_text", text: runReminder }] };
    projection.body.input = [...before, note];
    projection.deliveryInsertion = Object.freeze({ anchor: before.at(-1) ?? null, items: Object.freeze([note]) });
    projection.runReminder = runReminder;
  }
  return Object.freeze(projection);
}

export function continueNativeObservation(projection, continuation = null) {
  return retainNativeObservations(projection, continuation, (call) => matchesObservedNativeCall(call, projection));
}

// Serialized into code mode; only the admitted native tool owner executes this.
async function waitForNativeTerminal(args, nativeCommand = false, announcePlan = false, overlap = false) {
  // Advisory run length: store/load persist across this thread's code-mode cells and any
  // nokiy_execute cell resets it. The notice rides inside the result JSON because observers
  // parse a native cell's output as one JSON object.
  let notice = null;
  if (typeof load === "function" && typeof store === "function") {
    try {
      const run = nativeCommand ? (Number(load("nokiy_native_run")) || 0) + 1 : 0;
      store("nokiy_native_run", run);
      if (run >= 3) notice = "Native call " + run + " in a row since the last nokiy_execute call; each one re-reads this whole thread. Send the remaining read-only checks as one nokiy_execute inspect action and implementation or analysis as a worker run. Admitted deployment and closeout stay native but take one call: put the remaining deterministic steps in one script that stops at the first failed check, or run an admitted deployment or closeout plan.";
    } catch { notice = null; }
  }
  const noticed = (value) => (notice ? { ...value, nokiy_notice: notice } : value);
  const initial = await tools.exec_command(args);
  const originalSession = initial?.session_id ?? null;
  // Two bounded windows leave room for a 64-KiB inspection and its receipts.
  const limit = 64 * 1024;
  let result = initial, head = "", tail = "", truncated = false;
  const output = () => head + (truncated ? "\n[Native output truncated; initial receipt and tail retained]\n" : "") + tail;
  const uncertain = (message) => {
    let receipt = {
      ...(result && typeof result === "object" && !Array.isArray(result) ? result : {}),
      output: output(), truncated, output_truncated: truncated, terminal: false, exit_code: null,
      session_id: originalSession, recovery_session_id: originalSession,
      observed_session_id: result?.session_id ?? null,
      observed_exit_code: result?.exit_code ?? null,
      wait_error: message.slice(0, limit),
    };
    // Recovery parses a 256-KiB JSON envelope, including escaped text and framing.
    const budget = 240 * 1024;
    const bytes = (value) => {
      let size = 0;
      for (const character of JSON.stringify(value)) {
        const code = character.codePointAt(0);
        size += code < 0x80 ? 1 : code < 0x800 ? 2 : code < 0x10000 ? 3 : 4;
      }
      return size;
    };
    if (bytes(receipt) > budget) {
      let base = { ...receipt, output: "", wait_error: "", truncated: true, output_truncated: true };
      if (bytes(base) > budget / 2) {
        base = { output: "", truncated: true, output_truncated: true, terminal: false, exit_code: null,
          session_id: originalSession, recovery_session_id: originalSession,
          observed_session_id: result?.session_id ?? null, observed_exit_code: result?.exit_code ?? null,
          native_metadata_truncated: true };
        message = "Native metadata omitted because its serialized receipt exceeds the recovery budget. " + message;
      }
      const fit = (value, render, maximum) => {
        let low = 0, high = value.length;
        while (low < high) {
          const middle = Math.ceil((low + high) / 2);
          if (bytes(render(middle)) <= maximum) low = middle;
          else high = middle - 1;
        }
        return render(low);
      };
      const recoveryMessage = "Do not replay; recover only the original session. " + message;
      const error = fit(recoveryMessage, (size) => recoveryMessage.slice(0, size), 16 * 1024);
      const value = receipt.output;
      const marker = "\n[Native output truncated; initial receipt and tail retained]\n";
      receipt = fit(value, (size) => ({ ...base, wait_error: error,
        output: value.slice(0, Math.ceil(size / 2)) + marker
          + (Math.floor(size / 2) ? value.slice(-Math.floor(size / 2)) : "") }), budget);
    }
    text(receipt);
  };
  let planAnnounced = false;
  for (;;) {
    const chunk = typeof result?.output === "string" ? result.output : typeof result === "string" ? result : "";
    // Host clipping flags or warning text stay sticky across later unclipped waits.
    truncated ||= result?.truncated === true || result?.output_truncated === true
      || /^Warning: truncated output \(original token count: \d+\)\r?\n/.test(chunk);
    // Retain the initial batch receipt (plan_path/plan_sha256), not just a tail.
    const take = limit - head.length;
    head += chunk.slice(0, take);
    const rest = tail + chunk.slice(take);
    truncated ||= rest.length > limit;
    tail = rest.slice(-limit);
    if (announcePlan && !planAnnounced && !truncated && originalSession != null
        && result?.session_id === originalSession && head.includes("\n")) {
      try {
        const plan = JSON.parse(head.slice(0, head.indexOf("\n")));
        if (plan && typeof plan === "object" && !Array.isArray(plan)
            && Object.keys(plan).sort().join() === "plan_path,plan_sha256"
            && typeof plan.plan_path === "string" && plan.plan_path.startsWith("/")
            && typeof plan.plan_sha256 === "string"
            && /^[0-9a-f]{64}$/.test(plan.plan_sha256)) {
          text({ status: "PLAN_AVAILABLE_EXECUTION_PENDING", ...plan, parent_completion_required: true });
          planAnnounced = true;
        }
      } catch { /* A partial or non-plan first line is not a completion signal. */ }
    }
    if (!result || typeof result !== "object" || Array.isArray(result)) {
      uncertain("Invalid native result; outcome unknown. Do not replay the action."); return;
    }
    if (result.session_id === undefined || result.session_id === null) {
      if (!Number.isSafeInteger(result.exit_code)) {
        uncertain("Native result has no valid terminal exit status; outcome unknown. Do not replay the action."); return;
      }
      if (nativeCommand && result === initial && !truncated) { text(noticed(initial)); return; }
      const body = output();
      if (!nativeCommand && !truncated) {
        // A JSON terminal is embedded as an object: as a nested string every quote is
        // escaped, and every later coordinator call re-reads it. Exec chunk metadata is dropped.
        let parsed = null;
        try { parsed = JSON.parse(body); } catch { parsed = null; }
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          const { output: _output, chunk_id: _chunk, wall_time_seconds: _wall, original_token_count: _count,
            truncated: _truncated, output_truncated: _clipped, ...rest } = result;
          text(noticed({ ...rest, terminal: true, recovery_session_id: originalSession, output: parsed }));
          return;
        }
      }
      text(noticed({ ...result, output: body, truncated, output_truncated: truncated,
        terminal: true, recovery_session_id: originalSession }));
      return;
    }
    if (result.session_id !== originalSession) {
      uncertain("Native session changed; recover only the original session. Do not replay the action."); return;
    }
    if (!(Number.isSafeInteger(originalSession) && originalSession >= 0)
        && (nativeCommand || !(typeof originalSession === "string" && originalSession.trim()))) {
      uncertain("Invalid original native session; outcome unknown. Do not replay the action."); return;
    }
    if (result.exit_code !== undefined && result.exit_code !== null) {
      uncertain("Native result reports both a running session and an exit status; outcome unknown. Do not replay the action."); return;
    }
    if (typeof tools.write_stdin !== "function") {
      uncertain("Native wait unavailable; recover the original session. Do not replay the action."); return;
    }
    try {
      // Observation yielding is not a worker lifetime or a retry budget.
      result = await tools.write_stdin({ session_id: originalSession, chars: "", ...(!nativeCommand
        ? { yield_time_ms: overlap || (announcePlan && !planAnnounced) ? 1000 : 300000,
          max_output_tokens: args.max_output_tokens } : {}) });
    } catch (error) {
      uncertain(`Native wait failed: ${String(error)}. Recover the original session; do not replay the action.`); return;
    }
  }
}

function renderNativeExecution(args, nativeCommand = false, observeLater = false, announcePlan = false) {
  // The first observation covers a typical worker run in one round trip; longer runs and native
  // commands then report every CELL_WAIT_FLOOR_MS (see there), unless the parent asked to overlap
  // its own work with the worker (observe:"later").
  return `// @exec: {"yield_time_ms":${observeLater ? OBSERVE_LATER_YIELD_MS : FIRST_OBSERVATION_YIELD_MS},"max_output_tokens":262144}\n// ${nativeCommand
    ? "Native deterministic command: original-handle observation only."
    : "Nokiy native tool v2: executes only in the native host."}\nawait (${waitForNativeTerminal.toString()})(${JSON.stringify(args)}${observeLater ? `, ${nativeCommand}, ${announcePlan}, true` : announcePlan ? ", false, true" : nativeCommand ? ", true" : ""});`;
}

// Each coordinator wait is a full coordinator response that re-reads the whole thread. On
// 2026-10-18 waits that returned "Script running" were 5-32% of Ultra coordinator credits:
// models peeked at 1 s and polled at 60 s while a worker ran. A native wait returns as soon as
// the cell finishes and Codex instant_interrupt yields it on new user input, so short waits are
// raised to a floor. The floor was one hour until 2026-10-26, when it turned two explicit waits
// in 019ff1d0 (300000 ms right after an operator question, then a 1 s peek) into hour-long silent
// waits on a slow PAPER deployment whose stall surfaced only after the hour. The floor now matches
// the operator's global 300000 ms wait rule, so long work reports every five minutes. Larger
// explicit values are kept; a native schema maximum below the floor is used instead.
const CELL_WAIT_FLOOR_MS = 300000;
// First adapter yield: worker runs took 5-9 min in 019ff1d0 on 2026-10-26, so one observation
// usually returns the terminal result without an extra coordinator round trip.
const FIRST_OBSERVATION_YIELD_MS = 600000;
// observe:"later" (2026-10-23, I20): native Sol overlapped one helper with its own branch and
// finished in 518 s, while one Efficient loop needed 700 s for the same two branches. A short
// first yield returns the cell handle so the parent can work; its later wait keeps the floor.
const OBSERVE_LATER_YIELD_MS = 15000;
const OBSERVE_LATER_OPERATIONS = ["run", "run_ref", "run_batch", "run_batch_ref"];

function projectPendingCellWait(item, projection) {
  // Efficient also implements independent branches; preserve its native scheduling choice.
  if (projection.adaptiveNative) return item;
  const state = projection.nativeObservationState, control = state?.cellWaitControl;
  if ((!projection.coordinatorBindings && !projection.adaptiveNative) || item?.type !== "function_call" || item.name !== "wait"
      || (item.namespace !== undefined && item.namespace !== "functions") || control?.namespace !== "functions"
      || control.type !== "function_call" || !control.schema?.properties?.yield_time_ms
      || state.owner !== JSON.stringify([projection.target.type, projection.target.name, projection.target.namespace ?? null])) return item;
  try {
    if (typeof item.arguments !== "string" || Buffer.byteLength(item.arguments) > 4096) return item;
    const args = JSON.parse(item.arguments);
    const explicit = args && typeof args === "object" && Object.hasOwn(args, "yield_time_ms");
    if (!args || typeof args !== "object" || Array.isArray(args)
        || (explicit && (!Number.isSafeInteger(args.yield_time_ms) || args.yield_time_ms < 0
          || args.yield_time_ms >= CELL_WAIT_FLOOR_MS))
        || (Object.hasOwn(args, "terminate") && args.terminate !== false)
        || typeof args.cell_id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(args.cell_id)) return item;
    const owners = state.pendingCells?.filter((ref) => ref.cell_id === args.cell_id) ?? [];
    // Efficient also launches ordinary native cells: unknown origins cannot inherit Nokiy waits.
    if (!projection.coordinatorBindings && owners.length !== 1) return item;
    // A coordinator launches cells only through Nokiy projections. When compaction or a lost
    // connection removed the origin from the window, the floor still applies; a visible origin
    // keeps the tracker's verdict, so foreign or released cells get no default.
    if (owners.length > 1 || (owners.length === 1 && projection.nativeObservationConflicts?.includes(owners[0].source_call_id))
        || (owners.length === 0 && cellOriginVisible(projection, args.cell_id))) return item;
    // Only the observation yield changes; the native schema still has to accept it.
    const validate = nativeArgumentValidator(control.schema);
    for (const floor of [CELL_WAIT_FLOOR_MS, control.schema.properties.yield_time_ms.maximum]) {
      if (!Number.isSafeInteger(floor) || floor > CELL_WAIT_FLOOR_MS || (explicit && floor <= args.yield_time_ms)) continue;
      const extended = { ...args, yield_time_ms: floor };
      if (validate(extended)) return { ...item, arguments: JSON.stringify(extended) };
    }
    return item;
  } catch { return item; }
}

export function nativeToolItem(item, projection) {
  if (item?.name === "wait") return projectPendingCellWait(item, projection);
  if (item?.name === NOKIY_NATIVE && projection.coordinatorBindings) return projectNativeExecution(item, projection);
  if (item?.name === "nokiy_goal" && projection.coordinatorBindings) return projectGoalControl(item, projection.target);
  if (item?.name === NOKIY_OBSERVE && projection.coordinatorBindings) return projectNativeObservation(item, projection,
    (call) => matchesObservedNativeCall(call, projection));
  if (!["custom_tool_call", "function_call"].includes(item?.type) || item.name !== NOKIY_TOOL) return item;
  const input = item.type === "function_call" ? item.arguments : item.input;
  if (typeof input !== "string" || Buffer.byteLength(input) > MAX_PAYLOAD_BYTES) throw fail("Nokiy tool payload is missing or too large.");
  let payload;
  try { payload = JSON.parse(input); } catch { throw fail("Nokiy tool input must be JSON."); }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)
      || !projection.operations.includes(payload.operation)) throw fail("Unknown Nokiy operation.");
  let observeLater = false;
  if (Object.hasOwn(payload, "observe")) {
    if (payload.observe !== "later" || !OBSERVE_LATER_OPERATIONS.includes(payload.operation)) {
      throw fail('observe accepts only "later", on run, run_ref, run_batch or run_batch_ref.');
    }
    const { observe: _observe, ...rest } = payload;
    payload = rest;
    observeLater = true;
  }
  if (["check_closeout", "closeout", "check_deployment", "deploy"].includes(payload.operation) && (
      Object.keys(payload).sort().join() !== "operation,plan_path,plan_sha256"
      || typeof payload.plan_path !== "string" || !payload.plan_path.startsWith("/")
      || typeof payload.plan_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(payload.plan_sha256))) {
    throw fail("Exact parent-admitted closeout/deployment plan required.");
  }
  if (["read_closeout", "read_deployment"].includes(payload.operation) && (
      Object.keys(payload).sort().join() !== "action_id,artifact_root,operation"
      || typeof payload.artifact_root !== "string" || !payload.artifact_root.startsWith("/")
      || typeof payload.action_id !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$/.test(payload.action_id))) {
    throw fail("Exact original closeout/deployment reference required.");
  }
  if (payload.operation === "query_surfaces" && (Object.keys(payload).sort().join() !== "operation,targets,workspace"
      || typeof payload.workspace !== "string" || !payload.workspace.startsWith("/")
      || payload.workspace.startsWith("//") || [...payload.workspace].length > 4096
      || /[\x00\r\n]/.test(payload.workspace)
      || (payload.workspace !== "/" && payload.workspace.endsWith("/"))
      || (payload.workspace !== "/" && payload.workspace.split("/").slice(1).some((part) => ["", ".", ".."].includes(part)))
      || !Array.isArray(payload.targets) || payload.targets.length < 1 || payload.targets.length > 4
      || new Set(payload.targets).size !== payload.targets.length
      || payload.targets.some((name) => typeof name !== "string" || [...name].length < 1 || [...name].length > 512
        || name.startsWith("/") || /[*?\[\]{}\\\x00\r\n]/.test(name)
        || name.split("/").some((part) => ["", ".", "..", ".git"].includes(part))))) {
    throw fail("Exact surface query workspace and 1 through 4 normalized relative targets required.");
  }
  if (payload.operation === "navigate") {
    const allowed = new Set(["operation", "workspace", "read_paths", "query", "queries", "projection", "offset", "expected_map_sha256", "expected_source_snapshot_sha256"]);
    if (Object.keys(payload).some((k) => !allowed.has(k))
        || typeof payload.workspace !== "string" || !payload.workspace.startsWith("/")
        || !Array.isArray(payload.read_paths) || payload.read_paths.length < 1 || payload.read_paths.length > 32
        || new Set(payload.read_paths).size !== payload.read_paths.length
        || payload.read_paths.some((p) => typeof p !== "string" || !p || p.length > 512 || /[*?\[\]{}\\\x00\r\n]/.test(p)
          || (p !== "." && p.split("/").some((part) => !part || part.startsWith("."))))
        || (Object.hasOwn(payload, "query") && (typeof payload.query !== "string" || payload.query.length > 512))
        || (Object.hasOwn(payload, "queries") && (!Array.isArray(payload.queries)
          || payload.queries.length < 1 || payload.queries.length > 32
          || payload.queries.some((term) => typeof term !== "string" || !term || Buffer.byteLength(term, "utf8") > 256
            || Buffer.from(term, "utf8").toString("utf8") !== term)
          || (payload.query ?? "") !== ""))
        || (Object.hasOwn(payload, "projection") && !["full", "locations"].includes(payload.projection))
        || (payload.offset != null && (!Number.isSafeInteger(payload.offset) || payload.offset < 0))
        || (payload.expected_map_sha256 != null && !/^[0-9a-f]{64}$/.test(payload.expected_map_sha256))
        || (payload.expected_source_snapshot_sha256 != null && (typeof payload.expected_source_snapshot_sha256 !== "string"
          || !/^[0-9a-f]{64}$/.test(payload.expected_source_snapshot_sha256)))
        || (payload.offset > 0 && !payload.expected_map_sha256)) throw fail("Invalid source-navigation scope or cursor.");
  }
  if (payload.operation === "read_preparation" && (Object.keys(payload).length !== 3
      || typeof payload.artifact_root !== "string" || !payload.artifact_root.startsWith("/")
      || typeof payload.input_id !== "string" || !/^native-input-[0-9a-f]{64}$/.test(payload.input_id))) {
    throw fail("Exact original preparation reference required.");
  }
  const parentReference = Object.hasOwn(payload, "parent_completion");
  if (parentReference && (payload.parent_completion !== true || payload.operation !== "run_batch_ref"
      || !projection.adaptiveNative || !observeLater)) {
    throw fail("Parent completion reference requires Efficient run_batch_ref with observe:later.");
  }
  if (["run_ref", "run_batch_ref"].includes(payload.operation) && (Object.keys(payload).length !== (parentReference ? 4 : 3)
      || typeof payload.input_path !== "string" || !payload.input_path.startsWith("/")
      || typeof payload.input_sha256 !== "string" || payload.input_sha256.length !== 64
      || !/^[0-9a-f]{64}$/.test(payload.input_sha256))) throw fail("Exact reference path and lowercase SHA-256 required.");
  if (payload.operation === "run_batch") payload = applyBatchIntentDefaults(payload);
  if (Object.hasOwn(payload, "post_batch_verifier")) {
    const spec = lowerAcceptanceIntent(payload.post_batch_verifier);
    payload = { ...payload, post_batch_verifier: spec };
    if (projection.workflow !== "nokiy-direct" || payload.operation !== "run_batch"
        || !spec || typeof spec !== "object" || Array.isArray(spec)
        || Object.keys(spec).some((key) => !["draft", "action", "surface_id", "parent_source_targets"].includes(key))
        || !spec.draft || typeof spec.draft !== "object" || Array.isArray(spec.draft)
        || !spec.action || typeof spec.action !== "object" || Array.isArray(spec.action)
        || Object.keys(spec.draft).some((key) => !Object.hasOwn(contract.draft.properties, key))
        || Object.keys(spec.action).some((key) => ["operation", "run", "native_thread_id", "thread_id", "thread",
          "model", "worker", "effort", "reasoning_effort", "service_tier", "credentials", "HOME", "CODEX_HOME", "CODEX_THREAD_ID"].includes(key))
        || spec.draft.authority_effect !== "none"
        || ["operations", "allowed_operations"].some((key) => Object.hasOwn(spec.action, key)
          && (!Array.isArray(spec.action[key]) || spec.action[key].some((op) => !["read", "command"].includes(op))))
        || ["write_scopes", "target_paths"].some((key) => Object.hasOwn(spec.action, key)
          && (!Array.isArray(spec.action[key]) || spec.action[key].length))
        || !Array.isArray(spec.action.verifier_commands) || spec.action.verifier_commands.length !== 1
        || !spec.action.verifier_commands[0] || typeof spec.action.verifier_commands[0] !== "object"
        || Array.isArray(spec.action.verifier_commands[0])
        || (Object.hasOwn(spec, "surface_id") && (typeof spec.surface_id !== "string" || !spec.surface_id))) {
      throw fail("Exact run_batch post_batch_verifier read/command preparation packet required.");
    }
    if (Object.hasOwn(spec, "parent_source_targets") && (!projection.adaptiveNative || !observeLater
        || !Array.isArray(spec.parent_source_targets) || !spec.parent_source_targets.length
        || spec.parent_source_targets.length > 128
        || spec.parent_source_targets.some((name) => typeof name !== "string" || !name)
        || new Set(spec.parent_source_targets).size !== spec.parent_source_targets.length)) {
      throw fail("Parent completion requires Efficient inline run_batch with observe:later and exact parent source targets.");
    }
  }
  if (payload.operation === "run_batch" && Array.isArray(payload.members)) {
    payload = { ...payload, members: lowerMemberIntents(payload.members, payload.artifact_root,
      { operatorMessage: (projection.userTexts ?? []).length > 0 }) };
  }
  if (["run", "run_batch"].includes(payload.operation)) {
    payload = pinCatalogReferences(payload);
    payload = expandUserReferences(payload, projection.userTexts ?? []);
    if (Buffer.byteLength(JSON.stringify(payload)) > MAX_PAYLOAD_BYTES) throw fail("Expanded Nokiy tool payload is too large.");
  }
  const serial = projection.workflow === "nokiy";
  const envelope = Buffer.from(JSON.stringify({ schema_version: serial ? "nokiy_native_tool_v3" : "nokiy_native_tool_v2",
    ...(serial ? { workflow: "nokiy" } : {}), worker: projection.worker,
    reasoning_effort: projection.workerEffort, service_tier: projection.serviceTier, payload })).toString("base64url");
  const cmd = [projection.host.python, projection.host.script, "--payload", envelope].map(quote).join(" ");
  const args = { cmd, yield_time_ms: 1000, max_output_tokens: 131072 };
  if (projection.target.type === "function") {
    if (item.type !== "function_call") throw fail("Native tool call/output type mismatch.");
    if (!observeLater && projection.target.executionSchema) {
      const schema = projection.target.executionSchema;
      const maximum = schema.properties?.yield_time_ms?.maximum;
      const longer = { ...args, yield_time_ms: Number.isSafeInteger(maximum)
        ? Math.min(30000, maximum) : 30000 };
      if (longer.yield_time_ms > args.yield_time_ms && nativeArgumentValidator(schema)(longer)) {
        args.yield_time_ms = longer.yield_time_ms;
      }
    }
    return { ...item, name: projection.target.name,
      ...(projection.target.namespace ? { namespace: projection.target.namespace } : {}),
      arguments: JSON.stringify(args) };
  }
  if (item.type !== "custom_tool_call") throw fail("Native tool call/output type mismatch.");
  return { ...item, name: projection.target.name,
    ...(projection.target.namespace ? { namespace: projection.target.namespace } : {}),
    input: renderNativeExecution(args, false, observeLater,
      Boolean(payload.post_batch_verifier?.parent_source_targets) || parentReference) };
}

function matchesObservedNativeCall(item, projection) {
  if (matchesProjectedNativeCall(item, projection)) return true;
  if (!projection.nativeExecution || projection.target.type !== "custom" || item.type !== "custom_tool_call"
      || item.name !== projection.target.name || (item.namespace ?? null) !== projection.target.namespace
      || typeof item.input !== "string" || Buffer.byteLength(item.input) > 2 * MAX_PAYLOAD_BYTES) return false;
  const suffix = ", true);";
  const prefix = renderNativeExecution(null, true).slice(0, -("null" + suffix).length);
  if (!item.input.startsWith(prefix) || !item.input.endsWith(suffix)) return false;
  const raw = item.input.slice(prefix.length, -suffix.length);
  try {
    const args = JSON.parse(raw);
    return args && typeof args === "object" && !Array.isArray(args) && projection.nativeExecution.validate(args)
      && Object.keys(args).every((name) => Object.hasOwn(projection.nativeExecution.schema.properties, name))
      && renderNativeExecution(args, true) === item.input;
  } catch { return false; }
}

function matchesProjectedNativeCall(item, projection, recordedNativeCall = null) {
  if (item.name !== projection.target.name || (item.namespace ?? null) !== projection.target.namespace) return false;
  const field = item.type === "function_call" ? "arguments" : "input";
  const input = item[field];
  if (typeof input !== "string" || Buffer.byteLength(input) > 2 * MAX_PAYLOAD_BYTES) return false;
  const encoded = input.match(/'--payload' '([A-Za-z0-9_-]+)'/)?.[1];
  if (!encoded) return false;
  try {
    const envelope = JSON.parse(Buffer.from(encoded, "base64url").toString("utf8"));
    if (!["run", "run_ref", "run_batch", "run_batch_ref"].includes(envelope?.payload?.operation)) return false;
    // The envelope never carries observe; a deferred-observation cell differs only in its first yield.
    const current = [envelope.payload, { ...envelope.payload, observe: "later" }].some((payload) => {
      try { return nativeToolItem({ ...item, name: NOKIY_TOOL, [field]: JSON.stringify(payload) }, projection)[field] === input; }
      catch { return false; }
    });
    if (current) return true;
    // A previous release may have emitted a different immutable host path or wrapper.
    // Only an exact private replay-store record can explain that difference. This path
    // is result delivery only; execution/observation still use the current strict match.
    if (typeof recordedNativeCall !== "function" || envelope.worker !== projection.worker
        || envelope.reasoning_effort !== projection.workerEffort || envelope.service_tier !== projection.serviceTier
        || envelope.schema_version !== (projection.workflow === "nokiy" ? "nokiy_native_tool_v3" : "nokiy_native_tool_v2")
        || (projection.workflow === "nokiy" ? envelope.workflow !== "nokiy" : envelope.workflow !== undefined)) return false;
    const original = recordedNativeCall(item);
    return original?.name === NOKIY_TOOL && original.call_id === item.call_id && original.type === item.type;
  } catch { return false; }
}

function projectLocalToolRejection(item, error, projection) {
  const target = projection.target;
  const referenceRejection = item?.name === NOKIY_TOOL && error instanceof SwitchboardError
    && error.code === "NOKIY_USER_REFERENCE_UNRESOLVED";
  // Adapter validation precedes all host execution in every workflow. Ultra must
  // return the same correctable intake result as Efficient, not abort its stream.
  const inputRejection = item?.name === NOKIY_TOOL
    && error instanceof SwitchboardError && error.code === "NOKIY_NATIVE_TOOL_INVALID";
  const correctable = referenceRejection || inputRejection;
  if ((!projection.coordinatorBindings && !correctable) || !(error instanceof SwitchboardError) || error.status !== 422
      || ![null, "functions"].includes(target.namespace ?? null)
      || typeof item.call_id !== "string" || !item.call_id || item.call_id.length > 128
      || (item.id !== undefined && (typeof item.id !== "string" || !item.id))) return null;
  if (correctable) {
    // Correctable intake error: no host command, preparation or worker is started.
  } else if (item.name === NOKIY_NATIVE) {
    if (!projection.nativeExecution || error.code !== "NOKIY_NATIVE_TOOL_INVALID") return null;
  } else if (item.name === NOKIY_OBSERVE) {
    if (!projection.nativeObservation || error.code !== "NOKIY_NATIVE_OBSERVATION_INVALID"
        || !["HANDLE_KIND_MISMATCH", "HANDLE_ARGUMENTS_INVALID", "NATIVE_SCHEMA_MISMATCH",
          "ORIGINAL_PAIR_UNAVAILABLE", "ORIGINAL_RUNNING_HANDLE_UNVERIFIED"]
          .includes(error.details?.native_observation?.reason)) return null;
  } else return null;
  const rejection = JSON.stringify({ status: "REJECTED_LOCAL_TOOL_CALL", execution_started: false,
    same_input_retry_allowed: false, safe_to_correct_input: true,
    error: { code: error.code, message: error.message,
      ...(error.details === undefined ? {} : { details: error.details }) } });
  const result = { type: item.type, name: target.name, call_id: item.call_id,
    ...(item.id === undefined ? {} : { id: item.id }),
    ...(target.namespace ? { namespace: target.namespace } : {}) };
  if (item.status !== undefined) {
    if (!["in_progress", "completed", "incomplete"].includes(item.status)) return null;
    result.status = item.status;
  }
  if (item.type === "custom_tool_call" && target.type === "custom" && target.name === "exec") {
    return { ...result, input: `text(${rejection});` };
  }
  if (item.type !== "function_call" || target.type !== "function" || target.name !== "exec_command"
      || (!projection.nativeExecution && !correctable)) return null;
  const args = { cmd: `/usr/bin/printf '%s\\n' ${quote(rejection)}` };
  if (projection.nativeExecution && !projection.nativeExecution.validate(args)) return null;
  if (target.executionSchema && !nativeArgumentValidator(target.executionSchema)(args)) return null;
  return { ...result, arguments: JSON.stringify(args) };
}

// Tool call types and IDs are preserved for native call/output pairing.
// Nothing in this module executes the command; the native tool owner does that.
export function nativeEventProjector(projection, { onProjected = null } = {}) {
  const virtualIds = new Set();
  let deliveryOutcome = "unobserved";
  const deliver = resultDeliveryProjector(projection?.resultDelivery, (outcome) => { deliveryOutcome = outcome; },
    (original, expanded) => { if (onProjected) try { onProjected(original, expanded); } catch { /* optional */ } });
  const projectItem = (item) => {
    try { return nativeToolItem(item, projection); }
    catch (error) {
      const rejection = projectLocalToolRejection(item, error, projection);
      if (rejection) return rejection;
      throw error;
    }
  };
  // Lets a later full replay restore the model's own call; never affects this projection.
  const observed = (original, projected) => {
    if (onProjected && original !== projected) try { onProjected(original, projected); } catch { /* optional */ }
    return projected;
  };
  const project = (event) => {
    if (!projection || !event || typeof event !== "object") return event;
    const rawItem = event.item;
    const rawOutput = Array.isArray(event.response?.output) ? event.response.output : null;
    if (projection.coordinatorBindings) {
      const item = checkCoordinatorCall(event.item, projection.coordinatorBindings,
        { complete: event.type === "response.output_item.done", stage: event.type });
      if (item !== event.item) event = { ...event, item };
      if (Array.isArray(event.response?.output)) {
        event = { ...event, response: { ...event.response, output: event.response.output.map((output) =>
          checkCoordinatorCall(output, projection.coordinatorBindings, { complete: true, stage: event.type })) } };
      }
    }
    if (["custom_tool_call", "function_call"].includes(event.item?.type)
        && (event.item.name === NOKIY_TOOL || projection.coordinatorBindings
          || (projection.adaptiveNative && event.item.type === "function_call"
            && event.item.name === "wait"
            && (event.item.namespace === undefined || event.item.namespace === "functions")))) {
      if (event.item.id) virtualIds.add(event.item.id);
      if (event.type !== "response.output_item.done") return null;
      return { ...event, item: observed(rawItem, projectItem(event.item)) };
    }
    if (virtualIds.has(event.item_id) && (event.type?.startsWith("response.custom_tool_call_input.")
      || event.type?.startsWith("response.function_call_arguments."))) return null;
    if (Array.isArray(event.response?.output)) {
      return { ...event, response: { ...event.response,
        output: event.response.output.map((item, index) => observed(rawOutput?.[index] ?? item, projectItem(item))) } };
    }
    return event;
  };
  const projector = (event) => {
    try { return deliver(project(event)); }
    catch (error) { deliveryOutcome = "projection_failed"; throw error; }
  };
  // Metadata only: never expose the result text, reference or source paths.
  projector.resultDeliveryObservation = () => ({
    eligibility: !projection?.resultDeliveryEnabled ? "disabled"
      : projection.resultDelivery ? "eligible" : "ineligible",
    outcome: deliveryOutcome,
    ...(projection?.resultDeliveryRejection ? { reason: projection.resultDeliveryRejection } : {}),
  });
  return projector;
}

export function nativeSseProjector(project) {
  const decoder = new StringDecoder("utf8");
  let pending = "";
  function drain(final = false) {
    pending = pending.replaceAll("\r\n", "\n");
    const output = [];
    let boundary;
    while ((boundary = pending.indexOf("\n\n")) >= 0) {
      const frame = pending.slice(0, boundary);
      pending = pending.slice(boundary + 2);
      const data = frame.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
      if (!data || data === "[DONE]") { output.push(`${frame}\n\n`); continue; }
      let event;
      try { event = JSON.parse(data); } catch { throw fail("Malformed official SSE frame."); }
      const transformed = project(event);
      for (const item of [transformed].flat()) {
        if (item) output.push(`data: ${JSON.stringify(item)}\n\n`);
      }
    }
    if (Buffer.byteLength(pending) > 4 * 1024 * 1024) throw fail("Official SSE frame exceeds the bounded adapter buffer.");
    if (final && pending.trim()) throw fail("Incomplete official SSE frame; no replay attempted.");
    return output.join("");
  }
  return {
    push(chunk) { pending += decoder.write(chunk); return drain(); },
    end() { pending += decoder.end(); return drain(true); },
  };
}
