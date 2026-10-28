# SPDX-License-Identifier: MIT
"""Real Router/DB positional-read acceptance; scripted dispatch, no provider.

Uses the existing opt-in Router/DB fixture, not an authentication substitute.
The executor only relays real router results; it never manufactures receipts.
"""
import json
import os
from pathlib import Path
import shutil
import sys
import unittest

import test_full_core as fixtures
from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import local_context, result_inspection


DRIVER = r'''
import hashlib,json,os,pathlib,socket,sys
argv=sys.argv[1:]
def value(key): return argv[argv.index(key)+1]
workspace=pathlib.Path(value('-C'))
contract=json.loads(pathlib.Path(value('--jspace-contract')).read_text())
host,port=value('--router-address').rsplit(':',1)
digest=hashlib.sha256((workspace/'answer.txt').read_bytes()).hexdigest()
runtime_id='scripted-positional-read'
rows=[]
def call(label, arguments):
    line=json.dumps(arguments, separators=(',', ':'))
    execution=runtime_id+'.tool.command_run:'+label
    command={'command_type':'source_read','command_line':line,'step':1,
        'command_id':execution,'command_run_id':execution,
        'provider_tool_call_id':label,'command_index':0}
    payload={'session_id':value('--session-id'),'runtime_id':runtime_id,
        'session_directory':str(workspace),'arguments':{'execution_id':execution,
        'commands':[command]},'allowed_commands':['source_read'],
        'jspace_contract':contract,'sandbox':True}
    request={'request_id':execution,'kind':'call','method':'execution.command_run','payload':payload}
    with socket.create_connection((host,int(port)),timeout=20) as connection:
        connection.sendall((json.dumps(request)+'\n').encode())
        with connection.makefile('rb') as stream:
            reply=json.loads(stream.readline(1048576))
    assert reply['ok'],reply
    result=reply['payload']['result']['results'][0]
    rows.append({'label':label,'arguments':line,'result':result})
    print(json.dumps({'type':'item.completed','item':{'type':'command_execution',
        'status':'completed' if result['success'] else 'failed',
        'command_type':'source_read','command':line,'step':1,
        'aggregated_output':json.dumps(result.get('output', {}))}}),flush=True)
    return result
if MODE == 'rejection':
    for label,args,kind in [
        ('shape',['answer.txt','one',1],'json_syntax_or_shape'),
        ('digest',['answer.txt',1,1,'bad-digest'],'invalid_expected_sha256')]:
        result=call(label,args)
        assert not result['success'],result
        proof=result['output']['pre_execution_rejection']
        assert proof['rejection_kind']==kind,proof
        assert proof['process_started'] is False and proof['source_content_read'] is False,proof
        assert proof['mutation_count']==0,proof
else:
    for label,args in [('scope',['outside.txt',1,1]),('stale',['answer.txt',1,1,'0'*64])]:
        result=call(label,args)
        assert not result['success'],result
        assert 'pre_execution_rejection' not in result.get('output', {}),result
named=call('named',{'path':'answer.txt','start_line':1,'end_line':1,'expected_sha256':digest})
positional=call('positional',['answer.txt',1,1,digest])
assert named['success'] and positional['success'],rows
for key in ('stdout','source_sha256','start_line','end_line'):
    assert named['output'][key]==positional['output'][key],key
assert positional['output']['stdout']=='1: exact source evidence\n',positional
assert hashlib.sha256((workspace/'answer.txt').read_bytes()).hexdigest()==digest
state=pathlib.Path(os.environ['TURA_HOME'])
(state/'positional-readback.json').write_text(json.dumps(rows))
print(json.dumps({'type':'item.completed','item':{'type':'assistant_message','text':'scripted router contract check'}}))
print(json.dumps({'type':'turn.completed','status':'completed','usage':None,
    'model':value('-m'),'agent':value('-a'),'session_id':value('--session-id'),'cwd':value('-C'),
    'reasoning_effort':value('--model-reasoning-effort'),'service_tier':value('--service-tier')}))
'''


@unittest.skipUnless(sys.platform == 'darwin' and os.environ.get('NOKIY_VERIFIER_TEST_ROUTER')
                     and os.environ.get('NOKIY_VERIFIER_TEST_SESSION_DB'),
                     'requires exact candidate Router and Session DB')
