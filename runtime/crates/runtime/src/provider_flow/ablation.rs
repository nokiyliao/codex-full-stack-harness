//! Compiled only into an explicit, non-production read-only benchmark build.
use std::path::Path;

pub(super) fn admitted(directory: &Path, unrestricted: bool) -> Result<bool, String> {
    let Some(root) = std::env::var_os("NOKIY_ABLATION_READONLY_ROOT") else {
        return Ok(false);
    };
    let root = Path::new(&root).canonicalize().map_err(|_| "NOKIY_ABLATION_ROOT_INVALID")?;
    let current = directory.canonicalize().map_err(|_| "NOKIY_ABLATION_ROOT_INVALID")?;
    validate(
        root == current,
        unrestricted,
        crate::router_command_run::command_run_sandbox_enabled(),
        write_denied(&current.join("scripts/ops/dcf/jspace.py"))
            && write_denied(&current.join("scripts/ops/dcf/task_context.py")),
    )?;
    Ok(true)
}

fn validate(same_root: bool, unrestricted: bool, command_sandbox: bool, readonly: bool) -> Result<(), String> {
    if !same_root || unrestricted || !command_sandbox || !readonly {
        return Err("NOKIY_ABLATION_READONLY_SANDBOX_REQUIRED".into());
    }
    Ok(())
}

fn write_denied(path: &Path) -> bool {
    // Open without create/truncate/write: no source bytes change even if allowed.
    // Missing files and other errors must not be mistaken for enforced read-only.
    path.is_file() && matches!(std::fs::OpenOptions::new().write(true).open(path),
        Err(error) if error.kind() == std::io::ErrorKind::PermissionDenied)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_boundary_is_required() {
        assert!(validate(true, false, true, true).is_ok());
        for args in [(false, false, true, true), (true, true, true, true),
                     (true, false, false, true), (true, false, true, false)] {
            assert!(validate(args.0, args.1, args.2, args.3).is_err());
        }
    }

    #[test]
    fn ordinary_unsandboxed_process_is_not_admitted() {
        let file = tempfile::NamedTempFile::new().unwrap();
        assert!(!write_denied(file.path()));
        assert!(!write_denied(&file.path().with_extension("missing")));
    }
}
