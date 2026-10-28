# Invocation

## Bind current identities

The following identity discovery is for **manual CLI invocation**. For an exposed `functions.nokiy_execute` with verified native-host caller/runtime/native identity binding, use that binding and the tool's declared schema; do not add these filesystem reads or shell probes. Mandatory preparation, task scopes, source/authority revalidation and result/quality gates still apply. Missing or mismatched binding blocks dispatch.

Use the real target workspace, not UTM as a surrogate. Resolve `~/.local/bin/nokiy-embedded-run` at use time. Read `caller-binding.json` under `resolved_entry.parents[2]`. Its `runtime_image` supplies path and SHA-256; verify the actual image digest. Never synthesize an image or pin a release.

Use the existing native authority readback at `/Users/nokiy/Documents/unified_trading_model/scripts/codex_cli_safe_wrapper.sh --authority-json` when available, without changing the target workspace. Use `runtime.native_realpath` and `runtime.native_sha256` as the request's `codex` identity. Otherwise resolve the actual native Codex executable and hash it; never substitute NPM launcher metadata. Preserve HOME/CODEX_HOME and current CODEX_THREAD_ID.

For DCF-managed workspaces, require their `.venv/bin/python` and canonical `scripts/ops/dcf.py`. Query only the relevant target:

```sh
./.venv/bin/python scripts/ops/dcf.py query --capability surface-map --target <relevant-surface> --json
```

`surface-map --target` looks up an exact surface ID, never a file path. Select the exact returned ID and task-bounded scopes; no global catalog refresh. If the ID is not found, do not infer that there is no runtime or guess `research_execution_engine` / `tw_yolo_selection`. For an unregistered scratch subject, identify the actual declared operation boundary with the parent instead of inventing ownership or auto-selecting a research surface. For a project without DCF, omit `--surface-id`; no discovered surface or DCF generation is fabricated. A DCF marker in an ancestor prevents subdirectory downgrade. Resolve an existing absolute task artifact directory using the target project's storage policy; UTM uses its existing TB5 resolver.

When a surface ID is unknown but exact registered files are known, first check that the **current installed caller** implements `surface_targets` (not merely a skill document or another release). If supported, opt in with `"surface_targets": ["exact/relative/file.py"]` in the DCF action: 1..4 distinct normalized physical regular files, no globs, directories, symlinks or `.git`. Preparation uses one bounded query of current canonical surface-map and the project's canonical path matcher, then intersects candidates. Omit `--surface-id` only for a unique common match, or supply an exact ID and require it to match every path. It rejects absent, ambiguous, stale, partial and invalid evidence rather than guessing. The compact preparation resolution is diagnostic; it never expands `read_scopes`, `write_scopes`, operations or prompt context. This option is DCF-only; older callers require an exact ID established by other authorized evidence, not a silent fallback or a sourcegraph/code-to-authority refresh.

For a known exact symbol, the installed caller's `full_stack.py` supports `navigation_targets`. Put one to four distinct `symbol:module.path.name` values in the action, alongside a nonempty bounded `context_summary`. `prepare` calls DCF source-navigation against the current generation, requires one complete passing `path:line` for each symbol, adds only those locators to context, and then compiles J-Space from the separately declared scopes. Prefer this single preparation path; do not duplicate its DCF call in the parent. A locator never grants a file read or write, so declare the intended scopes independently.

Caller V38+ automatically supplies a verified excerpt when `include_source_excerpt` is omitted and the request has one exact Python `navigation_targets` symbol, one matching file in `read_scopes`, `authority_effect: "none"`, explicit `require_tool_call: false`, and only read capability (optionally `source_read` commands). It includes the definition and referenced same-file top-level bindings within a combined 120-line / 12,288-byte budget. This is syntactic support, not proof of complete semantic dependencies; existing read tools remain available. Source SHA and J-Space are rechecked before execution. Oversized excerpts retain the ordinary read path; stale or denied evidence still fails. Set `include_source_excerpt: false` to disable, or `true` for legacy explicit body-only behavior. V40+ also auto-selects exact edits with `authority_effect: "workspace"`, `require_tool_call: true`, `source_read: true`, 1..4 Python locators, and an exact read and write grant for each selected file. Operations are read/modify with optional command. Definitions and same-file support are deduplicated into one 120-line / 12,288-byte source budget and 24,576-byte JSON envelope. Extra separately authorized reads may remain in scope. Complete preimages are rechecked before execution; successful edits produce fresh postimage hashes, while read-only drift still rejects. Omit `include_source_excerpt` for this route; explicit `true` remains read-only. Unsupported shapes or oversized definitions retain ordinary reads. A locator-only answer still stops at preparation.

If the resolved installed caller lacks `navigation_targets`, use the bounded manual source-navigation command before any whole-file read:

```sh
./.venv/bin/python scripts/ops/dcf.py query --capability source-navigation --target <exact-path-or-symbol> --depth 1 --json
```

