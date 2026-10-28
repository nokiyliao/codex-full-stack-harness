use super::{
    ProviderConfig, ProviderLatencyConfig, ProviderLatencyTimeouts, apply_codex_account_env,
    load_codex_auth_tokens, normalize_response_content, openai_login_is_oauth,
    provider_latency_timeouts, refresh_openai_access_token_if_needed,
    set_provider_latency_timeouts,
};
use serde_json::json;
use std::path::PathBuf;
use uuid::Uuid;

struct EnvRestore {
    keys: Vec<(&'static str, Option<String>)>,
}

impl EnvRestore {
    fn capture(keys: &[&'static str]) -> Self {
        Self {
            keys: keys
                .iter()
                .map(|key| (*key, std::env::var(key).ok()))
                .collect(),
        }
    }
}

impl Drop for EnvRestore {
    fn drop(&mut self) {
        for (key, value) in &self.keys {
            if let Some(value) = value {
                // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
                #[allow(
                    unsafe_code,
                    reason = "Rust 2024 process-environment mutation audited at the caller"
                )]
                unsafe {
                    std::env::set_var(key, value)
                };
            } else {
                // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
                #[allow(
                    unsafe_code,
                    reason = "Rust 2024 process-environment mutation audited at the caller"
                )]
                unsafe {
                    std::env::remove_var(key)
                };
            }
        }
    }
}

fn unique_temp_dir(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!("tura-provider-{name}-{}", Uuid::new_v4()));
    std::fs::create_dir_all(&path).expect("temp dir");
    path
}

struct SyntheticAuthFixture {
    _restore: EnvRestore,
    root: PathBuf,
    auth_path: PathBuf,
    conf: crate::TuraConfig,
}

impl SyntheticAuthFixture {
    // Callers hold the shared environment lock throughout the fixture's lifetime.
    fn new() -> Self {
        let restore = EnvRestore::capture(&[
            "CODEX_HOME", "TURA_ENV_PATH", "TURA_PROVIDER_CONFIG", "OPENAI_LOGIN",
            "OPENAI_API_KEY", "OPENAI_REFRESH_TOKEN", "OPENAI_TOKEN_EXPIRES",
            "OPENAI_ACCOUNT_ID", "OPENAI_OAUTH_TOKEN_URL",
        ]);
        let root = unique_temp_dir("auth-owner");
        let env_path = root.join("fixture.env");
        let config_path = root.join("provider.json");
        std::fs::write(&env_path, "FIXTURE_ONLY=true\n").expect("synthetic env");
        std::fs::write(&config_path, "{}").expect("synthetic provider config");
        // SAFETY: fixture callers hold the shared environment lock.
        #[allow(unsafe_code, reason = "test fixture owns the shared environment lock")]
        unsafe {
            std::env::set_var("CODEX_HOME", &root);
            std::env::set_var("TURA_ENV_PATH", &env_path);
            std::env::set_var("TURA_PROVIDER_CONFIG", &config_path);
            std::env::set_var("OPENAI_LOGIN", "oauth");
            std::env::set_var("OPENAI_API_KEY", "fixture-access");
            std::env::set_var("OPENAI_REFRESH_TOKEN", "fixture-owned-refresh");
            // A mistaken reactive fallthrough must not reach the real auth endpoint.
            std::env::set_var("OPENAI_OAUTH_TOKEN_URL", "http://127.0.0.1:0");
            std::env::remove_var("OPENAI_TOKEN_EXPIRES");
            std::env::remove_var("OPENAI_ACCOUNT_ID");
        }
        Self {
            _restore: restore,
            auth_path: root.join("auth.json"),
            conf: crate::TuraConfig::new("unused.env"),
            root,
        }
    }

    fn write_native(&self, access: &str, account: Option<&str>) {
        std::fs::write(&self.auth_path, json!({
            "tokens": {
                "access_token": access,
                "refresh_token": "fixture-native-refresh-must-not-be-used",
                "account_id": account,
            }
        }).to_string()).expect("synthetic native auth");
    }
}

#[test]
fn provider_alias_preserves_auth_identity_and_resolves_runtime_protocol() {
    let gemini = ProviderConfig {
        provider: "gemini-api".to_string(),
        base_url: "https://generativelanguage.googleapis.com/v1beta".to_string(),
        model: "gemini-3.5-flash".to_string(),
        temperature: 0.2,
    };
    let mistral = ProviderConfig {
        provider: "mistral".to_string(),
        base_url: "https://api.mistral.ai/v1".to_string(),
        model: "mistral-medium-3.5".to_string(),
        temperature: 0.2,
    };

    assert_eq!(gemini.provider, "gemini-api");
    assert_eq!(gemini.runtime_provider(), "google");
    assert_eq!(mistral.runtime_provider(), "mistral");
}

