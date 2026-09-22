# Nokiy Kernel Skill Retirement

The `$tura-kernel` Skill and its implicit Native-versus-graph selection path are
retired. The measured Skill profile did not reproduce the external
runtime's model-loop, context, or provider gains, because it delegated those
surfaces back to Native Codex.

`nokiy-taskpacket` remains a stateless capsule inspection and Native task prompt
renderer. It does not install or invoke a Skill. New dispatch prompts contain
`execution_surface=native_codex_no_tura_skill` and never contain
`$tura-kernel` or a Skill contract digest.

The candidate `nokiy-graph-tool` is available only as an
explicit command-graph diagnostic/executor. It is not a full model runtime
and is not selected implicitly. Historical Skill bytes remain content-addressed
in prior immutable release wheels and are not an operational surface.
