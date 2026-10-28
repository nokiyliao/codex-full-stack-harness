//! Opt-in directory discovery. It never grants mutation or an arbitrary shell.
use super::{JSpaceError, JSpaceMatcher};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Component, Path, PathBuf};

fn denied(detail: impl Into<String>) -> JSpaceError {
    JSpaceError::new("JSPACE_READ_COMMAND_DENIED", "read", "", detail)
}

/// A refusal about one token or operand names it as the target, so the next command can fix
/// exactly that argument instead of guessing.
fn denied_operand(operand: &str, detail: impl Into<String>) -> JSpaceError {
    JSpaceError::new(
        "JSPACE_READ_COMMAND_DENIED",
        "read",
        &shown(operand),
        detail,
    )
}

/// An echoed argument is bounded so a refusal never repeats a long pattern or path.
fn shown(text: &str) -> String {
    const MAX_SHOWN_BYTES: usize = 160;
    if text.len() <= MAX_SHOWN_BYTES {
        return text.to_string();
    }
    let mut end = MAX_SHOWN_BYTES;
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    format!("{}...", &text[..end])
}

/// A decimal context, match or depth count up to 10000, as rg's -A/-B/-C/-m/-d take.
fn bounded_count(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 5
        && value.bytes().all(|byte| byte.is_ascii_digit())
        && value.parse::<u32>().is_ok_and(|count| count <= 10_000)
}

pub(super) fn validate(value: &Value) -> Result<Vec<String>, JSpaceError> {
    let object = value
        .as_object()
        .ok_or_else(|| denied("read_commands must be an object"))?;
    if object.len() != 3
        || !object.contains_key("roots")
        || !object.contains_key("rg")
        || !object.contains_key("cat")
    {
        return Err(denied("read_commands requires roots and pinned rg/cat"));
    }
    let roots = object["roots"]
        .as_array()
        .ok_or_else(|| denied("roots must be an array"))?;
    if roots.is_empty() || roots.len() > 8 {
        return Err(denied("require 1..8 roots"));
    }
    let mut names = Vec::new();
    for root in roots {
        let name = root
            .as_str()
            .ok_or_else(|| denied("root must be a string"))?;
        if name.is_empty()
            || name.contains(['*', '?', '[', ']', '\0', '\n', '\r'])
            || Path::new(name).is_absolute()
            || Path::new(name)
                .components()
                .any(|p| !matches!(p, Component::Normal(_)))
            || Path::new(name)
                .components()
                .any(|p| p.as_os_str().to_string_lossy().starts_with('.'))
        {
            return Err(denied(
                "roots must be explicit non-hidden relative directories",
            ));
        }
        if names.contains(&name.to_string()) {
            return Err(denied("duplicate root"));
        }
        names.push(name.to_string());
    }
    for tool in ["rg", "cat"] {
        let executable = object[tool]
            .as_object()
            .ok_or_else(|| denied("missing executable identity"))?;
        let (2, Some(path), Some(sha)) = (
            executable.len(),
            executable.get("path").and_then(Value::as_str),
            executable.get("sha256").and_then(Value::as_str),
        ) else {
            return Err(denied("executable requires path and sha256"));
        };
        if !Path::new(path).is_absolute()
            || sha.len() != 64
            || !sha.bytes().all(|b| b.is_ascii_hexdigit())
        {
            return Err(denied("invalid executable identity"));
        }
    }
    Ok(names)
}

/// The operand relative to the workspace, checked lexically: no filesystem access happens
/// before the operand is known to stay inside the workspace without hidden or traversal parts.
fn lexical_operand(root: &Path, path: &Path, operand: &str) -> Result<PathBuf, JSpaceError> {
    let relative = path
        .strip_prefix(root)
        .map_err(|_| denied_operand(operand, "path outside workspace"))?;
    if relative.components().any(|part| {
        !matches!(part, Component::Normal(_)) || part.as_os_str().to_string_lossy().starts_with('.')
    }) {
        return Err(denied_operand(operand, "hidden/traversal operands denied"));
    }
    Ok(relative.to_path_buf())
}

