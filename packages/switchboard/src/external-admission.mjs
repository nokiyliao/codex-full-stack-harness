import { SwitchboardError } from "./shared.mjs";

export const EXTERNAL_ORCHESTRATION_PROTOCOL = "responses-tool-loop-v2";
export const EXTERNAL_EFFECT_SCOPE = "local_inference_routing_only";

export function requireExternalAdmission(admission) {
  if (admission?.status !== "ADMITTED" || admission?.external_batch_admitted !== true) {
    throw new SwitchboardError("CAPABILITY_NOT_ADMITTED", "External model capability is not admitted.", 503);
  }
  if (admission?.protocol_compatibility?.external_model_orchestration !== EXTERNAL_ORCHESTRATION_PROTOCOL) {
    throw new SwitchboardError(
      "PROTOCOL_NOT_COMPATIBLE",
      `External model routing requires protocol ${EXTERNAL_ORCHESTRATION_PROTOCOL}.`,
      503,
    );
  }
  if (admission?.policy?.external_models !== "allow") {
    throw new SwitchboardError("EXTERNAL_MODELS_POLICY_DENIED", "External models are denied by local policy.", 403);
  }
  if (admission?.policy?.effect_scope !== EXTERNAL_EFFECT_SCOPE
    || admission?.authority_effect !== EXTERNAL_EFFECT_SCOPE) {
    throw new SwitchboardError("EXTERNAL_EFFECT_SCOPE_DENIED", "External model effect scope is not admitted.", 403);
  }
  return admission;
}

// Nokiy forwards to the official backend. The watcher's status demotion
// (installation inspection, external live tests) gates external providers;
// it must not take Nokiy offline. The static local policy still applies, and
// Nokiy's own deployment admission is checked by the caller.
export function requireNokiyRouteAdmission(admission) {
  if (admission?.policy?.external_models !== "allow") {
    throw new SwitchboardError("EXTERNAL_MODELS_POLICY_DENIED", "External models are denied by local policy.", 403);
  }
  if (admission?.policy?.effect_scope !== EXTERNAL_EFFECT_SCOPE
    || admission?.authority_effect !== EXTERNAL_EFFECT_SCOPE) {
    throw new SwitchboardError("EXTERNAL_EFFECT_SCOPE_DENIED", "External model effect scope is not admitted.", 403);
  }
  return admission;
}
