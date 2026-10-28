import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, describe, test } from "node:test";

import { projectNokiyDirect } from "../src/nokiy-direct-route.mjs";

const CONTINUATION = [
  "A valid capability_gap is diagnostic, not a grant: the native parent must first recover the original request terminal and reconcile effects/cleanup;",
  "then freshly admit only unfinished work whose exact path/operation is already within original human authorization, with fresh owner/CAS/lease checks, without asking again.",
  "Genuine prohibitions, new protected scopes, owner conflicts or unresolved effects stop continuation.",
  "A read-only patch proposal must contain a usable diff or exact missing-read evidence, never promises or placeholders.",
  "Prefer advertised Nokiy deterministic capabilities such as read_http for exact authorized HTTPS evidence. A coordinator-only parent cannot fall back to raw execution; outside that mode, authorized parent native execution must be labeled native execution.",
  "No self-grant, recursive dispatch, replay of consumed/maybe-executed effects, daemon/orchestrator or new controller.",
].join(" ");
const SOURCE_SECTIONS = [
  'Only after verifying installed caller support, optionally preload already-known exact authorized local non-DCF UTF-8 source ranges with action.source_sections=[{"path":"exact authorized path","start_line":1,"end_line":20}].',
  "Limits: max 16 ranges, 400 lines each, 12000 UTF-8 excerpt bytes total; the complete serialized summary including metadata and escaping must also fit 12000 characters.",
  "Preparation SHA-binds partial preimages, not dependency proof, a new grant, post-edit evidence or DCF bypass.",
  "Avoid rereading unchanged supplied ranges; missing context, tests and fresh postimages remain required.",
  "This is an action field, not a draft field.",
].join(" ");
const INITIAL_TASK_STATE = [
  'Only with a verified matching V132+ installed caller/runtime and an already-known task type, prefer action.initial_task_state={task_group:<bounded objective>,task_type:["debug"]} for debugging (example only, not a default for all tasks).',
  "Other IDs require a known supported task type; omit initial_task_state when type is unknown.",
  "This is an action field, not a draft field.",
  "Omission preserves legacy startup; resumed state and permissions remain unchanged.",
].join(" ");
const TERMINAL_DELIVERY = [
  'The installed caller/runtime supports action.terminal_delivery="evidence_only": prefer it for bounded execution/artifact deliverables the parent will inspect, so the worker ends with task_status done in the same command array as its final passing check instead of a separate report turn.',
  'Reviews, questions and narrative conclusions retain action.terminal_delivery="assistant_reply"; omission remains assistant_reply.',
  "terminal_delivery is an action field, not a draft field.",
  "Exact persisted same-request terminal_evidence, observed_terminal_delivery and cleanup are required; never infer success from absent prose.",
].join(" ");
const ACCEPTANCE_ECONOMY = [
  "When result_inspection.status is EVIDENCE_VERIFIED and the inspection is bound to the expected request/thread, reuse its proven artifact/command/cleanup checks rather than repeat hashes, key dumps or process probes;",
  "still inspect task-specific quality and necessary changed-file postimages, with fresh owner/CAS at effects.",
  "Where inputs are known, combine remaining task verification, postimage/quality readback, compact evidence publication and authorized closeout into one dependency-ordered parent acceptance call.",
  "This is not a call-count cap, grant or automatic acceptance; failed/incomplete/unknown evidence still requires original-ID recovery.",
].join(" ");
const ORIGINAL = "Preserve these original instructions byte-for-byte.\nOnly originally authorized synthetic paths may be changed.";
const catalog = {
  models: ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"].map((slug) => ({
    slug,
    supported_reasoning_levels: ["low", "medium", "high", "max", "xhigh", "ultra"]
      .map((effort) => ({ effort })),
    multi_agent_reasoning_effort: "xhigh",
  })),
};
const worker = { model: "gpt-6.1-sol", reasoning_effort: "max", service_tier: "default" };
const profiles = [
  {
    slug: "nokiy", display_name: "Nokiy serial test", workflow: "nokiy",
    coordinator: { model: "gpt-6-astra", reasoning_effort: "ultra" }, worker: { ...worker },
  },
  {
    slug: "nokiy-direct-sol", display_name: "Nokiy Ultra test", workflow: "nokiy-direct",
    coordinator: { model: "gpt-6-astra", reasoning_effort: "ultra" }, worker: { ...worker },
  },
  {
    slug: "nokiy-efficient", display_name: "Nokiy Efficient test", workflow: "nokiy-direct",
    coordinator: { model: "gpt-6.1-sol", reasoning_effort: "ultra" }, worker: { ...worker },
    worker_selector: {
      default_effort: "medium",
      models: { low: "gpt-6-luna", medium: "gpt-6.1-sol", high: "gpt-6-astra" },
    },
  },
];
// These nonexistent paths are descriptors only: no host execution is invoked.
const host = { python: "/synthetic/nokiy/python", script: "/synthetic/nokiy/host.py" };
const options = { catalog, profiles, host };
const actionContract = JSON.parse(readFileSync(
  new URL('../src/nokiy-native-contract.json', import.meta.url), 'utf8')).action;

