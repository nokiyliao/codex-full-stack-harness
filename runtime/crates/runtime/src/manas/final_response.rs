use crate::prompt_style::runtime_fallback;
use lifecycle::SessionManagement;

use crate::gateway_events::summarize_single_tool_output;

pub(crate) fn user_visible_runtime_text(text: &str) -> Option<String> {
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return None;
    }
    if is_code_fence_only(trimmed) {
        return None;
    }
    if let Some(reply_message) = extract_reply_message_from_json(trimmed) {
        // Decoded JSON strings can contain thought markup, including escaped tags.
        return user_visible_runtime_text(&reply_message);
    }

    let mut visible = String::new();
    let mut rest = trimmed;
    loop {
        if let Some(start) = rest.find("<think>") {
            visible.push_str(&rest[..start]);
            let after_start = &rest[start + "<think>".len()..];
            if let Some(end) = after_start.find("</think>") {
                rest = &after_start[end + "</think>".len()..];
                continue;
            }
            break;
        }
        visible.push_str(rest);
        break;
    }

    let visible = strip_tool_payload_suffix(&strip_runtime_markup(visible.trim()));
    if !visible.is_empty() {
        if is_code_fence_only(&visible) {
            return None;
        }
        if let Some(reply_message) = extract_reply_message_from_json(&visible) {
            return user_visible_runtime_text(&reply_message);
        }
        if looks_like_tool_payload(&visible) {
            return None;
        }
        return Some(visible);
    }

    // Never recover an answer from a hidden or unfinished thought block.
    None
}

pub(crate) fn user_visible_runtime_output_text(output: &serde_json::Value) -> Option<String> {
    for key in [
        "reply_message",
        "output_text",
        "final_text",
        "message",
        "text",
        "content",
        "summary",
    ] {
        if let Some(text) = output.get(key).and_then(serde_json::Value::as_str)
            && let Some(visible) = user_visible_runtime_text(text)
        {
            return Some(visible);
        }
    }
    let content = tura_llm_rust::normalize_response_content(output);
    let text = tura_llm_rust::extract_response_text(&content)?;
    user_visible_runtime_text(&text)
}

fn strip_tool_payload_suffix(text: &str) -> String {
    let Some(index) = text.find("{\"commands\"") else {
        return text.to_string();
    };
    let (prefix, suffix) = text.split_at(index);
    if looks_like_tool_payload(suffix) {
        return prefix.trim().to_string();
    }
    text.to_string()
}

pub(super) fn extract_reply_message_from_json(text: &str) -> Option<String> {
    serde_json::from_str::<serde_json::Value>(text)
        .ok()
        .and_then(|value| find_reply_message(&value))
}

fn is_code_fence_only(text: &str) -> bool {
    let trimmed = text.trim();
    if trimmed == "```" {
        return true;
    }
    let lower = trimmed.to_ascii_lowercase();
    matches!(
        lower.as_str(),
        "```json" | "```javascript" | "```js" | "```text"
    )
}

pub(super) fn find_reply_message(value: &serde_json::Value) -> Option<String> {
    match value {
        serde_json::Value::Object(object) => {
            if let Some(message) = object
                .get("reply_message")
                .and_then(|value| value.as_str())
                .map(str::trim)
                .filter(|value| !value.is_empty())
            {
                return Some(message.to_string());
            }
            object.values().find_map(find_reply_message)
        }
        serde_json::Value::Array(items) => items.iter().find_map(find_reply_message),
        _ => None,
    }
}

pub(super) fn looks_like_tool_payload(text: &str) -> bool {
    let trimmed = text.trim_start();
    if !trimmed.starts_with('{') {
        return false;
    }

    if let Ok(value) = serde_json::from_str::<serde_json::Value>(trimmed) {
        return json_looks_like_tool_payload(&value);
    }

    trimmed.contains("\"reply_message\"")
        || trimmed.contains("\"new_learning\"")
        || trimmed.contains("\"tool_calls\"")
        || trimmed.contains("\"commands\"")
        || trimmed.contains("\"input\"")
        || trimmed.contains("\"last_tool_call_status\"")
        || trimmed.contains("\"last_tool_call_summary\"")
        || trimmed.contains("\"task_group\"")
        || trimmed.contains("\"step_summary\"")
}

