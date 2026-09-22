# Invocation

## Bind current identities

Use the real target workspace, not UTM as a surrogate. Resolve `~/.local/bin/nokiy-embedded-run` at use time. Read `caller-binding.json` under `resolved_entry.parents[2]`. Its `runtime_image` supplies path and SHA-256; verify the actual image digest. Never synthesize an image or pin a release.

Use the existing native authority readback at `/Users/nokiy/Documents/unified_trading_model/scripts/codex_cli_safe_wrapper.sh --authority-json` when available, without changing the target workspace. Use `runtime.native_realpath` and `runtime.native_sha256` as the request's `codex` identity. Otherwise resolve the actual native Codex executable and hash it; never substitute NPM launcher metadata. Preserve HOME/CODEX_HOME and current CODEX_THREAD_ID.

For DCF-managed workspaces, require their `.venv/bin/python` and canonical `scripts/ops/dcf.py`. Query only the relevant target:

```sh
./.venv/bin/python scripts/ops/dcf.py query --capability surface-map --target <relevant-surface> --json
```

Select the exact returned surface ID and task-bounded scopes; no global catalog refresh. For a project without DCF, omit `--surface-id`; no discovered surface or DCF generation is fabricated. A DCF marker in an ancestor prevents subdirectory downgrade. Resolve an existing absolute task artifact directory using the target project's storage policy; UTM uses its existing TB5 resolver.

## New request

Every placeholder below must resolve to current verified identity or actual task data before use; these are not executable identities.

```json
{
  "runtime_image": {"path": "<bound-absolute-image-path>", "sha256": "<verified-image-sha256>"},
  "codex": {"path": "<runtime.native_realpath>", "sha256": "<runtime.native_sha256>"},
  "workspace": "<actual-absolute-target-workspace>",
  "artifact_root": "<resolved-existing-absolute-task-directory>",
  "prompt": "<bounded-task-and-required-evidence>",
  "require_tool_call": true,
  "timeout_seconds": 300,
  "max_context_age_seconds": 900,
  "max_trajectory_bytes": 2097152,
  "max_result_bytes": 8192,
  "allow_provider_network": true,
  "authority_effect": "none"
}
```

An explicit user request to execute through Nokiy authorizes its existing configured model connection, so this example sets provider networking true. Do not ask again solely for that connection. Without that authorization, do not execute; false is not an offline fallback. This never authorizes a different paid provider, credential copying, or wider tool effects. Use `workspace` authority only for authorized workspace effects. Allowed budgets: timeout 10..900 seconds, context age 1..604800 seconds, trajectory 1024..8388608 bytes, result 256..8192 bytes.

Omit context_capsule, jspace_contract, request_id, and request_sha256. Prepare defaults schema, current native_thread_id, persistence, model `gpt-6-astra`, reasoning_effort `high`, service_tier `default`, execution_profile `direct`. Honor explicit overrides, including `balanced`.

Author action.json with mission:{mission_id,task_id,mode:"DELIVERY",objective,current_predicate}, context_summary, operations, read_scopes, write_scopes, target_paths, command_templates, forbidden_effects. Use the parent's current mission/task identity. Require at least one operation; reads need scopes, commands need exact argv templates. Paths are workspace-relative. Leave unneeded write scopes empty. For mutations use the current canonical compiler typed-effect schema, never invented string prefixes.

For needed schema details consult `/Users/nokiy/Documents/Codex/2026-08-31/codex-collaboration-harness/docs/full-core-executor.md` and the installed `embedded_nokiy` request decoder.

Local mode requires all five mission fields and a bounded context summary containing the relevant project instructions. Explicit file scopes admit at most 32 exact regular files, not directories, glob patterns, `.git` paths or symlinks. Discovery-only tasks may omit exact files and use the separate `read_directories` grant described below. Read inputs must exist; absent create targets are also bound against concurrent creation. `operations` admits read/create/modify and optional command; deletion and deployment are not granted. The parent retains leases/CAS. Revalidation of source hashes does not replace writer ownership.

