//! Opt-in directory discovery. It never grants mutation or an arbitrary shell.
use super::{JSpaceError, JSpaceMatcher};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Component, Path, PathBuf};

fn denied(detail: &str) -> JSpaceError {
    JSpaceError::new("JSPACE_READ_COMMAND_DENIED", "read", "", detail)
}

pub(super) fn validate(value: &Value) -> Result<Vec<String>, JSpaceError> {
    let object = value.as_object().ok_or_else(|| denied("read_commands must be an object"))?;
    if object.len() != 3 || !object.contains_key("roots") || !object.contains_key("rg") || !object.contains_key("cat") {
        return Err(denied("read_commands requires roots and pinned rg/cat"));
    }
    let roots = object["roots"].as_array().ok_or_else(|| denied("roots must be an array"))?;
    if roots.is_empty() || roots.len() > 8 { return Err(denied("require 1..8 roots")); }
    let mut names = Vec::new();
    for root in roots {
        let name = root.as_str().ok_or_else(|| denied("root must be a string"))?;
        if name.is_empty() || name.contains(['*', '?', '[', ']', '\0', '\n', '\r']) || Path::new(name).is_absolute()
            || Path::new(name).components().any(|p| !matches!(p, Component::Normal(_)))
            || Path::new(name).components().any(|p| p.as_os_str().to_string_lossy().starts_with('.')) {
            return Err(denied("roots must be explicit non-hidden relative directories"));
        }
        if names.contains(&name.to_string()) { return Err(denied("duplicate root")); }
        names.push(name.to_string());
    }
    for tool in ["rg", "cat"] {
        let executable = object[tool].as_object().ok_or_else(|| denied("missing executable identity"))?;
        if executable.len() != 2 || executable.get("path").and_then(Value::as_str).is_none()
            || executable.get("sha256").and_then(Value::as_str).is_none() {
            return Err(denied("executable requires path and sha256"));
        }
        let path = executable["path"].as_str().unwrap();
        let sha = executable["sha256"].as_str().unwrap();
        if !Path::new(path).is_absolute() || sha.len() != 64 || !sha.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err(denied("invalid executable identity"));
        }
    }
    Ok(names)
}

fn no_links(root: &Path, path: &Path) -> Result<PathBuf, JSpaceError> {
    let relative = path.strip_prefix(root).map_err(|_| denied("path outside workspace"))?;
    let mut current = root.to_path_buf();
    for part in relative.components() {
        if !matches!(part, Component::Normal(_)) || part.as_os_str().to_string_lossy().starts_with('.') {
            return Err(denied("hidden/traversal operands denied"));
        }
        current.push(part);
        if fs::symlink_metadata(&current).map_err(|_| denied("unavailable operand"))?.file_type().is_symlink() {
            return Err(denied("symlink operand denied"));
        }
    }
    Ok(current)
}

pub(super) fn check(matcher: &JSpaceMatcher, argv: &[String], cwd: &Path) -> Result<(), JSpaceError> {
    matcher.check_operation("read", "discovery")?;
    let policy = matcher.read_commands.as_ref().ok_or_else(|| denied("no discovery grant"))?;
    let tool = ["rg", "cat"].into_iter().find(|tool| policy[*tool]["path"].as_str() == argv.first().map(String::as_str))
        .ok_or_else(|| denied("use the pinned executable, not PATH or a shell alias"))?;
    let executable = Path::new(policy[tool]["path"].as_str().unwrap());
    let bytes = fs::read(executable).map_err(|_| denied("executable unavailable"))?;
    if fs::canonicalize(executable).ok().as_deref() != Some(executable)
        || format!("{:x}", Sha256::digest(bytes)) != policy[tool]["sha256"].as_str().unwrap() {
        return Err(denied("executable identity changed"));
    }
    let mut paths = Vec::new();
    if tool == "cat" {
        let start = if argv.get(1).map(String::as_str) == Some("--") { 2 } else { 1 };
        if argv.len() <= start || argv.len() > start + 16 { return Err(denied("cat requires 1..16 files")); }
        paths.extend(&argv[start..]);
    } else {
        let mut no_config = false;
        let mut bounded_size = false;
        let mut files = false;
        let mut index = 1;
        while index < argv.len() && argv[index] != "--" {
            match argv[index].as_str() {
                "--no-config" => no_config = true,
                "--max-filesize=1M" => bounded_size = true,
                "--files" => files = true,
                "-n" | "--line-number" | "-l" | "--files-with-matches" | "-i" | "--ignore-case"
                | "-F" | "--fixed-strings" | "-S" | "--smart-case" | "--json" | "--no-heading"
                | "--with-filename" | "--color=never" => (),
                "-g" | "--glob" => {
                    index += 1;
                    if index >= argv.len() || argv[index].len() > 256 { return Err(denied("invalid glob")); }
                }
                _ => return Err(denied("unsupported rg flag; explicit -- separator required")),
            }
            index += 1;
        }
        if !no_config || !bounded_size || argv.get(index).map(String::as_str) != Some("--") {
            return Err(denied("rg requires --no-config --max-filesize=1M and --"));
        }
        index += 1;
        if !files {
            if argv.get(index).is_none_or(|pattern| pattern.is_empty() || pattern.len() > 2048) {
                return Err(denied("require a bounded pattern"));
            }
            index += 1;
        }
        if argv.len() <= index || argv.len() > index + 16 { return Err(denied("rg requires explicit search paths")); }
        paths.extend(&argv[index..]);
    }
    let roots = validate(policy)?;
    for operand in paths {
        if operand.starts_with('-') { return Err(denied("stdin/options are not paths")); }
        let path = if Path::new(operand).is_absolute() { PathBuf::from(operand) } else { cwd.join(operand) };
        let path = no_links(&matcher.repo_root, &path)?;
        if !roots.iter().any(|root| path.starts_with(matcher.repo_root.join(root))) {
            return Err(denied("operand outside discovery roots"));
        }
        matcher.check_path("read", &path)?;
        let meta = fs::metadata(&path).map_err(|_| denied("unavailable operand"))?;
        if (!meta.is_file() && !meta.is_dir()) || (tool == "cat" && (!meta.is_file() || meta.len() > 1024 * 1024)) {
            return Err(denied("only bounded regular files may be read"));
        }
    }
    Ok(())
}