#[test]
fn normalizes_openai_style_tool_calls() {
    let raw = json!({
        "choices": [{
            "message": {
                "role": "assistant",
                "content": null,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "glob",
                        "arguments": "{\"requests\":[{\"directory\":\".\"}]}"
                    }
                }]
            }
        }]
    });

    let content = normalize_response_content(&raw);

    assert_eq!(content["tool_calls"][0]["function"]["name"], "glob");
}

#[test]
fn keeps_codex_responses_style_output_unchanged_for_codex_normalizer() {
    let raw = json!({
        "output": [{
            "type": "function_call",
            "name": "read_line",
            "arguments": "{\"requests\":[]}"
        }]
    });

    let content = normalize_response_content(&raw);

    assert_eq!(content[0]["type"], "function_call");
}

#[test]
fn normalizes_codex_responses_events_when_output_array_is_empty() {
    let raw = json!({
        "object": "response",
        "output": [],
        "output_text": "I will inspect before editing.",
        "events": [
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "id": "msg_1",
                    "content": [{
                        "type": "output_text",
                        "text": "I will inspect before editing."
                    }]
                }
            },
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "command_run",
                    "arguments": ""
                }
            },
            {
                "type": "response.function_call_arguments.done",
                "item_id": "fc_1",
                "arguments": "{\"commands\":[{\"command_type\":\"shell_command\",\"command_line\":\"pwd\"}]}"
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "command_run",
                    "status": "completed",
                    "arguments": "{\"commands\":[{\"command_type\":\"shell_command\",\"command_line\":\"pwd\"}]}"
                }
            }
        ]
    });

    let content = normalize_response_content(&raw);

    assert_eq!(content["text"], "I will inspect before editing.");
    assert_eq!(content["tool_calls"][0]["id"], "call_1");
    assert_eq!(
        content["tool_calls"][0]["function"]["arguments"]["commands"][0]["command_line"],
        "pwd"
    );
}

#[test]
fn provider_latency_defaults_match_high_profile() {
    let selected = ProviderLatencyConfig::default().selected_timeouts();

    assert_eq!(selected.idle_output_timeout_ms, 80_000);
    assert_eq!(selected.first_output_timeout_ms, 160_000);
    assert_eq!(selected.total_timeout_ms, 960_000);
}

#[test]
fn provider_latency_level_tracks_tier_flag() {
    assert_eq!(super::latency_level_for_tier("thinking"), "x-high");
    assert_eq!(super::latency_level_for_tier("fast"), "high");
    assert_eq!(super::latency_level_for_tier("embedding_high"), "x-high");
    assert_eq!(super::latency_level_for_tier("embedding_low"), "high");
    // Unknown tiers fall back to the high level.
    assert_eq!(super::latency_level_for_tier("something_else"), "high");
}

#[test]
fn provider_latency_timeouts_for_tier_resolve_levels() {
    let config = ProviderLatencyConfig::default();

    let thinking = config.timeouts_for_tier("thinking");
    assert_eq!(thinking.total_timeout_ms, 1_200_000);

    let fast = config.timeouts_for_tier("fast");
    assert_eq!(fast.total_timeout_ms, 960_000);
}

#[test]
fn provider_latency_global_timeout_state_is_configurable() {
    set_provider_latency_timeouts(ProviderLatencyTimeouts {
        idle_output_timeout_ms: 50_000,
        first_output_timeout_ms: 90_000,
        total_timeout_ms: 600_000,
    });

    let selected = provider_latency_timeouts();
    assert_eq!(selected.idle_output_timeout_ms, 50_000);
    assert_eq!(selected.first_output_timeout_ms, 90_000);
    assert_eq!(selected.total_timeout_ms, 600_000);
}

