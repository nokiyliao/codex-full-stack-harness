//! Advisory per-call pacing under the existing parent's hard execution deadline.

use serde::Deserialize;
use serde_json::{Value, json};

pub(crate) const ENV: &str = "NOKIY_EXECUTION_BUDGET";
const SCHEMA: &str = "nokiy_execution_budget_v1";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Binding {
    schema_version: String,
    request_sha256: String,
    timeout_ms: u64,
    deadline_unix_ms: i64,
    reserve_ms: u64,
}

pub(crate) struct ExecutionBudget {
    remaining_ms: u64,
    reserve_ms: u64,
    timeout_ms: u64,
}

impl ExecutionBudget {
    pub(crate) fn observe(
        admitted: bool,
        session_id: &str,
        raw: Option<&str>,
        now_ms: i64,
    ) -> Result<Option<Self>, String> {
        // An inherited hint does not opt ordinary or native-Codex tasks in.
        if !admitted {
            return Ok(None);
        }
        let Some(raw) = raw else { return Ok(None) };
        if raw.len() > 1024 {
            return Err("NOKIY_EXECUTION_BUDGET_INVALID".into());
        }
        let binding: Binding =
            serde_json::from_str(raw).map_err(|_| "NOKIY_EXECUTION_BUDGET_INVALID".to_string())?;
        let remaining = binding.deadline_unix_ms.saturating_sub(now_ms).max(0) as u64;
        if binding.schema_version != SCHEMA
            || binding.request_sha256.len() != 64
            || !binding
                .request_sha256
                .bytes()
                .all(|b| b.is_ascii_hexdigit())
            || session_id != format!("full-{}", binding.request_sha256)
            || !(10_000..=900_000).contains(&binding.timeout_ms)
            || binding.reserve_ms != (binding.timeout_ms / 10).min(10_000)
            || remaining > binding.timeout_ms
        {
            return Err("NOKIY_EXECUTION_BUDGET_INVALID".into());
        }
        if remaining <= binding.reserve_ms {
            return Err(format!(
                "NOKIY_EXECUTION_BUDGET_EXHAUSTED: {remaining} ms left; no new provider call; reconcile existing effects without replay"
            ));
        }
        Ok(Some(Self {
            remaining_ms: remaining,
            reserve_ms: binding.reserve_ms,
            timeout_ms: binding.timeout_ms,
        }))
    }

    pub(crate) fn provider_timeout_ms(&self, configured_ms: u64) -> u64 {
        configured_ms
            .max(1000)
            .min(self.remaining_ms - self.reserve_ms)
    }

    pub(crate) fn observation(&self) -> Value {
        json!({"schema_version": SCHEMA, "remaining_ms": self.remaining_ms,
            "reserve_ms": self.reserve_ms, "timeout_ms": self.timeout_ms,
            "handoff_requested": self.remaining_ms <= (self.timeout_ms / 4).min(60_000)})
    }

    pub(crate) fn message(&self) -> Value {
        let phase = if self.observation()["handoff_requested"] == true {
            "Close out now with a concise result or partial handoff. Do not start optional work."
        } else {
            "Fit the smallest useful action and its concise handoff inside the remaining budget."
        };
        json!({"role": "developer", "content": format!(
            "Nokiy execution budget: {} seconds remain in this entire worker action; {} seconds are reserved for durable closeout. This budget never resets between tool/model calls. {phase} Preserve required validation; report unfinished work and unverified effects explicitly rather than claiming completion. Do not replay effects. The parent owns continuation and acceptance.",
            self.remaining_ms / 1000, self.reserve_ms / 1000)})
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const ID: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    fn binding() -> Value {
        json!({"schema_version": SCHEMA, "request_sha256": ID, "timeout_ms": 360_000,
            "deadline_unix_ms": 1_360_000, "reserve_ms": 10_000})
    }

    fn observe(value: Value, now: i64) -> Result<Option<ExecutionBudget>, String> {
        ExecutionBudget::observe(true, &format!("full-{ID}"), Some(&value.to_string()), now)
    }

    #[test]
    fn same_deadline_decreases_across_calls_and_reserves_closeout() {
        let first = observe(binding(), 1_000_000).unwrap().unwrap();
        let later = observe(binding(), 1_300_000).unwrap().unwrap();
        assert_eq!(first.provider_timeout_ms(960_000), 350_000);
        assert_eq!(later.provider_timeout_ms(960_000), 50_000);
        assert_eq!(later.provider_timeout_ms(25_000), 25_000);
        assert_eq!(later.observation()["handoff_requested"], true);
        assert_eq!(first.observation()["handoff_requested"], false);
        assert!(
            later.message()["content"]
                .as_str()
                .unwrap()
                .contains("partial handoff")
        );
    }

    #[test]
    fn original_five_second_remainder_cannot_start_another_call() {
        for now in [1_350_000, 1_355_000, 1_360_000, i64::MAX] {
            let error = observe(binding(), now).err().unwrap();
            assert!(error.starts_with("NOKIY_EXECUTION_BUDGET_EXHAUSTED"));
        }
    }

    #[test]
    fn rejects_foreign_malformed_or_extended_bindings() {
        assert!(
            ExecutionBudget::observe(true, "foreign", Some(&binding().to_string()), 1_000_000)
                .is_err()
        );
        for (key, value) in [
            ("reserve_ms", json!(0)),
            ("timeout_ms", json!(1_000_000)),
            ("deadline_unix_ms", json!(1_360_001)),
            ("schema_version", json!("other")),
            ("unexpected", json!(true)),
        ] {
            let mut input = binding();
            input[key] = value;
            assert!(observe(input, 1_000_000).is_err(), "{key}");
        }
        assert!(ExecutionBudget::observe(true, "unused", Some("{"), 0).is_err());
    }

    #[test]
    fn missing_budget_and_unscoped_native_path_stay_unchanged() {
        assert!(
            ExecutionBudget::observe(true, "unused", None, 0)
                .unwrap()
                .is_none()
        );
        assert!(
            ExecutionBudget::observe(false, "unused", Some("malformed"), 0)
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn pacing_is_appended_without_rewriting_or_accumulating_history() {
        let history = vec![
            json!({"role":"system","content":"stable"}),
            json!({"role":"user","content":"task"}),
        ];
        let mut first = history.clone();
        first.push(observe(binding(), 1_000_000).unwrap().unwrap().message());
        let mut second = history.clone();
        second.push(observe(binding(), 1_300_000).unwrap().unwrap().message());
        assert_eq!(&first[..2], history);
        assert_eq!(&second[..2], history);
        assert_eq!(first.len(), second.len());
        assert_ne!(first[2], second[2]);
    }
}
