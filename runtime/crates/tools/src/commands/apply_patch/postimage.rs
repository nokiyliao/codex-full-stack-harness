//! Optional exact text evidence, never authority, verifier success or acceptance.
use serde_json::{Value, json};

const MAX_BYTES: usize = 32768;
const MAX_LINES: usize = 600;
const MAX_FILE_BYTES: usize = 1024 * 1024;

pub(super) fn from_applied_buffers(
    before: &str, after: &str, replacements: &[(usize, usize, Vec<String>)],
) -> Option<Value> {
    if before.len() > MAX_FILE_BYTES || after.len() > MAX_FILE_BYTES { return None; }
    let old: Vec<&str> = before.split_inclusive('\n').collect();
    let new: Vec<&str> = after.split_inclusive('\n').collect();
    let mut hunks = Vec::new();
    let mut removed = 0usize;
    let mut added = 0usize;
    let mut previous_end = 0usize;
    let mut count = 0usize;
    let mut reconstructed = String::new();
    for (start, end, replacement) in replacements {
        if start > end || *end > old.len() || *start < previous_end { return None; }
        let new_start = start.checked_sub(removed)?.checked_add(added)?;
        let new_end = new_start.checked_add(replacement.len())?;
        if new_end > new.len() { return None; }
        let before_text = old[*start..*end].concat();
        let after_text = new[new_start..new_end].concat();
        reconstructed.push_str(&old[previous_end..*start].concat());
        reconstructed.push_str(&after_text);
        previous_end = *end;
        removed = removed.checked_add(end-start)?;
        added = added.checked_add(replacement.len())?;
        count = count.checked_add(end-start)?.checked_add(new_end-new_start)?;
        if count > MAX_LINES { return None; }
        hunks.push(json!({
            "before_start_line": start+1, "before_line_count": end-start,
            "after_start_line": new_start+1, "after_line_count": new_end-new_start,
            "before_text": before_text, "after_text": after_text,
        }));
    }
    reconstructed.push_str(&old[previous_end..].concat());
    // Line-ending normalization or any unrepresented edit invalidates the whole projection.
    if hunks.is_empty() || reconstructed != after { return None; }
    let value = json!({"schema_version":"nokiy_applied_source_delta_v1",
        "coverage":"all_text_changes", "line_count":count, "hunks":hunks,
        "notice":"Exact applied buffers bound by this change's preimage/postimage SHA and terminal receipt. Reuse only matching prior source plus these hunks; unseen surrounding context is not supplied. Not verifier facts, acceptance or current state after later edits."});
    (serde_json::to_vec(&value).ok()?.len() <= MAX_BYTES).then_some(value)
}

pub(super) fn bound_batch(changes: &mut [Value]) {
    let mut bytes = 0usize;
    let mut lines = 0u64;
    let mut files = 0usize;
    for change in changes.iter() {
        if let Some(delta) = change.get("source_delta") {
            bytes = bytes.saturating_add(serde_json::to_vec(delta).map_or(MAX_BYTES+1, |v| v.len()));
            lines = lines.saturating_add(delta["line_count"].as_u64().unwrap_or(u64::MAX));
            files += 1;
        }
    }
    if bytes > MAX_BYTES || lines > MAX_LINES as u64 || files > 4 {
        for change in changes {
            if let Some(object) = change.as_object_mut() { object.remove("source_delta"); }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exact_coordinates_and_unicode() {
        let value = from_applied_buffers("a\nb\nc\n", "a\nnew\nmore\nc\n",
            &[(1,2,vec!["new".into(),"more".into()])]).unwrap();
        assert_eq!(value["hunks"][0]["before_start_line"],2);
        assert_eq!(value["hunks"][0]["after_text"],"new\nmore\n");
        assert!(from_applied_buffers("\u{4e2d}\n", "\u{6587}\n", &[(0,1,vec!["\u{6587}".into()])]).is_some());
    }

    #[test]
    fn adjacent_insert_delete_and_empty_sides() {
        let value = from_applied_buffers("a\nb\nc\n", "x\ny\nc\n",
            &[(0,1,vec!["x".into(),"y".into()]),(1,2,vec![])]).unwrap();
        assert_eq!(value["hunks"][1]["after_start_line"],3);
        assert_eq!(value["hunks"][1]["after_line_count"],0);
        assert!(from_applied_buffers("", "a\n", &[(0,0,vec!["a".into()])]).is_some());
        assert!(from_applied_buffers("a\n", "", &[(0,1,vec![])]).is_some());
    }

    #[test]
    fn exact_crlf_and_missing_final_newline() {
        assert!(from_applied_buffers("a\r\nb\r\n", "a\r\nx\r\n", &[(1,2,vec!["x".into()])]).is_some());
        let value = from_applied_buffers("a\nb", "a\nx", &[(1,2,vec!["x".into()])]).unwrap();
        assert_eq!(value["hunks"][0]["after_text"],"x");
    }

    #[test]
    fn unrepresented_changes_and_overlap_omit_evidence() {
        assert!(from_applied_buffers("a\r\nb\n", "a\nx\n", &[(1,2,vec!["x".into()])]).is_none());
        assert!(from_applied_buffers("a\nb\n", "x\ny\n", &[(0,1,vec!["x".into()])]).is_none());
        assert!(from_applied_buffers("a\n", "x\n", &[(0,1,vec!["x".into()]),(0,1,vec!["x".into()])]).is_none());
    }

    #[test]
    fn budgets_omit_whole_evidence_without_affecting_changes() {
        let text = "x".repeat(MAX_BYTES);
        assert!(from_applied_buffers("a\n", &text, &[(0,1,vec![text.clone()])]).is_none());
        let mut changes = vec![json!({"path":"a","source_delta":{"line_count":400}}),
                               json!({"path":"b","source_delta":{"line_count":400}})];
        bound_batch(&mut changes);
        assert!(changes.iter().all(|c| c.get("source_delta").is_none()));
        assert_eq!(changes[0]["path"],"a");
    }
}