#[test]
fn loads_codex_oauth_tokens_from_codex_home() {
    let _lock = crate::test_support::env_lock();
    let _env = EnvRestore::capture(&[
        "CODEX_HOME",
        "OPENAI_LOGIN",
        "OPENAI_API_KEY",
        "OPENAI_REFRESH_TOKEN",
        "OPENAI_ACCOUNT_ID",
        "TURA_PROVIDER_CONFIG",
    ]);
    // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
    #[allow(
        unsafe_code,
        reason = "Rust 2024 process-environment mutation audited at the caller"
    )]
    unsafe {
        std::env::remove_var("OPENAI_LOGIN")
    };
    // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
    #[allow(
        unsafe_code,
        reason = "Rust 2024 process-environment mutation audited at the caller"
    )]
    unsafe {
        std::env::remove_var("OPENAI_API_KEY")
    };
    // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
    #[allow(
        unsafe_code,
        reason = "Rust 2024 process-environment mutation audited at the caller"
    )]
    unsafe {
        std::env::remove_var("OPENAI_REFRESH_TOKEN")
    };
    // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
    #[allow(
        unsafe_code,
        reason = "Rust 2024 process-environment mutation audited at the caller"
    )]
    unsafe {
        std::env::remove_var("OPENAI_ACCOUNT_ID")
    };
    // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
    #[allow(
        unsafe_code,
        reason = "Rust 2024 process-environment mutation audited at the caller"
    )]
    unsafe {
        std::env::remove_var("TURA_PROVIDER_CONFIG")
    };

    let codex_home = unique_temp_dir("codex-home");
    std::fs::write(
        codex_home.join("auth.json"),
        r#"{
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": null,
                "tokens": {
                    "access_token": "local-access-token",
                    "refresh_token": "local-refresh-token",
                    "account_id": "acct-local"
                }
            }"#,
    )
    .expect("auth json");
    // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
    #[allow(
        unsafe_code,
        reason = "Rust 2024 process-environment mutation audited at the caller"
    )]
    unsafe {
        std::env::set_var("CODEX_HOME", &codex_home)
    };

    let tokens = load_codex_auth_tokens().expect("codex auth tokens");
    apply_codex_account_env(&tokens);

    assert_eq!(tokens.access_token, "local-access-token");
    assert_eq!(tokens.account_id.as_deref(), Some("acct-local"));
    assert!(std::env::var("OPENAI_LOGIN").is_err());
    assert!(std::env::var("OPENAI_API_KEY").is_err());
    assert!(std::env::var("OPENAI_REFRESH_TOKEN").is_err());
    assert_eq!(
        std::env::var("OPENAI_ACCOUNT_ID").as_deref(),
        Ok("acct-local")
    );
}

#[tokio::test]
async fn prefers_codex_auth_when_env_token_is_still_marked_valid() {
    let _lock = crate::test_support::env_lock_async().await;
    let _restore = EnvRestore::capture(&[
        "CODEX_HOME",
        "TURA_ENV_PATH",
        "OPENAI_LOGIN",
        "OPENAI_API_KEY",
        "OPENAI_REFRESH_TOKEN",
        "OPENAI_TOKEN_EXPIRES",
        "OPENAI_ACCOUNT_ID",
    ]);
    let root = unique_temp_dir("rotated-codex-auth");
    let codex_home = root.join("codex-home");
    std::fs::create_dir_all(&codex_home).expect("create codex home");
    std::fs::write(
        codex_home.join("auth.json"),
        r#"{
            "tokens": {
                "access_token": "fresh-codex-access",
                "refresh_token": "fresh-codex-refresh",
                "account_id": "fresh-account"
            }
        }"#,
    )
    .expect("write codex auth");
    let env_path = root.join("tura.env");
    std::fs::write(
        &env_path,
        concat!(
            "OPENAI_LOGIN=oauth\n",
            "OPENAI_API_KEY=stale-env-access\n",
            "OPENAI_REFRESH_TOKEN=stale-env-refresh\n",
            "OPENAI_TOKEN_EXPIRES=4102444800000\n",
        ),
    )
    .expect("write tura env");

    // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
    #[allow(
        unsafe_code,
        reason = "Rust 2024 process-environment mutation audited at the caller"
    )]
    unsafe {
        std::env::set_var("CODEX_HOME", &codex_home)
    };
    // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
    #[allow(
        unsafe_code,
        reason = "Rust 2024 process-environment mutation audited at the caller"
    )]
    unsafe {
        std::env::set_var("TURA_ENV_PATH", &env_path)
    };
    for key in [
        "OPENAI_LOGIN",
        "OPENAI_API_KEY",
        "OPENAI_REFRESH_TOKEN",
        "OPENAI_TOKEN_EXPIRES",
        "OPENAI_ACCOUNT_ID",
    ] {
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::remove_var(key)
        };
    }
    let conf = crate::TuraConfig::new(".env.missing");

    // Regression: a future expiry on stale .env data must not mask a newer Codex login.
    let access = refresh_openai_access_token_if_needed(&conf)
        .await
        .expect("select codex access token");

    assert_eq!(access.token, "fresh-codex-access");
    assert!(matches!(&access.owner, super::AccessOwner::NativeCodex { .. }));
    assert!(std::env::var("OPENAI_API_KEY").is_err());
    assert!(std::env::var("OPENAI_REFRESH_TOKEN").is_err());
    assert_eq!(conf.get("OPENAI_REFRESH_TOKEN").as_deref(), Some("stale-env-refresh"));
    assert_eq!(
        std::env::var("OPENAI_ACCOUNT_ID").as_deref(),
        Ok("fresh-account")
    );
}

