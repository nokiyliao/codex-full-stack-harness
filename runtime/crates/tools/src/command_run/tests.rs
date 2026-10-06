use super::handler_parse::{
    command_values, parse_arguments_value, parse_command_item, string_field, u64_field,
};
use super::{
    normalize_command_steps, normalize_json_or_cli_command_arguments,
    normalize_shell_command_arguments, parse_args,
};
use serde_json::Value;
use serde_json::json;
use std::collections::BTreeSet;
use std::time::{SystemTime, UNIX_EPOCH};

#[tokio::test]
async fn focused_verifier_never_falls_back_to_shell_without_router_admission() {
    let root = tempfile::tempdir().unwrap();
    let marker = root.path().join("not-executed");
    let args = json!({"commands":[{"command_type":"focused_verifier",
        "command_line": format!("touch {}", marker.display())}]});
    let preflight = super::command_run_preflight_commands(&args).unwrap();
    assert_eq!(preflight[0].command, "focused_verifier");
    let result = super::execute_async_value_with_source_read_admission(
        args,
        root.path().to_path_buf(),
        None,
        None,
        false,
        crate::runtime::tool::CancellationToken::new(),
        None,
        None,
    )
    .await;
    assert_eq!(result["results"][0]["success"], false);
    assert!(!marker.exists());
}

#[test]
fn command_allowlist_matches_shell_aliases_by_canonical_identity() {
    let allowed = BTreeSet::from(["shell_command".to_string()]);

    assert!(super::command_allowed("zsh", Some(&allowed)));
    assert!(super::command_allowed("bash", Some(&allowed)));
    assert!(!super::command_allowed(
        "unregistered_command",
        Some(&allowed)
    ));
}

#[test]
fn parse_missing_steps_default_to_original_order_steps() {
    let args = parse_args(&json!({
        "commands": [
            { "command": "shell_command", "command_line": "pwd" },
            { "command": "shell_command", "command_line": "pwd" }
        ]
    }))
    .expect("parse args");

    assert_eq!(args.commands[0].effective_step(), 1);
    assert_eq!(args.commands[1].effective_step(), 2);
}

#[test]
fn normalize_preserves_duplicate_dependency_groups_and_extends_backwards_steps() {
    let mut args = parse_args(&json!({
        "commands": [
            { "command": "shell_command", "command_line": "echo a", "step": 1 },
            { "command": "shell_command", "command_line": "echo b", "step": 2 },
            { "command": "shell_command", "command_line": "echo c", "step": 2 },
            { "command": "shell_command", "command_line": "echo d", "step": 3 }
        ]
    }))
    .expect("parse args");

    normalize_command_steps(&mut args.commands);

    let steps = args
        .commands
        .iter()
        .map(|command| command.effective_step())
        .collect::<Vec<_>>();
    assert_eq!(steps, vec![1, 2, 2, 3]);
}

#[test]
fn normalize_scrambled_steps_never_move_backwards_or_merge_repaired_groups() {
    let mut args = parse_args(&json!({
        "commands": [
            { "command": "shell_command", "command_line": "echo three", "step": 3 },
            { "command": "shell_command", "command_line": "echo two", "step": 2 },
            { "command": "shell_command", "command_line": "echo four", "step": 4 },
            { "command": "shell_command", "command_line": "echo one", "step": 1 }
        ]
    }))
    .expect("parse args");

    normalize_command_steps(&mut args.commands);

    let steps = args
        .commands
        .iter()
        .map(|command| command.effective_step())
        .collect::<Vec<_>>();
    assert_eq!(steps, vec![3, 4, 5, 6]);
}

#[test]
fn parse_empty_command_run_is_error() {
    let error = parse_args(&json!({ "commands": [] })).expect_err("empty command run");

    assert_eq!(error, "command_run commands must not be empty");
}

#[test]
fn parse_task_status_compact_context_must_be_final_highest_step() {
    let error = parse_args(&json!({
        "commands": [
            {
                "step": 2,
                "command_type": "task_status",
                "command_line": "{\"compact_context\":\"summary\"}"
            },
            {
                "step": 3,
                "command_type": "shell_command",
                "command_line": "echo after"
            }
        ]
    }))
    .expect_err("task_status compact_context position");

    assert_eq!(
        error,
        "task_status compact_context must be the final command in the highest step of command_run"
    );
}

#[test]
fn parse_task_status_compact_context_from_inline_arguments_must_be_final() {
    let error = parse_args(&json!({
        "commands": [
            {
                "step": 1,
                "command_type": "task_status",
                "compact_context": "Inline handoff summary"
            },
            {
                "step": 2,
                "command_type": "shell_command",
                "command_line": "echo after-inline"
            }
        ]
    }))
    .expect_err("inline compact_context must obey final-position rules");

    assert_eq!(
        error,
        "task_status compact_context must be the final command in the highest step of command_run"
    );
}

#[test]
fn parse_empty_task_status_compact_context_does_not_force_checkpoint_rules() {
    let args = parse_args(&json!({
        "commands": [
            {
                "step": 1,
                "command_type": "task_status",
                "compact_context": "   "
            },
            {
                "step": 2,
                "command_type": "shell_command",
                "command_line": "echo after-empty"
            }
        ]
    }))
    .expect("blank compact_context is ignored for checkpoint positioning");

    assert_eq!(args.commands.len(), 2);
    assert_eq!(args.commands[0].command, "task_status");
    assert_eq!(args.commands[1].command, "shell_command");
}