The manual response may be a partial projection. Use it to select candidate paths, not as complete authority evidence; follow its generation/digest-bound continuation only when omitted paths matter. For one exact symbol with `freshness_status=current`, passing verdicts, `_projection.complete=true`, and exactly one resolved `path:line`, put only that locator in the action's `context_summary` as context-only data. Do not copy the raw navigation JSON into the prompt, or treat the locator as a read/write grant. Compile the actual J-Space action from the exact surface and scopes. Do not invoke this DCF command in a non-DCF workspace or replace it with a repository-wide scan.

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
  "timeout_seconds": null,
  "max_context_age_seconds": 900,
  "max_trajectory_bytes": null,
  "max_result_bytes": 8192,
  "allow_provider_network": true,
  "authority_effect": "none"
}
```

An explicit user request to execute through Nokiy authorizes its existing configured model connection, so this example sets provider networking true. Do not ask again solely for that connection. Without that authorization, do not execute; false is not an offline fallback. This never authorizes a different paid provider, credential copying, or wider tool effects. Use `workspace` authority only for authorized workspace effects. With verified caller V77+ and its matching native adapter, new full-core workers default to `timeout_seconds: null`: no fixed total execution deadline. Completion, a worker's bounded blocked handoff, or parent cancellation ends execution. Explicit finite 10..900-second budgets remain an opt-in compatibility contract; do not add one merely because a task is long. Legacy native-once execution still requires a finite timeout. Individual tool/network timeouts, source freshness, output limits, cancellation and owned-process cleanup remain in force; null grants no extra effect authority and never authorizes replay or model downgrade. Other budgets: context age 1..604800 seconds and result preview 256..8192 bytes.

For a matching caller with trajectory-spooling support, only v3 full-core requests (`execution_profile: "direct"` or `"balanced"`) accept `max_trajectory_bytes: null`: no total JSONL byte hard stop. `prepare` defaults a missing full-core trajectory budget to null; it never replaces an explicit integer. Explicit finite budgets retain the total-trajectory limit and must be 1024..67108864 bytes. Native-once and v1/v2 requests still require a finite trajectory budget. Verify installed caller/native intake support before using null; the caller source change alone does not update native-host intake or a deployed image.

`core.jsonl` retains task-local evidence on disk. Parsing and checksum/size verification are streamed; memory is bounded by one event and fixed-size evidence summaries, not total trajectory size. Supervisor transport has a separate fixed summary ceiling, independent of the optional trajectory budget. Large final assistant text stays complete in task-local `last-message.txt`, referenced by `result_artifact` with SHA-256 and byte count; `result_text` and supervisor output contain only the bounded UTF-8 head/tail preview and artifact reference. Missing, foreign, symlinked, checksum/size-mismatched or drifted artifacts fail closed. Existing command/file evidence completeness limits still apply and may block acceptance; null changes neither authority nor cleanup/source-freshness requirements.

With the coherent V116 runtime image, restored sessions project complete execution evidence from the existing canonical store, separately from retained model context. Typed execution-evidence references bind session identity and context/management/retention cursors; pages and the terminal cursor are checked without restoring old records to model prompts. Pages admit at most 128 records and 4 MiB; individual oversized records are rejected, not truncated. Missing prefixes, gaps, wrong identities, duplicates or drift remain blockers. Older images can still lose prior tool/usage events after context restoration; never reinterpret their retained tail as complete evidence or replay an uncertain request to recover it. This repair does not implement automatic tool/write-scope negotiation.

Omit context_capsule, jspace_contract, request_id, and request_sha256. Prepare defaults schema, current native_thread_id, persistence, model `gpt-6.1-sol`, reasoning_effort `max`, service_tier `default`, execution_profile `direct`. For operator-requested Nokiy Ultra (formerly Nokiy Direct), set the selected worker explicitly; Sol uses `model: "gpt-6.1-sol"` and `reasoning_effort: "max"`. Keep the compatible internal workflow ID `nokiy-direct`; the rename changes no permissions or native session identity. Honor other explicit overrides, including `balanced`, outside that mode.

Author action.json with mission:{mission_id,task_id,mode:"DELIVERY",objective,current_predicate}, context_summary, operations, read_scopes, write_scopes, target_paths, command_templates, forbidden_effects, and optional `navigation_targets` for exact DCF symbols. Use the parent's current mission/task identity. Require at least one operation; reads need scopes. Commands need exact argv templates except for the separately admitted `source_read: true` path below. Paths are workspace-relative. Leave unneeded write scopes empty. For mutations use the current canonical compiler typed-effect schema, never invented string prefixes.

For needed schema details consult `/Users/nokiy/Documents/Codex/2026-08-31/codex-collaboration-harness/docs/full-core-executor.md` and the installed `embedded_nokiy` request decoder.

On callers supporting `source_excerpt_decision: auto_outline`, DCF preparation also supplies ephemeral function/class navigation for read-only cross-file discovery without explicit symbol locators. This applies to 2..32 exact Python read scopes with `source_read: true`, read/command only, no writes or verifier/command templates, and `authority_effect: none`. The existing source-excerpt parser produces qualified names, complete ranges and file hashes, not source bodies, a second index or a dependency graph. Use those hashes and ranges for the first bounded implementation reads rather than rediscovering definitions. Canonical capsule binding and source freshness still apply. `include_source_excerpt: false` disables the optimization. Unsupported parsing, unavailable files or capacity overflow omit this optional outline and preserve ordinary preparation; the source/permission checks of subsequent reads remain unchanged. Single-file tasks and explicit symbol/excerpt paths retain their existing behavior.

Local mode requires all five mission fields and a bounded context summary containing the relevant project instructions. Explicit file scopes admit at most 32 exact existing regular files or supported exact `PLANNED-FILE` targets, not directories, glob patterns, `.git` paths or symlinks. Discovery-only tasks may omit exact files and use the separate `read_directories` grant described below. Read inputs must exist unless explicitly admitted as planned create targets; absent create targets are also bound against concurrent creation. With explicit `source_read: true` and read/command/create operations, local preparation adds an exact read scope for an absent canonical, non-hidden declared target with an exact write grant strictly beneath an admitted `read_directories` root, enabling same-worker create/readback. This only exactizes existing directory read authority before hashing, not general write-implies-read; ordinary missing read inputs still reject, and absence plus directory identity remain rechecked before execution. `operations` admits read/create/modify and optional command; deletion and deployment are not granted. The parent retains leases/CAS. Revalidation of source hashes does not replace writer ownership.

For known exact local-source ranges, prefer the existing optional `source_sections` preparation field over a worker round merely to fetch them: `[{"path":"src/main.rs","start_line":20,"end_line":45}]`. Verify installed support first; the language-neutral reader accepts UTF-8 text, while older callers accept Python only. Each path must already have an exact read grant. Preparation binds source and section hashes and includes only the requested text: 1..4 ranges, at most 120 lines and 6,144 bytes each, 10,000 bytes combined, within the existing context budget. These are partial pre-edit ranges, not complete definitions or dependency proof. Use verified unchanged supplied ranges directly; fetch missing support in one bounded independent-read batch. Never add unrelated preloads, skip required tests, or reuse preimages as post-edit readback. DCF workspaces retain their canonical navigation and Python AST excerpt path; this local option grants no DCF bypass.

For exact-file source inspection on a runtime whose current image and canonical compiler support `source_read`, prefer the opt-in tool for source files that may need several partial reads. Set `source_read: true`, include `read` and `command`, declare exact existing regular files in `read_scopes` (or exact local `PLANNED-FILE` targets as below), and leave `command_templates` empty when no other command is needed. The tool returns complete lines, `end_line`, and `next_line`; continue from that position rather than rereading the whole file. For DCF workspaces, the canonical DCF compiler must issue and digest-bind the opt-in; required-domain freshness and exact regular-file checks remain mandatory. Never edit a generated grant or substitute local mode. For a first exact-file read, `expected_sha256` is optional if no current exact-path digest is known; never borrow another file's SHA, and keep any explicit task-bound digest. Page the same file with its returned SHA as `expected_sha256` and `next_line` as `start_line`. After mutation use fresh scoped readback, not the old preimage SHA. Its per-call limits are 200 lines, 1 MiB per file, and the bounded response envelope. A path, SHA, or receipt error is not permission to silently switch tools or widen scopes. For example:

```json
{
  "operations": ["read", "command"],
  "read_scopes": ["src/core.py"],
  "write_scopes": [],
  "target_paths": [],
  "command_templates": [],
  "source_read": true
}
```

For an exact local `PLANNED-FILE` target with a known, canonical, non-hidden filename absent at preparation, explicitly grant the same path in `read_scopes`, `write_scopes` and `target_paths`, as below. No `read_directories` is needed: read is explicitly granted before execution, and write scope alone never grants read. Existing no-symlink, ancestor/path, absence, writer/ownership and leases/CAS requirements remain; missing or unrelated pre-existing sources are not automatically admitted. After the action's own successful creation, use the mutation receipt's postimage SHA as `expected_sha256` for same-action `source_read` of that path; do not start another worker merely for readback.

```json
{
  "operations": ["read", "command", "create"],
  "read_scopes": ["probe.txt"],
  "write_scopes": ["probe.txt"],
  "target_paths": ["probe.txt"],
  "command_templates": [],
  "source_read": true
}
```

When multiple exact reads are already known and independent, use one existing `command_run` with a `commands` array of bounded `source_read` entries. Preserve per-file grants, returned source hashes and pagination cursors; never speculate on dependent reads or dump entire files to fill a batch. No new tool or grant is needed. A few small files can also use an exact multi-file `cat` template, particularly on older images without `source_read`; select that route before preparation, not after a refusal. Each operand must have an exact `read` target with matching `argv_index`. `read_scopes` alone and an `apply_patch`-only route expose no reader. For example:

```json
{
  "operations": ["read", "command", "modify"],
  "read_scopes": ["src/core.py", "tests/test_core.py"],
  "write_scopes": ["src/core.py"],
  "target_paths": ["src/core.py"],
  "command_templates": [
    {"argv": ["cat", "src/core.py"], "effects": ["read"], "targets": [{"operation": "read", "path": "src/core.py", "argv_index": 1}]},
    {"argv": ["cat", "tests/test_core.py"], "effects": ["read"], "targets": [{"operation": "read", "path": "tests/test_core.py", "argv_index": 1}]}
  ]
}
```

This is the local-mode command shape only; DCF actions still use their own current surface and compiler. Local exact command templates remain `pwd` and `cat` on declared read files. For unknown filenames, use the separate opt-in directory capability below. Source modification uses existing scoped patch tools. Tests/builds are not reads.

### Inspect action (read-and-record chains)

With a verified V256+ caller (V255 accepts only the strict form), use one inspect action when the next step is three or more dependent read-only checks whose only writes are evidence files: git history, logs, localhost health, immutable SQLite reads, JSON receipts. Keep single reads on `source_read` or exact templates and every repository change on its existing scoped route. The action needs no DCF surface, even in a DCF-managed workspace:

```json
{
  "mission": {"mission_id": "<parent mission>", "task_id": "<bounded action id>", "mode": "DELIVERY", "objective": "<objective>", "current_predicate": "<first false predicate>"},
  "context_summary": "<bounded instructions and the checks to run>",
  "operations": ["read", "command"],
  "inspect": {},
  "forbidden_effects": ["deployment", "broker", "order"]
}
```

The worker may run any shell line inside an OS write fence that preparation binds into the authorization contract by digest. The workspace and everything outside the request artifact root and the system temporary directories are read-only, including `apply_patch`; the worker records evidence under the evidence root named in its context (`artifact_root/inspect/<action-digest>/evidence`). Reads, network and the credentials visible to the worker runtime are unchanged: the fence confines writes, not reads. Open SQLite with `file:<path>?mode=ro&immutable=1`; an OS write denial is the boundary, not a retry cue. A missing or mismatched fence fails closed before the model starts.

V256 preparation adapts fields that grant nothing instead of rejecting them, reporting each change in `input_adaptations` with `original_action_sha256`: empty grant fields are omitted, `source_read` and `include_source_excerpt` are subsumed by the shell, `operations` within read/command becomes both, and an explicit `surface_id` is dropped. Nonempty exact grants (`read_scopes`, `write_scopes`, `target_paths`, `command_templates`, navigation or surface targets, verifiers), write operations and a nonempty `inspect` object still fail preparation before any model starts.

With caller V257+, independent inspect actions can share one `run_batch`, directly or as compact members: the batch treats each inspect member as reading the workspace and writing only its own artifact root, so inspect members batch together but never beside a member that writes the workspace.

With caller V263+, inspect workers can run headless browser checks with playwright-cli: npm, the Playwright daemon, its socket directory and its outputs point at fence-writable places, so snapshots and screenshots land under the evidence root, and the fence also allows the Playwright browser cache. With caller V264+, a read-only command action (effects read only, no write scopes, targets or verifiers, a complete mission) whose commands the workspace cannot admit (local context admits only cat/pwd; DCF refuses unmodeled executables with `JSPACE_COMMAND_EFFECT_UNVERIFIED`) is prepared once more as the equivalent inspect action and reported as input adaptation `unadmitted_read_commands_to_inspect`. With caller V262+, ordinary `run_batch` members may use different workspaces when every artifact root lies outside all member workspaces.

### Citation-ready source access

When the verified runtime exposes the extended `source_read` schema, use DCF
locators first and request `line_numbers: true` for audit ranges. For an unknown
branch within an admitted file, use `{"path":"src/core.py","search_terms":["term1","term2"],"context_lines":2}`.
Search terms are literal OR matches, at most 16 terms and 2048 UTF-8 bytes total;
context is 0..5 lines. Search does not accept `end_line` or `line_numbers: false`.
Matching windows are merged and printed with their physical line numbers. No-match
is a successful empty result. Resume the same file with `start_line: next_line`
and its returned SHA as `expected_sha256`, only when further evidence is needed. Batch independent file
requests in the existing `command_run` rather than rereading file starts. Use the
printed line labels for citations, stripping labels from verbatim source quotes.
This adds no paths, shell grant, regex engine, index, or directory traversal.
When the installed tool advertises numbered ranges by default, an omitted
`line_numbers` returns physical line labels too; explicit `line_numbers: false`
retains raw range text. Older images default to raw text, so explicit `true`
remains the portable choice for citation-ready reads.

## In-loop public verification

On a verified image supporting `nokiy_focused_verifier_parent_v1`, local actions
may include `verifier_commands` to let the same worker test, patch admitted source,
and test again. This is the supported action input: `action.focused_verifiers` is
rejected immediately on both local and DCF preparation routes, even when empty or
supplied alongside `action.verifier_commands`. Do not translate that key; compiled
contract `focused_verifiers` metadata is separate and unchanged.
Each grant requires `argv`, `executable_sha256`,
`pinned_files` (path/SHA pairs), `timeout_seconds` (1..3600; size it to the suite, since a
verifier killed at its bound returns no summary), `scratch_root`, and
`network: false`. Bind a canonical native executable and immutable public-test
entry files; each pinned file must be in the existing read scope and exact argv.
Allocate an empty task-local scratch directory beneath the request's artifact
root, disjoint from the workspace, before preparation. Verifiers can read only
admitted sources plus necessary interpreter/OS files, and write only scratch.
No shell, inline interpreter code, arbitrary command, or network is granted.

An optional `python_import_roots` field admits 1..4 distinct absolute canonical
existing directories strictly beneath the workspace. No descendant component may
be hidden; missing/outside paths, workspace itself, symlink chains, noncanonical
spellings, duplicates, and colon-containing paths are rejected. Each root must
contain an existing admitted exact-file or directory read scope. This field is
only for a pinned CPython executable named `python` with an optional digits/dots
suffix, invoked without `-I` or `-E` (including clustered short flags).
The normal authorization digest binds the ordered roots, which are checked at
admission and revalidation. The parent requires an exact, presence-sensitive echo
from the verifier plan before spawn and sets `PYTHONPATH` only to these roots,
joined with `os.pathsep`; ambient Python environment settings are not inherited.
Omitting the field preserves old grants exactly: no roots or `PYTHONPATH` are
inferred. Roots add no Seatbelt read/write/network permissions; source and its
dependencies still need existing read grants, and writes remain scratch-only.

For verification, the model selects only `focused_verifier` with
`command_line: "{\"verifier_index\":0}"`. It cannot replace argv, workdir, timeout,
or scope. Verifier-only applies to the verifier's step group, not all commands in
a complete `command_run` response. Explicit task instructions that restrict the
whole `commands` array to verifiers take precedence over ordered batching guidance;
do not rewrite the task prompt or infer a new capability.

Only when `command` and `modify` are already granted, neither operation is denied,
and the patch targets are within existing exact write scopes, may fully-known,
granted final patches occupy earlier positive steps. When task instructions permit
such a mixed response, put the admitted `focused_verifier` in its own verifier-only
step group at a strictly later positive step in the same response. Same-step
dependent verification remains forbidden. Do not speculate on patches that still
depend on missing evidence. This ordering adds no operation or scope.

For supported sources, an admitted real successful verifier with proven cleanup
already makes optional SHA-bound `source_postimages` available to the worker.
Rust uses all-text-change deltas, not compilation or semantic proof. When feasible,
prefer a known legitimate admitted verifier in the worker over parent-only checks
to avoid redundant post-edit reads. Do not invent no-op verification, enlarge
scopes, or auto-complete semantic review; acceptance remains parent-owned.
Unsupported toolchains retain explicitly parent-owned checks.

Supply a new execution ID for an intended
rerun after a patch; an identical completed execution ID does not execute twice.
The existing full-core parent runs each test through a private per-run channel;
Router retains the single durable command receipt. Test failure is useful feedback,
not permission to modify pinned tests or expand scope. An uncertain result or
unproven process cleanup closes the channel and must be reconciled by the parent.
Older images fail preflight before a model call rather than silently using shell.
This local grant does not add verifier support to an unmodified DCF compiler.

## Directory discovery

For a bounded exploration task add `read_directories: ["src", "tests"]` to its action and grant `operations: ["read", "command"]`. Directories must already exist, be non-hidden canonical workspace-relative paths, and number at most eight. No whole-workspace `.`, symlinks, globs or parent traversal. DCF actions must also grant matching `read_scopes: ["src/**", "tests/**"]`. Local mode derives these recursive read scopes; it does not scan or copy the directory contents into the prompt. Exact reads/writes retain their existing content checks; discovery directory identity is checked before execution while unknown/new filenames remain discoverable.

Preparation binds the actual `rg` and `cat` executable paths and hashes in `read_commands`, covered by J-Space's authorization digest. The capsule includes their usage instructions. Models must use those pinned absolute executable paths, not shell aliases or PATH guesses. Roots, keywords and filenames are not precompiled into exact command strings. The provider prompt projects a bounded shell-quoted `bash` listing example and pinned `cat` argv prefix only for valid existing directory policy; append only a discovered path to the prefix. `source_read` still needs enablement and an exact file scope—recursive directory scopes do not qualify. No authority or pins change; prompts without valid directory routes remain unchanged.

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

`max_parallel_workers` in the SHA-bound batch plan lets the coordinator select
concurrency from 1 through the member count on the coordinator-concurrency caller.
With capacity-20 caller V244+, the total member count must fit the current configured
host capacity of 1 through 20, even when selecting only 1 or 2 concurrent workers.
Missing capacity settings default to 20; an existing configured value of 5 remains 5
until separately changed by the parent at coordinated deployment. Omitting parallelism
preserves all-member parallel execution. Omit this optional field for older callers;
do not assume that they understand it. Plan files are bounded to 32768 bytes, with
the historical 64-member format retained for recovery, not fresh admission above 20.
Reading original results never starts workers and does not reapply current capacity admission.

With caller V63+, large batch summaries may use
`nokiy_direct_batch_summary_v2`. Apply `member_defaults` only to omitted member
fields; merge its optional `usage.coverage` with each member's own usage numbers.
The terminal path is exactly `artifact_root/request_id/terminal.json`.
`batch.expand_summary` restores v1 metadata without inference or inspection.
Worker identities, models, usage numbers, evidence counts, cleanup and blockers
remain per member. Result text may still be marked truncated; recover the original
terminal when its full answer is required. A shared field never implies shared
permission or parent acceptance. Small v1 responses and raw results are unchanged.

Using the resolved caller:

```sh
nokiy-embedded-run prepare --request <draft.json> --action <action.json> --surface-id <verified-ID> --output-dir <new-absolute-preparation-directory>
nokiy-embedded-run run --request <new-absolute-preparation-directory>/request.json
```

For a locator-only question, a current complete single `navigation_locators` result from `prepare` is already the requested fact. If no file-content verification, tool effect or explicit model execution is required, report the locator with its DCF generation and `PREPARED_NOT_EXECUTED`; do not call `run` merely to have a model repeat `path:line`. This is a deterministic preparation result, not `RESULT_AVAILABLE`, a provider observation, or permission to access the located file. Any broader task still uses the prepared request and normal execution/acceptance path.

For a non-DCF project the same prepare command omits `--surface-id`; the returned `context_mode` is `local_workspace_jspace`, not `dcf_jspace_required`. Existing DCF failures do not fall back. Local mode rechecks source hashes, file modes, absent targets, workspace identity and age at each new execution boundary.

Always prepare new requests: legacy decoder omission may select native-once. Retain the prepared request's original ID. Wait on the same execution handle until terminal; inspect artifacts/tests before parent acceptance.

### Capability-gap diagnostic (caller-only)

Callers implementing this source feature append handoff guidance even when no read/verifier tool is presented. Prefer one LF-delimited `nokiy_capability_gap_v1` JSON fence for a blocked worker's handoff. The caller also recognizes the same complete object in one LF-delimited ordinary `json` fence, or as the whole raw JSON answer with surrounding JSON whitespace only. All three representations use the same schema/path/diff/binding validator and existing byte limits. Unrelated JSON is a no-op; duplicate or ambiguous handoffs are rejected, and prose promises, incomplete JSON or absent patches are never inferred or fabricated. Recognition remains a model-reported diagnostic, not a permission grant, execution proof or parent acceptance. Illustrative shape:

```nokiy_capability_gap_v1
{"schema_version":"nokiy_capability_gap_v1","missing_capabilities":[{"path":"src/dependency.py","operation":"read","tool":"source_read"}],"completed_work":["Reviewed the admitted definition; no edits or tests executed."],"remaining_work":["Read the exact dependency before proposing a patch."],"unified_diff":null}
```

Publish the complete valid gap fence, including a usable unified diff or precise missing-read evidence, as visible assistant text **before** any terminal `task_status` call with `status: "question"` or `status: "done"`. These statuses can end the run when a visible proposal is already present; never promise a patch below or in a later turn.

Only these five keys are accepted. Missing capabilities contain only `path`, `operation`, `tool`, with 1..8 distinct entries. Operations are `read`, `modify`, `create`, `delete`, `command`; tool names are exact identifiers, not commands/argv. Paths are exact workspace-relative strings (256 UTF-8 bytes maximum); only pathless command/tool gaps may use `null`. Completed work has 0..8 strings and remaining work 1..8, each nonempty and at most 512 UTF-8 bytes. JSON is bounded to 8192 UTF-8 bytes. `unified_diff` is null or a real plain unified diff (4096 UTF-8 bytes maximum, `--- a/path`, `+++ b/path`, counted `@@` hunks, no git metadata; `/dev/null` for create/delete). Every diff target must have the corresponding missing modify/create/delete capability. A mutation gap requires a real diff or a precise missing `read` entry in `missing_capabilities`. Include a diff only when admitted reads suffice; otherwise name that missing read evidence, not a placeholder or a claim that an absent patch/tests ran.

The offline inspection and CLI projection validate known keys/types, duplicate JSON/fences/capabilities, byte limits, diff structure and exact paths. They reject absolute/alias/wildcard/traversal/`.git` paths and existing symlink components against the original request's workspace. Absent exact targets remain proposals, not admission. A usable handoff requires the existing inspected request/thread binding; projection also checks the supplied terminal against that bound content. `capability_gap.status: MODEL_REPORTED` carries `handoff` and `binding`, including the original request/thread and terminal hashes. It always requires parent readmission, has no permission effect, and proves neither execution nor safe replay nor acceptance. It never changes durable terminal status or the first real blocker. Invalid/untrusted data yields `UNAVAILABLE`; diagnostics may be reduced or omitted to preserve the existing output budget. Summaries carry the same projection without a second inspection.

Truncated `result_text` is never decoded. For a full-core `RESULT_AVAILABLE` receipt, the existing trajectory scan decodes the complete final assistant message and retains only a bounded envelope candidate, absence, or typed parse-error code, replacing the outcome on every completed assistant message. An earlier message cannot supply the final handoff. The caller exposes a candidate only after the existing request/thread, trajectory/turn, exact preview/result-artifact identity and integrity checks pass, using the same schema/path/diff validator and unchanged 8192-byte gap, 4096-byte diff and 64-KiB projection ceilings. Verified absence is a no-op; malformed, duplicate, oversized or unbound data stays unavailable. No extra result-file read, trajectory pass, retained complete text or model execution is added. Legacy/preview-only receipts without that verified complete-message outcome still yield `CAPABILITY_GAP_RESULT_TRUNCATED` with the existing verified `result_artifact` reference when inspection can establish it. Recover the original ID, inspect effects/cleanup and reconcile uncertainty first. Then compare the exact requested capability with the original human authorization and fresh owner/CAS/lease. For a preparation omission within that authorization, the parent freshly prepares only unfinished bounded work without asking again or rerunning consumed effects. True prohibitions, new protected scope, owner conflicts or unresolved effects stop. This is parent re-admission, not a worker's denied-path bypass. Genuinely unsupported tools may run through parent native tools under the same original authorization, explicitly labeled native execution rather than Nokiy success. Parent review and verification still own acceptance; this source feature makes no deployment or runtime self-expansion claim.

### Native terminal-owned wait

Await repeated polls inside one `functions.exec` JavaScript call, keeping the original numeric session ID: `await tools.write_stdin({session_id: originalSessionId, chars: "", yield_time_ms: 300000, max_output_tokens: 2048})`. Use the longest permitted native per-call wait, not an invented infinite-wait option. Read the supported result fields `session_id`, `exit_code` and `output`: a returned `session_id` means still running; empty or unchanged output is not completion. Without a session handle, require an integer exit code for terminal status; otherwise report unknown and recover. Surface nonzero exits and tool errors, never swallow them.

Keep only a bounded output tail in memory; expose terminal status plus that tail, leaving full evidence in the original request artifacts. Start that cell with `// @exec: {"yield_time_ms": 3600000, "max_output_tokens": 32000}`. If `functions.exec` yields a cell ID, await that original cell via `functions.wait` only, with `yield_time_ms: 3600000`; do not start a second loop or another action. Both return as soon as the cell finishes or the user sends input, so shorter yields only add empty coordinator turns that re-read the whole thread; a smaller bound in the actual tool schema still controls.