pub(super) fn json_looks_like_tool_payload(value: &serde_json::Value) -> bool {
    match value {
        serde_json::Value::Object(object) => {
            let has_reporting_fields = object.contains_key("last_tool_call_status")
                || object.contains_key("last_tool_call_summary")
                || object.contains_key("task_group")
                || object.contains_key("step_summary");
            let has_tool_shape = object.contains_key("requests")
                || object.contains_key("commands")
                || object.contains_key("reply_message")
                || object.contains_key("new_learning")
                || object.contains_key("tool_calls")
                || object.contains_key("input")
                || object.contains_key("command_code")
                || object.contains_key("environment");

            (has_reporting_fields && has_tool_shape)
                || object.contains_key("tool_calls")
                || object.contains_key("commands")
                || object.values().any(json_looks_like_tool_payload)
        }
        serde_json::Value::Array(items) => items.iter().any(json_looks_like_tool_payload),
        _ => false,
    }
}

pub(super) fn strip_runtime_markup(text: &str) -> String {
    text.replace("\r\n", "\n")
        .replace('\r', "\n")
        .split('\n')
        .filter(|line| !is_runtime_markup_line(line.trim()))
        .collect::<Vec<_>>()
        .join("\n")
}

pub(super) fn is_runtime_markup_line(line: &str) -> bool {
    let lower = line.to_ascii_lowercase();
    if matches!(
        lower.as_str(),
        "<invoke>" | "</invoke>" | "<tool_call>" | "</tool_call>" | "<tool>" | "</tool>"
    ) {
        return true;
    }

    if lower.starts_with('<') && lower.ends_with('>') {
        return true;
    }

    if lower.starts_with("command_run:") && (lower.contains('{') || lower.contains('[')) {
        return true;
    }

    (lower.starts_with("<invoke") && lower.ends_with('>'))
        || (lower.starts_with("</invoke") && lower.ends_with('>'))
        || (lower.starts_with("<tool_call") && lower.ends_with('>'))
        || (lower.starts_with("</tool_call") && lower.ends_with('>'))
}

pub(crate) fn summarize_tool_results_for_user(session: &SessionManagement) -> Option<String> {
    let tool_results: Vec<_> = session
        .session_log
        .iter()
        .map(|entry| entry.value())
        .filter(|value| value.get("type").and_then(|kind| kind.as_str()) == Some("tool_result"))
        .collect();

    if tool_results.is_empty() {
        return None;
    }

    let mut lines = Vec::new();
    lines.push(runtime_fallback::tool_chain_summary_header().to_string());

    for result in tool_results.iter().rev().take(3).rev() {
        let tool_name = result
            .get("tool_name")
            .and_then(|value| value.as_str())
            .unwrap_or("unknown");
        let output = result
            .get("output")
            .cloned()
            .unwrap_or(serde_json::Value::Null);
        let summary = summarize_single_tool_output(tool_name, &output);
        lines.push(format!("- `{tool_name}`: {summary}"));
    }

    Some(lines.join("\n"))
}

#[cfg(test)]
mod tests {
    use super::{user_visible_runtime_output_text, user_visible_runtime_text};
    use serde_json::json;

    #[test]
    fn user_visible_runtime_text_rejects_thought_only_text() {
        for text in [
            "<think>planning only</think>",
            " \t<think>planning\nonly</think>\u{2003}",
            "<think>first</think>\n<think>second</think>",
            "<think>unfinished",
            "<think>unfinished</think",
            r#"<think>{"reply_message":"hidden answer"}</think>"#,
            r#"<think>{"answer":42}</think>"#,
            r#"<think>{"reply_message":"hidden answer"}"#,
        ] {
            assert_eq!(user_visible_runtime_text(text), None, "{text:?}");
        }
    }

    #[test]
    fn user_visible_runtime_text_filters_decoded_json_replies() {
        for reply in ["<think>hidden</think>", "<think>unfinished"] {
            let text = json!({"input": {"reply_message": reply}}).to_string();
            assert_eq!(user_visible_runtime_text(&text), None, "{text:?}");
        }
        assert_eq!(
            user_visible_runtime_text(
                r#"{"reply_message":"\u003cthink\u003ehidden\u003c/think\u003e"}"#
            ),
            None
        );
    }

    #[test]
    fn user_visible_runtime_text_preserves_visible_text_around_thoughts() {
        for (text, expected) in [
            ("<think>hidden</think>Done.", "Done."),
            (
                "Before.<think>hidden</think>\n\nAfter.",
                "Before.\n\nAfter.",
            ),
            ("Done.<think>unfinished", "Done."),
            ("<think>first</think>Done.<think>unfinished", "Done."),
            (
                r#"<think>hidden</think>{"reply_message":"Done."}"#,
                "Done.",
            ),
            (
                r#"<think>hidden</think>{"answer":42}"#,
                r#"{"answer":42}"#,
            ),
            (
                r#"{"input":{"reply_message":"<think>hidden</think>Done."}}"#,
                "Done.",
            ),
        ] {
            assert_eq!(
                user_visible_runtime_text(text).as_deref(),
                Some(expected),
                "{text:?}"
            );
        }
    }

