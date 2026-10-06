# Nokiy model-picker topology — implemented source candidate

**Status: CANDIDATE_LISTED_NOT_ADMITTED / native topology incomplete.**

The worker-selection implementation and real native model-list proof exist. The native root and delegation adapters are NOT implemented or activated. No production model catalog, installed caller, signed native binary, global config, user thread, service or trading process was changed.

## Requested behavior

| Local picker | Root/commander | Worker selector |
| --- | --- | --- |
| `nokiy` / Nokiy | Nokiy core without an extra LLM commander | Luna / Sol / Astra, each Max |
| `nokiy-direct` / Nokiy Direct | Fixed native `gpt-6-astra` / `ultra` | Luna / Sol / Astra, each Max |

The UI strings `luna`, `sol`, `astra` are LOCAL model-family selectors. They must not reach the provider as reasoning effort. Fast/service tier is independent. No Auto selection or implicit family fallback is introduced.

The existing runtime's `execution_profile=direct` does NOT mean the `Nokiy Direct` commander topology.

## Implemented files

- `src/codex_collaboration_harness/protocol/nokiy_model_topology_v1.json`: shared selection contract, no authority grants.
- `src/codex_collaboration_harness/model_topology.py`: six-way resolution, explicit conflict rejection, catalog capability checks, candidate-only catalog builder, prepared selection integrity validation.
- `src/codex_collaboration_harness/full_stack.py`: optional worker-family projection before existing DCF/local J-Space compilation. Original scopes, freshness, identity, image checks and preflight remain controlling. Selection and preparation are published before request.json.
- `src/codex_collaboration_harness/embedded_nokiy.py`: preparation flags and CLI validation before run/preflight. Original-ID read-result remains independent of the current picker.
- `tests/test_model_topology.py`, `tests/test_model_topology_prepare.py`: selection and real caller-preparation tests with existing synthetic DCF/image fixtures; no provider sends.
- `scripts/verify_nokiy_model_picker.py`: actual native parser and public model/list canary, using a separate candidate catalog. No thread/start or turn/start request.

## Source-only entrypoints

These flags are implemented in the source candidate, NOT installed into caller v33.

```sh
PYTHONPATH=src python3 -m codex_collaboration_harness.model_topology plan \
  --model nokiy-direct --worker-family luna
```

This resolves Astra/Ultra commander plus Luna/Max worker without executing a model.

In an actual native parent with separately authorized action and verified identities:

```sh
PYTHONPATH=src python3 -m codex_collaboration_harness.embedded_nokiy prepare \
  --request /absolute/new-draft.json --action /absolute/action.json \
  --output-dir /absolute/new-preparation \
  --topology-model nokiy-direct --worker-family luna
```

DCF workspaces still require their exact verified surface argument. This prepares the WORKER only, not a native commander. Explicit conflicting draft model/effort fields are rejected rather than silently rewritten. Scopes, network authorization, workspace and parent thread are not inferred from selection.

Selected requests carry a strictly validated, versioned model-selection marker in their own canonical request encoding and digest. CLI run/preflight require both the declared preparation and companion for marked requests, and decode/digest/use the same bounded request snapshot; removing adjacent files cannot downgrade a selected request to legacy. Unmarked historical requests retain their encoding and ID; read-result by original ID never consults the current picker. The companion is integrity evidence, not authentication or a root authority grant. It binds exact prepared request bytes, selected family and native thread; `root_topology_verified=false` and `provider_observed_model=null`. The native host must separately enforce root topology and grants. The CLI guard does not turn the low-level execute API into a host delegation fence.

## Real native model-list verification

Artifacts:
`/Volumes/NOKIY-TB5/UTM/governance/tasks/nokiy-model-topology-20260927-v1/picker-canary-v1/`

- Candidate SHA256: `418f488be634b9a145d19450d493a08454066ea668ac46d35536d0760e4aa603`.
- `verification.json`: native catalog parser and public model/list passed. Two aliases each advertise luna/sol/astra, default sol; pagination completed.
- CLI: `codex-cli 0.158.0-alpha.2.1`.
- Native binary SHA256: `3e11ccc743e8198a5ef84fb57c89941d845b0ea0302485ed1fbac2f0821aca5a`.
- Live catalog, config, admission, caller entry and native binary were unchanged.
- Requested methods: initialize, initialized, model/list. No provider generation, visual desktop test or complete topology canary is claimed.

The legacy authority wrapper reported NATIVE_PAYLOAD_UNAVAILABLE. The current packaged CLI was subsequently found and verified at `/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex`; CLI absence is not the remaining blocker.

## Lifecycle validation blocker — retained, not waived

The broader `test_embedded_nokiy test_full_core` run executed 56 tests and failed with 20 failure assertions/subtests and one error, mostly `NOKIY_FULL_CORE_SUPERVISION_INVALID` with empty supervision output and exit 71. This automatically rolled back the then-current hardening transaction; its failure receipt is retained.

A bounded fixture reproduction then used the installed caller's Python 3.11.13 with both (1) the changed source and (2) the unchanged installed v33 package. Both failed identically before the synthetic engine started:

```text
sandbox-exec: sandbox_apply: Operation not permitted
```

The baseline reproduction establishes a restriction of this execution channel; changing to the installed interpreter did not remove it. No sandbox, signal fence, auth or approval was disabled. This does NOT prove runtime lifecycle success in another environment. Full-core lifecycle and real provider tests remain BLOCKED, not passed or skipped-as-passed.

Evidence: DevSpace task `nokiy-model-topology-20260927-supervisor-repro-v6`, inspection SHA256 `f393fdc3a202938d02adb02f30c17cf2946b4485741fc11f6c1de85f6c0b4f6f`.
Failed transaction: `nokiy-model-topology-20260927-review-hardening-v4`, receipt SHA256 `d1fc935b479d168f2736e45b844cdfd9d4bc10bd399cc390c19ae06ef82bae5b`.

Reapplying source-only integrity hardening uses focused tests, explicitly retains this full-core BLOCKED status, and does not permit live activation.

## Remaining implementation gates

1. **Native turn projection:** decode the UI family BEFORE native effective effort and multi-agent policy. Preserve local alias for UI/history while loading actual root metadata, model instructions, context capacity and tools. A switchboard-only rewrite is too late.
2. **Plain Nokiy root adapter:** the existing caller currently expects a parent-authored bounded action/context. Add a genuine native-host path without hidden LLM commander, guessed scopes or a plain Sol alias pretending to be Nokiy.
3. **Direct delegation adapter:** intercept native commander delegation and invoke Nokiy directly, without an intermediate native subagent. Keep lifecycle, parallel completion, follow-up, cancellation and original-ID recovery parent-owned. Do not fake interactive messaging unsupported by the one-shot caller.
4. **Host-enforced no recursion:** Max is NOT a recursion lock. Native source maps Ultra to proactive delegation but non-Ultra may still allow explicit delegation. Enforce restrictions at tool/backend execution; the manifest boolean records the requirement only.
5. **Install and live acceptance:** bind source and installed versions, update the installed skill's Sol-only contract with real capability verification, run six actual provider/tool combinations plus switch/resume/cancel canaries, and only then admit/publish the new picker through existing activation.

The native source checkout contains unrelated pending changes; none were overwritten or bundled into this candidate. No live caller wheel, native binary, lease, private conversation state or other worktree was altered. Do not install the candidate catalog alone: current model routing does not implement its aliases.