#[test]
fn openai_oauth_login_uses_provider_auth_config() {
    let _lock = crate::test_support::env_lock();
    let _env = EnvRestore::capture(&[
        "CODEX_HOME",
        "OPENAI_LOGIN",
        "OPENAI_API_KEY",
        "TURA_PROVIDER_CONFIG",
        "TURA_ENV_PATH",
    ]);
    // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
    #[allow(
        unsafe_code,
        reason = "Rust 2024 process-environment mutation audited at the caller"
    )]
    unsafe {
        std::env::set_var("CODEX_HOME", unique_temp_dir("provider-config-codex-home"))
    };
    // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
    #[allow(
        unsafe_code,
        reason = "Rust 2024 process-environment mutation audited at the caller"
    )]
    unsafe {
        std::env::remove_var("OPENAI_LOGIN")
    };
    // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
    #[allow(
        unsafe_code,
        reason = "Rust 2024 process-environment mutation audited at the caller"
    )]
    unsafe {
        std::env::remove_var("OPENAI_API_KEY")
    };
    // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
    #[allow(
        unsafe_code,
        reason = "Rust 2024 process-environment mutation audited at the caller"
    )]
    unsafe {
        std::env::remove_var("TURA_PROVIDER_CONFIG")
    };

    let dir = unique_temp_dir("provider-config");
    let config = dir.join("provider_config.json");
    let env_path = dir.join("fixture.env");
    std::fs::write(&env_path, "").expect("synthetic env");
    std::fs::write(&config, r#"{"provider_auth":{"openai":{"login":"oauth"}}}"#)
        .expect("provider config");
    // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
    #[allow(
        unsafe_code,
        reason = "Rust 2024 process-environment mutation audited at the caller"
    )]
    unsafe {
        std::env::set_var("TURA_PROVIDER_CONFIG", &config);
        std::env::set_var("TURA_ENV_PATH", &env_path);
    };

    assert!(openai_login_is_oauth(
        &crate::tura_conf::TuraConfig::new("unused.env")
    ));
}

#[test]
fn borrowed_native_access_never_needs_proactive_refresh_or_credential_caching() {
    let _lock = crate::test_support::env_lock();
    let fixture = SyntheticAuthFixture::new();
    fixture.write_native("fixture-access", Some("fixture-account"));
    let original_auth = std::fs::read(&fixture.auth_path).expect("auth preimage");
    let original_env = std::fs::read(fixture.conf.env_path()).expect("env preimage");

    for expires in [None, Some("0"), Some("4102444800000")] {
        // SAFETY: this test holds the shared environment lock.
        #[allow(unsafe_code, reason = "test owns the shared environment lock")]
        unsafe {
            match expires {
                Some(value) => std::env::set_var("OPENAI_TOKEN_EXPIRES", value),
                None => std::env::remove_var("OPENAI_TOKEN_EXPIRES"),
            }
        }
        for _ in 0..2 {
            let access = super::select_openai_access_token(&fixture.conf).expect("native selection");
            assert_eq!(access.token, "fixture-access");
            assert!(matches!(&access.owner, super::AccessOwner::NativeCodex { .. }));
            assert!(!super::openai_access_needs_refresh(&access, &fixture.conf));
        }
    }
    assert_eq!(std::env::var("OPENAI_API_KEY").as_deref(), Ok("fixture-access"));
    assert_eq!(std::env::var("OPENAI_REFRESH_TOKEN").as_deref(), Ok("fixture-owned-refresh"));
    assert_eq!(std::fs::read(&fixture.auth_path).unwrap(), original_auth);
    assert_eq!(std::fs::read(fixture.conf.env_path()).unwrap(), original_env);
}