#[test]
fn parse_command_only_shell_text_is_mapped_to_active_shell_command() {
    let args = parse_args(&json!({
        "commands": [
            { "command": "echo ok", "step": 1 }
        ]
    }))
    .expect("parse args");

    assert_eq!(
        args.commands[0].command,
        crate::commands::active_shell_command_name()
    );
    assert_eq!(args.commands[0].command_line, "echo ok");
}

#[test]
fn normalize_shell_commands_default_to_five_minute_timeout() {
    let args = parse_args(&json!({
        "commands": [
            {
                "command": "shell_command",
                "command_line": "echo timeout-default-ok",
                "step": 1
            }
        ]
    }))
    .expect("parse command_run args");

    let arguments =
        normalize_shell_command_arguments(&args.commands[0]).expect("normalize shell arguments");

    assert_eq!(arguments["timeout_ms"], json!(300_000));
}

#[test]
fn parse_long_command_keeps_wall_and_stall_budgets_separate_and_bounded() {
    let args = parse_args(&json!({
        "timeout_ms": 14_400_000,
        "stall_timeout_ms": 60_000,
        "commands": [{"command_type": "bash", "command_line": "builder"}]
    }))
    .expect("bounded long command");

    assert_eq!(args.commands[0].timeout_ms, Some(14_400_000));
    assert_eq!(args.commands[0].stall_timeout_ms, Some(60_000));
    let error = parse_args(&json!({
        "timeout_ms": 14_400_001,
        "commands": [{"command_type": "bash", "command_line": "builder"}]
    }))
    .expect_err("wall budget above four hours must fail closed");
    assert!(error.contains("exceeds bounded maximum"), "{error}");
}

#[test]
fn parse_rejects_stall_budget_that_cannot_fire_before_wall_timeout() {
    let error = parse_args(&json!({
        "timeout_ms": 10_000,
        "stall_timeout_ms": 10_000,
        "commands": [{"command_type": "bash", "command_line": "builder"}]
    }))
    .expect_err("stall watchdog must be lower than wall timeout");

    assert!(error.contains("stall_timeout_ms must be lower"), "{error}");
}

#[test]
fn normalize_external_commands_default_to_registry_timeout() {
    let args = parse_args(&json!({
        "commands": [
            {
                "command_type": "read_media",
                "path": "note.txt",
                "step": 1
            }
        ]
    }))
    .expect("parse command_run args");

    let arguments = normalize_json_or_cli_command_arguments(&args.commands[0], "read_media")
        .expect("normalize read_media arguments");

    assert_eq!(arguments["path"], json!("note.txt"));
    assert_eq!(arguments["timeout_ms"], json!(60_000));
}

#[test]
fn normalize_generate_media_defaults_to_100_second_timeout() {
    let args = parse_args(&json!({
        "commands": [
            {
                "command_type": "generate_media",
                "command_line": "--prompt logo",
                "step": 1
            }
        ]
    }))
    .expect("parse command_run args");

    let arguments = normalize_json_or_cli_command_arguments(&args.commands[0], "generate_media")
        .expect("normalize generate_media arguments");

    assert_eq!(arguments["cli"], json!("--prompt logo"));
    assert_eq!(arguments["timeout_ms"], json!(100_000));
}

#[test]
fn normalize_external_commands_keep_explicit_timeout_fields() {
    let args = parse_args(&json!({
        "commands": [
            {
                "command_type": "web_discover",
                "command_line": "{\"query\":\"docs\",\"timeout_secs\":2}",
                "timeout_ms": 5000,
                "step": 1
            }
        ]
    }))
    .expect("parse command_run args");

    let arguments = normalize_json_or_cli_command_arguments(&args.commands[0], "web_discover")
        .expect("normalize web_discover arguments");

    assert_eq!(arguments["query"], json!("docs"));
    assert_eq!(arguments["timeout_secs"], json!(2));
    assert!(arguments.get("timeout_ms").is_none());
}

#[test]
fn parse_task_status_compact_context_accepts_json_with_raw_newlines() {
    let args = parse_args(&json!({
        "commands": [
            {
                "command_type": "task_status",
                "command_line": "{\"compact_context\":\"Goal: keep going.\nNext: rerun focused tests.\"}",
                "step": 1
            }
        ]
    }))
    .expect("parse args");

    assert_eq!(args.commands[0].command, "task_status");
}

#[test]
fn task_status_compact_context_normalizes_json_with_raw_newlines() {
    let args = parse_args(&json!({
        "commands": [
            {
                "command_type": "task_status",
                "command_line": "{\"compact_context\":\"Goal: keep going.\nNext: rerun focused tests.\"}",
                "step": 1
            }
        ]
    }))
    .expect("parse args");

    let output = crate::commands::task_status::normalize_output(
        args.commands[0].inline_arguments.as_ref(),
        &args.commands[0].command_line,
    )
    .expect("task_status should tolerate raw newlines inside compact_context JSON");

    assert_eq!(
        output["task_status"]["compact_context"],
        json!("Goal: keep going.\nNext: rerun focused tests.")
    );
}

