use super::{
    access, comparable_path_string, execute, normalize_apply_patch_text, patch_text_from_payload,
};
use crate::runtime::tool::ToolPayload;
use serde_json::json;
use sha2::{Digest, Sha256};
use std::fs;

#[test]
fn mismatch_guidance_uses_context_without_granting_replay() {
    for partial in [false, true] {
        let guidance = super::apply_patch_failure_guidance("ContextMismatch", partial);
        assert!(guidance.contains("Use any attached preimage context"));
        assert!(guidance.contains("file may have changed"));
        assert!(guidance.contains("not a patch match or authority to replay"));
        assert_eq!(guidance.contains("partially changed tree"), partial);
    }
}

#[test]
fn failed_patch_context_requires_readback_and_never_applies() {
    let original = "unique_long_anchor_actual\n";
    let hunks = vec![vec![
        "-unique_long_anchor_expected".to_owned(),
        "+replacement".to_owned(),
    ]];
    let denied = super::apply_hunks_with_delta(original, &hunks, false).unwrap_err();
    assert!(!denied.contains("Read-only preimage context"));
    let allowed = super::apply_hunks_with_delta(original, &hunks, true).unwrap_err();
    let (_, evidence) = allowed.split_once("Read-only preimage context: ").unwrap();
    let evidence: serde_json::Value = serde_json::from_str(evidence).unwrap();
    assert_eq!(evidence["text"], original);
    assert_eq!(evidence["start_line"], 1);
    assert_eq!(evidence["preimage_sha256"], format!("{:x}", Sha256::digest(original.as_bytes())));
}

