/// Classifies a raw tool-call envelope that leaked into visible agent text.
///
/// Shared by every visible-text exit (worker result, tura_exec CLI) so they cannot drift.
pub fn looks_like_tool_payload(text: &str) -> bool {
    let trimmed = text.trim_start();
    trimmed.starts_with('{')
        && (trimmed.contains("\"commands\"")
            || trimmed.contains("\"task_group\"")
            || trimmed.contains("\"step_summary\"")
            || trimmed.contains("\"tool_calls\"")
            || trimmed.contains("\"reply_message\""))
}

/// Canonical visible agent text: empty for a raw payload, otherwise the text without a payload suffix.
pub fn clean_agent_message(text: &str) -> String {
    let trimmed = text.trim();
    if trimmed.is_empty() || looks_like_tool_payload(trimmed) {
        return String::new();
    }
    strip_tool_payload_suffix(trimmed, looks_like_tool_payload)
}

/// Removes a tool-payload suffix outside Markdown code using the caller's classifier.
///
/// Fenced blocks and matched inline backtick spans are preserved. Filtering resumes
/// after closing code delimiters; unmatched inline delimiters remain literal text.
pub fn strip_tool_payload_suffix(
    text: &str,
    looks_like_tool_payload: impl Fn(&str) -> bool,
) -> String {
    if !text.contains("{\"commands\"") {
        return text.to_string();
    }

    // Skip Markdown code, but resume payload filtering after its closing delimiter.
    let mut fence = None;
    let mut code_end = 0;
    for (index, token) in text.match_indices(['`', '~', '{']) {
        if index < code_end {
            continue;
        }
        if token == "{" {
            if fence.is_none()
                && text[index..].starts_with("{\"commands\"")
                && looks_like_tool_payload(&text[index..])
            {
                return text[..index].trim().to_string();
            }
            continue;
        }

        let marker = token.as_bytes()[0];
        let width = text[index..]
            .bytes()
            .take_while(|&byte| byte == marker)
            .count();
        code_end = index + width;
        let line_start = text[..index].rfind('\n').map_or(0, |newline| newline + 1);
        let indentation = &text[line_start..index];
        let at_line_start =
            indentation.len() <= 3 && indentation.bytes().all(|byte| byte == b' ');
        let line_end = text[code_end..]
            .find('\n')
            .map_or(text.len(), |newline| code_end + newline);
        let remainder = &text[code_end..line_end];
        if let Some((fence_marker, fence_width)) = fence {
            if at_line_start
                && marker == fence_marker
                && width >= fence_width
                && remainder.trim_matches([' ', '\t']).is_empty()
            {
                fence = None;
            }
            continue;
        }
        if at_line_start && width >= 3 && (marker == b'~' || !remainder.contains('`')) {
            fence = Some((marker, width));
            code_end = line_end;
            continue;
        }
        if marker != b'`' {
            continue;
        }
        if text[..index]
            .bytes()
            .rev()
            .take_while(|&byte| byte == b'\\')
            .count()
            % 2
            != 0
        {
            code_end = index + 1;
            continue;
        }

        // An unmatched backtick run is literal text, not a payload-filter bypass.
        let mut end = code_end;
        while let Some(next) = text[end..].find('`') {
            let start = end + next;
            let closing_width = text[start..]
                .bytes()
                .take_while(|&byte| byte == b'`')
                .count();
            end = start + closing_width;
            if closing_width == width {
                code_end = end;
                break;
            }
        }
    }
    text.to_string()
}

#[cfg(test)]
mod tests {
    use super::strip_tool_payload_suffix;

    fn is_tool_payload(text: &str) -> bool {
        text.starts_with("{\"commands\"")
    }

    #[test]
    fn preserves_fences_and_filters_payloads_after_closing_delimiters() {
        for (open, nested, close) in [
            ("```rust", "", "```"),
            ("````rust", "```\n", "`````"),
            ("~~~rust", "```\n", "~~~~"),
            ("~~~~text", "~~~\n````\n~~~~not a closer\n", "~~~~"),
            ("   `````rust", "   ````\n", "   `````"),
        ] {
            let text = format!(
                "regression:\n{open}\n{nested}let args = json!({{\"commands\":[]}});\n{close}\nEND_ANSWER"
            );
            assert_eq!(strip_tool_payload_suffix(&text, is_tool_payload), text);
            for payload in [r#"{"commands":[]}"#, r#"{"commands":["#] {
                let leaked = format!("{text}\n{payload}");
                assert_eq!(strip_tool_payload_suffix(&leaked, is_tool_payload), text);
            }
        }
    }

    #[test]
    fn preserves_inline_spans_and_filters_payloads_after_closing_delimiters() {
        for text in [
            r#"`{"commands":[]}`"#,
            r#"Use `{"commands":[]}` inline. END_ANSWER"#,
            r#"Use ``json!({"commands":[]})`` inline. END_ANSWER"#,
            r#"Use ``a ` tick; {"commands":[]} `` inline. END_ANSWER"#,
            r#"Use ```json!({"commands":[]})``` inline. END_ANSWER"#,
            "Use ``json!(\n{\"commands\":[]}\n)`` inline. END_ANSWER",
            "Unicode λ `{\"commands\":[]}` retained. END_ANSWER",
        ] {
            assert_eq!(strip_tool_payload_suffix(text, is_tool_payload), text);
            for payload in [r#"{"commands":[]}"#, r#"{"commands":["#] {
                let leaked = format!("{text}\n{payload}");
                assert_eq!(strip_tool_payload_suffix(&leaked, is_tool_payload), text);
            }
        }
    }

    #[test]
    fn unmatched_or_escaped_delimiters_do_not_hide_payloads() {
        for prefix in [
            "Done.",
            "Unmatched ` delimiter.",
            "Mismatched ` delimiters ``.",
            r"Escaped \` delimiter.",
            "Not a fence: ~~~ prose.",
        ] {
            for payload in [r#"{"commands":[]}"#, r#"{"commands":["#] {
                let text = format!("{prefix}\n{payload}");
                assert_eq!(strip_tool_payload_suffix(&text, is_tool_payload), prefix);
            }
        }
    }

    #[test]
    fn defers_to_the_callers_payload_classifier() {
        let text = r#"Answer. {"commands":[]}"#;
        assert_eq!(strip_tool_payload_suffix(text, |_| false), text);
        assert_eq!(
            strip_tool_payload_suffix(text, |suffix| suffix == r#"{"commands":[]}"#),
            "Answer."
        );
        assert_eq!(strip_tool_payload_suffix(r#"{"commands":["#, is_tool_payload), "");
    }

    #[test]
    fn leaves_text_without_command_candidates_unchanged() {
        for text in ["", "  answer\n\nEND_ANSWER  ", "`code` and ~~~ prose"] {
            assert_eq!(
                strip_tool_payload_suffix(text, |_| panic!("no payload candidate")),
                text
            );
        }
    }
}
