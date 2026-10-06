//! Path helpers for the session store.
//!
//! All instance/home/db-path resolution lives in the `tura_path` crate so there
//! is a single source of truth for session_log call sites.

use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};

pub use tura_path::DB_DIR_NAME;

/// The instance's private database directory (see [`tura_path::home_db_dir`]).
pub fn default_db_dir() -> PathBuf {
    tura_path::home_db_dir()
}

/// Find the workspace root by ascending from `start`.
pub fn repo_root_from(start: impl AsRef<Path>) -> Option<PathBuf> {
    tura_path::repo_root_from(start)
}

/// Normalize a workspace directory used as a session key.
pub fn normalize_workspace(directory: &str) -> String {
    tura_path::normalize_workspace(directory)
}

/// Directory that stores the durable session log for a workspace.
///
/// Normally the session log follows the workspace (`<workspace>/.tura`). A
/// bounded Nokiy call keeps its internal database under its per-call state root
/// instead, without changing the logical workspace key.
pub fn workspace_session_log_dir(directory: &str) -> PathBuf {
    let bounded = std::env::var_os("TURA_NOKIY_BOUNDED_ONE_TURN").as_deref()
        == Some(std::ffi::OsStr::new("1"));
    let home = bounded
        .then(|| std::env::var_os("TURA_HOME"))
        .flatten()
        .map(PathBuf::from);
    let db_root = bounded
        .then(|| std::env::var_os("TURA_DB_ROOT"))
        .flatten()
        .map(PathBuf::from);
    workspace_session_log_dir_for(directory, bounded, home.as_deref(), db_root.as_deref())
}

// Accept only the paired per-call bindings: an incomplete bounded call must
// never fall back to creating the workspace-local .tura in read-only source.
fn workspace_session_log_dir_for(
    directory: &str,
    bounded: bool,
    home: Option<&Path>,
    db_root: Option<&Path>,
) -> PathBuf {
    let workspace = normalize_workspace(directory);
    if bounded {
        let (Some(home), Some(db_root)) = (home, db_root) else {
            panic!("bounded Nokiy session log requires TURA_HOME and TURA_DB_ROOT");
        };
        assert!(
            home.is_absolute() && db_root.is_absolute() && home == db_root,
            "bounded Nokiy session log requires matching absolute TURA_HOME and TURA_DB_ROOT"
        );
        // Hash the normalized key into a single fixed-length, path-safe component.
        // This keeps internal database paths bounded even for long workspaces.
        let key = format!("{:x}", Sha256::digest(workspace.as_bytes()));
        return db_root
            .join(DB_DIR_NAME)
            .join("workspaces")
            .join(key)
            .join(".tura");
    }
    if workspace.is_empty() {
        return default_db_dir()
            .join("workspaces")
            .join("_unknown")
            .join(".tura");
    }
    PathBuf::from(workspace).join(".tura")
}

/// SQLite database that stores the full session log for a workspace.
pub fn workspace_session_log_db(directory: &str) -> PathBuf {
    workspace_session_log_dir(directory).join("session_log.sqlite3")
}

/// SQLite database that stores the global session state/index.
pub fn index_db_path() -> PathBuf {
    default_db_dir().join("index.sqlite3")
}

#[cfg(test)]
mod tests {
    use super::{normalize_workspace, workspace_session_log_dir_for};

    #[test]
    fn workspace_session_log_lives_under_workspace_tura_directory() {
        let dir = tempfile::tempdir().expect("tempdir");
        let workspace = dir.path().join("project");
        std::fs::create_dir_all(&workspace).expect("workspace");
        let workspace_text = workspace.display().to_string();

        assert_eq!(
            workspace_session_log_dir_for(&workspace_text, false, None, None),
            workspace.join(".tura")
        );
        assert_eq!(
            workspace_session_log_dir_for(&workspace_text, false, None, None)
                .join("session_log.sqlite3"),
            workspace.join(".tura").join("session_log.sqlite3")
        );
    }