#[test]
fn parse_task_status_compact_context_detects_jsonish_unescaped_newline_before_later_command() {
    let error = parse_args(&json!({
        "commands": [
            {
                "command_type": "task_status",
                "command_line": "{\"compact_context\":\"Goal: keep going.\nNext: rerun tests.\"}",
                "step": 1
            },
            {
                "command_type": "shell_command",
                "command_line": "echo should-not-follow",
                "step": 2
            }
        ]
    }))
    .expect_err("jsonish compact_context with raw newline should still be detected");

    assert_eq!(
        error,
        "task_status compact_context must be the final command in the highest step of command_run"
    );
}

#[test]
fn parse_task_status_compact_context_rejects_multiple_checkpoints() {
    let error = parse_args(&json!({
        "commands": [
            {
                "command_type": "task_status",
                "command_line": "{\"compact_context\":\"first\"}",
                "step": 1
            },
            {
                "command_type": "task_status",
                "command_line": "{\"compact_context\":\"second\"}",
                "step": 2
            }
        ]
    }))
    .expect_err("only one compact_context checkpoint should be accepted");

    assert_eq!(
        error,
        "only one task_status compact_context command is allowed"
    );
}

#[test]
fn parse_standalone_compact_context_is_rejected() {
    let error = parse_args(&json!({
        "commands": [
            {
                "command_type": "compact_context",
                "command_line": "{\"summary\":\"legacy standalone command\"}",
                "step": 1
            }
        ]
    }))
    .expect_err("standalone compact_context should be removed");

    assert_eq!(
        error,
        "standalone compact_context command has been removed; use task_status compact_context"
    );
}

#[test]
fn parse_command_line_without_command_type_accepts_workdir_and_timeout() {
    let args = parse_args(&json!({
        "commands": [
            {
                "command_line": "pwd",
                "workdir": "subdir",
                "timeout_ms": 5000,
                "step": 1
            }
        ]
    }))
    .expect("parse args");

    assert_eq!(
        args.commands[0].command,
        crate::commands::active_shell_command_name()
    );
    assert_eq!(args.commands[0].command_line, "pwd");
    assert_eq!(args.commands[0].workdir.as_deref(), Some("subdir"));
    assert_eq!(args.commands[0].timeout_ms, Some(5000));
}

#[test]
fn normalize_command_value_for_execution_adds_actual_shell_command_type() {
    let normalized = super::normalize_command_value_for_execution(
        json!({
            "command_line": "Write-Output normalized-ok",
            "step": 3,
            "timeout_ms": 5000
        }),
        0,
    )
    .expect("normalize command value");

    assert_eq!(
        normalized["command_type"],
        crate::commands::active_shell_command_name()
    );
    assert_eq!(normalized["command_line"], "Write-Output normalized-ok");
    assert_eq!(normalized["step"], 3);
    assert_eq!(normalized["timeout_ms"], 5000);
}

#[test]
fn normalize_command_value_for_execution_does_not_type_plain_summary_text() {
    let normalized = super::normalize_command_value_for_execution(
        json!({
            "command": "large file scan",
            "step": 1
        }),
        0,
    )
    .expect("plain summary should still parse as a non-executable command record");

    assert!(normalized.get("command_type").is_none());
    assert_eq!(normalized["command"], "large file scan");
}

#[test]
fn parse_legacy_steps_shape_is_accepted() {
    let args = parse_args(&json!({
        "steps": [
            {
                "tool_name": "shell_command",
                "command_code": "echo legacy-steps-ok",
                "step": 1
            }
        ]
    }))
    .expect("parse args");

    assert_eq!(args.commands[0].command, "shell_command");
    assert_eq!(args.commands[0].command_line, "echo legacy-steps-ok");
}

#[test]
fn parse_command_run_arguments_accept_requests_wrapper_and_json_fence() {
    let args = parse_args(&Value::String(
            "```json\n{\"requests\":{\"commands\":[{\"command\":\"shell_command\",\"command_line\":\"echo fenced-ok\",\"step\":1}]}}\n```"
                .to_string(),
        ))
        .expect("parse args");

    assert_eq!(args.commands[0].command, "shell_command");
    assert_eq!(args.commands[0].command_line, "echo fenced-ok");
}

#[test]
fn parse_command_line_wrapped_apply_patch_routes_to_apply_patch() {
    let args = parse_args(&json!({
            "commands": [
                {
                    "command": "shell_command",
                    "command_line": "apply_patch <<'PATCH'\n*** Begin Patch\n*** Update File: app.txt\n@@\n-old\n+new\n*** End Patch\nPATCH",
                    "step": 1
                }
            ]
        }))
        .expect("parse args");

    assert_eq!(args.commands[0].command, "apply_patch");
    assert!(args.commands[0].command_line.starts_with("*** Begin Patch"));
}

#[test]
fn parse_apply_patch_missing_begin_marker_is_repaired() {
    let args = parse_args(&json!({
            "commands": [
                {
                    "command_type": "apply_patch",
                    "command_line": "apply_patch\n*** Update File: app.txt\n@@\n-old\n+new\n*** End Patch",
                    "step": 1
                }
            ]
        }))
        .expect("parse args");

    assert_eq!(args.commands[0].command, "apply_patch");
    assert_eq!(
        args.commands[0].command_line,
        "*** Begin Patch\n*** Update File: app.txt\n@@\n-old\n+new\n*** End Patch"
    );
}