#[tokio::test]
async fn borrowed_native_recovery_requires_changed_token_same_account_and_401() {
    let _lock = crate::test_support::env_lock_async().await;
    let fixture = SyntheticAuthFixture::new();
    fixture.write_native("fixture-access", Some("fixture-account"));
    let access = super::select_openai_access_token(&fixture.conf).expect("native selection");
    let original_env = std::fs::read(fixture.conf.env_path()).expect("env preimage");

    for (token, account, status, expected) in [
        ("fixture-access", Some("fixture-account"), 401, None),
        ("native-rotated", Some("fixture-account"), 401, Some("native-rotated")),
        ("native-rotated", Some("fixture-account"), 403, None),
        ("other-account-token", Some("other-account"), 401, None),
        ("other-account-token", Some("other-account"), 403, None),
        ("unknown-account-token", None, 401, None),
    ] {
        fixture.write_native(token, account);
        let auth_preimage = std::fs::read(&fixture.auth_path).unwrap();
        let recovery = super::try_refresh_oauth_access_token("codex", &fixture.conf, &access, status)
            .await.expect("native reload does not attempt OAuth refresh");
        assert_eq!(recovery.as_deref(), expected);
        assert_eq!(std::fs::read(&fixture.auth_path).unwrap(), auth_preimage);
        assert_eq!(std::env::var("OPENAI_ACCOUNT_ID").as_deref(), Ok("fixture-account"));
    }

    std::fs::write(&fixture.auth_path, "not-json").unwrap();
    assert!(super::try_refresh_oauth_access_token("codex", &fixture.conf, &access, 401)
        .await.expect("invalid native auth has no owned refresh fallback").is_none());
    std::fs::remove_file(&fixture.auth_path).unwrap();
    assert!(super::try_refresh_oauth_access_token("codex", &fixture.conf, &access, 401)
        .await.expect("deleted native auth has no owned refresh fallback").is_none());
    assert_eq!(std::fs::read(fixture.conf.env_path()).unwrap(), original_env);
    assert_eq!(std::env::var("OPENAI_API_KEY").as_deref(), Ok("fixture-access"));
    assert_eq!(std::env::var("OPENAI_REFRESH_TOKEN").as_deref(), Ok("fixture-owned-refresh"));
}

#[tokio::test]
async fn native_recovery_is_bound_to_initial_path_and_known_account() {
    let _lock = crate::test_support::env_lock_async().await;
    let fixture = SyntheticAuthFixture::new();
    fixture.write_native("fixture-access", None);
    let anonymous = super::select_openai_access_token(&fixture.conf).expect("native selection");
    fixture.write_native("native-rotated", None);
    assert!(super::try_refresh_oauth_access_token("codex", &fixture.conf, &anonymous, 401)
        .await.unwrap().is_none());

    fixture.write_native("fixture-access", Some("fixture-account"));
    let access = super::select_openai_access_token(&fixture.conf).expect("native selection");
    let other_home = fixture.root.join("other-home");
    std::fs::create_dir(&other_home).unwrap();
    std::fs::write(other_home.join("auth.json"), json!({"tokens": {
        "access_token": "unbound-native-token", "account_id": "fixture-account"
    }}).to_string()).unwrap();
    // SAFETY: this test holds the shared environment lock.
    #[allow(unsafe_code, reason = "test owns the shared environment lock")]
    unsafe { std::env::set_var("CODEX_HOME", &other_home); }
    assert!(super::try_refresh_oauth_access_token("codex", &fixture.conf, &access, 401)
        .await.unwrap().is_none());
    fixture.write_native("bound-native-token", Some("fixture-account"));
    assert_eq!(super::try_refresh_oauth_access_token("codex", &fixture.conf, &access, 401)
        .await.unwrap().as_deref(), Some("bound-native-token"));
}


