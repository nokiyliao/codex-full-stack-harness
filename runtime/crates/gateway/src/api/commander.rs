use axum::Json;
use axum::extract::rejection::JsonRejection;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use router_contract::{CommanderMutationCounts, CommanderTaskPacketV1};
use serde_json::json;

use crate::router_client::RouterClient;

pub async fn task_packet_capabilities() -> Response {
    match tokio::task::spawn_blocking(|| {
        RouterClient::global().commander_task_packet_capabilities()
    })
    .await
    {
        Ok(Ok(capabilities)) => Json(capabilities).into_response(),
        Ok(Err(error)) => task_packet_error(
            StatusCode::UPGRADE_REQUIRED,
            "capability_readback",
            &error.to_string(),
            true,
        ),
        Err(error) => task_packet_error(
            StatusCode::BAD_GATEWAY,
            "capability_readback",
            &format!("router capability task failed: {error}"),
            true,
        ),
    }
}

pub async fn compile_task_packet(
    payload: Result<Json<CommanderTaskPacketV1>, JsonRejection>,
) -> Response {
    let packet = match payload {
        Ok(Json(packet)) => packet,
        Err(error) => {
            return task_packet_error(
                StatusCode::BAD_REQUEST,
                "pre_admission",
                &format!("TASK_PACKET_WIRE_INVALID:{}", error.body_text()),
                true,
            );
        }
    };
    match tokio::task::spawn_blocking(move || {
        RouterClient::global().compile_commander_task_packet(packet)
    })
    .await
    {
        Ok(Ok(compilation)) => Json(compilation).into_response(),
        Ok(Err(error)) => task_packet_error(
            task_packet_status(&error.to_string()),
            "pre_admission",
            &error.to_string(),
            true,
        ),
        Err(error) => task_packet_error(
            StatusCode::BAD_GATEWAY,
            "pre_admission",
            &format!("router compile task failed: {error}"),
            true,
        ),
    }
}

pub async fn dispatch_task_packet(
    payload: Result<Json<CommanderTaskPacketV1>, JsonRejection>,
) -> Response {
    let packet = match payload {
        Ok(Json(packet)) => packet,
        Err(error) => {
            return task_packet_error(
                StatusCode::BAD_REQUEST,
                "pre_admission",
                &format!("TASK_PACKET_WIRE_INVALID:{}", error.body_text()),
                true,
            );
        }
    };
    match tokio::task::spawn_blocking(move || {
        RouterClient::global().dispatch_commander_task_packet(packet)
    })
    .await
    {
        Ok(Ok(response)) => Json(response).into_response(),
        Ok(Err(error)) => {
            let message = error.to_string();
            let pre_admission = message.contains("TASK_PACKET_PRE_ADMISSION:");
            task_packet_error(
                task_packet_status(&message),
                if pre_admission {
                    "pre_admission"
                } else {
                    "post_compile_dispatch"
                },
                &message,
                pre_admission,
            )
        }
        Err(error) => task_packet_error(
            StatusCode::BAD_GATEWAY,
            "post_compile_dispatch",
            &format!("router dispatch task failed: {error}"),
            false,
        ),
    }
}

fn task_packet_status(message: &str) -> StatusCode {
    if message.contains("NOT_FOUND") {
        StatusCode::NOT_FOUND
    } else if message.contains("CONFLICT") || message.contains("MISMATCH") {
        StatusCode::CONFLICT
    } else if message.contains("INVALID")
        || message.contains("UNSUPPORTED")
        || message.contains("MISSING")
        || message.contains("OUTSIDE")
    {
        StatusCode::BAD_REQUEST
    } else {
        StatusCode::BAD_GATEWAY
    }
}

fn task_packet_error(
    status: StatusCode,
    phase: &str,
    message: &str,
    proven_zero_effect: bool,
) -> Response {
    let code = message
        .split(':')
        .find(|part| part.starts_with("TASK_PACKET_") && *part != "TASK_PACKET_PRE_ADMISSION")
        .unwrap_or("TASK_PACKET_PROTOCOL_UNAVAILABLE");
    let mutation_counts = proven_zero_effect.then(CommanderMutationCounts::zero);
    let authority_effect = if proven_zero_effect {
        "none"
    } else {
        "unsettled"
    };
    (
        status,
        Json(json!({
            "schema_version": "tura_commander_task_packet_error_v1",
            "code": code,
            "phase": phase,
            "message": message,
            "authority_effect": authority_effect,
            "auto_retry_allowed": false,
            "commander_mission_verification_required": !proven_zero_effect,
            "mutation_counts": mutation_counts,
        })),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pre_admission_error_is_typed_zero_effect_and_never_retryable() {
        let response = task_packet_error(
            StatusCode::CONFLICT,
            "pre_admission",
            "TASK_PACKET_PRE_ADMISSION:TASK_PACKET_MISSION_REVISION_MISMATCH",
            true,
        );
        assert_eq!(response.status(), StatusCode::CONFLICT);
        let counts = CommanderMutationCounts::zero();
        assert_eq!(counts.parent_claim, 0);
        assert_eq!(counts.child, 0);
        assert_eq!(counts.runtime, 0);
        assert_eq!(counts.callback, 0);
    }
}