#[test]
fn parse_aliases_cmd_and_command_line_are_accepted() {
    let args = parse_args(&json!({
        "commands": [
            { "cmd": "shell_command", "commandLine": "echo ok", "step": 1 }
        ]
    }))
    .expect("parse args");

    assert_eq!(args.commands[0].command, "shell_command");
    assert_eq!(args.commands[0].command_line, "echo ok");
}

#[test]
fn parse_single_shell_object_without_commands_is_wrapped() {
    let args = parse_args(&json!({
        "command": "echo ok",
        "timeoutMs": 120000
    }))
    .expect("parse args");

    assert_eq!(args.commands.len(), 1);
    assert_eq!(args.commands[0].command_line, "echo ok");
    assert_eq!(args.commands[0].timeout_ms, Some(120000));
}

#[test]
fn parse_single_stringified_shell_object_without_commands_is_wrapped() {
    let args = parse_args(&json!({
        "command": json!({ "command": "echo ok", "timeout_ms": 5000 }).to_string(),
        "timeoutMs": 120000
    }))
    .expect("parse args");

    assert_eq!(args.commands.len(), 1);
    assert_eq!(
        args.commands[0].command,
        crate::commands::active_shell_command_name()
    );
    assert_eq!(
        args.commands[0].command_line,
        json!({ "command": "echo ok", "timeout_ms": 5000 }).to_string()
    );
    assert_eq!(args.commands[0].timeout_ms, Some(120000));
}

#[test]
fn parse_command_only_here_string_patch_is_routed_to_apply_patch() {
    let args = parse_args(&json!({
            "commands": [
                {
                    "command": "@'\n*** Begin Patch\n*** Update File: app.txt\n@@\n-old\n+new\n*** End Patch\n'@",
                    "step": 1
                }
            ]
        }))
        .expect("parse args");

    assert_eq!(args.commands[0].command, "apply_patch");
    assert!(args.commands[0].command_line.starts_with("*** Begin Patch"));
}

#[test]
fn parse_arguments_value_accepts_requests_wrapper_and_plain_values() {
    let wrapped = parse_arguments_value(&json!({
        "requests": {
            "commands": [
                {"command_type": "shell_command", "command_line": "echo wrapped"}
            ]
        }
    }))
    .expect("requests wrapper");
    let fenced = parse_arguments_value(&Value::String(
        "```json\n{\"requests\":{\"commands\":[{\"command\":\"shell_command\"}]}}\n```".to_string(),
    ))
    .expect("fenced requests wrapper");
    let plain = parse_arguments_value(&json!({"commands": [{"command": "echo plain"}]}))
        .expect("plain arguments");

    assert_eq!(wrapped["commands"][0]["command_line"], "echo wrapped");
    assert_eq!(fenced["commands"][0]["command"], "shell_command");
    assert_eq!(plain["commands"][0]["command"], "echo plain");
}

#[test]
fn parse_arguments_value_reports_jsonish_errors_with_context() {
    let error = parse_arguments_value(&Value::String("```json\n{\"commands\":[}\n```".to_string()))
        .expect_err("invalid fenced json should fail");

    assert!(error.contains("failed to parse command_run arguments"));
}

#[test]
fn command_values_wraps_single_objects_and_strings_but_drops_scalars() {
    assert_eq!(command_values(&json!([{"command": "one"}])).len(), 1);
    assert_eq!(command_values(&json!({"command": "one"})).len(), 1);
    assert_eq!(command_values(&json!("echo one")), vec![json!("echo one")]);
    assert!(command_values(&json!(false)).is_empty());
    assert!(command_values(&json!(42)).is_empty());
}

#[test]
fn parse_command_item_recovers_inline_arguments_and_residual_fields() {
    let item = parse_command_item(&json!({
        "id": "status_update",
        "command_type": "task_status",
        "command": "{\"status\":\"done\"}",
        "parameters": {"status": "done"},
        "workdir": "workspace",
        "step": "3",
        "timeoutMs": "4000"
    }))
    .expect("parse command item");

    assert_eq!(item.command, "task_status");
    assert_eq!(item.command_line, "{\"status\":\"done\"}");
    assert_eq!(item.inline_arguments, Some(json!({"status": "done"})));
    assert_eq!(item.workdir.as_deref(), Some("workspace"));
    assert_eq!(item.step, Some(3));
    assert_eq!(item.timeout_ms, Some(4000));
    assert_eq!(item.binding_id.as_deref(), Some("status_update"));
}

#[test]
fn parse_command_item_uses_shell_when_only_payload_field_is_present() {
    let item = parse_command_item(&json!({
        "payload": "echo payload-only",
        "extra": "kept"
    }))
    .expect("parse payload-only item");

    assert_eq!(item.command, crate::commands::active_shell_command_name());
    assert_eq!(item.command_line, "echo payload-only");
    assert_eq!(item.inline_arguments, Some(json!({"extra": "kept"})));
}

#[test]
fn parse_command_item_rejects_non_object_non_string_and_missing_command() {
    assert!(
        parse_command_item(&json!(null))
            .expect_err("null command item")
            .contains("expected object")
    );
    assert!(
        parse_command_item(&json!({"step": 1}))
            .expect_err("missing command")
            .contains("missing field `command_type`")
    );
}

