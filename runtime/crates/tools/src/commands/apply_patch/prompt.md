Use `apply_patch` for focused file edits. The `command_line` value is the raw patch body beginning with `*** Begin Patch`; do not wrap it in shell heredocs, `apply_patch` commands, or explanatory text.

Rules:
- Keep each `apply_patch` `command_line` focused on one file or one code block.
- Put focused patches in one `command_run` batch. Different files may share a `step`; patches to the same file must use later ordered steps or one combined patch.
- Use separate `apply_patch` commands for unrelated files even if the edits are small; batching keeps the turn efficient while keeping each patch easy to review and recover.
- Optional `source_delta` with `coverage: all_text_changes` supplies exact applied hunks at this change's `postimage_sha256`. Combine only with already-read source matching its `preimage_sha256`; review covered text without a redundant read solely to repeat these bytes. Missing, omitted, unseen, mismatched or later-changed context requires granted source reads. This is post-edit context, not verifier success, current-state proof after later edits, semantic acceptance or permission to skip explicit task requirements.

Example: update two files in one batch with multiple focused patches.

```json
{
  "commands": [
      {
        "command_type": "apply_patch",
        "step": 1,
        "command_line": "*** Begin Patch\n*** Update File: src/lib.rs\n@@\n-pub fn label() -> &'static str {\n-    \"old\"\n-}\n+pub fn label() -> &'static str {\n+    \"new\"\n+}\n*** End Patch\n"
      },
      {
        "command_type": "apply_patch",
        "step": 1,
        "command_line": "*** Begin Patch\n*** Update File: README.md\n@@\n-Old label\n+New label\n*** End Patch\n"
      }
  ]
}
```

Example: change two separate code blocks in the same file as two ordered `apply_patch` commands in one batch.

```json
{
  "commands": [
      {
        "command_type": "apply_patch",
        "step": 1,
        "command_line": "*** Begin Patch\n*** Update File: src/config.rs\n@@\n-pub const RETRIES: usize = 2;\n+pub const RETRIES: usize = 3;\n*** End Patch\n"
      },
      {
        "command_type": "apply_patch",
        "step": 2,
        "command_line": "*** Begin Patch\n*** Update File: src/config.rs\n@@\n-pub const TIMEOUT_MS: u64 = 1000;\n+pub const TIMEOUT_MS: u64 = 1500;\n*** End Patch\n"
      }
  ]
}
```