#[test]
#[ignore = "requires a hash-bound recorded fixture; run explicitly in offline acceptance"]
fn applied_source_delta_recorded_fixture_reconstructs_exact_postimage() {
    let fixture: serde_json::Value = serde_json::from_slice(&fs::read(
        std::env::var("NOKIY_PATCH_REPLAY_FIXTURE").expect("recorded fixture path")).unwrap()).unwrap();
    let root = temp_workspace("delta-recorded").canonicalize().unwrap();
    let name = fixture["path"].as_str().unwrap();
    let path = root.join(name);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    let before = fixture["before"].as_str().unwrap();
    let after = fixture["after"].as_str().unwrap();
    fs::write(&path, before).unwrap();
    let result = super::execute_with_readback(fixture["patch"].as_str().unwrap(), &root, std::slice::from_ref(&path));
    assert!(result.success, "{}", result.stderr);
    assert_eq!(fs::read_to_string(&path).unwrap(), after);
    assert_change_identity(&result.changes[0], Some(before.as_bytes()), Some(after.as_bytes()));
    let delta = &result.changes[0]["source_delta"];
    assert_eq!(delta["coverage"], "all_text_changes");
    let mut reconstructed: Vec<String> = before.split_inclusive('\n').map(str::to_owned).collect();
    for hunk in delta["hunks"].as_array().unwrap().iter().rev() {
        let start = hunk["before_start_line"].as_u64().unwrap() as usize - 1;
        let count = hunk["before_line_count"].as_u64().unwrap() as usize;
        assert_eq!(reconstructed[start..start+count].concat(), hunk["before_text"].as_str().unwrap());
        reconstructed.splice(start..start+count, hunk["after_text"].as_str().unwrap().split_inclusive('\n').map(str::to_owned));
    }
    assert_eq!(reconstructed.concat(), after);
    fs::write(std::env::var("NOKIY_PATCH_REPLAY_RESULT").unwrap(),
        serde_json::to_vec_pretty(&result.changes[0]).unwrap()).unwrap();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn applied_source_delta_uses_actual_whitespace_and_coordinates() {
    let root = temp_workspace("delta-actual").canonicalize().unwrap();
    let path = root.join("source.py");
    fs::write(&path, "    old\nkeep\n").unwrap();
    let patch = "*** Begin Patch\n*** Update File: source.py\n@@\n-old\n+new\n*** End Patch";
    let result = super::execute_with_readback(patch, &root, std::slice::from_ref(&path));
    assert!(result.success, "{}", result.stderr);
    let change = &result.changes[0];
    assert_eq!(change["source_delta"]["hunks"][0]["before_text"], "    old\n");
    assert_eq!(change["source_delta"]["hunks"][0]["after_text"], "new\n");
    assert_change_identity(change, Some(b"    old\nkeep\n"), Some(b"new\nkeep\n"));
    fs::write(&path, "later writer\n").unwrap();
    assert_eq!(change["source_delta"]["hunks"][0]["after_text"], "new\n");
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn applied_source_delta_requires_exact_native_read_admission() {
    let root = temp_workspace("delta-read-grant").canonicalize().unwrap();
    let path = root.join("source.py");
    let patch = "*** Begin Patch\n*** Update File: source.py\n@@\n-old\n+new\n*** End Patch";
    for grants in [vec![], vec![root.join("other.py")], vec![root.clone()]] {
        fs::write(&path, "old\n").unwrap();
        let result = super::execute_with_readback(patch, &root, &grants);
        assert!(result.success);
        assert!(result.changes[0].get("source_delta").is_none());
    }
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn applied_source_delta_failure_and_partial_failure_preserve_effects_without_projection() {
    let root = temp_workspace("delta-failure").canonicalize().unwrap();
    let path = root.join("source.py");
    fs::write(&path, "old\n").unwrap();
    let patch = "*** Begin Patch\n*** Update File: source.py\n@@\n-old\n+new\n*** Update File: missing.py\n@@\n-old\n+new\n*** End Patch";
    let result = super::execute_with_readback(patch, &root, std::slice::from_ref(&path));
    assert!(!result.success);
    assert_eq!(result.changes.len(), 1);
    assert!(result.changes[0].get("source_delta").is_none());
    assert_eq!(fs::read_to_string(path).unwrap(), "new\n");
    fs::remove_dir_all(root).unwrap();
}

fn temp_workspace(name: &str) -> std::path::PathBuf {
    let path = std::env::temp_dir().join(format!("tura-apply-patch-{name}-{}", std::process::id()));
    let _ = fs::remove_dir_all(&path);
    fs::create_dir_all(&path).expect("create temp workspace");
    path
}

fn assert_no_outer_message_or_guidance(output: &serde_json::Value) {
    assert!(output.get("message").is_none(), "{output}");
    assert!(output.get("guidance").is_none(), "{output}");
}

fn assert_change_identity(
    change: &serde_json::Value,
    preimage: Option<&[u8]>,
    postimage: Option<&[u8]>,
) {
    assert_eq!(
        change.get("preimage_sha256"),
        Some(&json!(preimage.map(|bytes| format!("{:x}", Sha256::digest(bytes))))),
        "{change}",
    );
    assert_eq!(
        change.get("postimage_sha256"),
        Some(&json!(postimage.map(|bytes| format!("{:x}", Sha256::digest(bytes))))),
        "{change}",
    );
}

fn assert_no_change_identity(change: &serde_json::Value) {
    assert!(change.get("preimage_sha256").is_none(), "{change}");
    assert!(change.get("postimage_sha256").is_none(), "{change}");
}

#[test]
fn add_file_accepts_relative_path_under_session_dir() {
    let root = temp_workspace("relative");
    let result = execute(
        "*** Begin Patch\n*** Add File: checked.txt\n+ok\n*** End Patch\n",
        &root,
    );

    assert!(result.success, "{}", result.stderr);
    assert_change_identity(&result.changes[0], None, Some(b"ok\n"));
    assert_eq!(
        fs::read_to_string(root.join("checked.txt")).expect("created file"),
        "ok\n"
    );
    let _ = fs::remove_dir_all(root);
}

#[test]
fn update_file_matches_lf_patch_against_crlf_file_and_preserves_crlf() {
    let root = temp_workspace("crlf");
    fs::write(root.join("app.txt"), "alpha\r\nold\r\nomega\r\n").expect("fixture");

    let result = execute(
        "*** Begin Patch\n*** Update File: app.txt\n@@\n alpha\n-old\n+new\n omega\n*** End Patch\n",
        &root,
    );

    assert!(result.success, "{}", result.stderr);
    assert_change_identity(
        &result.changes[0],
        Some(b"alpha\r\nold\r\nomega\r\n"),
        Some(b"alpha\r\nnew\r\nomega\r\n"),
    );
    assert_eq!(
        fs::read_to_string(root.join("app.txt")).expect("read fixture"),
        "alpha\r\nnew\r\nomega\r\n"
    );
    let _ = fs::remove_dir_all(root);
}

#[test]
fn update_file_tolerates_trailing_whitespace_context_mismatch() {
    let root = temp_workspace("trailing-space");
    fs::write(root.join("app.txt"), "alpha  \nold\t\nomega\n").expect("fixture");

    let result = execute(
        "*** Begin Patch\n*** Update File: app.txt\n@@\n alpha\n-old\n+new\n omega\n*** End Patch\n",
        &root,
    );

    assert!(result.success, "{}", result.stderr);
    assert_change_identity(
        &result.changes[0],
        Some(b"alpha  \nold\t\nomega\n"),
        Some(b"alpha  \nnew\nomega\n"),
    );
    assert_eq!(
        fs::read_to_string(root.join("app.txt")).expect("read fixture"),
        "alpha  \nnew\nomega\n"
    );
    let _ = fs::remove_dir_all(root);
}

#[test]
fn update_file_tolerates_normalized_unicode_punctuation_context() {
    let root = temp_workspace("unicode-normalize");
    fs::write(root.join("app.txt"), "say “hello”\nold – value\n").expect("fixture");

    let result = execute(
        "*** Begin Patch\n*** Update File: app.txt\n@@\n say \"hello\"\n-old - value\n+new - value\n*** End Patch\n",
        &root,
    );

    assert!(result.success, "{}", result.stderr);
    assert_change_identity(
        &result.changes[0],
        Some("say “hello”\nold – value\n".as_bytes()),
        Some("say “hello”\nnew - value\n".as_bytes()),
    );
    assert_eq!(
        fs::read_to_string(root.join("app.txt")).expect("read fixture"),
        "say “hello”\nnew - value\n"
    );
    let _ = fs::remove_dir_all(root);
}

#[test]
fn update_file_applies_multiple_hunks_without_position_shift() {
    let root = temp_workspace("multi-hunk");
    fs::write(root.join("app.txt"), "one\nold-a\nmiddle\nold-b\nend\n").expect("fixture");

    let result = execute(
        "*** Begin Patch\n*** Update File: app.txt\n@@\n-old-a\n+new-a\n@@\n-old-b\n+new-b\n*** End Patch\n",
        &root,
    );

    assert!(result.success, "{}", result.stderr);
    assert_eq!(
        fs::read_to_string(root.join("app.txt")).expect("read fixture"),
        "one\nnew-a\nmiddle\nnew-b\nend\n"
    );
    let _ = fs::remove_dir_all(root);
}

#[test]
fn delete_file_reports_exact_binary_preimage_and_absent_postimage() {
    let root = temp_workspace("delete-identity");
    fs::write(root.join("app.bin"), b"\x00\xff\r\n").expect("fixture");
    let result = execute(
        "*** Begin Patch\n*** Delete File: app.bin\n*** End Patch\n",
        &root,
    );

    assert!(result.success, "{}", result.stderr);
    assert_change_identity(&result.changes[0], Some(b"\x00\xff\r\n"), None);
    assert!(!root.join("app.bin").exists());
    let _ = fs::remove_dir_all(root);
}

#[test]
fn add_and_delete_empty_file_distinguish_empty_digest_from_absence() {
    let root = temp_workspace("empty-identities");
    let result = execute(
        "*** Begin Patch\n*** Add File: empty.txt\n+\n*** Delete File: empty.txt\n*** End Patch\n",
        &root,
    );

    assert!(result.success, "{}", result.stderr);
    assert_eq!(result.changes.len(), 2);
    assert_change_identity(&result.changes[0], None, Some(b""));
    assert_change_identity(&result.changes[1], Some(b""), None);
    assert!(!root.join("empty.txt").exists());
    let _ = fs::remove_dir_all(root);
}

#[test]
fn move_file_reports_source_preimage_and_written_destination_postimage() {
    let root = temp_workspace("move-identity");
    fs::write(root.join("app.txt"), "old\r\n").expect("source");
    fs::write(root.join("moved.txt"), "different destination\n").expect("destination");
    let result = execute(
        "*** Begin Patch\n*** Update File: app.txt\n*** Move to: moved.txt\n@@\n-old\n+new\n*** End Patch\n",
        &root,
    );

    assert!(result.success, "{}", result.stderr);
    assert_change_identity(&result.changes[0], Some(b"old\r\n"), Some(b"new\r\n"));
    assert_eq!(result.changes[0]["path"], "app.txt");
    assert_eq!(result.changes[0]["move_path"], "moved.txt");
    assert!(!root.join("app.txt").exists());
    assert_eq!(fs::read(root.join("moved.txt")).expect("destination"), b"new\r\n");
    let _ = fs::remove_dir_all(root);
}

#[test]
fn repeated_updates_report_operation_identities_not_a_final_reread() {
    let root = temp_workspace("repeated-identities");
    fs::write(root.join("app.txt"), "old").expect("fixture");
    let result = execute(
        "*** Begin Patch\n*** Update File: app.txt\n@@\n-old\n+middle\n*** Update File: app.txt\n@@\n-middle\n+new\n*** End Patch\n",
        &root,
    );

    assert!(result.success, "{}", result.stderr);
    assert_eq!(result.changes.len(), 2);
    assert_change_identity(&result.changes[0], Some(b"old"), Some(b"middle"));
    assert_change_identity(&result.changes[1], Some(b"middle"), Some(b"new"));
    assert_eq!(
        result.changes[0]["postimage_sha256"],
        result.changes[1]["preimage_sha256"],
    );
    assert_ne!(
        result.changes[0]["postimage_sha256"],
        result.changes[1]["postimage_sha256"],
    );
    assert_eq!(fs::read(root.join("app.txt")).expect("fixture"), b"new");
    let _ = fs::remove_dir_all(root);
}

#[test]
fn failed_late_hunk_has_no_successful_write_identity() {
    let root = temp_workspace("failed-hunk-identity");
    fs::write(root.join("app.txt"), "old\nactual\n").expect("fixture");
    let result = execute(
        "*** Begin Patch\n*** Update File: app.txt\n@@\n-old\n+new\n@@\n-missing\n+rejected\n*** End Patch\n",
        &root,
    );

    assert!(!result.success);
    assert!(result.changes.is_empty());
    assert_eq!(result.output["error_type"], "ContextMismatch");
    assert_no_change_identity(&result.output["failed_changes"][0]["failed_change"]);
    assert_eq!(fs::read(root.join("app.txt")).expect("fixture"), b"old\nactual\n");
    let _ = fs::remove_dir_all(root);
}

#[test]
fn failed_move_write_has_no_successful_write_identity() {
    let root = temp_workspace("failed-write-identity");
    fs::write(root.join("app.txt"), "old\n").expect("source");
    fs::create_dir(root.join("destination")).expect("unwritable file target");
    let result = execute(
        "*** Begin Patch\n*** Update File: app.txt\n*** Move to: destination\n@@\n-old\n+new\n*** End Patch\n",
        &root,
    );

    assert!(!result.success);
    assert!(result.changes.is_empty());
    assert!(!result.output.to_string().contains("preimage_sha256"));
    assert!(!result.output.to_string().contains("postimage_sha256"));
    assert_eq!(fs::read(root.join("app.txt")).expect("source"), b"old\n");
    assert!(root.join("destination").is_dir());
    let _ = fs::remove_dir_all(root);
}

#[test]
fn failed_middle_file_reports_successes_and_failures_separately() {
    let root = temp_workspace("partial");
    fs::write(root.join("first.txt"), "old\n").expect("first");
    fs::write(root.join("second.txt"), "actual\n").expect("second");
    fs::write(root.join("third.txt"), "old\n").expect("third");

    let result = execute(
        "*** Begin Patch\n*** Update File: first.txt\n@@\n-old\n+new\n*** Update File: second.txt\n@@\n-missing\n+value\n*** Update File: third.txt\n@@\n-old\n+new\n*** End Patch\n",
        &root,
    );

    assert!(!result.success);
    assert_eq!(result.output["error_type"], json!("ContextMismatch"));
    assert_no_outer_message_or_guidance(&result.output);
    assert!(result.output.get("failed_change").is_none());
    assert!(result.output.get("partial_changes").is_none());
    assert_eq!(result.changes[0]["path"], json!("first.txt"));
    assert_eq!(result.changes[1]["path"], json!("third.txt"));
    assert_eq!(result.changes.len(), 2);
    for change in &result.changes {
        assert_change_identity(change, Some(b"old\n"), Some(b"new\n"));
    }
    assert_no_change_identity(&result.output["failed_changes"][0]["failed_change"]);
    assert_eq!(
        result.output["failed_changes"][0]["failed_change"]["path"],
        json!("second.txt")
    );
    assert!(
        result.output["failed_changes"][0]["message"]
            .as_str()
            .is_some_and(|text| text.contains("patch context not found"))
    );
    assert!(
        result.output["failed_changes"][0]["guidance"]
            .as_str()
            .is_some_and(|text| text.contains("after earlier changes were applied"))
    );
    assert_eq!(
        fs::read_to_string(root.join("first.txt")).expect("first"),
        "new\n"
    );
    assert_eq!(
        fs::read_to_string(root.join("second.txt")).expect("second"),
        "actual\n"
    );
    assert_eq!(
        fs::read_to_string(root.join("third.txt")).expect("third"),
        "new\n"
    );
    let _ = fs::remove_dir_all(root);
}

#[test]
fn add_file_accepts_absolute_path_outside_session_dir() {
    let root = temp_workspace("outside-absolute");
    let outside = root
        .parent()
        .expect("temp workspace should have a parent")
        .join("outside-apply-patch-test.txt");
    let _ = fs::remove_file(&outside);

    let result = execute(
        &format!(
            "*** Begin Patch\n*** Add File: {}\n+ok\n*** End Patch\n",
            outside.display()
        ),
        &root,
    );

    assert!(result.success, "{}", result.stderr);
    assert_eq!(fs::read_to_string(&outside).expect("outside file"), "ok\n");
    assert_eq!(
        result.changes[0]["path"],
        json!(outside.display().to_string())
    );
    let _ = fs::remove_file(outside);
    let _ = fs::remove_dir_all(root);
}

#[test]
fn access_locks_absolute_path_outside_session_dir() {
    let root = temp_workspace("outside-access");
    let outside = root
        .parent()
        .expect("temp workspace should have a parent")
        .join("outside-apply-patch-access.txt");
    let patch = format!(
        "*** Begin Patch\n*** Add File: {}\n+ok\n*** End Patch\n",
        outside.display()
    );

    let access = access(&patch, &root);

    assert_eq!(
        access.write_paths,
        vec![format!("absolute:{}", comparable_path_string(&outside))]
    );
    assert!(!access.workspace_write);
    let _ = fs::remove_dir_all(root);
}

#[test]
fn parser_rejects_patch_without_begin_marker() {
    let root = temp_workspace("missing-begin");

    let result = execute("*** Add File: app.txt\n+ok\n*** End Patch\n", &root);

    assert!(!result.success);
    assert_eq!(result.output["error_type"], json!("ParseError"));
    assert!(
        result.output["message"]
            .as_str()
            .is_some_and(|text| text.contains("Begin Patch"))
    );
    assert!(!root.join("app.txt").exists());
    let _ = fs::remove_dir_all(root);
}

#[test]
fn normalizes_fenced_or_wrapped_patch_text() {
    let patch = "*** Begin Patch\n*** Add File: app.txt\n+ok\n*** End Patch";
    assert_eq!(
        normalize_apply_patch_text(&format!("```patch\n{patch}\n```")),
        patch
    );
    assert_eq!(
        normalize_apply_patch_text(&format!("I will apply this:\n{patch}\nDone.")),
        patch
    );
    assert_eq!(
        normalize_apply_patch_text(&json!({ "patch": patch }).to_string()),
        patch
    );
    assert_eq!(
        normalize_apply_patch_text(
            &json!({ "command_line": format!("prefix\n{patch}\nsuffix") }).to_string()
        ),
        patch
    );
}

#[test]
fn normalizes_patch_body_missing_begin_marker() {
    let patch = "*** Begin Patch\n*** Add File: app.txt\n+ok\n*** End Patch";

    assert_eq!(
        normalize_apply_patch_text("apply_patch\n*** Add File: app.txt\n+ok\n*** End Patch"),
        patch
    );
    assert_eq!(
        normalize_apply_patch_text("*** Add File: app.txt\n+ok"),
        patch
    );
}

#[test]
fn function_payload_recursively_extracts_patch_body() {
    let patch = "*** Begin Patch\n*** Add File: app.txt\n+ok\n*** End Patch";
    let payload = ToolPayload::Function {
        arguments: json!({
            "request": {
                "body": format!("Here is the patch:\n{patch}\n")
            }
        }),
    };

    assert_eq!(patch_text_from_payload(&payload), patch);
}

#[test]
fn update_file_without_hunks_is_noop_success() {
    let root = temp_workspace("empty-update");
    fs::write(root.join("app.txt"), "unchanged\n").expect("fixture");

    let result = execute(
        "*** Begin Patch\n*** Update File: app.txt\n*** End Patch\n",
        &root,
    );

    assert!(result.success, "{}", result.stderr);
    assert_eq!(
        fs::read_to_string(root.join("app.txt")).expect("read fixture"),
        "unchanged\n"
    );
    let _ = fs::remove_dir_all(root);
}

#[test]
fn update_file_accepts_end_of_file_marker_between_changes() {
    let root = temp_workspace("eof-marker");
    fs::write(root.join("app.txt"), "old\n").expect("fixture");

    let result = execute(
        "*** Begin Patch\n*** Update File: app.txt\n@@\n-old\n+new\n*** End of File\n*** Add File: other.txt\n+ok\n*** End Patch\n",
        &root,
    );

    assert!(result.success, "{}", result.stderr);
    assert_eq!(
        fs::read_to_string(root.join("app.txt")).expect("read fixture"),
        "new\n"
    );
    assert_eq!(
        fs::read_to_string(root.join("other.txt")).expect("read fixture"),
        "ok\n"
    );
    let _ = fs::remove_dir_all(root);
}

#[test]
fn parser_rejects_patch_without_end_marker() {
    let root = temp_workspace("missing-end");

    let result = execute("*** Begin Patch\n*** Add File: app.txt\n+ok\n", &root);

    assert!(!result.success);
    assert_eq!(result.output["error_type"], json!("ParseError"));
    assert!(
        result.output["message"]
            .as_str()
            .is_some_and(|text| text.contains("End Patch"))
    );
    assert!(!root.join("app.txt").exists());
    let _ = fs::remove_dir_all(root);
}

#[test]
fn parser_rejects_update_content_outside_hunk() {
    let root = temp_workspace("outside-hunk");
    fs::write(root.join("app.txt"), "old\n").expect("fixture");

    let result = execute(
        "*** Begin Patch\n*** Update File: app.txt\n-old\n+new\n*** End Patch\n",
        &root,
    );

    assert!(!result.success);
    assert_eq!(result.output["error_type"], json!("ParseError"));
    assert_eq!(
        fs::read_to_string(root.join("app.txt")).expect("fixture"),
        "old\n"
    );
    let _ = fs::remove_dir_all(root);
}

#[test]
fn update_file_missing_is_structured_error() {
    let root = temp_workspace("update-missing");

    let result = execute(
        "*** Begin Patch\n*** Update File: missing.txt\n@@\n-old\n+new\n*** End Patch\n",
        &root,
    );

    assert!(!result.success);
    assert_eq!(result.output["error_type"], json!("UpdateFileNotFound"));
    assert!(result.changes.is_empty());
    assert_no_change_identity(&result.output["failed_changes"][0]["failed_change"]);
    assert_no_outer_message_or_guidance(&result.output);
    assert!(result.output.get("failed_change").is_none());
    assert_eq!(
        result.output["failed_changes"][0]["failed_change"]["path"],
        json!("missing.txt")
    );
    assert_eq!(
        result.output["failed_changes"][0]["message"],
        json!("UpdateFileNotFound: missing.txt")
    );
    assert_eq!(
        result.output["failed_changes"][0]["guidance"],
        json!("apply_patch failed; inspect error_type and message before retrying.")
    );
    let _ = fs::remove_dir_all(root);
}

#[test]
fn delete_file_missing_is_structured_error() {
    let root = temp_workspace("delete-missing");

    let result = execute(
        "*** Begin Patch\n*** Delete File: missing.txt\n*** End Patch\n",
        &root,
    );

    assert!(!result.success);
    assert_eq!(result.output["error_type"], json!("DeleteFileNotFound"));
    assert!(result.changes.is_empty());
    assert_no_change_identity(&result.output["failed_changes"][0]["failed_change"]);
    assert_no_outer_message_or_guidance(&result.output);
    assert!(result.output.get("failed_change").is_none());
    assert_eq!(
        result.output["failed_changes"][0]["failed_change"]["path"],
        json!("missing.txt")
    );
    let _ = fs::remove_dir_all(root);
}

#[test]
fn add_file_existing_is_structured_error() {
    let root = temp_workspace("add-existing");
    fs::write(root.join("app.txt"), "old\n").expect("fixture");

    let result = execute(
        "*** Begin Patch\n*** Add File: app.txt\n+new\n*** End Patch\n",
        &root,
    );

    assert!(!result.success);
    assert_eq!(result.output["error_type"], json!("AddFileExists"));
    assert!(result.changes.is_empty());
    assert_no_change_identity(&result.output["failed_changes"][0]["failed_change"]);
    assert_eq!(
        fs::read_to_string(root.join("app.txt")).expect("fixture"),
        "old\n"
    );
    let _ = fs::remove_dir_all(root);
}

#[cfg(windows)]
#[test]
fn add_file_accepts_git_bash_absolute_path_inside_session_dir() {
    let root = temp_workspace("git-bash-path");
    let path = root.join("checked.txt");
    let raw = path.to_string_lossy().replace('\\', "/");
    let drive = raw
        .chars()
        .next()
        .expect("drive letter")
        .to_ascii_lowercase();
    let git_bash_path = format!("/{drive}/{}", &raw[3..]);
    let result = execute(
        &format!("*** Begin Patch\n*** Add File: {git_bash_path}\n+ok\n*** End Patch\n"),
        &root,
    );

    assert!(result.success, "{}", result.stderr);
    assert_eq!(fs::read_to_string(path).expect("created file"), "ok\n");
    let _ = fs::remove_dir_all(root);
}
