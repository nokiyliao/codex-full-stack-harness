import assert from 'node:assert/strict';
import { test } from 'node:test';
import { nativeToolProjection, nativeToolItem } from '../src/nokiy-native-tool.mjs';

for (const workflow of ['nokiy', 'nokiy-direct']) {
  test(`${workflow} projects original preparation recovery without a worker action`, () => {
    const projection = nativeToolProjection({ tools: [{ type: 'function', name: 'exec_command' }] }, {
      workflow, worker: 'gpt-6.1-sol', workerEffort: 'max',
      host: { python: '/stub/python', script: '/stub/host.py' },
    });
    const tool = projection.body.tools.at(-1);
    assert.ok(tool.parameters.properties.operation.enum.includes('read_preparation'));
    assert.match(tool.description, /starts no model/);
    const payload = { operation: 'read_preparation', artifact_root: '/task', input_id: 'native-input-' + 'a'.repeat(64) };
    const item = nativeToolItem({ type: 'function_call', name: 'nokiy_execute', arguments: JSON.stringify(payload) }, projection);
    const args = JSON.parse(item.arguments);
    const encoded = args.cmd.match(/'([A-Za-z0-9_-]+)'$/)[1];
    assert.deepEqual(JSON.parse(Buffer.from(encoded, 'base64url')).payload, payload);
    for (const invalid of [{ ...payload, input_id: '../bad' }, { ...payload, native_thread_id: 'foreign' }]) {
      assert.throws(() => nativeToolItem({ type: 'function_call', name: 'nokiy_execute', arguments: JSON.stringify(invalid) }, projection), /reference required/);
    }
  });
}