fn no_links(root: &Path, relative: &Path, operand: &str) -> Result<PathBuf, JSpaceError> {
    let mut current = root.to_path_buf();
    for part in relative.components() {
        current.push(part);
        let metadata = fs::symlink_metadata(&current).map_err(|error| {
            // Name the first missing component so a mistyped directory is fixed in one step.
            let missing = current
                .strip_prefix(root)
                .unwrap_or(&current)
                .display()
                .to_string();
            let reason = if error.kind() == std::io::ErrorKind::NotFound {
                "does not exist"
            } else {
                "cannot be inspected"
            };
            denied_operand(
                operand,
                format!("unavailable operand: {} {reason}", shown(&missing)),
            )
        })?;
        if metadata.file_type().is_symlink() {
            return Err(denied_operand(operand, "symlink operand denied"));
        }
    }
    Ok(current)
}

pub(super) fn check_file(matcher: &JSpaceMatcher, target: &Path) -> Result<(), JSpaceError> {
    let policy = matcher
        .read_commands
        .as_ref()
        .ok_or_else(|| denied("no discovery grant"))?;
    let executable = policy["cat"]["path"]
        .as_str()
        .ok_or_else(|| denied("missing executable identity"))?;
    let operand = target
        .to_str()
        .ok_or_else(|| denied("path must be UTF-8"))?;
    // Authorization only: reuse the pinned cat boundary without executing it.
    check(
        matcher,
        &[executable.to_owned(), "--".to_owned(), operand.to_owned()],
        &matcher.repo_root,
    )
}