Poll limits do not cap worker lifetime. Tool yields or new user input authorize neither replay, cancellation nor acceptance. Use native interruption/cancellation controls; reconcile unknowns via the original request ID or original batch plan + SHA. All existing identity, scoped-permission, cleanup, evidence, parent acceptance and task-closeout rules still apply.

Recover durable results without another provider send:

```sh
nokiy-embedded-run read-result --artifact-root <resolved-task-directory> --request-id <original-request-ID>
```

Missing terminal means uncertain execution: reconcile effects, never automatically resend or retry under a fresh ID.

For verified full-core assistant delivery, `result_inspection.parent_review_answer`
optionally carries the complete final `text`, its exact result `artifact`, and a
`terminal_content_sha256` binding. It reuses the existing checked trajectory
message, without another artifact read or model request. Prefer this text for
parent quality review; do not fetch the same answer again merely because the
durable `result_text` preview remains truncated. Parent acceptance is still
required. Failed, unbound, capability-gap and evidence-only deliveries do not
gain an answer body. The existing 32-KiB inspection and 64-KiB projection limits
remain unchanged: if the whole body cannot fit, it is omitted, not truncated,
and the original artifact reference remains available.

With verified caller V62+, avoid consuming unrelated evidence pages when the
exact required command identity is already known. Use the installed caller's
Python interpreter and existing inspector:

