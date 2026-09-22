//! Offline producer/consumer verifier; no model call or filesystem mutation.
use std::io::{self, Read};
use runtime_contract::{TaskContextCapsule, task_context_canonical_json_v1};
fn main() {
    if let Err(error) = run() { eprintln!("{error}"); std::process::exit(1); }
}
fn run() -> Result<(), String> {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input).map_err(|e| e.to_string())?;
    let value: serde_json::Value = serde_json::from_str(&input).map_err(|e| e.to_string())?;
    if std::env::args().any(|arg| arg == "--canonical") {
        println!("{}", task_context_canonical_json_v1(&value)?);
    } else {
        let capsule = TaskContextCapsule::from_value(value.clone())?;
        let mut payload = value;
        payload.as_object_mut().ok_or("capsule object required")?.remove("semantic_sha256");
        println!("{}", serde_json::json!({"capsule_sha256": capsule.semantic_sha256,
            "canonical_payload": task_context_canonical_json_v1(&payload)?,
            "provider_context": capsule.provider_context()}));
    }
    Ok(())
}