class PositionalReadRuntimeTests(unittest.TestCase):
    def _execute(self, mode):
        native = {key: os.environ.get(key) for key in ('HOME', 'CODEX_HOME', 'CODEX_THREAD_ID')}
        helper = fixtures.FullCoreTests()
        self.addCleanup(helper.doCleanups)
        try:
            helper.setUp()
        finally:
            for key, value in native.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        f = helper.f
        f.native_thread_id = native['CODEX_THREAD_ID']
        for name, key in (('tura_router', 'NOKIY_VERIFIER_TEST_ROUTER'),
                          ('tura_session_db', 'NOKIY_VERIFIER_TEST_SESSION_DB')):
            origin = Path(os.environ[key]).resolve(strict=True)
            shutil.copy2(origin, f.runtime / name)
            self.assertEqual(caller._file_sha256(origin), caller._file_sha256(f.runtime / name))
        target = f.workspace / 'answer.txt'
        target.write_text('exact source evidence\n')
        (f.workspace / 'outside.txt').write_text('must not be disclosed\n')
        action = {
            'mission': {'mission_id': 'positional-contract', 'task_id': mode,
                        'mode': 'DELIVERY', 'objective': 'Check exact Router source-read semantics',
                        'current_predicate': 'installed positional read not verified'},
            'context_summary': 'Scripted dispatch, real Router/DB, no provider or authentication.',
            'operations': ['read', 'command'], 'source_read': True,
            'read_scopes': ['answer.txt'], 'write_scopes': [], 'target_paths': [],
            'command_templates': [], 'forbidden_effects': ['network', 'delete', 'modify'],
        }
        capsule, contract = local_context.compile_context(f.workspace, action, artifact_root=f.artifacts)
        f.context.write_text(json.dumps(capsule))
        f.jspace.write_text(json.dumps(contract))
        f._write_executable(f.runtime / 'tura_exec',
                            f'#!{sys.executable}\nMODE={mode!r}\n' + DRIVER)
        helper._image()
        helper._request()
        value = json.loads(f.request_path.read_text())
        value.update(authority_effect='none', timeout_seconds=40)
        f.request_path.write_text(json.dumps(value))
        request = caller.load_request(f.request_path)
        terminal = caller.execute(request)
        run = request.artifact_root / request.request_id
        self.assertEqual(terminal['status'], 'RESULT_AVAILABLE', {
            'blocker': terminal.get('first_typed_blocker'),
            'stderr': (run / 'core.stderr').read_text()[-4000:]})
        self.assertTrue(terminal['cleanup_pass'], terminal)
        self.assertTrue(terminal['cleanup']['no_live_descendants'], terminal)
        self.assertEqual(target.read_text(), 'exact source evidence\n')
        self.assertEqual({key: os.environ.get(key) for key in native}, native)
        rows = json.loads((run / 'execution-state/positional-readback.json').read_text())
        self.assertEqual(len(rows), 4)
        inspected = result_inspection.inspect(request.artifact_root, request.request_id,
                                              request.native_thread_id)
        self.assertEqual(inspected['counts']['failed'], 2)
        self.assertEqual(inspected['command_success'], 'known_failure')
        return inspected, rows, run, request

    def test_real_positional_rejections_reconcile_only_with_physical_witness(self):
        inspected, rows, run, request = self._execute('rejection')
        self.assertEqual(inspected['status'], 'EVIDENCE_VERIFIED', {
            'blocker': inspected['first_blocker'], 'proof': rows[0]['result']['output']})
        self.assertEqual(inspected['source_read_rejection_reconciliation'], {
            'schema_version': 'nokiy_source_read_rejection_reconciliation_v1',
            'not_attempted_count': 2, 'unresolved_count': 0,
            'effective_command_success': 'known_success'})
        original = json.loads((run / 'request-identity.json').read_text())
        contract = json.loads((run / 'jspace-original.json').read_text())
        for row in rows[:2]:
            item = {'status': 'failed', 'command_type': 'source_read',
                    'command': row['arguments'], 'step': 1}
            result_inspection._source_read_rejection(
                run, item, row['result']['output'], original, contract)
            # Equivalent decoded JSON is not the authenticated raw command.
            with self.assertRaisesRegex(result_inspection.InspectionError,
                                        'SOURCE_READ_REJECTION_INVALID'):
                result_inspection._source_read_rejection(
                    run, {**item, 'command': ' ' + item['command']},
                    row['result']['output'], original, contract)
        witnesses = sorted((run / 'execution-state/command_receipts').glob('source-read-preexecution-*.json'))
        self.assertEqual(len(witnesses), 2)
        # Move one disposable witness away: in-memory claims must not suffice.
        witnesses[0].rename(witnesses[0].with_suffix('.withheld'))
        invalid = result_inspection.inspect(request.artifact_root, request.request_id,
                                            request.native_thread_id)
        self.assertNotEqual(invalid['status'], 'EVIDENCE_VERIFIED', invalid)

    def test_real_scope_and_stale_hash_failures_are_not_parser_non_attempts(self):
        inspected, rows, _, _ = self._execute('semantic')
        self.assertEqual(inspected['status'], 'INCOMPLETE_EVIDENCE', inspected)
        self.assertEqual(inspected['first_blocker'], 'COMMAND_FAILURE', inspected)
        self.assertNotIn('source_read_rejection_reconciliation', inspected)
        self.assertNotIn('must not be disclosed', json.dumps(rows))


if __name__ == '__main__':
    unittest.main()
