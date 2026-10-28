//! Parent-executed read-only shell for worker actions that may also edit the repository.
//! A writing worker's engine stays inside its signal fence and Seatbelt cannot nest, so no
//! per-shell profile can be applied inside the engine. J-Space therefore admits an arbitrary
//! shell line only for execution by the parent over the inherited verifier channel, which
//! applies the caller-written profile this policy binds by digest. Repository edits stay
//! apply_patch-only, so file-change evidence keeps a single source.
use super::JSpaceError;
use serde_json::Value;
use std::path::Path;

pub const POLICY_SCHEMA: &str = "nokiy_parent_shell_v1";

fn denied(detail: &str) -> JSpaceError {
    JSpaceError::new("JSPACE_PARENT_SHELL_DENIED", "command", "parent_shell", detail)
}

pub(super) fn validate(value: &Value) -> Result<(), JSpaceError> {
    let object = value
        .as_object()
        .ok_or_else(|| denied("parent_shell must be an object"))?;
    let mut keys = object.keys().map(String::as_str).collect::<Vec<_>>();
    keys.sort_unstable();
    if keys != ["evidence_root", "network", "profile_sha256", "schema_version"] {
        return Err(denied(
            "parent_shell requires schema_version, profile_sha256, evidence_root and network",
        ));
    }
    if object["schema_version"].as_str() != Some(POLICY_SCHEMA) {
        return Err(denied("unsupported parent_shell schema"));
    }
    let digest = object["profile_sha256"].as_str().unwrap_or_default();
    if digest.len() != 64 || !digest.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')) {
        return Err(denied("profile_sha256 must be a lowercase SHA-256"));
    }
    if !Path::new(object["evidence_root"].as_str().unwrap_or_default()).is_absolute() {
        return Err(denied("evidence_root must be an absolute path"));
    }
    if !matches!(object["network"].as_str(), Some("inherit" | "none")) {
        return Err(denied("network must be inherit or none"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn policy() -> Value {
        json!({"schema_version": POLICY_SCHEMA, "profile_sha256": "a".repeat(64),
            "evidence_root": "/tmp/evidence", "network": "inherit"})
    }

    #[test]
    fn policy_shape_is_exact() {
        validate(&policy()).expect("valid policy");
        let mut cases = vec![json!(null), json!([]), json!({})];
        for (key, value) in [("schema_version", json!("nokiy_inspect_shell_v1")),
            ("profile_sha256", json!("A".repeat(64))), ("profile_sha256", json!("a".repeat(63))),
            ("evidence_root", json!("relative")), ("network", json!("loopback")),
            ("network", json!(true)), ("extra", json!(1))] {
            let mut changed = policy();
            changed[key] = value;
            cases.push(changed);
        }
        for case in cases {
            assert_eq!(validate(&case).expect_err("rejected").code(), "JSPACE_PARENT_SHELL_DENIED", "{case}");
        }
        let mut offline = policy();
        offline["network"] = json!("none");
        validate(&offline).expect("an offline parent shell is valid");
    }
}