#[test]
fn field_helpers_trim_only_string_presence_and_parse_unsigned_numbers() {
    let object = json!({
        "blank": " ",
        "array": ["a", "b"],
        "object": {"k": "v"},
        "number": 42,
        "numberString": "43",
        "badNumber": "-1"
    });
    let object = object.as_object().expect("object");

    assert_eq!(string_field(object, &["blank"]), None);
    assert_eq!(
        string_field(object, &["array"]),
        Some("[\"a\",\"b\"]".to_string())
    );
    assert_eq!(
        string_field(object, &["object"]),
        Some("{\"k\":\"v\"}".to_string())
    );
    assert_eq!(u64_field(object, &["number"]), Some(42));
    assert_eq!(u64_field(object, &["numberString"]), Some(43));
    assert_eq!(u64_field(object, &["badNumber"]), None);
}

#[tokio::test]
async fn streaming_executor_returns_safe_shell_result_before_finish() {
    let workspace = temporary_workspace("streaming-safe-shell-before-finish");
    let mut executor = super::StreamingCommandRunExecutor::new(workspace.clone());

    let result = executor
        .push_command_value(json!({
            "command": "shell_command",
            "command_line": "echo streamed-safe-shell",
            "timeout_ms": 3000,
            "step": 1
        }))
        .await;

    assert!(
        !result.is_empty(),
        "streaming shell result should be available before finish()"
    );
    assert_eq!(
        result[0].get("success").and_then(Value::as_bool),
        Some(true)
    );

    let _ = std::fs::remove_dir_all(workspace);
}

#[cfg(unix)]
#[tokio::test]
async fn direct_command_run_keeps_receipts_in_original_directory_after_replacement() {
    let workspace = temporary_workspace("direct-receipt-directory-replacement");
    let output = super::execute_async_value(
        json!({
            "execution_id": "direct-receipt-directory-replacement",
            "commands": [
                {
                    "command_type": "shell_command",
                    "command_line": "mv .tura/run/command_receipts .tura/run/previous_command_receipts && mkdir .tura/run/command_receipts",
                    "workdir": workspace,
                    "timeout_ms": 3000,
                    "step": 1
                },
                {
                    "command_type": "shell_command",
                    "command_line": "printf 'second step\\n'",
                    "workdir": workspace,
                    "timeout_ms": 3000,
                    "step": 2
                }
            ]
        }),
        workspace.clone(),
    )
    .await;

    assert_eq!(output["results"][0]["success"], true, "{output}");
    assert_eq!(output["results"][1]["success"], true, "{output}");
    let receipt_root = workspace.join(".tura/run");
    assert!(
        std::fs::read_dir(receipt_root.join("previous_command_receipts"))
            .expect("original receipt directory")
            .count()
            > 0
    );
    assert_eq!(
        std::fs::read_dir(receipt_root.join("command_receipts"))
            .expect("replacement receipt directory")
            .count(),
        0,
        "no command_run operation may re-open the replacement directory"
    );

    let _ = std::fs::remove_dir_all(workspace);
}

#[cfg(unix)]
#[tokio::test]
async fn streaming_command_run_keeps_receipts_in_original_directory_after_replacement() {
    let workspace = temporary_workspace("streaming-receipt-directory-replacement");
    let mut executor = super::StreamingCommandRunExecutor::new(workspace.clone());
    let receipt_root = workspace.join(".tura/run");
    std::fs::rename(
        receipt_root.join("command_receipts"),
        receipt_root.join("previous_command_receipts"),
    )
    .expect("replace receipt directory after executor construction");
    std::fs::create_dir(receipt_root.join("command_receipts"))
        .expect("replacement receipt directory");

    let results = executor
        .push_command_value(json!({
            "command_type": "shell_command",
            "command_line": "printf 'bound receipt store\\n'",
            "workdir": workspace,
            "timeout_ms": 3000
        }))
        .await;
    assert_eq!(results[0]["success"], true, "{results:?}");
    assert!(
        std::fs::read_dir(receipt_root.join("previous_command_receipts"))
            .expect("original receipt directory")
            .count()
            > 0
    );
    assert_eq!(
        std::fs::read_dir(receipt_root.join("command_receipts"))
            .expect("replacement receipt directory")
            .count(),
        0
    );

    let _ = executor.finish().await;
    let _ = std::fs::remove_dir_all(workspace);
}

#[cfg(unix)]
#[tokio::test]
async fn command_run_entrypoints_fail_closed_when_receipt_store_cannot_open() {
    let workspace = temporary_workspace("receipt-store-open-failure");
    std::fs::create_dir(workspace.join("outside")).expect("outside directory");
    std::os::unix::fs::symlink("outside", workspace.join(".tura"))
        .expect("symlinked receipt ancestor");
    let marker = workspace.join("must-not-run");
    let command = json!({
        "command_type": "shell_command",
        "command_line": "touch must-not-run",
        "workdir": workspace,
        "timeout_ms": 3000
    });

    let direct =
        super::execute_async_value(json!({"commands": [command.clone()]}), workspace.clone()).await;
    assert_eq!(direct["results"][0]["success"], false, "{direct}");
    assert!(
        direct["results"][0]["error"]
            .as_str()
            .is_some_and(|error| error.contains("failed to bind command_run receipt store")),
        "{direct}"
    );

    let mut executor = super::StreamingCommandRunExecutor::new(workspace.clone());
    let streamed = executor.push_command_value(command).await;
    assert_eq!(streamed[0]["success"], false, "{streamed:?}");
    assert!(
        streamed[0]["error"]
            .as_str()
            .is_some_and(|error| error.contains("failed to bind command_run receipt store")),
        "{streamed:?}"
    );
    assert!(
        !marker.exists(),
        "failed receipt binding must precede execution"
    );

    let _ = executor.finish().await;
    let _ = std::fs::remove_dir_all(workspace);
}