Local exact command templates remain `pwd` and `cat` on declared read files. For discovery, use the separate opt-in directory capability below rather than guessing filenames or granting an arbitrary command template. Source modification uses existing scoped patch tools. Tests/builds are not reads.

## Directory discovery

For a bounded exploration task add `read_directories: ["src", "tests"]` to its action and grant `operations: ["read", "command"]`. Directories must already exist, be non-hidden canonical workspace-relative paths, and number at most eight. No whole-workspace `.`, symlinks, globs or parent traversal. DCF actions must also grant matching `read_scopes: ["src/**", "tests/**"]`. Local mode derives these recursive read scopes; it does not scan or copy the directory contents into the prompt. Exact reads/writes retain their existing content checks; discovery directory identity is checked before execution while unknown/new filenames remain discoverable.

Preparation binds the actual `rg` and `cat` executable paths and hashes in `read_commands`, covered by J-Space's authorization digest. The capsule includes their usage instructions. Models must use those pinned absolute executable paths, not shell aliases or PATH guesses. Roots, keywords and filenames are not precompiled into exact command strings.

Supported forms:

```text
<pinned-rg> --no-config --max-filesize=1M --files -- src
<pinned-rg> --no-config --max-filesize=1M -n -- 'search expression' src
<pinned-rg> --no-config --max-filesize=1M -l -F -- 'another keyword' src
<pinned-cat> -- src/discovered-file.py
```

Additional rg flags: `-i`/`--ignore-case`, `-S`/`--smart-case`, `--json`, `--no-heading`, `--with-filename`, `--color=never`, and `-g`/`--glob` with a quoted bounded filter. Search patterns are limited to 2048 characters, operands to 16, and file reads to 1 MiB per file; existing runtime output/time limits still apply. `--pre`, `--follow`, compressed-file search, hidden files, external configuration, stdin, redirection, pipelines and command substitution are not admitted. A no-match rg exit code is evidence of an empty search, not permission to broaden roots or switch executors.

Finding a file grants only read access. Modify only separately declared exact write targets; otherwise return the discovered path/evidence to the parent for the next explicitly authorized action. Existing contracts without `read_commands` do not acquire any broader command permission. Use a newly prepared request with the currently installed image, not an already-submitted request or old image.

## Prepare, run, recover

Using the resolved caller:

```sh
nokiy-embedded-run prepare --request <draft.json> --action <action.json> --surface-id <verified-ID> --output-dir <new-absolute-preparation-directory>
nokiy-embedded-run run --request <new-absolute-preparation-directory>/request.json
```

For a non-DCF project the same prepare command omits `--surface-id`; the returned `context_mode` is `local_workspace_jspace`, not `dcf_jspace_required`. Existing DCF failures do not fall back. Local mode rechecks source hashes, file modes, absent targets, workspace identity and age at each new execution boundary.

Always prepare new requests: legacy decoder omission may select native-once. Retain the prepared request's original ID. Wait on the same execution handle until terminal; inspect artifacts/tests before parent acceptance.

Recover durable results without another provider send:

```sh
nokiy-embedded-run read-result --artifact-root <resolved-task-directory> --request-id <original-request-ID>
```

Missing terminal means uncertain execution: reconcile effects, never automatically resend or retry under a fresh ID.

## Authorized deployment

Use the existing caller's `deploy` lane for an already authorized installation/restart. This lane is deterministic: no model, DCF compilation, Rust worker or command generation. It is not a fallback for a rejected source request. First resolve the target's native deployment entrypoint and applicable authority rules. Preserve HOME/CODEX_HOME, current task ownership, and the parent task's effective execution policy. Nokiy does not add a Seatbelt or another permission layer around the approved commands. Do not edit live authority, forge a lease, or infer broker/order permission from deployment permission.