```sh
<resolved-caller-python> -B -m codex_collaboration_harness.result_inspection --artifact-root <original-root> --request-id <original-ID> --expected-thread-id <bound-thread-ID> --command-sha256 <exact-command-SHA256>
```

The selector is the SHA-256 of the exact UTF-8 command string recorded in
evidence, not a guessed shell rendering or the command's output hash. Supply at
most eight distinct selectors by repeating the option. Selection changes only
display: all indexed events, receipts, file-change and cleanup evidence are
still verified. An unselected failure still blocks; a missing requested command
is `REQUESTED_COMMAND_NOT_FOUND`, not success. Inspect `command_selection.matches`
to see repeated executions. If selected results need pagination, continue with
the same selectors and returned `next_offset`; never infer completeness from
one matching row. Do not call a model to select or hash a known command.

With verified caller V112+, automatic terminal output uses `pagination.stream: "priority"`:
proven successful source-read-only commands are counted, while verifier/effect/unknown/failure
evidence stays visible. Every indexed receipt is still checked. If this page has a
`next_offset`, use `--command-stream priority --offset <next_offset>` with the same
root, request and thread. Explicit inspection without this option retains complete
chronological history and its original offsets. Never use a priority cursor as a history offset.

### Retained inspection summary handoff

With a caller implementing this API, a **new** serial CLI `run` or batch
`execute_one` publishes one create-only `artifact_root/request_id/inspection-summary.json`
from its already computed projection. The canonical JSON plus newline is at most
64 KiB; `terminal.json` is never modified. Full output and compact serial/batch
summaries carry `inspection_summary: {path, sha256, bytes}`. Expand a compact v2
batch with `batch.expand_summary` first to restore factored reference paths.
Publication does not inspect again, read trajectories or probe processes. Only
exact identical existing bytes are reused; interrupted/conflicting files are
never overwritten. Publication failure returns a null reference and a bounded
`inspection_summary_diagnostic`, preserving the original result, evidence and
blockers, not preexecution failure or permission to replay.

