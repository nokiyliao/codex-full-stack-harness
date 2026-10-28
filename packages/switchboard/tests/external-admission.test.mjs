import assert from "node:assert/strict";
import test from "node:test";

import {
  EXTERNAL_EFFECT_SCOPE,
  EXTERNAL_ORCHESTRATION_PROTOCOL,
  requireExternalAdmission,
} from "../src/external-admission.mjs";

function admitted(overrides = {}) {
  return {
    schema_version: "codex_inference_switchboard_admission_v2",
    status: "ADMITTED",
    app_version: "26.1.0",
    external_batch_admitted: true,
    authority_effect: EXTERNAL_EFFECT_SCOPE,
    protocol_compatibility: { external_model_orchestration: EXTERNAL_ORCHESTRATION_PROTOCOL },
    policy: { external_models: "allow", effect_scope: EXTERNAL_EFFECT_SCOPE },
    ...overrides,
  };
}

test("older client version passes when capability and policy admit it", () => {
  assert.equal(requireExternalAdmission(admitted({ app_version: "1.0.0" })).app_version, "1.0.0");
});

test("insufficient protocol compatibility fails closed", () => {
  assert.throws(
    () => requireExternalAdmission(admitted({
      protocol_compatibility: { external_model_orchestration: "responses-tool-loop-v1" },
    })),
    (error) => error.code === "PROTOCOL_NOT_COMPATIBLE" && error.status === 503,
  );
});

test("explicit policy denial rejects an otherwise compatible client", () => {
  assert.throws(
    () => requireExternalAdmission(admitted({
      policy: { external_models: "deny", effect_scope: EXTERNAL_EFFECT_SCOPE },
    })),
    (error) => error.code === "EXTERNAL_MODELS_POLICY_DENIED" && error.status === 403,
  );
});

test("effect scope cannot expand beyond local inference routing", () => {
  assert.throws(
    () => requireExternalAdmission(admitted({
      policy: { external_models: "allow", effect_scope: "live_runtime" },
    })),
    (error) => error.code === "EXTERNAL_EFFECT_SCOPE_DENIED" && error.status === 403,
  );
});
