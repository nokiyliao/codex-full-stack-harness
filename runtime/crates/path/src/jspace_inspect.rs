//! Opt-in OS-sandboxed inspection shell for read-and-record worker actions.
//! J-Space admits an arbitrary shell line only while this process carries the
//! caller-written Seatbelt profile that the policy binds by digest; the shell
//! executor applies that same profile to every shell it starts.
use super::JSpaceError;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::ffi::OsString;
use std::path::Path;

pub const POLICY_SCHEMA: &str = "nokiy_inspect_shell_v1";
/// Seatbelt profile the shell executor applies to every shell in this process.
pub const PROFILE_ENV: &str = "TURA_INSPECT_SANDBOX_PROFILE";

fn denied(detail: &str) -> JSpaceError {
    JSpaceError::new("JSPACE_INSPECT_DENIED", "command", "inspect_shell", detail)
}

pub(super) fn validate(value: &Value) -> Result<(), JSpaceError> {
    let object = value
        .as_object()
        .ok_or_else(|| denied("inspect_shell must be an object"))?;
    let mut keys = object.keys().map(String::as_str).collect::<Vec<_>>();
    keys.sort_unstable();
    if keys != ["evidence_root", "network", "profile_sha256", "schema_version"] {
        return Err(denied(
            "inspect_shell requires schema_version, profile_sha256, evidence_root and network",
        ));
    }
    if object["schema_version"].as_str() != Some(POLICY_SCHEMA) {
        return Err(denied("unsupported inspect_shell schema"));
    }
    let digest = object["profile_sha256"].as_str().unwrap_or_default();
    if digest.len() != 64 || !digest.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')) {
        return Err(denied("profile_sha256 must be a lowercase SHA-256"));
    }
    if !Path::new(object["evidence_root"].as_str().unwrap_or_default()).is_absolute() {
        return Err(denied("evidence_root must be an absolute path"));
    }
    // "inherit": the caller ran the whole engine under the profile, so shells share its network.
    if !matches!(object["network"].as_str(), Some("loopback" | "none" | "inherit")) {
        return Err(denied("network must be loopback, none or inherit"));
    }
    Ok(())
}

/// Admission holds only where the shell executor will apply the bound profile.
pub(super) fn check_binding(value: &Value) -> Result<(), JSpaceError> {
    check_profile(value, std::env::var_os(PROFILE_ENV))
}

fn check_profile(value: &Value, profile: Option<OsString>) -> Result<(), JSpaceError> {
    let expected = value
        .get("profile_sha256")
        .and_then(Value::as_str)
        .ok_or_else(|| denied("inspect_shell policy is incomplete"))?;
    let path = profile.ok_or_else(|| denied("inspect sandbox profile is not bound to this process"))?;
    let bytes = std::fs::read(&path).map_err(|_| denied("inspect sandbox profile is unavailable"))?;
    if format!("{:x}", Sha256::digest(&bytes)) != expected {
        return Err(denied("inspect sandbox profile digest does not match the contract"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn policy(digest: &str) -> Value {
        json!({"schema_version": POLICY_SCHEMA, "profile_sha256": digest,
            "evidence_root": "/tmp/evidence", "network": "loopback"})
    }

    #[test]
    fn policy_shape_is_exact() {
        let digest = "a".repeat(64);
        validate(&policy(&digest)).expect("valid policy");
        let mut cases = vec![json!(null), json!([]), json!({})];
        for (key, value) in [("schema_version", json!("v0")), ("profile_sha256", json!("A".repeat(64))),
            ("profile_sha256", json!("a".repeat(63))), ("evidence_root", json!("relative/evidence")),
            ("network", json!("internet")), ("network", json!(true)), ("extra", json!(1))] {
            let mut changed = policy(&digest);
            changed[key] = value;
            cases.push(changed);
        }
        for case in cases {
            assert_eq!(validate(&case).expect_err("rejected").code(), "JSPACE_INSPECT_DENIED", "{case}");
        }
        let mut inherited = policy(&digest);
        inherited["network"] = json!("inherit");
        validate(&inherited).expect("an engine-fence policy inherits the runtime network");
    }

    #[test]
    fn admission_requires_the_bound_profile_bytes() {
        let temp = tempfile::tempdir().expect("tempdir");
        let profile = temp.path().join("inspect.sb");
        std::fs::write(&profile, "(version 1)\n(deny default)\n").expect("profile");
        let digest = format!("{:x}", Sha256::digest(std::fs::read(&profile).expect("read")));
        check_profile(&policy(&digest), Some(profile.clone().into())).expect("bound profile");
        for (value, path) in [(policy(&digest), None), (policy(&"b".repeat(64)), Some(profile.clone().into())),
            (policy(&digest), Some(temp.path().join("missing.sb").into()))] {
            assert_eq!(check_profile(&value, path).expect_err("unbound").code(), "JSPACE_INSPECT_DENIED");
        }
    }
}