#[tokio::test]
async fn parallel_read_only_shells_each_preserve_a_known_terminal_receipt() {
    let workspace = temporary_workspace("parallel-read-only-terminal-receipts");
    let active_shell = crate::commands::active_shell_command_name();
    let commands = (0..6)
        .map(|index| {
            json!({
                "id": format!("read-{index}"),
                "command_type": active_shell,
                "command_line": format!("printf 'read-{index}\\n'"),
                "timeout_ms": 3000,
                "step": 1
            })
        })
        .collect::<Vec<_>>();

    let output = super::execute_async_value(
        json!({"execution_id": "parallel-read-only", "commands": commands}),
        workspace.clone(),
    )
    .await;

    let results = output["results"].as_array().expect("command results");
    assert_eq!(results.len(), 6, "{output}");
    for result in results {
        assert_eq!(result["success"], true, "{result}");
        assert_eq!(
            result["output"]["terminal_receipt"]["terminal_state"], "completed",
            "{result}"
        );
        assert_eq!(
            result["output"]["terminal_receipt"]["failure_class"], "none",
            "{result}"
        );
    }

    let _ = std::fs::remove_dir_all(workspace);
}

#[tokio::test]
async fn batch_executor_passes_internal_json_projection_without_changing_output() {
    let workspace = temporary_workspace("batch-output-binding");
    let active_shell = crate::commands::active_shell_command_name();
    let output = super::execute_async_value(
        json!({
            "commands": [
                {
                    "id": "producer",
                    "command_type": active_shell,
                    "command_line": json_producer_command(),
                    "timeout_ms": 3000,
                    "step": 1
                },
                {
                    "command_type": active_shell,
                    "command_line": binding_consumer_command("#@#${producer.filename}#@#$"),
                    "timeout_ms": 3000,
                    "step": 2
                }
            ]
        }),
        workspace.clone(),
    )
    .await;

    assert_eq!(output["results"][0]["success"], true, "{output}");
    assert!(output["results"][0]["output"].get("filename").is_none());
    assert!(
        output["results"][0]["output"]
            .get("parsed_stdout")
            .is_none()
    );
    assert!(
        output["results"][0]["output"]["stdout"]
            .as_str()
            .is_some_and(|stdout| stdout.contains("\"filename\":\"created.txt\""))
    );
    assert_eq!(output["results"][1]["success"], true, "{output}");
    assert_eq!(
        output["results"][1]["output"]["stdout"]
            .as_str()
            .unwrap_or_default()
            .trim(),
        "created.txt"
    );

    let _ = std::fs::remove_dir_all(workspace);
}

#[tokio::test]
async fn streaming_executor_publishes_bindings_only_after_the_producer_step() {
    let workspace = temporary_workspace("streaming-output-binding");
    let active_shell = crate::commands::active_shell_command_name();
    let mut executor = super::StreamingCommandRunExecutor::new(workspace.clone());

    let producer = executor
        .push_command_value(json!({
            "id": "producer",
            "command_type": active_shell,
            "command_line": json_producer_command(),
            "timeout_ms": 3000,
            "step": 1
        }))
        .await;
    assert!(producer[0]["output"].get("filename").is_none());
    assert!(producer[0]["output"].get("parsed_stdout").is_none());

    let consumer = executor
        .push_command_value(json!({
            "command_type": active_shell,
            "command_line": binding_consumer_command("#@#${producer.filename}#@#$"),
            "timeout_ms": 3000,
            "step": 2
        }))
        .await;
    assert_eq!(consumer[0]["success"], true, "{consumer:?}");
    assert_eq!(
        consumer[0]["output"]["stdout"]
            .as_str()
            .unwrap_or_default()
            .trim(),
        "created.txt"
    );

    let _ = executor.finish().await;
    let _ = std::fs::remove_dir_all(workspace);
}

#[test]
fn terminal_status_guard_keeps_unknown_predecessor_after_successful_verification() {
    let mut guard = super::CommandRunTerminalStatusGuard::default();
    guard.observe_step(Some(1));
    guard.observe_result(None);
    guard.observe_step(Some(2));
    guard.observe_result(Some(true));
    assert!(guard.done_error(Some(3), true).is_some_and(|error|
        error.starts_with("TERMINAL_STATUS_PRIOR_RESULT")));
}