function request(profile) {
  return {
    model: profile.slug,
    instructions: ORIGINAL,
    reasoning: { effort: profile.worker_selector ? "medium" : "xhigh", summary: "auto" },
    service_tier: "priority",
    parallel_tool_calls: true,
    metadata: { original_authorization: "synthetic-only" },
    tools: [
      { type: "namespace", name: "functions", tools: [
        { type: "function", name: "exec_command", parameters: {
          type: "object", properties: { cmd: { type: "string" } }, required: ["cmd"],
        } },
        { type: "function", name: "source_read", parameters: {
          type: "object", properties: { path: { type: "string" } }, required: ["path"],
        } },
      ] },
      { type: "namespace", name: "collaboration", tools: [
        { type: "function", name: "spawn_agent", parameters: { type: "object" } },
      ] },
    ],
    input: [
      { type: "message", role: "user", content: "Complete only the bounded synthetic patch." },
      { type: "additional_tools", tools: [
        { type: "function", name: "resume_agent", parameters: { type: "object" } },
        { type: "function", name: "inspect_fixture", parameters: { type: "object" } },
      ] },
    ],
  };
}

function toolNames(tools) {
  return tools.flatMap((tool) => tool.type === "namespace" ? toolNames(tool.tools) : [tool.name]);
}

function nonExecutionBody(body, profile) {
  return { ...body, model: profile.coordinator.model,
    ...(profile.worker_selector ? { reasoning: { ...body.reasoning, effort: "xhigh" } } : {}),
  };
}