// 2026-10-22 (Nokiy I-11): a worker read auth.json during a native rewrite, found it missing and
// failed with "Configuration key 'OPENAI_API_KEY' not found". The settled selection waits briefly
// only where failing is the only alternative.
#[tokio::test]
async fn native_auth_rewrite_window_settles_instead_of_failing() {
    let _lock = crate::test_support::env_lock_async().await;
    let fixture = SyntheticAuthFixture::new();
    // SAFETY: this test holds the shared environment lock.
    #[allow(unsafe_code, reason = "test owns the shared environment lock")]
    unsafe {
        std::env::remove_var("OPENAI_API_KEY");
    }
    // TuraConfig captures the environment when it is built.
    let conf = crate::TuraConfig::new("unused.env");
    let auth_path = fixture.auth_path.clone();
    let writer = tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(300)).await;
        std::fs::write(&auth_path, r#"{"tokens":{"access_token":"rewritten-native","account_id":"acct"}}"#).unwrap();
    });
    let started = std::time::Instant::now();
    let access = super::select_openai_access_token_settled(&conf).await.expect("settled native auth");
    writer.await.unwrap();
    assert_eq!(access.token, "rewritten-native");
    assert!(matches!(&access.owner, super::AccessOwner::NativeCodex { .. }));
    assert!(started.elapsed() >= std::time::Duration::from_millis(250));

    // A half-written file reads as invalid; it settles the same way.
    std::fs::write(&fixture.auth_path, r#"{"tokens":{"access_tok"#).unwrap();
    let auth_path = fixture.auth_path.clone();
    let writer = tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        std::fs::write(&auth_path, r#"{"tokens":{"access_token":"complete-native"}}"#).unwrap();
    });
    let access = super::select_openai_access_token_settled(&conf).await.expect("settled rewrite");
    writer.await.unwrap();
    assert_eq!(access.token, "complete-native");

    // Still missing after the bounded wait: the original typed error, after about two seconds.
    std::fs::remove_file(&fixture.auth_path).unwrap();
    let started = std::time::Instant::now();
    let error = super::select_openai_access_token_settled(&conf).await.unwrap_err();
    assert!(matches!(error, super::TuraError::Config { .. }), "{error:?}");
    let waited = started.elapsed();
    assert!(waited >= std::time::Duration::from_millis(1800) && waited < std::time::Duration::from_secs(10), "{waited:?}");
}

#[tokio::test]
async fn provider_api_key_fallback_never_waits_for_native_auth() {
    let _lock = crate::test_support::env_lock_async().await;
    let fixture = SyntheticAuthFixture::new();
    let started = std::time::Instant::now();
    let access = super::select_openai_access_token_settled(&fixture.conf).await.expect("provider key");
    assert_eq!(access.token, "fixture-access");
    assert!(matches!(&access.owner, super::AccessOwner::Provider));
    assert!(started.elapsed() < std::time::Duration::from_millis(100));
}

