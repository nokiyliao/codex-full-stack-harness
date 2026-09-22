import type { JsonObject } from "./common.js";
import type { RegisterChildSessionResponse } from "./session.js";

export const COMMANDER_DISPATCH_PROTOCOL_VERSION = "tura_commander_dispatch_protocol_v1";
export const COMMANDER_TASK_PACKET_SCHEMA_VERSION = "tura_commander_task_packet_v1";

export type CommanderTaskPacket = JsonObject;

export interface CommanderTaskPacketCapabilities {
  schema_version: "tura_commander_task_packet_capabilities_v1";
  protocol_version: string;
  task_packet_schema_versions: string[];
  callback_delivery_route: "trusted_tura_direct_thread_writer";
  compile_only: boolean;
  idempotent_replay: boolean;
}

export interface CommanderTaskPacketCompileResult {
  schema_version: "tura_commander_task_packet_compile_result_v1";
  protocol_version: string;
  task_packet_schema_version: string;
  compile_identity_sha256: string;
  semantic_dispatch_key: string;
  parent_task_plan_sha256: string;
  task_scheduling_contract_sha256: string;
  scope_claim_sha256: string;
  task_context_capsule_semantic_sha256: string;
  jspace_authorization_semantic_sha256: string;
  delegated_input_sha256: string;
  parent_session_id: string;
  parent_mission_revision_sha256: string;
  commander_thread_id: string;
  task_id: string;
  child_session_id: string;
  child_runtime_id: string;
  child_transaction_id: string;
  child_lease_id: string;
  callback_request_id: string;
  effect_id: string;
  callback_delivery_route: "trusted_tura_direct_thread_writer";
  authority_effect: "none";
  mutation_counts: { parent_claim: 0; child: 0; runtime: 0; callback: 0 };
}

export interface CommanderTaskPacketDispatchResponse {
  schema_version: "tura_commander_task_packet_dispatch_response_v1";
  compilation: CommanderTaskPacketCompileResult;
  admission: RegisterChildSessionResponse;
  duplicate_effect_count: number;
  commander_mission_verification_required: boolean;
}