describe("alias capability-gap continuation guidance", { concurrency: false }, () => {
  let fixtureHome;
  let originalHome;
  before(() => {
    originalHome = process.env.CODEX_HOME;
    fixtureHome = mkdtempSync(join(tmpdir(), "nokiy-capability-continuation-"));
    // Direct projection reads capacity; keep that read entirely in this synthetic fixture.
    writeFileSync(join(fixtureHome, "config.toml"), "[agents]\nmax_threads = 2\n");
    process.env.CODEX_HOME = fixtureHome;
  });
  after(() => {
    if (originalHome === undefined) delete process.env.CODEX_HOME;
    else process.env.CODEX_HOME = originalHome;
    if (fixtureHome) rmSync(fixtureHome, { recursive: true, force: true });
  });

  for (const profile of profiles) {
    test(`${profile.slug}: one constant recovery/admission/proposal/fallback paragraph`, () => {
      const projected = projectNokiyDirect(request(profile), options);
      assert.ok(projected.body.instructions.startsWith(`${ORIGINAL}\n\n`));
      const lines = projected.body.instructions.slice(ORIGINAL.length + 2).split("\n");
      assert.equal(lines.length, profile.workflow === "nokiy" ? 6 : 7);
      assert.equal(lines.at(-1), CONTINUATION);
      assert.equal(lines.filter((line) => line.includes("capability_gap")).length, 1);
      assert.ok(lines[0].includes("native Codex remains the task/Goal, permission and tool owner"));
      assert.ok(lines[1].startsWith(`Selected workflow ${profile.slug} applies to this request`));
      assert.ok(lines[2].includes("Read the installed $nokiy skill and invocation details only when task-scoping/DCF guidance is needed."));
      assert.ok(lines[3].includes("Never recursively dispatch Nokiy or treat selecting this mode as permission for effects."));
      assert.ok(lines[4].includes("Recover uncertain outcomes with read-result on the ORIGINAL ID, never retry uncertain effects."));
      assert.ok(lines[4].includes(ACCEPTANCE_ECONOMY));
      assert.equal(projected.body.instructions.split(ACCEPTANCE_ECONOMY).length, 2);
      assert.ok(ACCEPTANCE_ECONOMY.split(/\s+/).length <= 120);
      assert.ok(lines[4].endsWith("No bare provider inference counts as the Nokiy runtime."));
    });

    test(`${profile.slug}: bound native dispatch does not repeat manual CLI preparation`, () => {
      const projected = projectNokiyDirect(request(profile), options);
      const guidance = projected.body.instructions.slice(ORIGINAL.length + 2);
      const tool = projected.body.tools.find((item) => item.name === "nokiy_execute");
      assert.match(tool.description, /Native host binds its own thread, runtime image, model, effort and tier/);
      assert.match(guidance, /native-host adapter verifies the installed caller, runtime image and native identity/);
      assert.match(guidance, /do not duplicate that binding with shell discovery, manifest reads or manual hash checks/);
      assert.match(guidance, /Read a Nokiy result JSON in one piece: give the cell that waits for or prints it an output budget \(functions\.exec max_output_tokens, functions\.wait max_tokens\) of at least 64000\. Run and read-result summaries are bounded at 64 KiB and batch summaries at 128 KiB/);
      assert.match(guidance, /dispatch directly with the advertised tool schema instead of rereading the skill or CLI invocation guide/);
      assert.match(guidance, /If a task-scoping or capability detail is missing, inspect only the relevant guidance/);
      assert.match(guidance, /Manual CLI use without the verified adapter still requires the installed skill binding checks/);
      assert.match(guidance, /the host performs preparation and fresh source\/authority validation before execution/);
      assert.doesNotMatch(guidance, /In the native task resolve and verify the installed caller/);
      assert.ok(projected.body.instructions.startsWith(`${ORIGINAL}\n\n`));
      assert.equal(projected.nativeTool.worker, worker.model);
      assert.equal(projected.nativeTool.workerEffort, worker.reasoning_effort);
      assert.match(tool.description, /source_read:true supports existing exact regular files/);
      assert.match(tool.description, /same-action create\/readback of explicitly planned absent canonical non-hidden exact targets, without symlink ancestry/);
      assert.match(tool.description, /requires read\/command\/create operations and the same exact workspace-relative path in read_scopes, write_scopes and target_paths/);
      assert.match(tool.description, /explicit read authorization, never write-implies-read/);
      assert.match(tool.description, /path\/absence checks and current writer ownership\/CAS still apply/);
      assert.match(tool.description, /matching read_scopes/);
      assert.match(tool.description, /diagnostics\.child_error_code reports a validated DCF cause/);
      assert.match(tool.description, /do not run another worker merely to diagnose preparation/);
    });

    test(`${profile.slug}: outer observation waits separate inner limits, yield first at 600000 ms and wait 300000 ms`, () => {
      const guidance = projectNokiyDirect(request(profile), options).body.instructions.slice(ORIGINAL.length + 2).split("\n")[4];
      assert.match(guidance, /await the original handle/);
      assert.match(guidance, /Keep repeated inner tools\.write_stdin waits in the same functions\.exec\./);
      assert.match(guidance, /The inner tools\.write_stdin 300000 ms limit is not an outer observation cap\./);
      assert.match(guidance, /The adapter cell's first observation yields after at most 600000 ms; if functions\.exec yields a cell ID, use functions\.wait on that original cell with yield_time_ms 300000, not a 1s\/10s polling ladder; any explicitly smaller schema bound still controls\./);
      assert.match(guidance, /These are observation cadences, neither measured maxima nor universal host guarantees\./);
      assert.doesNotMatch(guidance, /3600000/);
      assert.match(guidance, /Use the actual tool schema; never invent an infinite wait\./);
      assert.match(guidance, /This is an observation wait, not a worker deadline; preserve user interruption, original-ID recovery, unknown-effect reconciliation, cleanup and acceptance\./);
      assert.match(guidance, /When an operator message interrupts an observation, answer it from current evidence first, then resume waiting on the original cell\./);
    });

    test(`${profile.slug}: complete batch evidence is consumed without a mandatory artifact reread`, () => {
      const guidance = projectNokiyDirect(request(profile), options).body.instructions.slice(ORIGINAL.length + 2);
      if (profile.workflow === "nokiy") {
        assert.doesNotMatch(guidance, /For nokiy_direct_batch_summary_v1\/v2/);
      } else {
        assert.match(guidance, /For nokiy_direct_batch_summary_v1\/v2/);
        assert.match(guidance, /native_thread_id, request_sha256, evidence_status, cleanup_pass and result_truncated/);
        assert.match(guidance, /v2 member_defaults supplies factored shared fields/);
        assert.match(guidance, /recovery references, not instructions to reopen the same artifacts/);
        assert.match(guidance, /Missing, mismatched, blocked or truncated evidence still requires original-ID recovery/);
        assert.match(guidance, /does not waive semantic acceptance, changed-file readback or fresh effect authority/);
      }
    });

    for (const execType of ["function", "custom"]) {
      test(`${profile.slug}: ${execType} exec exposes one qualified paragraph per action hint`, () => {
        const body = request(profile);
        if (execType === "custom") body.tools[0].tools[0] = {
          type: "custom", name: "exec", format: { type: "text" },
        };
        const snapshot = structuredClone(body);
        const projected = projectNokiyDirect(body, options);
        const executor = projected.body.tools.find((tool) => tool.name === "nokiy_execute");
        assert.deepEqual(body, snapshot);
        assert.deepEqual(projected.body.tools[0], body.tools[0]);
        assert.equal(executor.type, execType);
        assert.deepEqual(projected.nativeTool.target, {
          namespace: "functions", name: execType === "custom" ? "exec" : "exec_command", type: execType,
        });
        assert.equal(executor.description.split("\n\n").at(-3), SOURCE_SECTIONS);
        assert.equal(executor.description.split(SOURCE_SECTIONS).length, 2);
        assert.ok(Buffer.byteLength(SOURCE_SECTIONS, "utf8") <= 900);
        assert.doesNotMatch(executor.description.replace(SOURCE_SECTIONS, ""), /source_sections/);
        assert.equal(executor.description.split("\n\n").at(-2), INITIAL_TASK_STATE);
        assert.equal(executor.description.split(INITIAL_TASK_STATE).length, 2);
        assert.ok(INITIAL_TASK_STATE.split(/\s+/).length <= 85);
        assert.doesNotMatch(executor.description.replace(INITIAL_TASK_STATE, ""), /initial_task_state|V132\+/);
        assert.doesNotMatch(projected.body.instructions, /source_sections|initial_task_state|V132\+/);
        assert.equal(executor.description.split("\n\n").at(-1), TERMINAL_DELIVERY);
        assert.equal(executor.description.split(TERMINAL_DELIVERY).length, 2);
        assert.doesNotMatch(executor.description.replace(TERMINAL_DELIVERY, ""), /terminal_delivery|terminal_evidence/);
        assert.doesNotMatch(projected.body.instructions, /terminal_delivery|terminal_evidence|V127\+/);
        assert.ok(executor.description.includes('{"operation":"read_result","artifact_root":"same absolute artifact directory","request_id":"original tura_embedded_... ID"}'));
        assert.ok(executor.description.includes("Never create a new action after uncertain execution."));
        assert.ok(executor.description.includes("Unknown or in-flight execution still requires original-ID recovery."));
        if (execType === "function") {
          // The advertised action is the intake contract plus adapter-owned verifier and source-query fields.
          const contractPart = (schema) => {
            const extensions = Object.keys(schema.properties).filter((key) => !Object.hasOwn(actionContract.properties, key));
            assert.deepEqual(extensions.sort(), ["source_queries", "verifier_commands"]);
            return { ...schema, properties: Object.fromEntries(Object.entries(schema.properties)
              .filter(([key]) => !extensions.includes(key))) };
          };
          assert.deepEqual(contractPart(executor.parameters.properties.action), actionContract);
          assert.equal(executor.parameters.properties.source_sections, undefined);
          assert.equal(executor.parameters.properties.draft.properties.source_sections, undefined);
          assert.equal(executor.parameters.properties.terminal_delivery, undefined);
          assert.equal(executor.parameters.properties.draft.properties.terminal_delivery, undefined);
          assert.equal(executor.parameters.properties.initial_task_state, undefined);
          assert.equal(executor.parameters.properties.draft.properties.initial_task_state, undefined);
          if (profile.workflow === "nokiy-direct") {
            const member = executor.parameters.properties.members.items.properties;
            assert.deepEqual(contractPart(member.action), actionContract);
            assert.equal(member.source_sections, undefined);
            assert.equal(member.draft.properties.source_sections, undefined);
            assert.equal(member.draft.properties.terminal_delivery, undefined);
            assert.equal(member.initial_task_state, undefined);
            assert.equal(member.draft.properties.initial_task_state, undefined);
          }
        } else {
          assert.deepEqual(executor.format, { type: "text" });
          assert.equal(executor.parameters, undefined);
        }
      });
    }

    test(`${profile.slug}: original routing, schemas and sole executor remain intact`, () => {
      const body = request(profile);
      const snapshot = structuredClone(body);
      const projected = projectNokiyDirect(body, options);
      assert.deepEqual(body, snapshot);
      assert.equal(projected.requestedAlias, profile.slug);
      assert.equal(projected.body.model, profile.coordinator.model);
      assert.deepEqual(projected.body.reasoning, { effort: "xhigh", summary: "auto" });
      assert.equal(projected.body.service_tier, "priority");
      assert.deepEqual(projected.body.metadata, body.metadata);
      assert.equal(projected.body.parallel_tool_calls, profile.workflow !== "nokiy");
      assert.equal(projected.dispatch, "native-tool");
      assert.equal(projected.nativeEffortObserved, null);
      assert.equal(projected.worker, worker.model);
      assert.equal(projected.nativeTool.workerEffort, "max");
      assert.equal(projected.nativeTool.serviceTier, "default");
      assert.equal(projected.nativeTool.workflow, profile.workflow);
      assert.deepEqual(projected.nativeTool.host, host);
      assert.deepEqual(projected.nativeTool.target, { namespace: "functions", name: "exec_command", type: "function" });
      assert.deepEqual(projected.body.tools[0], body.tools[0]);
      assert.deepEqual(toolNames(projected.body.tools), ["exec_command", "source_read", "nokiy_execute"]);
      assert.deepEqual(projected.body.input, [body.input[0], { ...body.input[1], tools: [body.input[1].tools[1]] }]);
      const executor = projected.body.tools.find((tool) => tool.name === "nokiy_execute");
      assert.equal(executor.type, "function");
      assert.deepEqual(executor.parameters.required, ["operation"]);
      assert.equal(executor.parameters.additionalProperties, false);
      assert.deepEqual(executor.parameters.properties.operation.enum, profile.workflow === "nokiy"
        ? ["run", "run_ref", "read_result", "read_preparation", "read_http", "query_surfaces", "navigate"]
        : ["run", "run_ref", "read_result", "read_preparation", "read_http", "query_surfaces", "navigate", "run_batch", "read_batch"]);
      const { query, queries, projection } = executor.parameters.properties;
      assert.equal(query.maxLength, 512);
      assert.equal(queries.type, "array");
      assert.equal(queries.minItems, 1);
      assert.equal(queries.maxItems, 32);
      assert.deepEqual(queries.items, { type: "string", minLength: 1, maxLength: 256 });
      assert.deepEqual(projection.enum, ["full", "locations"]);
      assert.equal(projection.default, "full");
      assert.match(executor.description, /Batch known lookups/);
      assert.match(executor.description, /locations for assignment and full \(default\) for relationships/);
      assert.match(executor.description, /map is syntax evidence, not authority/);
      if (profile.workflow === "nokiy") assert.equal(executor.parameters.properties.max_parallel_workers, undefined);
      else assert.equal(executor.parameters.properties.max_parallel_workers.maximum, 2);
      // Projection does not recursively route the coordinator or install a second executor.
      assert.equal(projectNokiyDirect(projected.body, options), null);
    });

    test(`${profile.slug}: native-tool guards do not become a router fallback`, () => {
      const body = request(profile);
      // 2026-10-27: a full request without native exec is answered by the coordinator without
      // dispatch; an unbound chained request can answer but cannot call tools.
      const answered = projectNokiyDirect({ ...body, tools: [] }, options);
      assert.notEqual(answered, null);
      assert.equal(answered.dispatch, "none");
      assert.equal(answered.nativeTool, null);
      const chained = projectNokiyDirect({ ...body, tools: [], previous_response_id: "resp_1" }, options);
      assert.equal(chained.dispatch, "none");
      assert.equal(chained.body.tool_choice, "none");
      assert.throws(() => projectNokiyDirect({ ...body, tool_choice: { type: "function", name: "spawn_agent" } }, options),
        { code: "NOKIY_DIRECT_SUBAGENT_FORBIDDEN" });
      assert.throws(() => projectNokiyDirect({ ...body, tools: [...body.tools,
        { type: "function", name: "nokiy_execute", parameters: { type: "object" } },
      ] }, options), { code: "NOKIY_NATIVE_TOOL_INVALID" });
    });

    test(`${profile.slug}: prewarm without exec is unchanged and has no guidance`, () => {
      const body = { ...request(profile), generate: false, tools: [] };
      delete body.reasoning;
      const projected = projectNokiyDirect(body, options);
      assert.deepEqual(projected.body, nonExecutionBody(body, profile));
      assert.doesNotMatch(projected.body.instructions, /result_inspection|dependency-ordered parent acceptance/);
      assert.doesNotMatch(JSON.stringify(projected.body), /source_sections|initial_task_state|V132\+/);
      assert.equal(projected.nativeTool, null);
      assert.equal(projected.dispatch, "none");
    });

    test(`${profile.slug}: prewarm with exec has one adapter but dispatches nothing`, () => {
      const body = { ...request(profile), generate: false };
      const snapshot = structuredClone(body);
      const projected = projectNokiyDirect(body, options);
      assert.deepEqual(body, snapshot);
      assert.equal(projected.dispatch, "none");
      assert.ok(projected.body.instructions.startsWith(`${ORIGINAL}\n\n`));
      assert.ok(projected.body.instructions.endsWith(CONTINUATION));
      assert.ok(projected.body.instructions.includes(ACCEPTANCE_ECONOMY));
      // The prewarm only caches a prefix the executing request will reuse byte-for-byte.
      const executing = projectNokiyDirect(request(profile), options).body;
      for (const field of ["instructions", "tools", "parallel_tool_calls", "reasoning", "model"]) {
        assert.deepEqual(projected.body[field], executing[field], field);
      }
      assert.equal(toolNames(projected.body.tools).filter((name) => name === "nokiy_execute").length, 1);
      assert.equal(projected.nativeTool.target.name, "exec_command");
    });

    for (const endpoint of ["/responses/compact", "/responses"]) {
      test(`${profile.slug}: ${endpoint} compaction never executes`, () => {
        const body = request(profile);
        delete body.reasoning;
        if (endpoint === "/responses") body.input.push({ type: "compaction_trigger" });
        const snapshot = structuredClone(body);
        const projected = projectNokiyDirect(body, { ...options, endpoint });
        assert.deepEqual(body, snapshot);
        assert.equal(projected.nativeTool, null);
        assert.equal(projected.dispatch, "none");
        if (endpoint === "/responses/compact") {
          assert.equal(projected.cacheAlignment, "skipped:legacy_endpoint");
          assert.deepEqual(projected.body, nonExecutionBody(body, profile));
          assert.doesNotMatch(projected.body.instructions, /result_inspection|dependency-ordered parent acceptance/);
          assert.doesNotMatch(JSON.stringify(projected.body), /source_sections|initial_task_state|V132\+/);
          return;
        }
        // Remote compaction v2 replays the conversation: its prefix must match the executing
        // request byte for byte, or the 300-400K-token request misses the prompt cache.
        assert.equal(projected.cacheAlignment, "applied");
        const executing = projectNokiyDirect(request(profile), options).body;
        for (const field of ["instructions", "tools", "parallel_tool_calls", "model"]) {
          assert.deepEqual(projected.body[field], executing[field], field);
        }
        assert.deepEqual(projected.body.input, [...executing.input, { type: "compaction_trigger" }]);
      });
    }

    test(`${profile.slug}: compaction falls back to the unmodified request when projection fails`, () => {
      const body = request(profile);
      delete body.reasoning;
      body.tools.push({ type: "function", name: "nokiy_execute", parameters: { type: "object" } });
      body.input.push({ type: "compaction_trigger" });
      assert.throws(() => projectNokiyDirect({ ...request(profile), tools: body.tools }, options));
      const projected = projectNokiyDirect(body, { ...options, endpoint: "/responses" });
      assert.deepEqual(projected.body, nonExecutionBody(body, profile));
      assert.equal(projected.nativeTool, null);
      assert.equal(projected.dispatch, "none");
      assert.match(projected.cacheAlignment, /^fallback:/);
      const withoutExec = { ...request(profile), tools: [], input: [{ type: "compaction_trigger" }] };
      delete withoutExec.reasoning;
      assert.equal(projectNokiyDirect(withoutExec, { ...options, endpoint: "/responses" }).cacheAlignment, "skipped:no_native_exec");
    });
  }

  test("ordinary routing still bypasses alias profile and host validation", () => {
    for (const model of ["gpt-6.1-sol", "ordinary-synthetic"]) {
      const body = { model, instructions: 42, tools: [], metadata: { unchanged: "yes" } };
      const snapshot = structuredClone(body);
      assert.equal(projectNokiyDirect(body, { catalog: null, profiles: [], host: null }), null);
      assert.deepEqual(body, snapshot);
      assert.doesNotMatch(JSON.stringify(body), /source_sections|initial_task_state|V132\+/);
    }
  });

  test("unknown Nokiy aliases still fail without creating an executor", () => {
    assert.throws(() => projectNokiyDirect({ ...request(profiles[0]), model: "nokiy-direct-unknown" }, options),
      { code: "NOKIY_MODE_UNSUPPORTED" });
  });
});