The parent must bind the actual operator authorization and current target-specific admission in `authorization_ref` (absolute canonical file path and SHA-256). This reference and the explicit CLI digest are integrity bindings, **not an authentication system or self-issued permission**. The target's existing preflight and installer must validate deployment authority, current ownership, expected preimage and CAS under their existing lock. Preflight alone cannot close a time-of-check/time-of-use race. If that deployment entrypoint does not exist, report the specific gap rather than inventing a pass-through authorization script.

Create a `nokiy_deployment_plan_v1` JSON object with exactly these fields:

- `action_id`: stable identity for this deployment effect, 1..96 ASCII letters/digits/`.`/`_`/`-`, starting with a letter or digit. Keep it and `artifact_root` across recovery; a new ID is not authorization to repeat effects.
- `native_thread_id`: actual current `CODEX_THREAD_ID`.
- `workspace`, `artifact_root`: existing canonical absolute directories, using the project's storage policy.
- `target`, `release`: exact intended service/component and release identity.
- `expires_at`: numeric Unix UTC deadline for new execution, not for reading an existing terminal.
- `authorization_ref`: `{ "path": "<canonical-file>", "sha256": "<digest>" }`.
- `commands`: exactly `preflight`, `apply`, `verify`. Each contains `argv` (literal array, absolute executable), `files` (path/SHA-256 identities for executable, script and relevant static dependencies), `timeout_seconds` (integer 1..3600). A symlink invocation such as a virtualenv Python must additionally bind its canonical `realpath`; keep the original argv path to preserve virtualenv behavior. No `sh -c`, interpreter inline code, module lookup, substitutions or model-generated modifications. Script operands must be pinned. The parent must bind transitive deployment inputs/manifests, not only the launcher. Scripts must not self-edit or remove these identities during the operation.
- `schema_version`: `nokiy_deployment_plan_v1`.

All commands execute directly in the native parent task environment. The deployment lane does not add a Seatbelt or another permission layer. The caller adds `NOKIY_DEPLOYMENT_PLAN_SHA256` and `NOKIY_DEPLOYMENT_ACTION_ID`, and uses an ordinary per-command process group for timeout and cancellation cleanup. It does not issue deployment authority: the immutable approved plan and the target installer's existing lease, CAS, admission and verification remain controlling. Use the target's existing service manager for persistent service lifecycle. Exact command arguments may appear in local artifacts, so pass credential references, never secret values.

`preflight` must emit a single JSON object with `admitted:true`, plus exact `action_id`, `target`, `release`, `plan_sha256`. `verify` must emit the same bindings plus `verified:true`, `healthy:true`, `observed_release` equal to the requested release. These must come from real admission and service/version/functional readback, not hard-coded success or just PID/exit status. Adapt an existing verifier's output only when its actual checks establish these claims. All three commands must exit zero and leave no owned descendants.

Compute the approved plan digest **after** parent review using canonical JSON (sorted keys, ASCII, compact separators, no NaN). Never recalculate a changed plan to bypass a digest mismatch. Invoke the resolved installed caller:

```sh
nokiy-embedded-run check-deployment --plan <plan.json> --approved-plan-sha256 <parent-approved-digest>
nokiy-embedded-run deploy --plan <plan.json> --approved-plan-sha256 <same-digest>
nokiy-embedded-run read-deployment-result --artifact-root <same-root> --action-id <original-action-id>
```

`check-deployment` validates bindings only; `READY` does not mean target admission has run. `VERIFIED` means the three commands and bound verifier completed; mission acceptance remains parent-owned. A rejected preflight returns `BLOCKED_BEFORE_APPLY`. Failed/unknown apply or verification returns `EFFECT_UNCERTAIN`, never automatic retry or rollback. An interrupted request without a terminal blocks reuse. Completed requests read their terminal without repeating commands, even after input expiry/removal. Durable result storage must remain intact throughout the recovery window; changing roots/IDs is not a retry mechanism. Concurrent different actions still rely on the target's existing installer lock/CAS, not a new Nokiy deployment controller.