#[test]
fn invalid_native_auth_does_not_select_cached_owned_refresh_credentials() {
    let _lock = crate::test_support::env_lock();
    let fixture = SyntheticAuthFixture::new();
    for invalid in ["not-json", "{}", r#"{"tokens":{"access_token":""}}"#] {
        std::fs::write(&fixture.auth_path, invalid).unwrap();
        let error = super::select_openai_access_token(&fixture.conf).unwrap_err();
        assert!(matches!(error, super::TuraError::NativeAuthRepairRequired { .. }));
        assert!(error.is_non_retryable_provider_failure());
    }
    std::fs::write(&fixture.auth_path, [0xff, 0xfe]).unwrap();
    assert!(super::select_openai_access_token(&fixture.conf).unwrap_err()
        .is_non_retryable_provider_failure());
    std::fs::write(&fixture.auth_path, r#"{"tokens":{"access_token":"native-without-refresh"}}"#)
        .unwrap();
    let access = super::select_openai_access_token(&fixture.conf).expect("borrow only access token");
    assert_eq!(access.token, "native-without-refresh");
    assert!(!super::openai_access_needs_refresh(&access, &fixture.conf));
}

#[test]
fn native_auth_read_error_preserves_permanent_and_transient_distinction() {
    for code in [1, 13] {
        let error = super::native_auth_read_error(std::io::Error::from_raw_os_error(code));
        assert!(matches!(error, super::TuraError::NativeAuthRepairRequired { .. }));
        let error = error.before_model_authentication()
            .with_failed_attempts(vec![Default::default()]);
        assert!(error.model_attempt_not_started());
        assert!(error.is_non_retryable_provider_failure());
        let error = error.after_prior_model_attempt(true);
        assert!(!error.model_attempt_not_started());
        assert!(error.is_non_retryable_provider_failure());
        assert_eq!(error.failed_attempts().len(), 1);
    }
    for kind in [std::io::ErrorKind::Interrupted, std::io::ErrorKind::TimedOut,
                 std::io::ErrorKind::WouldBlock, std::io::ErrorKind::Other] {
        let error = super::native_auth_read_error(std::io::Error::from(kind))
            .before_model_authentication();
        assert!(error.model_attempt_not_started());
        assert!(!error.is_non_retryable_provider_failure());
    }
}

#[tokio::test]
async fn native_auth_repair_stops_route_before_fallback_or_model_entry() {
    let _lock = crate::test_support::env_lock_async().await;
    let fixture = SyntheticAuthFixture::new();
    std::fs::write(&fixture.auth_path, "{}").unwrap();
    let route = super::RouteConfig {
        default_temperature: 0.0,
        providers: vec![super::ProviderConfig {
            provider: "codex".into(), base_url: "http://127.0.0.1:0".into(),
            model: "fixture-model".into(), temperature: 0.0,
        }, super::ProviderConfig {
            provider: "nokiy_fixture_missing_fallback_key".into(),
            base_url: "http://127.0.0.1:0".into(),
            model: "fixture-fallback".into(), temperature: 0.0,
        }],
    };
    let error = route.run(&fixture.conf, Vec::new(), super::CallOptions::default())
        .await.expect_err("native auth must stop before provider or fallback");
    assert!(error.model_attempt_not_started());
    assert!(error.is_non_retryable_provider_failure());
    assert_eq!(error.failed_attempts().len(), 1);
    assert_eq!(error.failed_attempts()[0].provider, "codex");
    assert!(error.failed_attempts()[0].model_attempt_not_started);
    let super::TuraError::ObservedFailure { source, .. } = error else {
        panic!("route must retain failure accounting");
    };
    let super::TuraError::AuthenticationBeforeModel { source } = *source else {
        panic!("route must retain the typed authentication failure");
    };
    assert!(matches!(*source, super::TuraError::NativeAuthRepairRequired { .. }));
}

#[cfg(unix)]
#[test]
fn native_auth_permission_denial_does_not_select_fallback_credentials() {
    use std::os::unix::fs::PermissionsExt;
    let _lock = crate::test_support::env_lock();
    let fixture = SyntheticAuthFixture::new();
    fixture.write_native("synthetic-native-access", Some("fixture-account"));
    let permissions = std::fs::metadata(&fixture.auth_path).unwrap().permissions();
    std::fs::set_permissions(&fixture.auth_path, std::fs::Permissions::from_mode(0o000)).unwrap();
    let selected = super::select_openai_access_token(&fixture.conf);
    std::fs::set_permissions(&fixture.auth_path, permissions).unwrap();
    let error = selected.expect_err("unreadable native auth cannot use cached provider credentials");
    assert!(matches!(error, super::TuraError::NativeAuthRepairRequired { .. }));
    assert!(error.is_non_retryable_provider_failure());
}

#[tokio::test]
async fn standalone_oauth_refresh_selection_and_api_key_routing_are_preserved() {
    let _lock = crate::test_support::env_lock_async().await;
    let fixture = SyntheticAuthFixture::new();
    let access = super::select_openai_access_token(&fixture.conf).expect("standalone selection");
    assert_eq!(access.token, "fixture-access");
    assert!(matches!(&access.owner, super::AccessOwner::Provider));
    assert!(super::openai_access_needs_refresh(&access, &fixture.conf));
    // SAFETY: this test holds the shared environment lock.
    #[allow(unsafe_code, reason = "test owns the shared environment lock")]
    unsafe {
        std::env::set_var("OPENAI_TOKEN_EXPIRES", "4102444800000");
        std::env::set_var("OPENAI_REFRESH_TOKEN", "");
    }
    assert!(!super::openai_access_needs_refresh(&access, &fixture.conf));
    assert!(super::try_refresh_oauth_access_token("codex", &fixture.conf, &access, 401)
        .await.expect("no configured refresh token, no network").is_none());
    fixture.write_native("native-access", Some("fixture-account"));
    for (provider, base_url) in [
        ("openai-api", "https://api.openai.com/v1"),
        ("openai", "https://fixture.invalid/v1"),
        ("anthropic", "https://api.anthropic.com"),
    ] {
        assert!(!super::should_use_openai_oauth(provider, base_url, &fixture.conf));
    }
    // SAFETY: this test holds the shared environment lock.
    #[allow(unsafe_code, reason = "test owns the shared environment lock")]
    unsafe { std::env::set_var("OPENAI_LOGIN", "apikey"); }
    assert!(!super::should_use_openai_oauth("openai", "https://api.openai.com/v1", &fixture.conf));
}

#[test]
fn auth_expired_error_detects_401_and_403_only() {
    let unauthorized: Result<(), super::TuraError> = Err(super::TuraError::HttpStatus {
        status: 401,
        body: "expired".to_string(),
    });
    let forbidden: Result<(), super::TuraError> = Err(super::TuraError::HttpStatus {
        status: 403,
        body: "forbidden".to_string(),
    });
    let rate_limited: Result<(), super::TuraError> = Err(super::TuraError::HttpStatus {
        status: 429,
        body: "slow down".to_string(),
    });
    let network: Result<(), super::TuraError> = Err(super::TuraError::Network {
        message: "boom".to_string(),
    });
    let ok: Result<(), super::TuraError> = Ok(());

    assert!(super::is_auth_expired_error(&unauthorized));
    assert!(super::is_auth_expired_error(&forbidden));
    assert!(!super::is_auth_expired_error(&rate_limited));
    assert!(!super::is_auth_expired_error(&network));
    assert!(!super::is_auth_expired_error(&ok));
}

#[test]
fn authentication_scope_preserves_display_retry_and_unknown_model_failure() {
    for error in [
        super::TuraError::Network { message: "OAuth token endpoint unavailable".into() },
        super::TuraError::HttpStatus { status: 401, body: "expired".into() },
        super::TuraError::Config { message: "API Key not found for provider fixture".into() },
    ] {
        let display = error.to_string();
        let retry = error.is_non_retryable_provider_failure();
        let scoped = error.before_model_authentication();
        assert!(scoped.model_attempt_not_started());
        assert_eq!(scoped.to_string(), display);
        assert_eq!(scoped.is_non_retryable_provider_failure(), retry);
        assert!(!scoped.after_prior_model_attempt(true).model_attempt_not_started());
    }
    // A provider auth response, including failed reactive refresh, stays a model attempt.
    let model_error = super::TuraError::HttpStatus { status: 401, body: "expired".into() };
    assert!(!model_error.model_attempt_not_started());
    assert!(super::is_auth_expired_error(&Err::<(), _>(model_error)));
}

#[tokio::test]
async fn authentication_scope_missing_fixture_key_stops_before_model_entry() {
    let _lock = crate::test_support::env_lock_async().await;
    let fixture = SyntheticAuthFixture::new();
    let provider = super::ProviderConfig {
        provider: "nokiy_stage_fixture_no_key".into(),
        base_url: "http://127.0.0.1:0".into(),
        model: "fixture-model".into(),
        temperature: 0.0,
    };
    let config = fixture.conf;
    assert!(provider.get_api_key(&config).is_err(), "fixture must have no configured key");
    let error = provider.call(&config, Vec::new(), super::CallOptions::default())
        .await.expect_err("authentication must stop before any provider invocation");
    assert!(error.model_attempt_not_started());
    assert!(error.is_non_retryable_provider_failure());
    assert!(matches!(error, super::TuraError::AuthenticationBeforeModel { .. }));
}

#[test]
fn provider_failure_retry_classifier_rejects_auth_billing_and_missing_model_errors() {
    for status in [401, 402, 403, 404] {
        assert!(
            super::TuraError::HttpStatus {
                status,
                body: "provider rejected request".to_string(),
            }
            .is_non_retryable_provider_failure()
        );
    }

    assert!(
        super::TuraError::Config {
            message: "API Key not found for provider 'openai'".to_string(),
        }
        .is_non_retryable_provider_failure()
    );
    assert!(
        super::TuraError::AllProvidersFailed {
            message: "openai:gpt => http status 404: model not found".to_string(),
        }
        .is_non_retryable_provider_failure()
    );
    assert!(
        super::TuraError::ProviderRequest {
            provider: "openai".to_string(),
            message: "billing quota is not enabled".to_string(),
        }
        .is_non_retryable_provider_failure()
    );

    assert!(
        !super::TuraError::HttpStatus {
            status: 429,
            body: "slow down".to_string(),
        }
        .is_non_retryable_provider_failure()
    );
    assert!(
        !super::TuraError::Network {
            message: "connection reset".to_string(),
        }
        .is_non_retryable_provider_failure()
    );
}

#[test]
fn persist_env_var_upserts_and_appends_preserving_other_keys() {
    let dir = unique_temp_dir("persist-env");
    let path = dir.join(".env");
    std::fs::write(&path, "ALPHA=1\nTOKEN=old\nBETA=2\n").expect("seed env");

    // Update existing key in place.
    super::persist_env_var(&path, "TOKEN", "new");
    let after = std::fs::read_to_string(&path).expect("read env");
    assert!(after.contains("TOKEN=new"));
    assert!(!after.contains("TOKEN=old"));
    assert!(after.contains("ALPHA=1"));
    assert!(after.contains("BETA=2"));

    // Append a brand-new key.
    super::persist_env_var(&path, "EXPIRES", "12345");
    let after = std::fs::read_to_string(&path).expect("read env");
    assert!(after.contains("EXPIRES=12345"));
    assert!(after.contains("TOKEN=new"));
}
