use std::ffi::OsStr;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

const TURA_EXCLUDE_LINES: &[&str] = &[".tura/", "sessions/"];

pub fn ensure_workspace_git_repo(workspace: impl AsRef<Path>) -> Result<(), String> {
    let bounded_mode = std::env::var_os("TURA_NOKIY_BOUNDED_ONE_TURN");
    ensure_workspace_git_repo_with_mode(workspace.as_ref(), bounded_mode.as_deref())
}

fn ensure_workspace_git_repo_with_mode(
    workspace: &Path,
    bounded_mode: Option<&OsStr>,
) -> Result<(), String> {
    if workspace.as_os_str().is_empty() {
        return Err("workspace path is empty".to_string());
    }
    // Bounded startup validates only; it must not bootstrap workspace Git state.
    if bounded_mode == Some(OsStr::new("1")) {
        let metadata = fs::metadata(workspace).map_err(|error| {
            format!(
                "failed to inspect workspace directory {}: {error}",
                workspace.display()
            )
        })?;
        if !metadata.is_dir() {
            return Err(format!(
                "workspace path is not a directory: {}",
                workspace.display()
            ));
        }
        return Ok(());
    }
    fs::create_dir_all(workspace).map_err(|error| {
        format!(
            "failed to create workspace directory {}: {error}",
            workspace.display()
        )
    })?;

    if !workspace.join(".git").exists() && run_git(workspace, &["init"]).is_err() {
        return Ok(());
    }
    let _ = ensure_tura_git_exclude(workspace);
    Ok(())
}

fn ensure_tura_git_exclude(workspace: &Path) -> Result<(), String> {
    let output = run_git(workspace, &["rev-parse", "--git-path", "info/exclude"])?;
    let raw_path = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if raw_path.is_empty() {
        return Ok(());
    }
    let exclude_path = if Path::new(&raw_path).is_absolute() {
        PathBuf::from(raw_path)
    } else {
        workspace.join(raw_path)
    };
    if let Some(parent) = exclude_path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            format!(
                "failed to create git exclude directory {}: {error}",
                parent.display()
            )
        })?;
    }
    let existing = fs::read_to_string(&exclude_path).unwrap_or_default();
    let existing_lines = existing.lines().map(str::trim).collect::<Vec<_>>();
    let missing = TURA_EXCLUDE_LINES
        .iter()
        .copied()
        .filter(|line| !existing_lines.iter().any(|existing| existing == line))
        .collect::<Vec<_>>();
    if missing.is_empty() {
        return Ok(());
    }
    let mut updated = existing;
    if !updated.is_empty() && !updated.ends_with('\n') {
        updated.push('\n');
    }
    for line in missing {
        updated.push_str(line);
        updated.push('\n');
    }
    fs::write(&exclude_path, updated).map_err(|error| {
        format!(
            "failed to update git exclude {}: {error}",
            exclude_path.display()
        )
    })
}

fn run_git(workspace: &Path, args: &[&str]) -> Result<Output, String> {
    let mut command = Command::new("git");
    command.arg("-C").arg(workspace).args(args);
    crate::process_hardening::hide_child_console_window(&mut command);
    let output = command
        .output()
        .map_err(|error| format!("failed to run git in {}: {error}", workspace.display()))?;
    if output.status.success() {
        return Ok(output);
    }
    Err(format!(
        "git -C {} {} failed with status {}\nstdout:\n{}\nstderr:\n{}",
        workspace.display(),
        args.join(" "),
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    ))
}

#[cfg(test)]
mod tests {
    use super::{ensure_workspace_git_repo_with_mode, run_git};
    use std::collections::BTreeMap;
    use std::ffi::OsStr;
    use std::fs;
    use std::path::{Path, PathBuf};