    #[test]
    fn user_visible_runtime_text_preserves_concise_plain_and_json_answers() {
        for reply in ["Done.", "完", "42", r#"{"answer":42}"#] {
            assert_eq!(user_visible_runtime_text(reply).as_deref(), Some(reply));
            let text = json!({"input": {"reply_message": reply}}).to_string();
            assert_eq!(user_visible_runtime_text(&text).as_deref(), Some(reply));
        }
    }

    #[test]
    fn user_visible_runtime_text_keeps_tool_payload_and_fence_filtering() {
        let payload = r#"{"commands":[{"command_type":"task_status"}]}"#;
        for suffix in [payload, "```", "```json"] {
            let text = format!("<think>hidden</think>{suffix}");
            assert_eq!(user_visible_runtime_text(&text), None, "{text:?}");
        }
        let text = format!("<think>hidden</think>Done.{payload}");
        assert_eq!(user_visible_runtime_text(&text).as_deref(), Some("Done."));
    }

    #[test]
    fn user_visible_runtime_output_text_filters_thoughts_in_provider_text() {
        for (text, expected) in [
            ("<think>hidden</think>", None),
            ("<think>unfinished", None),
            (r#"<think>{"reply_message":"hidden"}</think>"#, None),
            (r#"{"reply_message":"<think>hidden</think>"}"#, None),
            ("<think>hidden</think>Done.", Some("Done.")),
        ] {
            for output in [
                json!(text),
                json!({"reply_message": text}),
                json!({"text": text}),
                json!({"choices": [{"message": {"content": text}}]}),
                json!({"parts": [{"text": text}]}),
            ] {
                assert_eq!(
                    user_visible_runtime_output_text(&output).as_deref(),
                    expected,
                    "{output:?}"
                );
            }
        }
    }

    #[test]
    fn user_visible_runtime_text_extracts_reply_message_from_tool_payload() {
        let text = json!({
            "error": null,
            "input": {
                "reply_message": "final answer",
                "new_learning": "",
                "runtime_id": "runtime-1"
            }
        })
        .to_string();

        assert_eq!(
            user_visible_runtime_text(&text).as_deref(),
            Some("final answer")
        );
    }

    #[test]
    fn user_visible_runtime_text_hides_raw_tool_argument_payload() {
        let text = json!({
                "requests": [{
                    "path": "services/sd-text-to-image/main.py",
                    "start_line": 1,
                    "end_line": 250
                }],
                "step_summary": "Read the Stable Diffusion image service main.py to find the port it runs on."
            })
            .to_string();

        assert_eq!(user_visible_runtime_text(&text), None);
    }

    #[test]
    fn user_visible_runtime_text_preserves_internal_blank_lines_and_indentation() {
        let text = "First paragraph.\r\n\r\n    indented detail\r\n\r\nLast paragraph.";

        assert_eq!(
            user_visible_runtime_text(text).as_deref(),
            Some("First paragraph.\n\n    indented detail\n\nLast paragraph.")
        );
    }

    #[test]
    fn user_visible_runtime_text_removes_markup_without_compacting_surrounding_lines() {
        let text = "First paragraph.\n\n<tool_call>\n\nSecond paragraph.";

        assert_eq!(
            user_visible_runtime_text(text).as_deref(),
            Some("First paragraph.\n\n\nSecond paragraph.")
        );
    }

    #[test]
    fn user_visible_runtime_output_text_extracts_string_output() {
        let output = json!("你好。小主管已上线。");

        assert_eq!(
            user_visible_runtime_output_text(&output).as_deref(),
            Some("你好。小主管已上线。")
        );
    }

    #[test]
    fn user_visible_runtime_output_text_extracts_fixed_provider_text_locations() {
        for (output, expected) in [
            (
                json!({"output_text": "from output_text"}),
                "from output_text",
            ),
            (json!({"final_text": "from final_text"}), "from final_text"),
            (json!({"message": "from message"}), "from message"),
            (json!({"text": "from text"}), "from text"),
            (json!({"content": "from content"}), "from content"),
            (
                json!({"choices": [{"message": {"content": "from choice"}}]}),
                "from choice",
            ),
            (
                json!({"parts": [{"text": "from "}, {"text": "parts"}]}),
                "from parts",
            ),
        ] {
            assert_eq!(
                user_visible_runtime_output_text(&output).as_deref(),
                Some(expected)
            );
        }
    }
}