pub(super) fn check(
    matcher: &JSpaceMatcher,
    argv: &[String],
    cwd: &Path,
) -> Result<(), JSpaceError> {
    matcher.check_operation("read", "discovery")?;
    let policy = matcher
        .read_commands
        .as_ref()
        .ok_or_else(|| denied("no discovery grant"))?;
    let program = argv.first().map(String::as_str).unwrap_or("");
    let tool = ["rg", "cat"]
        .into_iter()
        .find(|tool| policy[*tool]["path"].as_str() == Some(program))
        .ok_or_else(|| match Path::new(program).file_name().and_then(|name| name.to_str()) {
            Some(name @ ("rg" | "cat")) => denied_operand(
                program,
                format!(
                    "use the pinned executable {}, not PATH or a shell alias",
                    policy[name]["path"].as_str().unwrap_or("<missing>")
                ),
            ),
            _ => denied_operand(
                program,
                "discovery runs only the pinned rg and cat; tests and other programs run only as declared verifier commands",
            ),
        })?;
    let (Some(path), Some(expected_sha)) =
        (policy[tool]["path"].as_str(), policy[tool]["sha256"].as_str())
    else {
        return Err(denied("missing executable identity"));
    };
    let executable = Path::new(path);
    let bytes = fs::read(executable).map_err(|_| denied("executable unavailable"))?;
    if fs::canonicalize(executable).ok().as_deref() != Some(executable)
        || format!("{:x}", Sha256::digest(bytes)) != expected_sha
    {
        return Err(denied("executable identity changed"));
    }
    let mut paths = Vec::new();
    if tool == "cat" {
        let start = if argv.get(1).map(String::as_str) == Some("--") {
            2
        } else {
            1
        };
        if argv.len() <= start || argv.len() > start + 16 {
            return Err(denied("cat requires 1..16 files"));
        }
        paths.extend(&argv[start..]);
    } else {
        let mut no_config = false;
        let mut bounded_size = false;
        let mut files = false;
        let mut pattern_bytes = 0;
        let mut index = 1;
        while index < argv.len() && argv[index] != "--" {
            match argv[index].as_str() {
                "--no-config" => no_config = true,
                "--max-filesize=1M" => bounded_size = true,
                "--files" => files = true,
                // Matching, output and narrowing options only (-g, -d): none widens which files
                // are read or runs another program. Widening file selection (--hidden,
                // --no-ignore, -L, --follow, -z, --pre) and file pattern sources (-f) stay refused.
                "-n"
                | "--line-number"
                | "-N"
                | "--no-line-number"
                | "-l"
                | "--files-with-matches"
                | "--files-without-match"
                | "-c"
                | "--count"
                | "--count-matches"
                | "-o"
                | "--only-matching"
                | "-i"
                | "--ignore-case"
                | "-F"
                | "--fixed-strings"
                | "-S"
                | "--smart-case"
                | "-w"
                | "--word-regexp"
                | "-x"
                | "--line-regexp"
                | "-v"
                | "--invert-match"
                | "-q"
                | "--quiet"
                | "--json"
                | "--no-heading"
                | "-H"
                | "--with-filename"
                | "-I"
                | "--no-filename"
                | "--column"
                | "-b"
                | "--byte-offset"
                | "--trim"
                | "--color=never" => (),
                "-A" | "-B" | "-C" | "-m" | "-d" | "--after-context" | "--before-context"
                | "--context" | "--max-count" | "--max-depth" => {
                    index += 1;
                    if !argv.get(index).is_some_and(|value| bounded_count(value)) {
                        return Err(denied_operand(
                            &argv[index - 1],
                            "context, count and depth options take a number 0..10000",
                        ));
                    }
                }
                flag if [
                    "--after-context=",
                    "--before-context=",
                    "--context=",
                    "--max-count=",
                    "--max-depth=",
                ]
                .iter()
                .any(|prefix| flag.strip_prefix(prefix).is_some_and(bounded_count)) =>
                {}
                "-g" | "--glob" => {
                    index += 1;
                    if index >= argv.len() || argv[index].len() > 256 {
                        return Err(denied_operand(
                            &argv[index - 1],
                            "-g/--glob takes a glob of at most 256 bytes",
                        ));
                    }
                }
                "-e" | "--regexp" => {
                    index += 1;
                    let Some(pattern) = argv.get(index) else {
                        return Err(denied_operand(&argv[index - 1], "pattern option requires a value"));
                    };
                    pattern_bytes += pattern.len();
                    if pattern.is_empty() || pattern_bytes > 2048 {
                        return Err(denied_operand(
                            &argv[index - 1],
                            "require nonempty patterns totaling at most 2048 bytes",
                        ));
                    }
                }
                other => {
                    return Err(denied_operand(
                        other,
                        "unsupported rg option before --; allowed: match and output options, \
                         -A/-B/-C/-m/-d N and -g GLOB, then -- <pattern> <paths>, \
                         or -e/--regexp <pattern> before -- <paths>",
                    ));
                }
            }
            index += 1;
        }
        if !no_config || !bounded_size || argv.get(index).map(String::as_str) != Some("--") {
            return Err(denied("rg requires --no-config --max-filesize=1M and --"));
        }
        index += 1;
        if files && pattern_bytes > 0 {
            return Err(denied("--files cannot be combined with pattern options"));
        }
        if !files && pattern_bytes == 0 {
            if argv
                .get(index)
                .is_none_or(|pattern| pattern.is_empty() || pattern.len() > 2048)
            {
                return Err(denied("require a bounded pattern"));
            }
            index += 1;
        }
        if argv.len() <= index || argv.len() > index + 16 {
            return Err(denied("rg requires explicit search paths"));
        }
        paths.extend(&argv[index..]);
    }
    let roots = validate(policy)?;
    for operand in paths {
        if operand.starts_with('-') {
            return Err(denied_operand(operand, "stdin/options are not paths"));
        }
        let path = if Path::new(operand).is_absolute() {
            PathBuf::from(operand)
        } else {
            cwd.join(operand)
        };
        let relative = lexical_operand(&matcher.repo_root, &path, operand)?;
        if !roots.iter().any(|root| relative.starts_with(root)) {
            return Err(denied_operand(
                operand,
                format!("operand outside discovery roots {roots:?}"),
            ));
        }
        let path = no_links(&matcher.repo_root, &relative, operand)?;
        matcher.check_path("read", &path)?;
        let meta =
            fs::metadata(&path).map_err(|_| denied_operand(operand, "unavailable operand"))?;
        if !meta.is_file() && !meta.is_dir() {
            return Err(denied_operand(
                operand,
                "only regular files and directories may be read",
            ));
        }
        if tool == "cat" && meta.is_dir() {
            return Err(denied_operand(
                operand,
                "cat reads files; list a directory with rg --files",
            ));
        }
        if tool == "cat" && meta.len() > 1024 * 1024 {
            return Err(denied_operand(operand, "cat reads files up to 1 MiB"));
        }
    }
    Ok(())
}