    fn workspace_snapshot(workspace: &Path) -> BTreeMap<PathBuf, Option<Vec<u8>>> {
        fn collect(
            root: &Path,
            directory: &Path,
            snapshot: &mut BTreeMap<PathBuf, Option<Vec<u8>>>,
        ) {
            for entry in fs::read_dir(directory).expect("read workspace directory") {
                let path = entry.expect("workspace entry").path();
                let relative = path.strip_prefix(root).expect("workspace path").to_path_buf();
                if path.is_dir() {
                    snapshot.insert(relative, None);
                    collect(root, &path, snapshot);
                } else {
                    snapshot.insert(relative, Some(fs::read(path).expect("read workspace file")));
                }
            }
        }

        let mut snapshot = BTreeMap::new();
        collect(workspace, workspace, &mut snapshot);
        snapshot
    }

    #[test]
    fn initializes_repository_and_excludes_runtime_state() {
        for mode in [
            None,
            Some(""),
            Some("0"),
            Some("true"),
            Some("01"),
            Some(" 1"),
            Some("1 "),
            Some("1\n"),
        ] {
            let temp = tempfile::tempdir().expect("temp workspace");
            let workspace = temp.path().join("missing").join("workspace");

            ensure_workspace_git_repo_with_mode(&workspace, mode.map(OsStr::new))
                .expect("workspace should initialize");

            assert!(workspace.join(".git").exists(), "mode: {mode:?}");
            let exclude = fs::read_to_string(workspace.join(".git/info/exclude"))
                .expect("git exclude should exist");
            assert!(exclude.lines().any(|line| line.trim() == ".tura/"));
            assert!(exclude.lines().any(|line| line.trim() == "sessions/"));
        }
    }

    #[test]
    fn bounded_mode_leaves_existing_non_git_workspace_unchanged() {
        let temp = tempfile::tempdir().expect("temp workspace");
        fs::create_dir(temp.path().join("src")).expect("source directory");
        fs::write(temp.path().join("src/main.rs"), b"fn main() {}\n").expect("source file");
        let before = workspace_snapshot(temp.path());

        ensure_workspace_git_repo_with_mode(temp.path(), Some(OsStr::new("1")))
            .expect("existing directory should be usable without Git");

        assert!(!temp.path().join(".git").exists());
        assert_eq!(workspace_snapshot(temp.path()), before);
    }

    #[test]
    fn bounded_mode_leaves_existing_git_metadata_unchanged() {
        let temp = tempfile::tempdir().expect("temp workspace");
        run_git(temp.path(), &["init"]).expect("initialize test repository");
        fs::write(temp.path().join(".git/info/exclude"), b"# retain exactly\n")
            .expect("seed Git excludes without runtime entries");
        let before = workspace_snapshot(temp.path());

        ensure_workspace_git_repo_with_mode(temp.path(), Some(OsStr::new("1")))
            .expect("existing Git workspace should be usable");

        assert_eq!(workspace_snapshot(temp.path()), before);
    }

    #[test]
    fn bounded_mode_rejects_missing_workspace_without_creating_directories() {
        let temp = tempfile::tempdir().expect("temp workspace");
        let workspace = temp.path().join("missing").join("workspace");
        let before = workspace_snapshot(temp.path());

        let error = ensure_workspace_git_repo_with_mode(&workspace, Some(OsStr::new("1")))
            .expect_err("missing workspace should fail");

        assert!(error.starts_with("failed to inspect workspace directory "));
        assert_eq!(workspace_snapshot(temp.path()), before);
    }

    #[test]
    fn bounded_mode_rejects_file_workspace_without_changes() {
        let temp = tempfile::tempdir().expect("temp workspace");
        let workspace = temp.path().join("workspace");
        fs::write(&workspace, b"not a directory").expect("workspace file");
        let before = workspace_snapshot(temp.path());

        let error = ensure_workspace_git_repo_with_mode(&workspace, Some(OsStr::new("1")))
            .expect_err("file workspace should fail");

        assert_eq!(
            error,
            format!("workspace path is not a directory: {}", workspace.display())
        );
        assert_eq!(workspace_snapshot(temp.path()), before);
    }

    #[test]
    fn rejects_empty_workspace_path() {
        for mode in [None, Some(OsStr::new("1"))] {
            assert_eq!(
                ensure_workspace_git_repo_with_mode(Path::new(""), mode)
                    .expect_err("empty path should fail"),
                "workspace path is empty"
            );
        }
    }
}
