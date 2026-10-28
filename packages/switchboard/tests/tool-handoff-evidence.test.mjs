import assert from 'node:assert/strict';
import test from 'node:test';
import { summarizeToolHandoff, createInferenceAttemptEvidence } from '../src/inference-attempt-evidence.mjs';

test('tool handoff correlates hashed calls and handles without retaining content', () => {
  const rows = summarizeToolHandoff([
    { type: 'custom_tool_call', name: 'exec', call_id: 'call_private', input: 'await tools.apply_patch("SECRET");' },
    { type: 'custom_tool_call_output', call_id: 'call_private', output: [
      { type: 'input_text', text: 'Script running with cell ID private_cell\nWall time 1 seconds\nOutput:\nSECRET' }] },
    { type: 'function_call', name: 'wait', call_id: 'wait_private', arguments: '{"cell_id":"private_cell"}' },
  ]).entries;
  assert.equal(rows[0].call_id, rows[1].call_id);
  assert.equal(rows[1].cell_id, rows[2].wait_handle);
  assert.deepEqual(rows[0].code_hints, ['apply_patch']);
  assert.equal(rows[1].native_cell_state, 'running');
  for (const secret of ['SECRET', 'private_cell', 'call_private']) assert.ok(!JSON.stringify(rows).includes(secret));
});

test('unknown outputs and bounded history do not imply completion', () => {
  const input = Array.from({length: 20}, (_, i) => ({type:'function_call_output',call_id:`c${i}`,output:'opaque'}));
  const result = summarizeToolHandoff(input);
  assert.equal(result.entries.length, 16);
  assert.equal(result.preceding_items_omitted, 4);
  assert.ok(result.entries.every(row => row.native_cell_state === 'unknown'));
  assert.equal(summarizeToolHandoff([]), null);
});

test('attempt terminal retains tool metadata but not generated code', () => {
  const records = [];
  const tracker = createInferenceAttemptEvidence({ emit: row => records.push(row), now: () => 10 });
  const request = tracker.start({requestId:'r',modelRequested:'nokiy-efficient',transport:'http'});
  request.dispatch('gpt-6.1-sol');
  request.observeOutput({type:'response.output_item.done',item:{type:'custom_tool_call',name:'exec',
    call_id:'call_x',input:'await tools.apply_patch("SECRET");'}});
  request.finish({outcome:'success'});
  const terminal = records.find(row => row.event_type === 'attempt_terminal');
  assert.equal(terminal.provider_output_deltas.completed_tool_calls[0].name, 'exec');
  assert.ok(!JSON.stringify(records).includes('SECRET'));
});
