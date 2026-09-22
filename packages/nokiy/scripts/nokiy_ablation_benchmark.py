"""Finite read-only Direct ablation; never installs or changes production routes."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time

U = Path('/Users/nokiy/Documents/unified_trading_model')
P = Path('/Users/nokiy/Documents/Codex/2026-08-31/codex-collaboration-harness')
R = Path('/Volumes/NOKIY-TB5/UTM/compute_worktrees/tura-oneshot-simplify-20260921-v1')
RELEASE = Path('/Volumes/NOKIY-TB5/UTM/runtime_releases/codex-collaboration-harness/nokiy-20260922-v3')
OUT = Path('/Volumes/NOKIY-TB5/UTM/rebuildable_caches/nokiy-direct-ablation-20260922-v2')
PYTHON = RELEASE/'caller/bin/python'
SCRIPT = Path(__file__).resolve()
SOURCES = ['scripts/ops/dcf/jspace.py', 'scripts/ops/dcf/task_context.py']


def identity(path):
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def save(path, data):
    with path.open('x') as stream:
        json.dump(data, stream, ensure_ascii=True, indent=2)
        stream.write('\n')


def check(records):
    for row in records:
        assert identity(Path(row['path'])) == row, 'SOURCE_DRIFT:' + row['path']


def call(argv, **kwargs):
    result = subprocess.run(list(map(str, argv)), capture_output=True, text=True, **kwargs)
    assert result.returncode == 0, (result.stderr[-1500:], result.stdout[-1500:])
    return json.loads(result.stdout)


def select_argv(argv, arm):
    assert arm in {'clean', 'full'}
    argv = list(argv)
    if arm == 'clean':
        for option in ('--task-context-capsule', '--jspace-contract'):
            index = argv.index(option)
            del argv[index:index+2]
    assert '--sandbox' in argv and argv[argv.index('-a')+1] == 'direct'
    assert all(option not in argv for option in ('--no-sandbox', '--disable-permission-restrictions'))
    return argv + ['--log']


def sandbox_policy():
    # Same policy for both arms. Runtime receipts are the only workspace writes.
    return ('(version 1) (allow default) (deny signal) '
            '(allow signal (target same-sandbox)) '
            f'(deny file-write* (subpath "{U}")) '
            f'(allow file-write* (subpath "{U}/.tura")) '
            f'(deny file-write* (subpath "{RELEASE}")) '
            f'(deny file-write* (subpath "{P}")) '
            f'(deny file-write* (subpath "{R}")) '
            f'(deny file-write* (literal "{Path.home()}/.codex/config.toml"))')


def readonly_probe():
    for relative in SOURCES:
        path = U/relative
        assert path.is_file()
        try:
            descriptor = os.open(path, os.O_WRONLY)
        except PermissionError:
            continue
        else:
            os.close(descriptor)
            raise RuntimeError('BENCHMARK_READONLY_SANDBOX_MISSING')


def _engine(spec_path):
    from codex_collaboration_harness import full_core as core
    from codex_collaboration_harness import embedded_nokiy as caller
    spec = json.loads(spec_path.read_text())
    arm = spec['arm']
    readonly_probe()
    original_argv, original_environment = core._cli_argv, core._environment

    def argv(request, runtime, run, address):
        selected = select_argv(original_argv(request, runtime, run, address), arm)
        save(run/'observed-argv.json', selected)
        return selected

    def environment(request, runtime, state):
        env = original_environment(request, runtime, state)
        env['NOKIY_ABLATION_READONLY_ROOT'] = str(U)
        return env

    core._cli_argv, core._environment = argv, environment
    if arm == 'clean':
        # Process-local experiment only: clean does not validate/load DCF inputs.
        # All binary, thread, scope supervisor and native sandbox checks stay on.
        caller._verify_context = lambda request: ({'benchmark_arm': 'clean'}, None, None)
    result = core._engine(spec_path)
    logs = (spec_path.parent/'core.stderr').read_text().splitlines()
    summaries = [json.loads(line.removeprefix('TURA_TURN_LOG ')) for line in logs
                 if line.startswith('TURA_TURN_LOG ')]
    assert len(summaries) == 1, 'TURN_LOG_NOT_OBSERVED'
    result['turn_log'] = summaries[0]
    result['arm'] = arm
    return result


def supervise(spec_path, parent):
    from codex_collaboration_harness.graph_process import supervise as bounded_supervise
    cancelled = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: cancelled.set())
    outcome = bounded_supervise([str(PYTHON), '-B', str(SCRIPT), 'engine', str(spec_path)],
        cwd=str(U), env=dict(os.environ), input_bytes=b'', timeout=240,
        cancelled=cancelled, max_stdout=1048576, max_stderr=65536, parent_pid=parent)
    result = json.loads(outcome.stdout) if outcome.stdout else {}
    return {'result': result, 'scope': outcome.scope, 'failure': outcome.failure,
            'returncode': outcome.returncode, 'stderr': outcome.stderr.decode('utf-8', errors='replace')}


def prepare():
    lease = json.loads((P/'output/triagent_runtime/codex_task_harness/active/nokiy-ablation-driver-20260922-v1.json').read_text())
    assert lease['ok'] and lease['thread_id'] == os.environ['CODEX_THREAD_ID']
    from codex_collaboration_harness import embedded_nokiy as caller
    from codex_collaboration_harness import full_core
    caller.verify_runtime_image(caller.FileIdentity(path=RELEASE/'runtime-image.json',
        sha256=identity(RELEASE/'runtime-image.json')['sha256']), required_artifacts=full_core.REQUIRED)
    OUT.mkdir()
    image = json.loads((RELEASE/'runtime-image.json').read_text())
    original_root = Path(image['runtime_root'])
    root = OUT/'runtime'
    for name, row in image['artifacts'].items():
        destination = root/Path(row['path']).relative_to(original_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = Path('/Volumes/NOKIY-TB5/UTM/rebuildable_caches/cargo-target/debug/tura_runtime') if name == 'tura_runtime' else Path(row['path'])
        shutil.copy2(source, destination)
        image['artifacts'][name] = {**row, **identity(destination), 'size': destination.stat().st_size}
    image.update(runtime_root=str(root), runtime_build_identity='nokiy-readonly-ablation-candidate-v1')
    save(OUT/'runtime-image.json', image)
    questions = [
        ('digest-versus-domain',
         'Trace canonical_contract_bytes and validate_capsule_payload_v1 across the two files. Return JSON with these exact keys: '
         'v2_authorization_checked_before_content (boolean); capsule_domain_checked_before_digest (boolean); '
         'capsule_explicit_null_task_id_allowed (boolean); capsule_absent_task_id_allowed_by_domain_validator (boolean); '
         'v2_bad_authorization_error (string); capsule_bad_digest_error (string).',
         {'v2_authorization_checked_before_content': True, 'capsule_domain_checked_before_digest': True,
          'capsule_explicit_null_task_id_allowed': False, 'capsule_absent_task_id_allowed_by_domain_validator': True,
          'v2_bad_authorization_error': 'JSPACE_AUTHORIZATION_DIGEST_MISMATCH',
          'capsule_bad_digest_error': 'JSPACE_SEMANTIC_DIGEST_MISMATCH'}),
        ('validation-versus-presentation',
         'Trace render_task_context_capsule into render_context_v1 across the two files. Return JSON with these exact keys: '
         'canonical_verification_precedes_render (boolean); expected_task_id_mismatch_error (string); '
         'expected_jspace_mismatch_error (string); decimal_json_summary_remains_string (boolean); '
         'duplicate_json_keys_summary_remains_string (boolean); nested_inheritance_schema (string); '
         'render_mutates_input (boolean).',
         {'canonical_verification_precedes_render': True, 'expected_task_id_mismatch_error': 'TASK_CONTEXT_TASK_MISMATCH',
          'expected_jspace_mismatch_error': 'TASK_CONTEXT_JSPACE_MISMATCH', 'decimal_json_summary_remains_string': True,
          'duplicate_json_keys_summary_remains_string': True, 'nested_inheritance_schema': 'utm-dcf-generic-task/v1',
          'render_mutates_input': False}),
    ]
    start = time.monotonic()
    nav = call([U/'.venv/bin/python', '-B', U/'scripts/ops/dcf.py', 'query', '--capability',
                'source-navigation', '--target', 'symbol:scripts.ops.dcf.jspace.render_task_context_capsule',
                '--depth', '1', '--json'], cwd=U)
    assert nav['freshness_status'] == 'current' and nav['result']['_projection']['complete']
    nav_seconds = time.monotonic()-start
    save(OUT/'navigation.json', nav)
    rows = []
    for index, (name, question, expected) in enumerate(questions):
        case = OUT/name; case.mkdir()
        mission = {'mission_id': OUT.name, 'task_id': name, 'mode': 'DELIVERY',
                   'objective': question, 'current_predicate': 'source_trace_correct'}
        action = {'operations': ['read', 'command'], 'read_scopes': SOURCES, 'write_scopes': [],
                  'target_paths': SOURCES, 'command_templates': ['cat '+path for path in SOURCES],
                  'focused_verifiers': [], 'mission': mission,
                  'forbidden_effects': ['write', 'broker', 'deployment', 'network_tools'],
                  'task_projection': {'schema_version': 'nokiy-dcf-source-navigation/v1', 'task_id': name,
                    'projection_kind': 'source_navigation', 'target': SOURCES[0], 'capability_ids': ['source-navigation'],
                    'task_visible_pre_task_evidence_only': True, 'answer_key_used': False, 'data': nav}}
        start = time.monotonic()
        compiled = call([U/'.venv/bin/python','-B',U/'scripts/ops/dcf.py','jspace','compile',
                         '--surface-id','research_execution_engine','--action-stdin','--inline','--json'],
                        cwd=U, input=json.dumps(action))
        preparation_seconds = time.monotonic()-start
        save(case/'capsule.json', compiled['task_context_capsule']); save(case/'jspace.json', compiled['contract'])
        prompt = ('Read-only repository analysis. Read the necessary source before answering. '
                  'Only these commands are allowed: '+json.dumps(['cat '+path for path in SOURCES])+'. '
                  'Do not edit, delegate, use network tools or execute other commands. '
                  +question+' Return only JSON without markdown.')
        for arm in (['clean','full'] if index == 0 else ['full','clean']):
            folder = case/arm; folder.mkdir()
            request = json.loads((RELEASE/'acceptance/request.json').read_text())
            request.update(runtime_image=identity(OUT/'runtime-image.json'), artifact_root=str(folder),
                           context_capsule=identity(case/'capsule.json'), jspace_contract=identity(case/'jspace.json'),
                           prompt=prompt, execution_profile='direct', timeout_seconds=240,
                           max_context_age_seconds=7200, native_thread_id=os.environ['CODEX_THREAD_ID'], max_result_bytes=8192)
            save(folder/'execution.json', {'request':request,'arm':arm})
            save(folder/'capsule.json', compiled['task_context_capsule']); save(folder/'jspace.json', compiled['contract'])
            with (folder/'prompt.txt').open('x') as file:file.write(prompt)
            rows.append({'case':name,'arm':arm,'spec':identity(folder/'execution.json'), 'expected':expected,
                         'preparation_seconds':preparation_seconds if arm == 'full' else 0})
    save(OUT/'protocol.json', {
        'MISSION':'Direct clean versus DCF plus JSpace paired behavior tasks',
        'FIRST_FALSE_PREDICATE':'Both isolated arms execute and independent answers are correct',
        'SHORTEST_VALID_ROUTE':'Two cases, clean/full then full/clean; no retries',
        'EXPECTED_PREDICATE_DELTA':'Full combination accuracy, reported usage, time and tool observations',
        'ABANDON_IF':'Sandbox failure, source drift, uncertain cleanup or provider failure',
        'trials':rows, 'shared_dcf_navigation_seconds':nav_seconds, 'sandbox':sandbox_policy(),
        'source':[identity(U/p) for p in SOURCES]+[identity(SCRIPT),identity(RELEASE/'runtime-image.json'),
                  identity(Path.home()/'.codex/config.toml')],
        'candidate_assets':list(image['artifacts'].values()), 'provider_calls_max':4,
        'parent_orchestration_tokens':None, 'scope':'read-only cross-file analysis, not editing productivity'})
    print(json.dumps({'prepared':True,'protocol':str(OUT/'protocol.json'),'trials':len(rows)}))


def run():
    protocol = json.loads((OUT/'protocol.json').read_text())
    results = []
    for trial in protocol['trials']:
        check(protocol['source']); check([trial['spec']])
        check([{'path':r['path'],'sha256':r['sha256']} for r in protocol['candidate_assets']])
        folder = Path(trial['spec']['path']).parent
        save(folder/'attempt.json', {'spec':trial['spec'],'provider_execution_limit':1})
        started = time.monotonic()
        command = ['/usr/bin/sandbox-exec','-p',protocol['sandbox'],str(PYTHON),'-B',str(SCRIPT),
                   'supervise',trial['spec']['path'],'--parent-pid',str(os.getpid())]
        response = subprocess.run(command,capture_output=True,text=True,timeout=285,cwd=U)
        elapsed = time.monotonic()-started
        save(folder/'transport.json', {'code':response.returncode,'stdout':response.stdout,'stderr':response.stderr})
        terminal = json.loads(response.stdout)
        save(folder/'outcome.json',terminal)
        scope=terminal['scope']
        assert scope.get('engine_reaped') and scope.get('no_live_descendants') and not scope.get('cleanup_error'), 'CLEANUP_UNPROVEN'
        assert not terminal['failure'] and terminal['returncode']==0, terminal
        result=terminal['result']
        try:
            answer=json.loads(result['final_text'])
            correct=json.dumps(answer,sort_keys=True)==json.dumps(trial['expected'],sort_keys=True)
        except ValueError: answer=None;correct=False
        row={'case':trial['case'],'arm':trial['arm'],'correct':correct,'answer':answer,
             'wall_seconds':elapsed,'usage':result.get('usage'),'turn_log':result['turn_log'],
             'cleanup_pass':True,'observed_model':None,'observed_service_tier':None}
        save(folder/'score.json',row);results.append(row)
        check(protocol['source'])
        print(json.dumps({k:v for k,v in row.items() if k not in {'turn_log','answer'}}),flush=True)
    save(OUT/'results.json',results)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['prepare','run','engine','supervise','probe'])
    parser.add_argument('spec',nargs='?',type=Path);parser.add_argument('--parent-pid',type=int)
    args=parser.parse_args()
    if args.mode=='prepare':prepare()
    elif args.mode=='run':run()
    elif args.mode=='probe':readonly_probe();print('{"readonly":true}')
    elif args.mode=='engine':print(json.dumps(_engine(args.spec)))
    else: print(json.dumps(supervise(args.spec,args.parent_pid)))
