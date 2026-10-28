//! Applies the caller-bound inspect Seatbelt profile to every shell this process starts.
//! J-Space admits an inspect shell line only while the same profile is bound here, so a
//! bound profile that cannot be applied refuses execution instead of running unsandboxed.
use std::path::PathBuf;
use tura_path::jspace::inspect_shell::PROFILE_ENV;

const SANDBOX_EXEC: &str = "/usr/bin/sandbox-exec";

pub(super) struct InspectSandbox {
    profile: PathBuf,
    scratch: Option<PathBuf>,
    /// The caller already runs this whole process tree under the profile (Seatbelt cannot nest).
    inherited: bool,
}

/// None when no inspect profile is bound; Err when one is bound but cannot be applied.
pub(super) fn bound() -> Result<Option<InspectSandbox>, String> {
    let Some(profile) = std::env::var_os(PROFILE_ENV) else {
        return Ok(None);
    };
    let profile = PathBuf::from(profile);
    if !cfg!(target_os = "macos") || !std::path::Path::new(SANDBOX_EXEC).is_file() {
        return Err("inspect sandbox unavailable: Seatbelt sandbox-exec is required".into());
    }
    if !profile.is_absolute() || !profile.is_file() {
        return Err("inspect sandbox profile is unavailable".into());
    }
    let scratch = profile.parent().map(|parent| parent.join("scratch")).filter(|path| path.is_dir());
    // A caller binds the profile in an already sandboxed engine only after starting that engine
    // under the same profile; otherwise this process applies it to each shell itself.
    let inherited = if super::focused_verifier_sandbox_available() {
        false
    } else if super::focused_verifier::inherited_seatbelt_active() {
        true
    } else {
        return Err("inspect sandbox unavailable: Seatbelt cannot be applied here".into());
    };
    Ok(Some(InspectSandbox { profile, scratch, inherited }))
}

impl InspectSandbox {
    pub(super) fn wrap(&self, inner: std::process::Command) -> Result<std::process::Command, String> {
        if self.inherited {
            return Ok(inner);
        }
        let program = self.program(&inner)?;
        let mut command = std::process::Command::new(SANDBOX_EXEC);
        command.arg("-f").arg(&self.profile).arg(program).args(inner.get_args());
        Ok(command)
    }

    pub(super) fn wrap_async(&self, inner: tokio::process::Command) -> Result<tokio::process::Command, String> {
        if self.inherited {
            return Ok(inner);
        }
        let program = self.program(inner.as_std())?;
        let mut command = tokio::process::Command::new(SANDBOX_EXEC);
        command.arg("-f").arg(&self.profile).arg(program).args(inner.as_std().get_args());
        Ok(command)
    }

    fn program(&self, inner: &std::process::Command) -> Result<std::ffi::OsString, String> {
        let program = inner.get_program().to_os_string();
        if std::path::Path::new(&program).file_name() == Some("docker".as_ref()) {
            return Err("inspect sandbox does not run container shells".into());
        }
        Ok(program)
    }

    /// Temporary files go to the bound scratch directory; tools must not write caches.
    pub(super) fn environment(&self) -> Vec<(&'static str, std::ffi::OsString)> {
        let mut env = vec![("PYTHONDONTWRITEBYTECODE", "1".into()), ("GIT_OPTIONAL_LOCKS", "0".into())];
        if let Some(scratch) = &self.scratch {
            env.push(("TMPDIR", scratch.clone().into_os_string()));
        }
        env
    }
}