#[test]
fn terminal_status_guard_fails_closed_and_reuses_status_parsing() {
    for success in [None, Some(false), Some(true)] {
        let mut guard = super::CommandRunTerminalStatusGuard::default();
        guard.observe_step(Some(1));
        guard.observe_result(success);
        assert_eq!(guard.done_error(Some(2), true).is_none(), success == Some(true));
        assert!(guard.done_error(Some(1), true).is_some());
        assert!(guard.done_error(Some(2), false).is_some());
    }
    for command in [
        json!({"command_type": "task_status", "command_line": "status: DONE", "step": 2}),
        json!({"command": "task-status", "arguments": {"status": "done"}, "step": 2}),
        json!({"command_type": "task_status", "command": "{\"status\":\"done\"}", "step": 2}),
        json!({"command_type": "task_status", "status": "done", "step": 2}),
    ] {
        assert_eq!(
            super::CommandRunTerminalStatusGuard::command_metadata(&command),
            (Some(2), true),
            "{command}"
        );
    }
    assert_eq!(
        super::CommandRunTerminalStatusGuard::command_metadata(&json!({
            "command_type": "task_status", "arguments": {"status": "done"},
            "command_line": "doing", "step": 2
        })),
        (Some(2), false),
        "command_line must keep its existing precedence over inline arguments"
    );
}

#[tokio::test]
async fn batch_terminal_status_accepts_successful_work_at_a_later_final_step() {
    let workspace = tempfile::tempdir().unwrap();
    let output = super::execute_async_value(
        json!({"commands": [terminal_readback(1), terminal_readback(1), terminal_done(2)]}),
        workspace.path().to_path_buf(),
    )
    .await;
    let results = output["results"].as_array().expect("results");
    assert_eq!(results.len(), 3, "{output}");
    assert!(results.iter().all(|result| result["success"] == true), "{output}");
    assert_eq!(results[2]["output"]["task_status"]["status"], "done");
    assert_eq!(results[2]["step"], 2);
    assert_eq!(results[2]["id"], "terminal");
}

#[tokio::test]
async fn batch_terminal_status_fences_failures_without_halting_exploration_or_nonterminal_status() {
    let workspace = tempfile::tempdir().unwrap();
    for failure in terminal_prior_failures() {
        let output = super::execute_async_value(
            json!({"commands": [
                failure, terminal_readback(2),
                {"command_type": "task_status", "command_line": "doing", "step": 3},
                {"command_type": "task_status", "command_line": "question", "step": 3},
                {"command_type": "task_status", "task_group": "command batch execution", "step": 3},
                terminal_done(4)
            ]}),
            workspace.path().to_path_buf(),
        )
        .await;
        let results = output["results"].as_array().expect("results");
        assert_eq!(results.len(), 6, "{output}");
        assert_eq!(results[0]["success"], false, "{output}");
        assert!(results[1..5].iter().all(|result| result["success"] == true), "{output}");
        assert_terminal_status_blocked(&results[5], "TERMINAL_STATUS_PRIOR_RESULT");
        assert!(output.get("cancelled").is_none(), "ordinary errors must not halt: {output}");
    }
    let recovered = super::execute_async_value(
        json!({"commands": [terminal_readback(1), terminal_done(2)]}),
        workspace.path().to_path_buf(),
    )
    .await;
    assert_eq!(recovered["results"][1]["success"], true, "new batch: {recovered}");
}

#[tokio::test]
async fn batch_terminal_status_rejects_shared_nonfinal_and_repaired_terminal_steps() {
    let workspace = tempfile::tempdir().unwrap();
    for commands in [
        vec![terminal_readback(1), terminal_done(1)],
        vec![terminal_done(1), terminal_readback(1)],
        vec![terminal_done(1), terminal_readback(2)],
        vec![terminal_readback(3), terminal_done(2)],
        vec![terminal_readback(1), terminal_done(2), terminal_readback(3)],
    ] {
        let expected_count = commands.len();
        let output = super::execute_async_value(
            json!({"commands": commands}), workspace.path().to_path_buf(),
        ).await;
        let results = output["results"].as_array().expect("results");
        assert_eq!(results.len(), expected_count, "every command needs a result: {output}");
        assert_eq!(results.iter().filter(|result| result["id"] == "terminal").count(), 1);
        for result in results {
            if result["id"] == "terminal" {
                assert_terminal_status_blocked(result, "TERMINAL_STATUS_BATCH_ORDER");
            } else {
                assert_eq!(result["success"], true, "{output}");
            }
        }
        assert!(output.get("cancelled").is_none(), "{output}");
    }
}

#[tokio::test]
async fn streaming_terminal_status_waits_for_finish_after_successful_work() {
    let workspace = tempfile::tempdir().unwrap();
    let mut executor = super::StreamingCommandRunExecutor::new(workspace.path().to_path_buf());
    let earlier = executor.push_command_value(terminal_readback(1)).await;
    assert_eq!(earlier.len(), 1);
    assert_eq!(earlier[0]["success"], true, "{earlier:?}");
    assert!(executor.push_command_value(terminal_done(2)).await.is_empty());
    let final_results = executor.finish().await;
    assert_eq!(final_results.len(), 1);
    assert_eq!(final_results[0]["success"], true, "{final_results:?}");
    assert_eq!(final_results[0]["output"]["task_status"]["status"], "done");
    assert_eq!(final_results[0]["id"], "terminal");
}

