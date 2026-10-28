pub(super) const CONTEXT_OUTPUT_MAX_CHARS: usize = 10_000;
pub(super) const COMMAND_RUN_RESULT_OUTPUT_MAX_CHARS: usize = 10_000;
// JSON pointer reads are not range pages, so their stdout gets this budget's middle cut: a JSON
// page must fit it whole. Checked at compile time.
const _: () = assert!(
    code_tools::commands::source_read::MAX_JSON_TEXT_BYTES <= COMMAND_RUN_RESULT_OUTPUT_MAX_CHARS
);
pub(super) const COMPACT_CONTEXT_FALLBACK_MAX_ESTIMATED_TOKENS: usize = 25_500;
pub(super) const COMPACT_CONTEXT_ESTIMATED_TOKEN_BYTES: usize = 4;

pub(crate) fn estimated_tokens_from_bytes(bytes: usize) -> usize {
    bytes.div_ceil(COMPACT_CONTEXT_ESTIMATED_TOKEN_BYTES)
}

pub(crate) fn estimated_tokens_from_bytes_u64(bytes: u64) -> u64 {
    bytes.div_ceil(COMPACT_CONTEXT_ESTIMATED_TOKEN_BYTES as u64)
}

pub(super) fn compact_context_byte_budget(max_estimated_tokens: usize) -> usize {
    max_estimated_tokens * COMPACT_CONTEXT_ESTIMATED_TOKEN_BYTES
}

// Budgets here count characters. A byte length at or under the budget proves the character
// count is too (UTF-8 is at least one byte per character); otherwise count characters, so CJK
// text within the budget is neither marked nor reported as truncated.
pub(super) fn within_char_budget(text: &str, max_chars: usize) -> bool {
    text.len() <= max_chars || text.chars().count() <= max_chars
}

pub(super) fn truncate_text_to_char_budget(text: &str, max_chars: usize) -> String {
    if within_char_budget(text, max_chars) {
        return text.to_string();
    }
    let mut out = text.chars().take(max_chars).collect::<String>();
    out.push_str("\n\n[context checkpoint truncated to fit the compact_context summary limit]");
    out
}

pub(super) fn context_output_byte_budget() -> usize {
    CONTEXT_OUTPUT_MAX_CHARS
}

pub(super) fn formatted_truncate_text(content: &str, max_chars: usize) -> String {
    if within_char_budget(content, max_chars) {
        return content.to_string();
    }
    let total_lines = content.lines().count();
    let truncated = truncate_middle_with_char_budget(content, max_chars);
    format!("Total output lines: {total_lines}\n\n{truncated}")
}

pub(super) fn truncate_middle_with_char_budget(content: &str, max_chars: usize) -> String {
    if max_chars == 0 {
        return format!("...{} characters truncated...", content.chars().count());
    }
    let keep_each_side = max_chars / 2;
    let mut head_end = 0usize;
    for (count, (index, ch)) in content.char_indices().enumerate() {
        if count >= keep_each_side {
            break;
        }
        head_end = index + ch.len_utf8();
    }
    let mut tail_start = content.len();
    for (count, (index, _)) in content.char_indices().rev().enumerate() {
        if count >= keep_each_side {
            break;
        }
        tail_start = index;
    }
    if head_end >= tail_start {
        return content.to_string();
    }
    let removed = content[head_end..tail_start].chars().count();
    format!(
        "{}...{} characters truncated...{}",
        &content[..head_end],
        removed,
        &content[tail_start..]
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cjk_within_the_character_budget_is_neither_marked_nor_truncated() {
        let text = "中".repeat(4_000); // 4,000 characters, 12,000 bytes
        assert!(text.len() > 10_000);
        assert_eq!(formatted_truncate_text(&text, 10_000), text);
        assert_eq!(truncate_text_to_char_budget(&text, 10_000), text);
    }

    #[test]
    fn cjk_over_the_character_budget_reports_removed_characters() {
        let text = "中".repeat(12_000);
        let out = formatted_truncate_text(&text, 10_000);
        assert!(out.starts_with("Total output lines: 1\n\n"));
        assert!(out.contains("...2000 characters truncated..."), "{}", &out[..80]);
        assert_eq!(out.matches('中').count(), 10_000);
        let checkpoint = truncate_text_to_char_budget(&text, 10_000);
        assert!(checkpoint.ends_with("[context checkpoint truncated to fit the compact_context summary limit]"));
        assert_eq!(checkpoint.matches('中').count(), 10_000);
    }

    #[test]
    fn ascii_behavior_is_unchanged() {
        let short = "a".repeat(10_000);
        assert_eq!(formatted_truncate_text(&short, 10_000), short);
        let long = "a".repeat(10_010);
        let out = formatted_truncate_text(&long, 10_000);
        assert!(out.contains("...10 characters truncated..."));
        assert_eq!(truncate_middle_with_char_budget("abcdef", 0), "...6 characters truncated...");
        assert_eq!(truncate_middle_with_char_budget("中文字", 0), "...3 characters truncated...");
    }
}