Consume the exact reference from that delivery, with the **independently retained**
prepared request/thread and original terminal identity, using the caller's Python:

```python
from codex_collaboration_harness.result_inspection import load_inspection_summary

summary = load_inspection_summary(delivered["inspection_summary"],
    expected_request_id=prepared_request_id,
    expected_thread_id=bound_thread_id,
    terminal_reference={"path": original_terminal_path, "sha256": original_terminal_sha256})
```

Batch consumption: keep `original_plan_bindings`, keyed by `(request_id, artifact_root)`,
independently from the SHA-bound original plan and prepared requests. Keep
`original_deliveries`' summary references (original path/SHA/bytes) and terminal
references from first delivery; never rebuild these bindings from loaded files:

```python
from codex_collaboration_harness import batch

members = batch.expand_summary(delivered_batch)["members"]  # Once, without inspection.
identities = [(m["request_id"], m["artifact_root"]) for m in members]
if len(identities) != len(original_plan_bindings) or set(identities) != set(original_plan_bindings):
    raise ValueError("Batch membership differs from the retained original plan")
retained = []
for member, identity in zip(members, identities):
    bound = original_plan_bindings[identity]
    original = original_deliveries[identity]
    if member.get("inspection_summary") != original["inspection_summary"]:
        raise ValueError("Summary reference differs from original delivery")
    snapshot = load_inspection_summary(original["inspection_summary"],
        expected_request_id=bound["request_id"],
        expected_thread_id=bound["native_thread_id"],
        terminal_reference=original["terminal_reference"])
    if snapshot.get("result_truncated") is not False or not isinstance(snapshot.get("result_text"), str):
        raise ValueError("Refuse missing or truncated answer acceptance")
    retained.append(snapshot)
consumed = [(s["result_text"], s.get("usage"), s.get("wall_time_seconds"))
            for s in retained]
```