#[tokio::test]
async fn streaming_terminal_status_remembers_drained_failures_and_allows_a_new_batch() {
    let workspace = tempfile::tempdir().unwrap();
    let mut failures = terminal_prior_failures();
    failures.push(json!({"step": 1})); // Streamed parse error, not a tool result.
    for failure in failures {
        let mut executor = super::StreamingCommandRunExecutor::new(workspace.path().to_path_buf());
        let failed = executor.push_command_value(failure).await;
        assert_eq!(failed.len(), 1, "{failed:?}");
        assert_eq!(failed[0]["success"], false, "{failed:?}");
        let recovery = executor.push_command_value(json!({
            "command_type": "task_status", "command_line": "doing", "step": 2
        })).await;
        assert_eq!(recovery[0]["success"], true, "{recovery:?}");
        let blocked = executor.push_command_value(terminal_done(3)).await;
        assert_eq!(blocked.len(), 1);
        assert_terminal_status_blocked(&blocked[0], "TERMINAL_STATUS_PRIOR_RESULT");
        assert!(!executor.is_halted());
        assert!(executor.finish().await.is_empty());
    }
    let mut fresh = super::StreamingCommandRunExecutor::new(workspace.path().to_path_buf());
    assert!(fresh.push_command_value(terminal_done(1)).await.is_empty());
    assert_eq!(fresh.finish().await[0]["success"], true);
}

#[tokio::test]
async fn streaming_terminal_status_rejects_following_work_and_ambiguous_steps() {
    let workspace = tempfile::tempdir().unwrap();
    for next_step in [1, 2] {
        let mut executor = super::StreamingCommandRunExecutor::new(workspace.path().to_path_buf());
        assert!(executor.push_command_value(terminal_done(1)).await.is_empty());
        let results = executor.push_command_value(json!({
            "command_type": "task_status", "command_line": "doing", "step": next_step
        })).await;
        assert_eq!(results.len(), 2);
        assert_terminal_status_blocked(&results[0], "TERMINAL_STATUS_BATCH_ORDER");
        assert_eq!(results[1]["success"], true, "{results:?}");
        assert!(!executor.is_halted());
        assert!(executor.finish().await.is_empty());
    }
    for steps in [vec![None], vec![Some(1)], vec![Some(3), Some(2)]] {
        let mut executor = super::StreamingCommandRunExecutor::new(workspace.path().to_path_buf());
        for step in &steps {
            let result = executor.push_command_value(json!({
                "command_type": "task_status", "command_line": "doing", "step": step
            })).await;
            assert_eq!(result[0]["success"], true, "{result:?}");
        }
        let done_step = if steps == vec![Some(1)] { 1 } else { 5 };
        let result = executor.push_command_value(terminal_done(done_step)).await;
        assert_eq!(result.len(), 1);
        assert_terminal_status_blocked(&result[0], "TERMINAL_STATUS_BATCH_ORDER");
        assert!(!executor.is_halted());
    }
}

fn terminal_readback(step: u64) -> Value {
    json!({"command_type": crate::commands::active_shell_command_name(),
        "command_line": binding_consumer_command("verified"), "timeout_ms": 3000, "step": step})
}

fn terminal_done(step: u64) -> Value {
    json!({"command_type": "task_status", "command_line": "{\"status\":\"done\"}",
        "id": "terminal", "step": step})
}

fn terminal_prior_failures() -> Vec<Value> {
    let no_match_search = if cfg!(windows) {
        "if (Select-String -Pattern absent -InputObject present -Quiet) { exit 0 } else { exit 1 }"
    } else {
        "printf 'present\\n' | grep absent"
    };
    vec![
        json!({"command_type": "source_read", "command_line":
            "{\"path\":\"missing.rs\",\"start_line\":1,\"end_line\":1}", "step": 1}),
        json!({"command_type": "focused_verifier", "command_line": "{\"verifier_index\":0}", "step": 1}),
        json!({"command_type": "task_status", "command_line": "{invalid json", "step": 1}),
        json!({"command_type": "task_status", "command_line":
            concat!("#@#", "${missing.status}", "#@#$"), "step": 1}),
        json!({"command_type": crate::commands::active_shell_command_name(),
            "command_line": "exit 1", "timeout_ms": 3000, "step": 1}),
        json!({"command_type": crate::commands::active_shell_command_name(),
            "command_line": no_match_search, "timeout_ms": 3000, "step": 1}),
    ]
}

fn assert_terminal_status_blocked(result: &Value, error_prefix: &str) {
    assert_eq!(result["command_type"], "task_status", "{result}");
    assert_eq!(result["success"], false, "{result}");
    assert!(result.get("output").is_none(), "blocked done must not carry a status output: {result}");
    assert!(result["error"].as_str().is_some_and(|error| error.starts_with(error_prefix)), "{result}");
}

fn json_producer_command() -> &'static str {
    if cfg!(windows) {
        "Write-Output '{\"filename\":\"created.txt\"}'"
    } else {
        "printf '%s\\n' '{\"filename\":\"created.txt\"}'"
    }
}

fn binding_consumer_command(placeholder: &str) -> String {
    if cfg!(windows) {
        format!("Write-Output '{placeholder}'")
    } else {
        format!("printf '%s\\n' '{placeholder}'")
    }
}

fn temporary_workspace(prefix: &str) -> std::path::PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock should be after UNIX_EPOCH")
        .as_nanos();
    let path = std::env::temp_dir()
        .canonicalize()
        .expect("canonical temp root")
        .join(format!("{prefix}-{}-{nanos}", std::process::id()));
    std::fs::create_dir_all(&path)
        .unwrap_or_else(|error| panic!("failed to create {}: {error}", path.display()));
    path
}