    #[test]
    fn empty_workspace_uses_unknown_workspace_bucket_in_instance_db() {
        let dir = workspace_session_log_dir_for("", false, None, None);

        assert!(
            dir.ends_with(
                std::path::Path::new("workspaces")
                    .join("_unknown")
                    .join(".tura")
            )
        );
        assert!(
            dir.join("session_log.sqlite3").ends_with(
                std::path::Path::new("workspaces")
                    .join("_unknown")
                    .join(".tura")
                    .join("session_log.sqlite3")
            )
        );
    }

    #[test]
    fn normalize_workspace_trims_and_normalizes_separators() {
        let normalized = normalize_workspace("  C:\\Users\\liuliu\\Documents\\tura  ");

        assert!(!normalized.starts_with(' '));
        assert!(!normalized.ends_with(' '));
        assert!(
            normalized.contains('/'),
            "normalized workspace should use stable slash separators: {normalized}"
        );
    }

    #[test]
    fn unbounded_path_still_follows_workspace_even_with_state_bindings() {
        let state = std::path::Path::new("/tmp/one-call-state");
        assert_eq!(
            workspace_session_log_dir_for(" /source/project/ ", false, Some(state), Some(state)),
            std::path::Path::new("/source/project/.tura")
        );
    }

    #[test]
    fn bounded_path_stays_in_state_and_separates_normalized_workspaces() {
        let state = std::path::Path::new("/tmp/one-call-state");
        let dir =
            |workspace| workspace_session_log_dir_for(workspace, true, Some(state), Some(state));
        let project = dir(" /source/project/ ");
        assert_eq!(project, dir("/source\\project"));
        assert_ne!(project, dir("/source/other"));
        assert_ne!(dir(""), dir("_unknown"));
        assert_ne!(dir("/a/b"), dir("/a//b"));
        assert_ne!(dir("../source"), dir("/source"));
        assert!(project.starts_with(state.join(super::DB_DIR_NAME).join("workspaces")));
        assert!(project.ends_with(".tura"));
        assert!(!project.starts_with("/source"));
    }

    #[test]
    fn bounded_path_has_fixed_key_for_long_normalized_workspace() {
        let state = std::path::Path::new("/tmp/one-call-state");
        let root = state.join(super::DB_DIR_NAME).join("workspaces");
        let long = format!("/source/{}project", "segment/".repeat(1024));
        let equivalent = format!("  {}  ", long.replace('/', "\\"));
        let other = format!("{long}-other");
        assert_eq!(normalize_workspace(&long), normalize_workspace(&equivalent));

        let dir = |workspace: &str| {
            workspace_session_log_dir_for(workspace, true, Some(state), Some(state))
        };
        let path = dir(&long).join("session_log.sqlite3");
        let relative = path.strip_prefix(&root).expect("inside state root");
        let key = relative.components().next().expect("workspace key");
        let key = key.as_os_str().to_str().expect("ASCII workspace key");
        assert_eq!(key.len(), 64);
        assert!(key.bytes().all(|byte| byte.is_ascii_hexdigit()));
        assert_eq!(
            relative,
            std::path::Path::new(key)
                .join(".tura")
                .join("session_log.sqlite3")
        );
        assert_eq!(
            relative.as_os_str().len(),
            64 + "/.tura/session_log.sqlite3".len()
        );
        assert_eq!(dir(&long), dir(&equivalent));
        assert_ne!(dir(&long), dir(&other));
    }

    #[test]
    fn bounded_path_rejects_missing_or_mismatched_state_without_workspace_fallback() {
        let state = std::path::Path::new("/tmp/one-call-state");
        for (home, db_root) in [
            (None, None),
            (Some(state), None),
            (None, Some(state)),
            (Some(state), Some(std::path::Path::new("/tmp/other-state"))),
            (
                Some(std::path::Path::new("relative")),
                Some(std::path::Path::new("relative")),
            ),
        ] {
            assert!(
                std::panic::catch_unwind(|| {
                    workspace_session_log_dir_for("/source/project", true, home, db_root)
                })
                .is_err()
            );
        }
    }
}
