export type ControlDeckFreshness = "current" | "degraded" | "truncated";

export type ControlDeckTypedAbsence = {
  scope: string;
  identity: string;
  field: string;
  reason: string;
};

export type ControlDeckTask = {
  task_id: string;
  step: number;
  sub_session_id: string;
  start_condition: string;
  status: string;
  start_at_ms: number;
};

export type ControlDeckTaskReadinessState = "ready" | "blocked" | "typed_unknown" | "terminal";

export type ControlDeckTaskReadiness = {
  session_id: string;
  task_id: string;
  semantic_dispatch_key_sha256?: string | null;
  task_scheduling_contract_sha256?: string | null;
  state: ControlDeckTaskReadinessState;
  reason_codes: string[];
  dependency_task_ids: string[];
  blocking_task_ids: string[];
  scope_claim_sha256?: string | null;
  active_lease_ids: string[];
  active_runtime_count: number;
  maximum_parallel_runtime_workers?: number | null;
  state_head: string;
};

export type ControlDeckSession = {
  session_id: string;
  workspace_sha256: string;
  parent_id?: string | null;
  state: string;
  terminal: boolean;
  name_sha256?: string | null;
  created_at_ms: number;
  updated_at_ms: number;
  model?: string | null;
  agent?: string | null;
  runtime_ids: string[];
  active_runtime_id?: string | null;
  tasks: ControlDeckTask[];
};

export type ControlDeckRuntime = {
  runtime_id: string;
  session_id: string;
  database_path_sha256: string;
  commander_session_id?: string | null;
  transaction_id?: string | null;
  task_id?: string | null;
  goal_id?: string | null;
  lease_id?: string | null;
  lease_active: boolean;
  revision: number;
  last_event_seq: number;
  terminal: boolean;
  session_event_seq: number;
  runtime_state?: string | null;
};

export type ControlDeckMission = {
  commander_session_id: string;
  mission_revision_sha256?: string | null;
  mission_revision_status: "present" | "typed_absence" | "typed_conflict";
  goal_ids: string[];
  availability: string;
};

export type ControlDeckConvergenceEntry = {
  commander_session_id: string;
  availability: "present" | "typed_absence" | "unavailable";
  projection?: {
    schema_version?: string;
    state_head?: string;
    admissions?: unknown[];
    receipts?: Array<{ stage?: string; terminal_state?: string }>;
    callbacks?: Array<{ stage?: string; terminal_state?: string }>;
    continuations?: Array<{ state?: string; requested_action?: string }>;
    last_blocker?: { code?: string };
  } | null;
  blocker_code?: string | null;
};

export type ControlDeckSnapshot = {
  schema_version:
    | "tura_commander_control_deck_snapshot_v1"
    | "tura_commander_control_deck_snapshot_v2";
  state_head: string;
  session_db_state_head: string;
  convergence_state_head: string;
  ready_set_state_head?: string;
  observed_at_ms: number;
  freshness_status: ControlDeckFreshness;
  authority_effect: "none";
  truncated: boolean;
  missions: ControlDeckMission[];
  sessions: ControlDeckSession[];
  runtimes: ControlDeckRuntime[];
  ready_set?: ControlDeckTaskReadiness[];
  convergence: ControlDeckConvergenceEntry[];
  typed_absences: ControlDeckTypedAbsence[];
};

export type ControlDeckChange = {
  schema_version: "tura_control_deck_change_v1";
  state_head: string;
  freshness_status: ControlDeckFreshness;
  authority_effect: "none";
};
