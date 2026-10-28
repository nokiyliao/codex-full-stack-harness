use std::path::{Path, PathBuf};

/// IANA zone for the environment block: `TZ` when set, otherwise the system zone
/// that macOS and Linux expose through the `/etc/localtime` link, otherwise UTC.
pub fn system_timezone() -> String {
    resolve_timezone(
        std::env::var("TZ").ok(),
        std::fs::read_link("/etc/localtime").ok(),
    )
}

fn resolve_timezone(tz: Option<String>, localtime: Option<PathBuf>) -> String {
    let zone_after_zoneinfo = |text: &str| {
        text.split_once("zoneinfo/")
            .map(|(_, zone)| zone.to_string())
            .filter(|zone| !zone.is_empty())
    };
    tz.map(|value| value.trim().trim_start_matches(':').to_string())
        .filter(|value| !value.is_empty())
        .map(|value| zone_after_zoneinfo(&value).unwrap_or(value))
        .or_else(|| localtime.and_then(|path| zone_after_zoneinfo(&path.to_string_lossy())))
        .unwrap_or_else(|| "UTC".to_string())
}

pub fn environment_context(
    cwd: &Path,
    shell: &str,
    current_date: impl std::fmt::Display,
    timezone: &str,
    system_language: &str,
) -> String {
    format!(
        "<environment_context>\n  <cwd>{}</cwd>\n  <workspace_roots>\n    <root>{}</root>\n  </workspace_roots>\n  <shell>{}</shell>\n  <current_date>{}</current_date>\n  <timezone>{}</timezone>\n  <system_language>{}</system_language>\n</environment_context>",
        cwd.display(),
        cwd.display(),
        shell.trim(),
        current_date,
        timezone.trim(),
        system_language.trim(),
    )
}

pub fn current_objective_block(overall: &str, current_task: Option<&str>) -> String {
    let overall = overall.trim();
    match current_task.map(str::trim).filter(|task| !task.is_empty()) {
        Some(task) => format!("[current objective]:\n{overall}\n\n{task}"),
        None => format!("[current objective]:\n{overall}"),
    }
}

pub fn current_task_text(task_summary: &str) -> &str {
    task_summary.trim()
}

#[cfg(test)]
mod tests {
    use super::{current_objective_block, environment_context, resolve_timezone};
    use std::path::{Path, PathBuf};

    #[test]
    fn timezone_prefers_tz_then_localtime_link_then_utc() {
        let link = |path: &str| Some(PathBuf::from(path));
        assert_eq!(resolve_timezone(Some("Asia/Taipei".into()), None), "Asia/Taipei");
        assert_eq!(resolve_timezone(Some(":Asia/Tokyo".into()), None), "Asia/Tokyo");
        assert_eq!(
            resolve_timezone(Some("/usr/share/zoneinfo/America/Chicago".into()), None),
            "America/Chicago"
        );
        assert_eq!(
            resolve_timezone(None, link("/var/db/timezone/zoneinfo/Asia/Taipei")),
            "Asia/Taipei"
        );
        assert_eq!(
            resolve_timezone(Some("  ".into()), link("../usr/share/zoneinfo/Europe/Berlin")),
            "Europe/Berlin"
        );
        assert_eq!(resolve_timezone(None, link("/etc/custom-zone")), "UTC");
        assert_eq!(resolve_timezone(None, None), "UTC");
    }

    #[test]
    fn environment_context_formats_dynamic_values() {
        let content = environment_context(
            Path::new("C:/workspace"),
            "powershell",
            "2026-06-19",
            "Europe/Paris",
            "zh",
        );

        assert!(content.contains("<cwd>C:/workspace</cwd>"));
        assert!(content.contains("<shell>powershell</shell>"));
        assert!(content.contains("<current_date>2026-06-19</current_date>"));
        assert!(content.contains("<timezone>Europe/Paris</timezone>"));
        assert!(content.contains("<system_language>zh</system_language>"));
    }

    #[test]
    fn current_objective_block_keeps_optional_current_task_shape() {
        assert_eq!(
            current_objective_block("ship feature", None),
            "[current objective]:\nship feature"
        );
        assert_eq!(
            current_objective_block("ship feature", Some("patch parser")),
            "[current objective]:\nship feature\n\npatch parser"
        );
    }
}