Consume complete retained answers, not truncated batch previews. Load each snapshot
only once; do not re-inspect raw terminals or trajectories. Unknown usage/timing
stays unknown, and summing worker wall times is not batch latency or acceptance.

`terminal_reference` has exactly `path`/`sha256`; the summary reference has exactly
`path`/`sha256`/`bytes`. The loader reads only that bounded regular file, rejects
duplicate-key/invalid JSON, unsafe parent components/symlinks/traversal, wrong
request/thread/terminal identity, size/hash drift and nonfixed filenames. It does
not read `terminal.json`, trajectories, receipts or processes. A retained snapshot
is **historical evidence**, including historical cleanup observations, not fresh
source, ownership, permissions, live process clearance, acceptance or a lease.
The parent still supplies fresh task-specific verification and an explicit review
decision. Loading preserves partial/failed evidence; it cannot upgrade status or
make incomplete evidence acceptable to `parent_review_record`.

`read-result`, `read-batch`, existing-run serial recovery and result-only
`run-batch` remain read-only, do not require publication, and explicitly omit the
reference with `inspection_summary_omission: "READ_ONLY_RECOVERY"` for recovered
terminals. Do not rerun/replay to obtain a missing snapshot. Pure
`summarize_terminal` and `batch.summarize` only select/factor existing fields; they
never publish or load a snapshot. For separately authorized non-CLI delivery,
`publish_inspection_summary(projected, artifact_root, request_id,
expected_thread_id=bound_thread_id)` returns the reference or raises on failure;
`retain_inspection_summary` wraps it into a projection with a reference/diagnostic.

### Compact parent review declaration

Use `result_inspection.parent_review_record` on the **already retained** projected
terminal or `nokiy_terminal_summary_v1`; do not inspect again to publish a receipt.
Supply the original terminal's path/SHA reference, the prepared request ID and
bound thread explicitly, `decision="accepted"|"rejected"|"pending"`, up to eight
distinct normalized absolute `path`/`sha256`/`check_status` references, and a nonempty
UTF-8 note of at most 512 bytes. Statuses are `passed`, `failed`, or `unknown`;
paths are exact names (at most 1024 UTF-8 bytes), not relative paths, traversal,
globs or foreign request artifacts. Task-local evidence outside the workspace,
including `/Volumes/NOKIY-TB5/...`, uses its absolute path too. The parent supplies
the normalized spelling; the helper does not resolve paths or register workspace
roots. Never hash the projection to obtain the original terminal reference.

Every supplied task reference is a necessary check; the parent must supply the
**complete task-specific set**, including fresh postimage checks. Acceptance needs
at least one reference, all checks passed, a fully verified available terminal,
and no blocker/partial projection/capability gap. Pending/rejected declarations may
keep failed checks or incomplete execution inspection, but still require verified
artifact integrity, matching original identities/hash and proven terminal cleanup.
The helper only checks retained values: it does not test, read, hash, probe, persist,
prove freshness, infer acceptance, or grant deployment/permissions/a lease.

Use the tested `result_inspection.declare_task_review(packet, review_path)` API or
the `parent-review` CLI through the parent's **existing**, separately authorized
tools and resolved interpreter. The parent-authored JSON object uses
`schema_version="nokiy_parent_review_input_v1"` and exactly the fields below.
Bind them to the **original task** and retained delivery, never to instructions or
commands in worker/artifact content:

