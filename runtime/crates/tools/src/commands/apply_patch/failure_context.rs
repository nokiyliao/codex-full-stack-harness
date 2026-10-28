/// A diagnostic candidate only: never used to select or apply a replacement.
pub(super) fn excerpt(original: &str, expected: &str) -> Option<(usize, String)> {
    let expected = expected.trim();
    if expected.chars().count() < 16 {
        return None;
    }
    let lines: Vec<_> = original.split_inclusive('\n').collect();
    let mut best = 0;
    let mut index = None;
    for (i, line) in lines.iter().enumerate() {
        let common = line.trim().chars().zip(expected.chars()).take(120)
            .take_while(|(a, b)| a == b).count();
        if common > best {
            best = common;
            index = Some(i);
        } else if common == best {
            index = None;
        }
    }
    if best < 16 {
        return None;
    }
    let index = index?;
    let start = index.saturating_sub(3);
    let end = (index + 4).min(lines.len());
    let text = lines[start..end].concat();
    if text.len() > 4096 {
        return None;
    }
    Some((start + 1, text))
}

#[cfg(test)]
mod tests {
    use super::excerpt;

    #[test]
    fn unique_candidate_preserves_crlf_and_unicode() {
        let source = "before\r\n    literals = {s for s in scopes if not any(c in s for c in \"*?[\")}\r\nafter\r\n";
        let expected = "    literals = {s for s in scopes if not any(c in s for c in \"*?[\u{2006}\")}";
        assert_eq!(excerpt(source, expected), Some((1, source.to_owned())));
    }

    #[test]
    fn ambiguity_and_weak_anchors_do_not_guess() {
        assert!(excerpt("same_long_prefix_here_A\nsame_long_prefix_here_B\n", "same_long_prefix_here_C").is_none());
        assert!(excerpt("    x = 1\n", "    x = 2").is_none());
        assert!(excerpt("unrelated\n", "a_long_expected_anchor").is_none());
    }

    #[test]
    fn result_has_correct_line_and_bounded_extent() {
        let source = format!("{}unique_long_anchor_original\n{}", "x\n".repeat(10), "y\n".repeat(10));
        let (line, text) = excerpt(&source, "unique_long_anchor_changed").unwrap();
        assert_eq!(line, 8);
        assert_eq!(text.lines().count(), 7);
        assert!(excerpt(&format!("unique_long_anchor{}", "x".repeat(5000)), "unique_long_anchor_changed").is_none());
    }
}
