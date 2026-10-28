import assert from 'node:assert/strict';
import test from 'node:test';
import {summarizeRequestProvenance} from '../src/inference-attempt-evidence.mjs';

test('effort is observed from exact wire, not promoted to provider evidence', () => {
  for (const effort of ['xhigh', 'max', 'ultra']) {
    const wire = Buffer.from(JSON.stringify({reasoning: {effort}}));
    const before = Buffer.from(wire);
    const value = summarizeRequestProvenance({wire});
    assert.equal(value.reasoning.effort, effort);
    assert.equal(value.reasoning.scope, 'request_wire_not_provider_observed');
    assert.deepEqual(wire, before);
  }
});

test('missing or arbitrary effort remains unknown without logging arbitrary values', () => {
  for (const reasoning of [undefined, null, {}, {effort: 'private-value'}, {effort: 3}]) {
    const result = summarizeRequestProvenance({wire: Buffer.from(JSON.stringify({reasoning}))});
    assert.equal(result.reasoning.effort, 'UNKNOWN');
    assert.ok(!JSON.stringify(result).includes('private-value'));
  }
  assert.equal(summarizeRequestProvenance().status, 'UNKNOWN');
});
