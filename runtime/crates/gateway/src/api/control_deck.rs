//! Read-only Commander/Tura control-deck projection.
//!
//! Session DB remains authoritative for sessions, task plans, runtimes and
//! leases. Router exposes only the existing convergence sidecar. This module
//! joins those reads into a bounded digest-only HTTP projection.

use crate::router_client::RouterClient;
use crate::session_db_client::SessionDbClient;
use axum::{
    Json,
    http::{HeaderMap, StatusCode},
    response::sse::{Event as SseEvent, KeepAlive, Sse},
};
use futures::Stream;
use router_contract::{
    ControlDeckConvergenceAvailability, ControlDeckConvergenceEntry,
    ReadControlDeckConvergenceResponse, ReadTaskReadySetRequest, TaskReadySetEntry,
    TaskReadySetWireState,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::convert::Infallible;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const MAX_WORKSPACES: usize = 64;
const MAX_SESSIONS: usize = 512;
const MAX_RUNTIMES: usize = 2_048;
const MAX_READY_SET_ENTRIES: usize = 4_096;
const SESSION_PAGE_SIZE: u64 = 100;
const CONTROL_DECK_WATCHDOG_INTERVAL: Duration = Duration::from_secs(30);

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ControlDeckFreshness {
    Current,
    Degraded,
    Truncated,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlDeckTypedAbsence {
    pub scope: String,
    pub identity: String,
    pub field: String,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlDeckTask {
    pub task_id: String,
    pub step: u64,
    pub sub_session_id: String,
    pub start_condition: String,
    pub status: String,
    pub start_at_ms: i64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ControlDeckTaskReadinessState {
    Ready,
    Blocked,
    TypedUnknown,
    Terminal,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlDeckTaskReadiness {
    pub session_id: String,
    pub task_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub semantic_dispatch_key_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub task_scheduling_contract_sha256: Option<String>,
    pub state: ControlDeckTaskReadinessState,
    pub reason_codes: Vec<String>,
    pub dependency_task_ids: Vec<String>,
    pub blocking_task_ids: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scope_claim_sha256: Option<String>,
    pub active_lease_ids: Vec<String>,
    pub active_runtime_count: usize,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub maximum_parallel_runtime_workers: Option<usize>,
    pub state_head: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlDeckSession {
    pub session_id: String,
    pub workspace_sha256: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub parent_id: Option<String>,
    pub state: String,
    pub terminal: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name_sha256: Option<String>,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent: Option<String>,
    pub runtime_ids: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub active_runtime_id: Option<String>,
    pub tasks: Vec<ControlDeckTask>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlDeckRuntime {
    pub runtime_id: String,
    pub session_id: String,
    pub database_path_sha256: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub commander_session_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub transaction_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub task_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub goal_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub lease_id: Option<String>,
    pub lease_active: bool,
    pub revision: u64,
    pub last_event_seq: u64,
    pub terminal: bool,
    pub session_event_seq: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub runtime_state: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlDeckMission {
    pub commander_session_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mission_revision_sha256: Option<String>,
    pub mission_revision_status: String,
    pub goal_ids: Vec<String>,
    pub availability: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlDeckSnapshot {
    pub schema_version: String,
    pub state_head: String,
    pub session_db_state_head: String,
    pub convergence_state_head: String,
    pub ready_set_state_head: String,
    pub observed_at_ms: i64,
    pub freshness_status: ControlDeckFreshness,
    pub authority_effect: String,
    pub truncated: bool,
    pub missions: Vec<ControlDeckMission>,
    pub sessions: Vec<ControlDeckSession>,
    pub runtimes: Vec<ControlDeckRuntime>,
    pub ready_set: Vec<ControlDeckTaskReadiness>,
    pub convergence: Vec<ControlDeckConvergenceEntry>,
    pub typed_absences: Vec<ControlDeckTypedAbsence>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct ControlDeckStateIdentity<'a> {
    schema_version: &'a str,
    session_db_state_head: &'a str,
    convergence_state_head: &'a str,
    ready_set_state_head: &'a str,
    freshness_status: &'a ControlDeckFreshness,
    truncated: bool,
    missions: &'a [ControlDeckMission],
    sessions: &'a [ControlDeckSession],
    runtimes: &'a [ControlDeckRuntime],
    ready_set: &'a [ControlDeckTaskReadiness],
    convergence: &'a [ControlDeckConvergenceEntry],
    typed_absences: &'a [ControlDeckTypedAbsence],
}

pub async fn snapshot() -> Result<Json<ControlDeckSnapshot>, (StatusCode, Json<Value>)> {
    build_snapshot_async()
        .await
        .map(Json)
        .map_err(control_deck_read_error)
}

pub async fn events(headers: HeaderMap) -> Sse<impl Stream<Item = Result<SseEvent, Infallible>>> {
    let last_event_id = headers
        .get("last-event-id")
        .and_then(|value| value.to_str().ok())
        .map(str::to_string);
    Sse::new(control_deck_event_stream(last_event_id)).keep_alive(KeepAlive::default())
}

fn control_deck_event_stream(
    last_event_id: Option<String>,
) -> impl Stream<Item = Result<SseEvent, Infallible>> {
    let state = ControlDeckEventState {
        last_event_id,
        event_cursor: crate::session::session_store().event_cursor(),
        first: true,
        last_watchdog: Instant::now(),
    };
    futures::stream::unfold(state, |mut state| async move {
        loop {
            let session_event = crate::session::session_store()
                .next_event(&mut state.event_cursor)
                .is_some();
            let watchdog_due = state.last_watchdog.elapsed() >= CONTROL_DECK_WATCHDOG_INTERVAL;
            if !state.first && !session_event && !watchdog_due {
                tokio::time::sleep(Duration::from_millis(250)).await;
                continue;
            }
            state.first = false;
            state.last_watchdog = Instant::now();
            match build_snapshot_async().await {
                Ok(snapshot)
                    if snapshot_changed(state.last_event_id.as_deref(), &snapshot.state_head) =>
                {
                    state.last_event_id = Some(snapshot.state_head.clone());
                    let payload = json!({
                        "schema_version": "tura_control_deck_change_v1",
                        "state_head": snapshot.state_head,
                        "freshness_status": snapshot.freshness_status,
                        "authority_effect": "none",
                    });
                    let event = SseEvent::default()
                        .event("control_deck_changed")
                        .id(state.last_event_id.clone().unwrap_or_default())
                        .data(payload.to_string());
                    return Some((Ok(event), state));
                }
                Ok(_) => {}
                Err(error) => {
                    let payload = json!({
                        "schema_version": "tura_control_deck_unavailable_v1",
                        "blocker_code": error_code(&error),
                        "authority_effect": "none",
                    });
                    let event = SseEvent::default()
                        .event("control_deck_unavailable")
                        .data(payload.to_string());
                    return Some((Ok(event), state));
                }
            }
        }
    })
}

async fn build_snapshot_async() -> anyhow::Result<ControlDeckSnapshot> {
    tokio::task::spawn_blocking(build_snapshot)
        .await
        .map_err(|error| anyhow::anyhow!("CONTROL_DECK_SNAPSHOT_WORKER_FAILED:{error}"))?
}

struct ControlDeckEventState {
    last_event_id: Option<String>,
    event_cursor: u64,
    first: bool,
    last_watchdog: Instant,
}

fn snapshot_changed(last_event_id: Option<&str>, current_state_head: &str) -> bool {
    last_event_id != Some(current_state_head)
}

pub(crate) fn build_snapshot() -> anyhow::Result<ControlDeckSnapshot> {
    let client = SessionDbClient::discover_for_blocking_context()?;
    let mut typed_absences = Vec::new();
    let mut truncated = false;
    let mut workspaces = client.list_workspaces()?;
    workspaces.sort_by(|left, right| left.directory.cmp(&right.directory));
    if workspaces.len() > MAX_WORKSPACES {
        workspaces.truncate(MAX_WORKSPACES);
        truncated = true;
        typed_absences.push(limit_absence("workspace", MAX_WORKSPACES));
    }

    let mut snapshots = Vec::new();
    'workspace: for workspace in workspaces {
        let mut page = 0;
        loop {
            let (page_info, mut items) =
                client.list_sessions(workspace.directory.clone(), page, SESSION_PAGE_SIZE)?;
            items.sort_by(|left, right| left.session_id.cmp(&right.session_id));
            for item in items {
                if snapshots.len() == MAX_SESSIONS {
                    truncated = true;
                    typed_absences.push(limit_absence("session", MAX_SESSIONS));
                    break 'workspace;
                }
                snapshots.push(item);
            }
            let consumed = (page + 1).saturating_mul(page_info.page_size);
            if consumed >= page_info.total {
                break;
            }
            page = page.saturating_add(1);
        }
    }
    snapshots.sort_by(|left, right| left.session_id.cmp(&right.session_id));

    let sessions = snapshots
        .iter()
        .map(project_session)
        .collect::<anyhow::Result<Vec<_>>>()?;
    let root_session_ids = sessions
        .iter()
        .filter(|session| session.parent_id.is_none())
        .map(|session| session.session_id.clone())
        .take(router_contract::MAX_CONTROL_DECK_COMMANDER_SESSION_IDS)
        .collect::<Vec<_>>();
    if sessions
        .iter()
        .filter(|session| session.parent_id.is_none())
        .count()
        > root_session_ids.len()
    {
        truncated = true;
        typed_absences.push(limit_absence(
            "commander_session",
            router_contract::MAX_CONTROL_DECK_COMMANDER_SESSION_IDS,
        ));
    }

    let mut runtime_ids = snapshots
        .iter()
        .flat_map(|snapshot| snapshot.lifecycle_projection.runtime_ids.iter().cloned())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();
    if runtime_ids.len() > MAX_RUNTIMES {
        runtime_ids.truncate(MAX_RUNTIMES);
        truncated = true;
        typed_absences.push(limit_absence("runtime", MAX_RUNTIMES));
    }
    let mut runtimes = Vec::new();
    for runtime_id in runtime_ids {
        match client.get_runtime_lease(runtime_id.clone())? {
            Some(runtime) => runtimes.push(project_runtime(runtime)?),
            None => typed_absences.push(ControlDeckTypedAbsence {
                scope: "runtime".to_string(),
                identity: runtime_id,
                field: "runtime_lease".to_string(),
                reason: "RUNTIME_LEASE_TYPED_ABSENCE".to_string(),
            }),
        }
    }
    runtimes.sort_by(|left, right| left.runtime_id.cmp(&right.runtime_id));

    let convergence_response = read_convergence(root_session_ids.clone());
    let convergence = convergence_response.entries;
    let mut missions = project_missions(&root_session_ids, &runtimes, &convergence)?;
    for mission in &missions {
        typed_absences.push(ControlDeckTypedAbsence {
            scope: "mission".to_string(),
            identity: mission.commander_session_id.clone(),
            field: "mode".to_string(),
            reason: "MISSION_MODE_NOT_DURABLE_IN_CURRENT_STORE".to_string(),
        });
        typed_absences.push(ControlDeckTypedAbsence {
            scope: "mission".to_string(),
            identity: mission.commander_session_id.clone(),
            field: "first_false_predicate".to_string(),
            reason: "MISSION_PREDICATE_NOT_DURABLE_IN_CURRENT_STORE".to_string(),
        });
        if mission.mission_revision_status != "present" {
            typed_absences.push(ControlDeckTypedAbsence {
                scope: "mission".to_string(),
                identity: mission.commander_session_id.clone(),
                field: "mission_revision_sha256".to_string(),
                reason: match mission.mission_revision_status.as_str() {
                    "typed_conflict" => "MISSION_REVISION_TYPED_CONFLICT",
                    _ => "MISSION_REVISION_TYPED_ABSENCE",
                }
                .to_string(),
            });
        }
    }
    missions.sort_by(|left, right| left.commander_session_id.cmp(&right.commander_session_id));
    let ControlDeckReadySetProjection {
        entries: ready_set,
        router_state_heads,
        typed_absences: ready_set_absences,
        truncated: ready_set_truncated,
    } = project_ready_set(&snapshots, &sessions, &missions);
    truncated |= ready_set_truncated;
    typed_absences.extend(ready_set_absences);
    let ready_set_state_head = semantic_sha256(&json!({
        "schema_version": "tura_control_deck_ready_set_projection_v1",
        "router_state_heads": &router_state_heads,
        "entries": &ready_set,
    }));
    typed_absences.sort_by(|left, right| {
        (&left.scope, &left.identity, &left.field).cmp(&(
            &right.scope,
            &right.identity,
            &right.field,
        ))
    });

    let session_db_state_head = semantic_sha256(&json!({
        "sessions": sessions,
        "runtimes": runtimes,
    }));
    let convergence_state_head = semantic_sha256(&serde_json::to_value(&convergence)?);
    let degraded = convergence
        .iter()
        .any(|entry| entry.availability == ControlDeckConvergenceAvailability::Unavailable)
        || ready_set
            .iter()
            .any(|entry| entry.state == ControlDeckTaskReadinessState::TypedUnknown);
    let freshness_status = if truncated {
        ControlDeckFreshness::Truncated
    } else if degraded {
        ControlDeckFreshness::Degraded
    } else {
        ControlDeckFreshness::Current
    };
    let identity = ControlDeckStateIdentity {
        schema_version: "tura_commander_control_deck_snapshot_v2",
        session_db_state_head: &session_db_state_head,
        convergence_state_head: &convergence_state_head,
        ready_set_state_head: &ready_set_state_head,
        freshness_status: &freshness_status,
        truncated,
        missions: &missions,
        sessions: &sessions,
        runtimes: &runtimes,
        ready_set: &ready_set,
        convergence: &convergence,
        typed_absences: &typed_absences,
    };
    let state_head = semantic_sha256(&serde_json::to_value(identity)?);
    Ok(ControlDeckSnapshot {
        schema_version: "tura_commander_control_deck_snapshot_v2".to_string(),
        state_head,
        session_db_state_head,
        convergence_state_head,
        ready_set_state_head,
        observed_at_ms: now_ms(),
        freshness_status,
        authority_effect: "none".to_string(),
        truncated,
        missions,
        sessions,
        runtimes,
        ready_set,
        convergence,
        typed_absences,
    })
}

fn read_convergence(commander_session_ids: Vec<String>) -> ReadControlDeckConvergenceResponse {
    match RouterClient::global().read_control_deck_convergence(commander_session_ids.clone()) {
        Ok(response) => response,
        Err(_) => ReadControlDeckConvergenceResponse {
            schema_version: "tura_control_deck_convergence_response_v1".to_string(),
            entries: commander_session_ids
                .into_iter()
                .map(|commander_session_id| ControlDeckConvergenceEntry {
                    commander_session_id,
                    availability: ControlDeckConvergenceAvailability::Unavailable,
                    projection: None,
                    blocker_code: Some("ROUTER_UNAVAILABLE".to_string()),
                })
                .collect(),
        },
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(deny_unknown_fields)]
struct ControlDeckReadySetRouterIdentity {
    parent_session_id: String,
    parent_task_plan_sha256: String,
    state_head: String,
}

struct ControlDeckReadySetProjection {
    entries: Vec<ControlDeckTaskReadiness>,
    router_state_heads: Vec<ControlDeckReadySetRouterIdentity>,
    typed_absences: Vec<ControlDeckTypedAbsence>,
    truncated: bool,
}

fn project_ready_set(
    snapshots: &[session_log_contract::SessionSnapshot],
    sessions: &[ControlDeckSession],
    missions: &[ControlDeckMission],
) -> ControlDeckReadySetProjection {
    let session_by_id = sessions
        .iter()
        .map(|session| (session.session_id.as_str(), session))
        .collect::<BTreeMap<_, _>>();
    let mission_by_id = missions
        .iter()
        .map(|mission| (mission.commander_session_id.as_str(), mission))
        .collect::<BTreeMap<_, _>>();
    let snapshot_by_id = snapshots
        .iter()
        .map(|snapshot| (snapshot.session_id.as_str(), snapshot))
        .collect::<BTreeMap<_, _>>();
    let mut entries = Vec::new();
    let mut router_state_heads = Vec::new();
    let mut typed_absences = Vec::new();
    let mut truncated = false;

    'sessions: for session in sessions {
        let Some(snapshot) = snapshot_by_id.get(session.session_id.as_str()) else {
            typed_absences.push(ready_set_absence(
                &session.session_id,
                "CONTROL_DECK_SESSION_SNAPSHOT_TYPED_ABSENCE",
            ));
            continue;
        };
        let tasks = &snapshot.lifecycle_projection.task_plan.detailed_tasks;
        if tasks.is_empty() {
            continue;
        }
        let task_counts = tasks.iter().fold(BTreeMap::new(), |mut counts, task| {
            *counts.entry(task.task_id.as_str()).or_insert(0_usize) += 1;
            counts
        });
        let mut nonterminal_task_ids = Vec::new();
        for (task_id, count) in task_counts {
            if entries.len() == MAX_READY_SET_ENTRIES {
                truncated = true;
                typed_absences.push(limit_absence("task_readiness", MAX_READY_SET_ENTRIES));
                break 'sessions;
            }
            let task = tasks
                .iter()
                .find(|task| task.task_id == task_id)
                .expect("counted task identity remains in the immutable snapshot");
            if task_id.trim().is_empty() {
                entries.push(typed_unknown_readiness(
                    &session.session_id,
                    task,
                    "TASK_READY_SET_TASK_ID_MISSING",
                ));
            } else if count != 1 {
                entries.push(typed_unknown_readiness(
                    &session.session_id,
                    task,
                    "TASK_READY_SET_TASK_ID_DUPLICATE",
                ));
            } else if matches!(
                task.status,
                lifecycle::PlanStatus::Done | lifecycle::PlanStatus::Archived
            ) {
                entries.push(terminal_readiness(&session.session_id, task));
            } else {
                nonterminal_task_ids.push(task.task_id.clone());
            }
        }
        if nonterminal_task_ids.is_empty() {
            continue;
        }
        nonterminal_task_ids.sort();
        if nonterminal_task_ids.len() > router_contract::MAX_TASK_READY_SET_TASK_IDS {
            for task_id in nonterminal_task_ids {
                if entries.len() == MAX_READY_SET_ENTRIES {
                    truncated = true;
                    typed_absences.push(limit_absence("task_readiness", MAX_READY_SET_ENTRIES));
                    break 'sessions;
                }
                let task = tasks
                    .iter()
                    .find(|task| task.task_id == task_id)
                    .expect("requested task identity remains in the immutable snapshot");
                entries.push(typed_unknown_readiness(
                    &session.session_id,
                    task,
                    "TASK_READY_SET_TASK_ID_LIMIT_EXCEEDED",
                ));
            }
            typed_absences.push(ready_set_absence(
                &session.session_id,
                "TASK_READY_SET_TASK_ID_LIMIT_EXCEEDED",
            ));
            continue;
        }
        let root_session_id = match commander_root_session_id(&session.session_id, &session_by_id) {
            Ok(root_session_id) => root_session_id,
            Err(reason) => {
                append_unknown_readiness(
                    &mut entries,
                    tasks,
                    &session.session_id,
                    &nonterminal_task_ids,
                    reason,
                );
                typed_absences.push(ready_set_absence(&session.session_id, reason));
                continue;
            }
        };
        let authority_mission_revision_sha256 = match ready_set_authority_revision(
            tasks,
            &nonterminal_task_ids,
            mission_by_id.get(root_session_id.as_str()).copied(),
        ) {
            Ok(revision) => revision,
            Err(reason) => {
                append_unknown_readiness(
                    &mut entries,
                    tasks,
                    &session.session_id,
                    &nonterminal_task_ids,
                    reason,
                );
                typed_absences.push(ready_set_absence(&session.session_id, reason));
                continue;
            }
        };
        let expected_parent_task_plan_sha256 =
            lifecycle::task_plan_ready_set_sha256(&snapshot.lifecycle_projection.task_plan);
        let request = ReadTaskReadySetRequest {
            parent_session_id: session.session_id.clone(),
            authority_mission_revision_sha256,
            expected_parent_task_plan_sha256: Some(expected_parent_task_plan_sha256.clone()),
            task_ids: nonterminal_task_ids.clone(),
        };
        let response = RouterClient::global().read_task_ready_set(request);
        match response {
            Ok(response)
                if response.schema_version == "tura_task_ready_set_response_v1"
                    && response.parent_session_id == session.session_id
                    && response.parent_task_plan_sha256 == expected_parent_task_plan_sha256
                    && response.authority_effect == "none"
                    && is_lower_sha256(&response.parent_task_plan_sha256)
                    && is_lower_sha256(&response.state_head)
                    && ready_set_response_matches(&response.entries, &nonterminal_task_ids) =>
            {
                router_state_heads.push(ControlDeckReadySetRouterIdentity {
                    parent_session_id: response.parent_session_id,
                    parent_task_plan_sha256: response.parent_task_plan_sha256,
                    state_head: response.state_head,
                });
                entries.extend(
                    response
                        .entries
                        .into_iter()
                        .map(|entry| project_wire_readiness(&session.session_id, entry)),
                );
            }
            Ok(_) => {
                append_unknown_readiness(
                    &mut entries,
                    tasks,
                    &session.session_id,
                    &nonterminal_task_ids,
                    "ROUTER_READY_SET_RESPONSE_INVALID",
                );
                typed_absences.push(ready_set_absence(
                    &session.session_id,
                    "ROUTER_READY_SET_RESPONSE_INVALID",
                ));
            }
            Err(_) => {
                append_unknown_readiness(
                    &mut entries,
                    tasks,
                    &session.session_id,
                    &nonterminal_task_ids,
                    "ROUTER_READY_SET_UNAVAILABLE",
                );
                typed_absences.push(ready_set_absence(
                    &session.session_id,
                    "ROUTER_READY_SET_UNAVAILABLE",
                ));
            }
        }
    }
    entries.sort_by(|left, right| {
        (&left.session_id, &left.task_id).cmp(&(&right.session_id, &right.task_id))
    });
    router_state_heads.sort_by(|left, right| left.parent_session_id.cmp(&right.parent_session_id));
    ControlDeckReadySetProjection {
        entries,
        router_state_heads,
        typed_absences,
        truncated,
    }
}

fn ready_set_authority_revision(
    tasks: &[lifecycle::TaskStep],
    task_ids: &[String],
    root_mission: Option<&ControlDeckMission>,
) -> Result<String, &'static str> {
    let requested = task_ids.iter().map(String::as_str).collect::<BTreeSet<_>>();
    let revisions = tasks
        .iter()
        .filter(|task| requested.contains(task.task_id.as_str()))
        .filter_map(|task| task.scheduling_contract.as_ref())
        .map(|contract| contract.authority_mission_revision_sha256.clone())
        .collect::<BTreeSet<_>>();
    let revision = match revisions.len() {
        0 => return Err("TASK_SCHEDULING_AUTHORITY_REVISION_TYPED_ABSENCE"),
        1 => revisions
            .into_iter()
            .next()
            .expect("one scheduling authority revision exists after cardinality check"),
        _ => return Err("TASK_SCHEDULING_AUTHORITY_REVISION_TYPED_CONFLICT"),
    };
    if let Some(mission) = root_mission
        && (mission.mission_revision_status == "typed_conflict"
            || (mission.mission_revision_status == "present"
                && mission.mission_revision_sha256.is_none())
            || mission
                .mission_revision_sha256
                .as_deref()
                .is_some_and(|current| current != revision))
    {
        return Err("MISSION_REVISION_TYPED_CONFLICT");
    }
    Ok(revision)
}

fn commander_root_session_id(
    session_id: &str,
    sessions: &BTreeMap<&str, &ControlDeckSession>,
) -> Result<String, &'static str> {
    let mut current = session_id;
    let mut visited = BTreeSet::new();
    loop {
        if !visited.insert(current) {
            return Err("COMMANDER_SESSION_PARENT_CYCLE");
        }
        let Some(session) = sessions.get(current) else {
            return Err("COMMANDER_SESSION_PARENT_TYPED_ABSENCE");
        };
        match session.parent_id.as_deref() {
            Some(parent_id) => current = parent_id,
            None => return Ok(current.to_string()),
        }
    }
}

fn ready_set_response_matches(entries: &[TaskReadySetEntry], requested: &[String]) -> bool {
    if entries.iter().any(|entry| {
        [
            entry.semantic_dispatch_key.as_deref(),
            entry.task_scheduling_contract_sha256.as_deref(),
            entry.scope_claim_sha256.as_deref(),
        ]
        .into_iter()
        .flatten()
        .any(|value| !is_lower_sha256(value))
    }) {
        return false;
    }
    let mut response_ids = entries
        .iter()
        .map(|entry| entry.task_id.clone())
        .collect::<Vec<_>>();
    response_ids.sort();
    response_ids.dedup();
    response_ids == requested && entries.len() == requested.len()
}

fn append_unknown_readiness(
    entries: &mut Vec<ControlDeckTaskReadiness>,
    tasks: &[lifecycle::TaskStep],
    session_id: &str,
    task_ids: &[String],
    reason: &str,
) {
    for task_id in task_ids {
        if entries.len() == MAX_READY_SET_ENTRIES {
            return;
        }
        if let Some(task) = tasks.iter().find(|task| task.task_id == *task_id) {
            entries.push(typed_unknown_readiness(session_id, task, reason));
        }
    }
}

fn project_wire_readiness(session_id: &str, entry: TaskReadySetEntry) -> ControlDeckTaskReadiness {
    let state = match entry.state {
        TaskReadySetWireState::Ready => ControlDeckTaskReadinessState::Ready,
        TaskReadySetWireState::TypedUnknown => ControlDeckTaskReadinessState::TypedUnknown,
        TaskReadySetWireState::BlockedState
        | TaskReadySetWireState::BlockedDependency
        | TaskReadySetWireState::BlockedScope
        | TaskReadySetWireState::BlockedLease
        | TaskReadySetWireState::BlockedCapacity => ControlDeckTaskReadinessState::Blocked,
    };
    seal_readiness(ControlDeckTaskReadiness {
        session_id: session_id.to_string(),
        task_id: entry.task_id,
        semantic_dispatch_key_sha256: entry.semantic_dispatch_key,
        task_scheduling_contract_sha256: entry.task_scheduling_contract_sha256,
        state,
        reason_codes: canonical_strings(entry.reason_codes),
        dependency_task_ids: canonical_strings(entry.dependency_task_ids),
        blocking_task_ids: canonical_strings(entry.blocking_task_ids),
        scope_claim_sha256: entry.scope_claim_sha256,
        active_lease_ids: canonical_strings(entry.active_lease_ids),
        active_runtime_count: entry.active_runtime_count,
        maximum_parallel_runtime_workers: entry.maximum_parallel_runtime_workers,
        state_head: String::new(),
    })
}

fn terminal_readiness(session_id: &str, task: &lifecycle::TaskStep) -> ControlDeckTaskReadiness {
    let contract = task.scheduling_contract.as_ref();
    seal_readiness(ControlDeckTaskReadiness {
        session_id: session_id.to_string(),
        task_id: task.task_id.clone(),
        semantic_dispatch_key_sha256: contract
            .map(|contract| contract.semantic_dispatch_key.clone()),
        task_scheduling_contract_sha256: contract.and_then(|contract| {
            serde_json::to_value(contract)
                .ok()
                .map(|value| semantic_sha256(&value))
        }),
        state: ControlDeckTaskReadinessState::Terminal,
        reason_codes: vec!["TASK_READY_SET_TASK_TERMINAL".to_string()],
        dependency_task_ids: contract
            .map(|contract| contract.dependency_task_ids.clone())
            .unwrap_or_default(),
        blocking_task_ids: Vec::new(),
        scope_claim_sha256: None,
        active_lease_ids: Vec::new(),
        active_runtime_count: 0,
        maximum_parallel_runtime_workers: contract
            .map(|contract| contract.maximum_parallel_runtime_workers),
        state_head: String::new(),
    })
}

fn typed_unknown_readiness(
    session_id: &str,
    task: &lifecycle::TaskStep,
    reason: &str,
) -> ControlDeckTaskReadiness {
    let contract = task.scheduling_contract.as_ref();
    seal_readiness(ControlDeckTaskReadiness {
        session_id: session_id.to_string(),
        task_id: task.task_id.clone(),
        semantic_dispatch_key_sha256: contract
            .map(|contract| contract.semantic_dispatch_key.clone()),
        task_scheduling_contract_sha256: contract.and_then(|contract| {
            serde_json::to_value(contract)
                .ok()
                .map(|value| semantic_sha256(&value))
        }),
        state: ControlDeckTaskReadinessState::TypedUnknown,
        reason_codes: vec![reason.to_string()],
        dependency_task_ids: contract
            .map(|contract| contract.dependency_task_ids.clone())
            .unwrap_or_default(),
        blocking_task_ids: Vec::new(),
        scope_claim_sha256: None,
        active_lease_ids: Vec::new(),
        active_runtime_count: 0,
        maximum_parallel_runtime_workers: contract
            .map(|contract| contract.maximum_parallel_runtime_workers),
        state_head: String::new(),
    })
}

fn seal_readiness(mut readiness: ControlDeckTaskReadiness) -> ControlDeckTaskReadiness {
    let mut identity = serde_json::to_value(&readiness)
        .expect("control deck task readiness is always serializable");
    identity
        .as_object_mut()
        .expect("control deck task readiness serializes as an object")
        .remove("state_head");
    readiness.state_head = semantic_sha256(&identity);
    readiness
}

fn canonical_strings(mut values: Vec<String>) -> Vec<String> {
    values.sort();
    values.dedup();
    values
}

fn ready_set_absence(session_id: &str, reason: &str) -> ControlDeckTypedAbsence {
    ControlDeckTypedAbsence {
        scope: "task_readiness".to_string(),
        identity: session_id.to_string(),
        field: "ready_set".to_string(),
        reason: reason.to_string(),
    }
}

fn project_session(
    snapshot: &session_log_contract::SessionSnapshot,
) -> anyhow::Result<ControlDeckSession> {
    let state = snapshot.lifecycle_projection.state;
    let mut tasks = snapshot
        .lifecycle_projection
        .task_plan
        .detailed_tasks
        .iter()
        .map(|task| {
            Ok(ControlDeckTask {
                task_id: task.task_id.clone(),
                step: task.step,
                sub_session_id: task.sub_session_id.clone(),
                start_condition: serialized_enum_name(task.start_condition)?,
                status: serialized_enum_name(task.status)?,
                start_at_ms: task.start_at.timestamp_millis(),
            })
        })
        .collect::<anyhow::Result<Vec<_>>>()?;
    tasks.sort_by(|left, right| (left.step, &left.task_id).cmp(&(right.step, &right.task_id)));
    let mut runtime_ids = snapshot.lifecycle_projection.runtime_ids.clone();
    runtime_ids.sort();
    Ok(ControlDeckSession {
        session_id: snapshot.session_id.clone(),
        workspace_sha256: semantic_sha256(&Value::String(snapshot.workspace.clone())),
        parent_id: snapshot.lifecycle_projection.parent_id.clone(),
        state: serialized_enum_name(state)?,
        terminal: state.is_terminal(),
        name_sha256: snapshot
            .name
            .as_ref()
            .map(|name| semantic_sha256(&Value::String(name.clone()))),
        created_at_ms: snapshot.created_at,
        updated_at_ms: snapshot.updated_at,
        model: snapshot.metadata.model.clone(),
        agent: snapshot.metadata.agent.clone(),
        runtime_ids,
        active_runtime_id: snapshot.lifecycle_projection.active_runtime_id.clone(),
        tasks,
    })
}

fn project_runtime(
    runtime: session_log_contract::RuntimeLeaseSnapshot,
) -> anyhow::Result<ControlDeckRuntime> {
    let lifecycle = runtime.lifecycle.as_ref();
    Ok(ControlDeckRuntime {
        runtime_id: runtime.runtime_id,
        session_id: runtime.session_id,
        database_path_sha256: semantic_sha256(&Value::String(runtime.database_path)),
        commander_session_id: lifecycle.map(|value| value.commander_session_id.clone()),
        transaction_id: lifecycle.map(|value| value.transaction_id.clone()),
        task_id: lifecycle.and_then(|value| value.task_id.clone()),
        goal_id: lifecycle.and_then(|value| value.goal_id.clone()),
        lease_id: runtime.lease_id,
        lease_active: runtime.lease_active,
        revision: runtime.revision,
        last_event_seq: runtime.last_event_seq,
        terminal: runtime.terminal,
        session_event_seq: runtime.session_event_seq,
        runtime_state: runtime
            .runtime_state
            .map(serialized_enum_name)
            .transpose()?,
    })
}

fn project_missions(
    commander_session_ids: &[String],
    runtimes: &[ControlDeckRuntime],
    convergence: &[ControlDeckConvergenceEntry],
) -> anyhow::Result<Vec<ControlDeckMission>> {
    commander_session_ids
        .iter()
        .map(
            |commander_session_id| -> anyhow::Result<ControlDeckMission> {
                let goal_ids = runtimes
                    .iter()
                    .filter(|runtime| {
                        runtime.commander_session_id.as_deref()
                            == Some(commander_session_id.as_str())
                    })
                    .filter_map(|runtime| runtime.goal_id.clone())
                    .collect::<BTreeSet<_>>()
                    .into_iter()
                    .collect::<Vec<_>>();
                let mission_revision =
                    convergence_mission_revision(convergence, commander_session_id);
                let (mission_revision_sha256, mission_revision_status) = match mission_revision {
                    MissionRevisionProjection::Present(revision) => {
                        (Some(revision), "present".to_string())
                    }
                    MissionRevisionProjection::TypedAbsence => (None, "typed_absence".to_string()),
                    MissionRevisionProjection::TypedConflict => {
                        (None, "typed_conflict".to_string())
                    }
                };
                let availability = convergence
                    .iter()
                    .find(|entry| entry.commander_session_id == *commander_session_id)
                    .map(|entry| serialized_enum_name(entry.availability))
                    .transpose()?
                    .unwrap_or_else(|| "typed_absence".to_string());
                Ok(ControlDeckMission {
                    commander_session_id: commander_session_id.clone(),
                    mission_revision_sha256,
                    mission_revision_status,
                    goal_ids,
                    availability,
                })
            },
        )
        .collect()
}

enum MissionRevisionProjection {
    Present(String),
    TypedAbsence,
    TypedConflict,
}

fn convergence_mission_revision(
    convergence: &[ControlDeckConvergenceEntry],
    commander_session_id: &str,
) -> MissionRevisionProjection {
    let Some(admissions) = convergence
        .iter()
        .find(|entry| entry.commander_session_id == commander_session_id)
        .and_then(|entry| entry.projection.as_ref())
        .and_then(|projection| projection.get("admissions"))
        .and_then(Value::as_array)
    else {
        return MissionRevisionProjection::TypedAbsence;
    };
    let revisions = admissions
        .iter()
        .filter_map(|admission| admission.get("parent_mission_revision_sha256"))
        .filter_map(Value::as_str)
        .map(str::to_string)
        .collect::<BTreeSet<_>>();
    match revisions.len() {
        0 => MissionRevisionProjection::TypedAbsence,
        1 => MissionRevisionProjection::Present(
            revisions
                .into_iter()
                .next()
                .expect("one revision exists after exact cardinality check"),
        ),
        _ => MissionRevisionProjection::TypedConflict,
    }
}

fn limit_absence(scope: &str, limit: usize) -> ControlDeckTypedAbsence {
    ControlDeckTypedAbsence {
        scope: scope.to_string(),
        identity: "collection".to_string(),
        field: "remaining_items".to_string(),
        reason: format!("CONTROL_DECK_COLLECTION_LIMIT_REACHED:{limit}"),
    }
}

fn serialized_enum_name(value: impl Serialize) -> anyhow::Result<String> {
    serde_json::to_value(value)?
        .as_str()
        .map(str::to_string)
        .ok_or_else(|| anyhow::anyhow!("CONTROL_DECK_ENUM_SERIALIZATION_NOT_STRING"))
}

fn semantic_sha256(value: &Value) -> String {
    let bytes = serde_json::to_vec(value).expect("serializing serde_json::Value cannot fail");
    format!("{:x}", Sha256::digest(bytes))
}

fn is_lower_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .min(i64::MAX as u128) as i64
}

fn error_code(error: &impl std::fmt::Display) -> String {
    error
        .to_string()
        .split(':')
        .next()
        .unwrap_or("CONTROL_DECK_READ_FAILED")
        .to_string()
}

fn control_deck_read_error(error: anyhow::Error) -> (StatusCode, Json<Value>) {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({
            "schema_version": "tura_control_deck_error_v1",
            "blocker_code": error_code(&error),
            "authority_effect": "none",
        })),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn state_head_excludes_observation_time() {
        let identity = json!({
            "schema_version": "tura_commander_control_deck_snapshot_v1",
            "session_db_state_head": "a",
            "convergence_state_head": "b",
            "sessions": [],
        });
        assert_eq!(semantic_sha256(&identity), semantic_sha256(&identity));
    }

    #[test]
    fn error_projection_does_not_expose_detail() {
        let error = anyhow::anyhow!("ROUTER_UNAVAILABLE:/private/runtime/socket");
        let (_, Json(payload)) = control_deck_read_error(error);
        assert_eq!(payload["blocker_code"], "ROUTER_UNAVAILABLE");
        assert!(!payload.to_string().contains("/private/runtime/socket"));
    }

    #[test]
    fn enum_projection_uses_canonical_serde_name() {
        assert_eq!(
            serialized_enum_name(lifecycle::PlanStatus::WaitingUser).unwrap(),
            "waiting_user"
        );
    }

    #[test]
    fn mission_revision_conflict_is_not_ordered_by_digest() {
        let convergence = vec![ControlDeckConvergenceEntry {
            commander_session_id: "commander".to_string(),
            availability: ControlDeckConvergenceAvailability::Present,
            projection: Some(json!({
                "admissions": [
                    {"parent_mission_revision_sha256": "a".repeat(64)},
                    {"parent_mission_revision_sha256": "f".repeat(64)}
                ]
            })),
            blocker_code: None,
        }];
        assert!(matches!(
            convergence_mission_revision(&convergence, "commander"),
            MissionRevisionProjection::TypedConflict
        ));
    }

    #[test]
    fn resumable_stream_replays_only_when_state_head_differs() {
        let current = "a".repeat(64);
        assert!(!snapshot_changed(Some(&current), &current));
        assert!(snapshot_changed(Some(&"b".repeat(64)), &current));
        assert!(snapshot_changed(None, &current));
    }

    #[test]
    fn router_blocked_states_are_never_promoted_to_ready() {
        let entry = project_wire_readiness(
            "session-1",
            TaskReadySetEntry {
                task_id: "task-1".to_string(),
                state: TaskReadySetWireState::BlockedScope,
                reason_codes: vec!["TASK_SCOPE_CONFLICT".to_string()],
                blocking_task_ids: vec!["task-2".to_string()],
                dependency_task_ids: Vec::new(),
                active_runtime_count: 1,
                active_lease_ids: vec!["lease-1".to_string()],
                semantic_dispatch_key: Some("c".repeat(64)),
                task_scheduling_contract_sha256: Some("a".repeat(64)),
                scope_claim_sha256: Some("b".repeat(64)),
                maximum_parallel_runtime_workers: Some(6),
            },
        );
        assert_eq!(entry.state, ControlDeckTaskReadinessState::Blocked);
        assert_eq!(entry.blocking_task_ids, ["task-2"]);
        assert_eq!(entry.state_head.len(), 64);
        assert_eq!(
            entry.semantic_dispatch_key_sha256.as_deref(),
            Some("cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc")
        );
    }

    #[test]
    fn commander_parent_cycle_is_typed_instead_of_guessed() {
        let first = ControlDeckSession {
            session_id: "session-a".to_string(),
            workspace_sha256: "a".repeat(64),
            parent_id: Some("session-b".to_string()),
            state: "idle".to_string(),
            terminal: false,
            name_sha256: None,
            created_at_ms: 0,
            updated_at_ms: 0,
            model: None,
            agent: None,
            runtime_ids: Vec::new(),
            active_runtime_id: None,
            tasks: Vec::new(),
        };
        let second = ControlDeckSession {
            session_id: "session-b".to_string(),
            parent_id: Some("session-a".to_string()),
            ..first.clone()
        };
        let sessions = BTreeMap::from([
            (first.session_id.as_str(), &first),
            (second.session_id.as_str(), &second),
        ]);
        assert_eq!(
            commander_root_session_id("session-a", &sessions),
            Err("COMMANDER_SESSION_PARENT_CYCLE")
        );
    }

    #[test]
    fn ready_set_response_requires_exact_requested_identity_set() {
        let entries = vec![TaskReadySetEntry {
            task_id: "task-a".to_string(),
            state: TaskReadySetWireState::Ready,
            reason_codes: Vec::new(),
            blocking_task_ids: Vec::new(),
            dependency_task_ids: Vec::new(),
            active_runtime_count: 0,
            active_lease_ids: Vec::new(),
            semantic_dispatch_key: None,
            task_scheduling_contract_sha256: None,
            scope_claim_sha256: None,
            maximum_parallel_runtime_workers: None,
        }];
        assert!(ready_set_response_matches(
            &entries,
            &["task-a".to_string()]
        ));
        assert!(!ready_set_response_matches(
            &entries,
            &["task-b".to_string()]
        ));
        assert!(!ready_set_response_matches(
            &[entries[0].clone(), entries[0].clone()],
            &["task-a".to_string()]
        ));
    }

    #[test]
    fn brand_new_queue_uses_durable_task_contract_authority_revision() {
        let task = task_with_authority_revision("task-a", 'a');
        assert_eq!(
            ready_set_authority_revision(&[task], &["task-a".to_string()], None),
            Ok("a".repeat(64))
        );
    }

    #[test]
    fn matching_root_mission_revision_preserves_pre_admission_route() {
        let task = task_with_authority_revision("task-a", 'a');
        let mission = ControlDeckMission {
            commander_session_id: "commander".to_string(),
            mission_revision_sha256: Some("a".repeat(64)),
            mission_revision_status: "present".to_string(),
            goal_ids: Vec::new(),
            availability: "present".to_string(),
        };
        assert_eq!(
            ready_set_authority_revision(&[task], &["task-a".to_string()], Some(&mission)),
            Ok("a".repeat(64))
        );
    }

    #[test]
    fn conflicting_authority_revisions_fail_before_router_admission() {
        let tasks = [
            task_with_authority_revision("task-a", 'a'),
            task_with_authority_revision("task-b", 'b'),
        ];
        assert_eq!(
            ready_set_authority_revision(
                &tasks,
                &["task-a".to_string(), "task-b".to_string()],
                None
            ),
            Err("TASK_SCHEDULING_AUTHORITY_REVISION_TYPED_CONFLICT")
        );

        let mission = ControlDeckMission {
            commander_session_id: "commander".to_string(),
            mission_revision_sha256: Some("b".repeat(64)),
            mission_revision_status: "present".to_string(),
            goal_ids: Vec::new(),
            availability: "present".to_string(),
        };
        assert_eq!(
            ready_set_authority_revision(
                &[task_with_authority_revision("task-a", 'a')],
                &["task-a".to_string()],
                Some(&mission)
            ),
            Err("MISSION_REVISION_TYPED_CONFLICT")
        );
    }

    fn task_with_authority_revision(task_id: &str, revision: char) -> lifecycle::TaskStep {
        lifecycle::TaskStep {
            task_id: task_id.to_string(),
            scheduling_contract: Some(lifecycle::TaskSchedulingContractV1 {
                schema_version: "tura_task_scheduling_contract_v1".to_string(),
                mission_id: "mission".to_string(),
                semantic_dispatch_key: "c".repeat(64),
                authority_mission_revision_sha256: revision.to_string().repeat(64),
                delegated_input_sha256: "d".repeat(64),
                task_context_capsule_semantic_sha256: "e".repeat(64),
                dependency_task_ids: Vec::new(),
                exact_input_sha256s: Vec::new(),
                jspace_authorization_semantic_sha256: "f".repeat(64),
                read_scopes: Vec::new(),
                write_scopes: Vec::new(),
                declared_targets: Vec::new(),
                conflict_identities: Vec::new(),
                maximum_parallel_runtime_workers: 6,
            }),
            ..lifecycle::TaskStep::default()
        }
    }
}