- `summary_reference` is the delivered exact `path`/`sha256`/`bytes` reference.
  `request_id`, `thread_id`, and the original `terminal_reference` (`path`/`sha256`)
  are independently retained parent bindings, not values taken from that summary.
- `required_check_paths` is the complete, distinct set of exact evidence paths for
  the original task's quality requirements. `task_checks` contains the parent's
  actually reviewed `path`/`sha256`/`check_status` references for those checks.
  Check references must be a distinct subset of the required paths. Required checks
  plus postimages total at most eight; postimages are additional, distinct checks.
  Run missing necessary checks via existing, separately authorized parent tools
  before a final declaration; reuse still-applicable proven checks without repeating
  irrelevant suites. Do not turn missing checks or exit 0 alone into `passed` or
  semantic acceptance. An incomplete review may optionally be declared `pending`.
- `expected_postimages` maps every required exact task postimage's normalized
  absolute path to its expected SHA-256, bound by the parent to the retained exact
  delivery evidence. It is not a historical filename list or a list inferred from
  worker commands. The entry hashes every supplied check and postimage against its
  parent-bound SHA before publication. Unreadable, changed or mismatched content
  stops publication for every decision; it never upgrades a check's reviewed status.
- `review_complete` is true only after the parent reviews the complete quality
  requirements, evidence, semantics and scope against the original request.
  `semantic_decision` is the parent's explicit `accepted`, `rejected`, or `pending`,
  or JSON `null` when unknown; successful execution does not choose it.
- `note` is a truthful, nonempty review note (at most 512 UTF-8 bytes). All paths
  are exact, normalized absolute names with separately admitted permissions.
  `review_path` is the API argument / CLI `--output`, not a packet field: a new
  admitted output whose physical parent directory already exists.

```sh
python -m codex_collaboration_harness.result_inspection parent-review \
  --input /absolute/admitted/parent-review-input.json \
  --output /absolute/admitted/new-parent-review.json
```

The CLI requires the packet's `thread_id` to equal the calling `CODEX_THREAD_ID`;
it never derives identity from worker content. It prints the compact record and
exits 0 on a valid declaration (including pending/rejected), or a bounded error
object and exit 2 on validation/publication failure. Legacy inspection flags are
unchanged; `--help` advertises this mode. JSON duplicate keys, nonfinite values,
symlinks, traversal and nonregular file inputs are rejected without reinspection.
The retained original terminal is checked only for regular-file kind, not reread;
its path/SHA identity remains bound through the verified summary.

Call once with resolved parent bindings. A complete actual review may directly
declare `accepted` or `rejected`; no pending-then-final ceremony is mandatory.
Unknown semantics stay pending; attempted acceptance with incomplete parent review
or missing required checks stays pending. Explicit pending/rejected decisions are
preserved. Failed/unknown necessary checks, incomplete execution inspection or a
blocker cannot support acceptance, and unproven cleanup prevents every declaration.
Creation is exclusive and occurs only after validation; a later declaration needs
another new admitted output, never an overwrite. This remains a **parent-owned
declaration**, not independent acceptance proof, deployment authority or a lease grant.

Reuse the proven retained inspection and cleanup; do not reinspect trajectories or
processes for this declaration. All original task-specific quality requirements
remain in force. This record is historical evidence, not fresh effect-time authority:
ownership, expected preimage/CAS under the existing lock, deployment authorization
and lease closeout still require their separate fresh parent admission and native
validation at effect time. Unit tests establish behavior, not actual round-trip or
elapsed-time savings.

## Authorized deployment

Use the existing caller's `deploy` lane for an already authorized installation/restart. This lane is deterministic: no model, DCF compilation, Rust worker or command generation. It is not a fallback for a rejected source request. First resolve the target's native deployment entrypoint and applicable authority rules. Preserve HOME/CODEX_HOME, current task ownership, and the parent task's effective execution policy. Nokiy does not add a Seatbelt or another permission layer around the approved commands. Do not edit live authority, forge a lease, or infer broker/order permission from deployment permission.

The parent must bind the actual operator authorization and current target-specific admission in `authorization_ref` (absolute canonical file path and SHA-256). This reference and the explicit CLI digest are integrity bindings, **not an authentication system or self-issued permission**. The target's existing preflight and installer must validate deployment authority, current ownership, expected preimage and CAS under their existing lock. Preflight alone cannot close a time-of-check/time-of-use race. If that deployment entrypoint does not exist, report the specific gap rather than inventing a pass-through authorization script.

Create a `nokiy_deployment_plan_v1` JSON object with these required fields and, for a leased delivery, the `task_closeout` field below:

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

### Single-call preparation and execution (capability-gated)

When the verified installed caller's `prepare --help` exposes `--run`, a fully
specified, already authorized bounded action can use `prepare --run` with the
same draft, action, output directory and optional topology arguments. Check this
capability once per immutable caller identity, not once per model round. Older
callers keep the existing prepare/run route; do not pass unsupported flags.
Do not combine stages when the task explicitly requires parent review between
preparation and execution. This option reuses the existing create-only prepare,
request/model binding, execute, cancellation, source checks and offline inspector;
it grants no authority and never creates another worker, daemon or model call.

`--summary` optionally compacts the returned terminal. With
`--require-verified-evidence`, exit 0 requires verified evidence; exit 3 means a
terminal is available but its evidence is incomplete. Exit 2 remains a blocked
or uncertain operation. None of these exits establishes semantic acceptance.
The same strict flag is available on `run` and `read-result`; without it their
legacy exit behavior is unchanged. Never interpret exit 3 as permission to retry.

Combined results carry `prepared_request` (the original request path and byte
SHA). If execution consumed the request and an exception interrupts delivery,
`EXECUTION_UNCERTAIN` carries the original request ID/artifact root and reports
mutation count as unknown, not zero. Recover with that original `read-result`;
never rerun `prepare --run`, invent a new request ID, or replay an unobserved
effect. A failed preparation remains create-only and must not be reused.

Batch preflight overlaps independent, provider-free validation under the existing
parallel limit. Every member must still pass, in deterministic member order,
before any execution; original scope-conflict, capacity, cancellation and
create-only reentry checks remain. This is not a cross-request authority cache.
The summary byte budget and truncation order remain byte-equivalent while JSON
size is updated incrementally instead of repeatedly encoding all worker text.

These optimizations apply to local execution/orchestration. Do not reduce the
Web parent's context, reasoning, explicit model selection or acceptance duties.

### Parent-only caller release preparation

Use `scripts/prepare_nokiy_caller_release.py`, not per-task Python delivery wrappers.
It parameterizes the SHA-bound legacy helper's existing `release.py`, `deploy.py`
and CAS installer; it is not a worker tool, install command or authority issuer.
The parent creates one regular canonical task-local `TASK.json` with schema
`nokiy_caller_release_task_v1` and supplies its **byte** SHA-256 explicitly:

```sh
<parent-python> -B <repo>/scripts/prepare_nokiy_caller_release.py --manifest <task-root>/TASK.json --manifest-sha256 <exact-byte-sha256> --phase prepare
```

The strict manifest binds `native_thread_id`, `repo`, `artifact_root` (the manifest's
parent), `environment` (`HOME`, `CODEX_HOME`, null only when unset), `action_id` and
`expires_at`; `installed` binds canonical `release`, symlink-aware `entry` identity,
the three release `metadata` file hashes and `package_sha256`; `candidate` binds
the exact sibling `release`, `version`, `build_id`. `modules` are normalized
package-relative `.py`/`.json` paths; `tests` are `test_*` module names. `postimages`
binds exactly those source/test files plus `pyproject.toml`, `skills/nokiy/SKILL.md`
and this reference. `adapter`, `templates` (`legacy`, `release`, `deploy`, `installer`),
`interpreters` (`controller`, `source`, `candidate`), `config` (or null if absent)
and task-local `authorization_ref` are explicit path/SHA-256 identities; interpreter
symlinks also bind `realpath`, retaining the invocation path. `runtime_dependencies`
has `files` and `trees` identity lists, both of which may be empty; supplied entries
retain exact dependency SHA-256 validation.
Tree hashes use the adapter's `tree_sha256`: canonical JSON of all relative regular
file paths to byte SHA-256, excluding nonexecuted `__pycache__`; symlinks reject.
The adapter validates exact declared inputs and automatically recorded bindings,
not a hermetic attestation of the entire host OS or standard library. Host stdlib
remains the existing trusted platform, without a new mandatory global scan.
No manifest field admits shell commands, permission/model changes or lease clearance.

`prepare` runs source/candidate validation, stages once and emits a single existing
deployment plan for parent review; it never applies it. Plan argv calls this same
script with the exact manifest/digest and phase, and pins all templates. Staged/live
checks reuse passed candidate tests only with identical exact declared inputs and
automatically recorded bindings (complete package/source/test hashes, interpreter
executable/realpath, `pyvenv.cfg` when present, runtime-image artifacts, recipes and
effective environment). For caller-only staged/live checks, a valid change solely
to the environment fingerprint runs the already-admitted tests afresh and records
non-reused evidence; it never discards the mismatch or rewrites candidate evidence.
Other binding drift, failed fresh validation or partial preparation preserves
evidence and rejects; there is no resume, retry or deployment-phase replay.
Use `--help` for CLI options. Approve/run/read the plan through the existing commands
above; native approval, installer lock/CAS and parent acceptance/closeout still apply.

### Required closeout for leased delivery

Do not stop a leased task at worker completion or deployment verification. The
parent first accepts the actual source/test evidence and intended committed
delivery. It then binds `task_closeout` into the same approved deployment plan.
This stage runs automatically after successful verification, without a model call:

```json
{
  "task_id": "<exact parent-owned task id>",
  "active_lease": {"path": "<absolute active lease>", "sha256": "<fresh lease SHA>"},
  "completion_path": "<absolute project completion receipt>",
  "command": {
    "argv": ["<project python>", "-B", "<existing finish script>", "--task-id", "<same task>", "--finish-state", "committed", "--task-commit", "<accepted full commit SHA>", "--json"],
    "files": ["<same pinned executable/script identity objects as deployment commands>"],
    "timeout_seconds": 120
  },
  "expected_receipt": {
    "task_id": "<same task>", "thread_id": "<current native parent>",
    "finish_state": "committed",
    "commit_attribution": {"task_commit_ids": ["<accepted full commit SHA>"]}
  }
}
```

Use real identity objects for `files`; the placeholder string is not executable.
Bind the existing finish command and its policy dependencies, never a script that
fabricates completion or removes the lease. For UTM use its canonical
`scripts/ops/finish_codex_task.py` and project interpreter; repeat `--task-commit`
for multiple exact accepted commits. Keep `workspace` at the governed repository.
The stage checks the exact lease SHA and owner again before execution, requires
the actual completion receipt to have `ok:true` and all expected fields, and
requires the active lease to be absent. Governed symlink parents are pinned by
realpath/device/inode; leaf symlinks are rejected. No baseline rewrite, automatic
commit, forced unlock, or fallback to a different finish state occurs.

For a genuinely external-only deployment lease with no in-lease Git changes,
set `task_closeout.mode` to `external_deployment`. The native project finish
command must support this case without inventing commits or rewriting its
baseline. Bind `expected_receipt` to `finish_state: committed`, equal full-SHA
`base_commit` and `head_commit`, `committed_paths: []`, and
`commit_attribution: {}`. Do not pass `--task-commit`. The pinned lease must be
valid, owned by the same task/thread, and admit only explicit absolute write
scopes outside its canonical repository root, never an ancestor of that root.
This mode still runs only after the same plan's successful apply and verify,
then requires a matching successful native receipt and no active lease or
task-owned dirty paths. It grants no deployment authority. Missing mode or
`mode: source` preserves the original nonempty source-commit requirement.

`CLOSEOUT_BLOCKED` is incomplete delivery even when `deployment_verified:true`.
Preserve its receipt and first blocker; never repeat deployment merely to retry
closeout. `read-deployment-result` returns historical evidence without side effects,
not a new claim about current ownership. Source-only tasks without a deployment
plan must still call the existing project finish in the same parent turn and
verify receipt plus active-lease absence before reporting completion.

### Standalone parent closeout

With verified caller support for `check-closeout`, `closeout`, and
`read-closeout-result`, source-only closeout does not require a deployment or a
worker. Use schema `nokiy_task_closeout_plan_v1` with exactly `schema_version`,
`action_id`, `native_thread_id`, `workspace`, `artifact_root`, `expires_at`,
`authorization_ref`, and `task_closeout`. Reuse the identity objects and source
closeout spec above, including the actual project finish script and dependencies.
Do not add fake deployment stages, source permissions, or DCF surface IDs.
External-deployment mode retains the existing verified deployment lane.

The approved canonical JSON digest binds the plan; it is not an authority grant.
The current native thread must actually own the lease, parent acceptance and
effect authorization must already exist, and paused tasks remain paused.
Missing owner evidence is a blocker, never a reason to substitute the caller ID.

Native adapters advertising `parent_closeout: direct_v1` expose
`operation: check_closeout` or `closeout` with only `plan_path` and `plan_sha256`.
The CLI equivalents take `--plan` and `--approved-plan-sha256`. Check performs no
finish; execute freshly checks identities and runs the admitted command once.
Successful closeout requires matching receipt and active-lease absence, not
deployment or business acceptance. An uncertain attempt must not be replayed.

Recover original results using `operation: read_closeout` with `artifact_root`
and `action_id`, or `read-closeout-result --artifact-root ... --action-id ...`.
This reads historical evidence without a worker, renewed execution permission,
or effects. Expired execution authorization must not block evidence recovery.
