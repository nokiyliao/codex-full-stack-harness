"""Workers can run the tests they write when the coordinator names a verifier by argv alone.

Live 2026-10-22 (i8, Nokiy Ultra, 3 of 3 runs): coordinators gave workers no way to run tests
(command_templates run only pinned rg/cat discovery; programs run only as verifier commands, which
nokiy_execute never advertised). Each worker returned untested new tests, the parent's validator
found a fixture path that failed only from the validator's working directory, and a corrective
round cost 3 to 6 minutes. The host now fills the derivable verifier fields so a coordinator can
grant worker-run tests without computing digests or allocating scratch directories itself.
"""
import hashlib
import importlib.util
import tempfile
import unittest
import unittest.mock
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'src'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SRC / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host = load('verifier_completion_host', 'nokiy-native-host.py')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class VerifierCompletionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='nokiy-verifier-completion-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.workspace = root / 'workspace'
        (self.workspace / 'pkg').mkdir(parents=True)
        (self.workspace / 'pkg/module.py').write_text('x = 1\n')
        (self.workspace / 'pkg/test_old.py').write_text('import unittest\n')
        (self.workspace / 'pkg/test_new.py').write_text('import unittest\n')
        self.interpreter = root / 'bin/python3.11'
        self.interpreter.parent.mkdir()
        self.interpreter.write_bytes(b'\xcf\xfa\xed\xfe fake interpreter')
        self.artifact = root / 'artifacts/task-a'
        self.artifact.mkdir(parents=True)
        self.draft = {'workspace': str(self.workspace), 'artifact_root': str(self.artifact),
                      'prompt': 'Implement pkg/module.py with focused tests.'}

    def action(self, verifiers, operations=('read', 'modify', 'command')):
        return {'operations': list(operations), 'source_read': True,
                'read_scopes': ['pkg/module.py', 'pkg/test_old.py', 'pkg/test_new.py'],
                'write_scopes': ['pkg/module.py', 'pkg/test_new.py'],
                'target_paths': ['pkg/module.py', 'pkg/test_new.py'], 'verifier_commands': verifiers}

    def minimal(self):
        return {'argv': [str(self.interpreter), '-B', '-m', 'unittest', '-v',
                         'pkg/test_old.py', 'pkg/test_new.py'],
                'python_import_roots': ['pkg']}

    def test_argv_alone_becomes_a_complete_verifier_pinning_only_unwritten_files(self):
        action, repairs = host.complete_verifier_commands(self.action([self.minimal()]), self.draft, self.workspace)
        [command] = action['verifier_commands']
        old, new = str(self.workspace / 'pkg/test_old.py'), str(self.workspace / 'pkg/test_new.py')
        self.assertEqual(command['argv'], [str(self.interpreter), '-B', '-m', 'unittest', '-v', old, new])
        self.assertEqual(command['executable_sha256'], digest(self.interpreter))
        # The worker rewrites test_new.py, so pinning it would fail the execution-time digest check.
        self.assertEqual(command['pinned_files'], [{'path': old, 'sha256': digest(old)}])
        self.assertIs(command['network'], False)
        self.assertEqual(command['timeout_seconds'], 300)
        self.assertEqual(command['scratch_root'], str(self.artifact / 'verifier-0'))
        self.assertTrue((self.artifact / 'verifier-0').is_dir())
        self.assertEqual(command['python_import_roots'], [str(self.workspace / 'pkg')])
        self.assertEqual(repairs, [{'path': 'action.verifier_commands', 'rule': 'verifier_fields_completed',
                                    'fields': ['argv', 'executable_sha256', 'network', 'pinned_files',
                                               'pinned_files.sha256', 'python_import_roots', 'scratch_root',
                                               'timeout_seconds']}])

    def test_complete_verifier_is_unchanged_and_each_command_gets_its_own_scratch(self):
        complete, _ = host.complete_verifier_commands(self.action([self.minimal()]), self.draft, self.workspace)
        again, repairs = host.complete_verifier_commands(complete, self.draft, self.workspace)
        self.assertEqual(again, complete)
        self.assertEqual(repairs, [])
        second = dict(self.minimal(), argv=self.minimal()['argv'][:-1])
        both, _ = host.complete_verifier_commands(self.action([self.minimal(), second]), self.draft, self.workspace)
        self.assertEqual([c['scratch_root'] for c in both['verifier_commands']],
                         [str(self.artifact / 'verifier-0'), str(self.artifact / 'verifier-1')])

    def test_missing_command_operation_is_added_only_for_a_verifier(self):
        action, repairs = host.complete_verifier_commands(
            self.action([self.minimal()], operations=('read', 'modify')), self.draft, self.workspace)
        self.assertEqual(action['operations'], ['read', 'modify', 'command'])
        self.assertIn({'path': 'action.operations', 'rule': 'verifier_to_command_operation'}, repairs)
        plain = {k: v for k, v in self.action([]).items() if k != 'verifier_commands'}
        self.assertEqual(host.complete_verifier_commands(plain, self.draft, self.workspace), (plain, []))

    # V321 (2026-10-22, i19): a coordinator put scratch_root beside the workspace to keep test
    # artifacts in a task-named directory; the bare unsafe_path refusal offered no correction, so it
    # dropped worker verification. Unnamed scratch outside the artifact root now moves to the default.
    def test_scratch_outside_the_artifact_root_moves_to_the_default_unless_named(self):
        outside = self.artifact.parent.parent / 'verifier-scratch' / 'a'
        moved = dict(self.minimal(), scratch_root=str(outside))
        action, repairs = host.complete_verifier_commands(self.action([moved]), self.draft, self.workspace)
        self.assertEqual(action['verifier_commands'][0]['scratch_root'], str(self.artifact / 'verifier-0'))
        self.assertTrue((self.artifact / 'verifier-0').is_dir())
        self.assertFalse(outside.exists())
        self.assertIn('scratch_root_relocated', repairs[0]['fields'])
        self.assertNotIn('scratch_root', repairs[0]['fields'])
        again, repairs = host.complete_verifier_commands(action, self.draft, self.workspace)
        self.assertEqual((again, repairs), (action, []))
        for root in (self.artifact, self.workspace / 'scratch'):
            command = dict(self.minimal(), scratch_root=str(root))
            action, _ = host.complete_verifier_commands(self.action([command]), self.draft, self.workspace)
            self.assertEqual(action['verifier_commands'][0]['scratch_root'], str(self.artifact / 'verifier-0'))
        inside = dict(self.minimal(), scratch_root=str(self.artifact / 'mine'))
        action, _ = host.complete_verifier_commands(self.action([inside]), self.draft, self.workspace)
        self.assertEqual(action['verifier_commands'][0]['scratch_root'], str(self.artifact / 'mine'))
        named = dict(self.minimal(), scratch_root=str(outside),
                     argv=self.minimal()['argv'] + ['--report', str(outside / 'report.json')])
        action, _ = host.complete_verifier_commands(self.action([named]), self.draft, self.workspace)
        self.assertEqual(action['verifier_commands'][0]['scratch_root'], str(outside))

    def test_explicit_string_pins_get_digests_and_explicit_values_are_kept(self):
        command = dict(self.minimal(), pinned_files=['pkg/test_old.py'], timeout_seconds=60, network=False)
        action, _ = host.complete_verifier_commands(self.action([command]), self.draft, self.workspace)
        [done] = action['verifier_commands']
        old = str(self.workspace / 'pkg/test_old.py')
        self.assertEqual(done['pinned_files'], [{'path': old, 'sha256': digest(old)}])
        self.assertEqual(done['timeout_seconds'], 60)

    def test_explicit_external_verifier_only_pin_preserves_scope_without_source_access(self):
        external = self.workspace.parent / 'required-check.py'
        external.write_text('pass\n')
        command = {'argv': [str(self.interpreter), str(external)],
                   'pinned_files': [{'path': str(external), 'scope': 'verifier_only'}]}
        original = self.action([command])
        action, _ = host.complete_verifier_commands(original, self.draft, self.workspace)
        self.assertEqual(action['verifier_commands'][0]['pinned_files'], [{
            'path': str(external), 'scope': 'verifier_only', 'sha256': digest(external)}])
        for key in ('operations', 'read_scopes', 'write_scopes', 'target_paths', 'source_read'):
            self.assertEqual(action[key], original[key])
        self.assertIs(action['verifier_commands'][0]['network'], False)
        # Completion never repairs an explicitly supplied wrong digest into a valid grant.
        command['pinned_files'][0]['sha256'] = '0' * 64
        refused, _ = host.complete_verifier_commands(self.action([command]), self.draft, self.workspace)
        self.assertEqual(refused['verifier_commands'][0]['pinned_files'][0]['sha256'], '0' * 64)

    def test_operand_naming_an_external_pin_takes_its_spelling(self):
        # V439 (2026-10-26, I26 Efficient): argv named the pinned external check relative to the
        # verifier cwd; the binding refusal cost a 73 s corrective coordinator round.
        tools = self.workspace.parent / 'tools'
        tools.mkdir()
        check, other = tools / 'validate.py', tools / 'other.py'
        check.write_text('pass\n')
        other.write_text('pass\n')
        for cwd, operand in ((str(self.workspace), '../tools/validate.py'),
                             (str(self.workspace.parent), 'tools/validate.py')):
            command = {'argv': [str(self.interpreter), '-B', operand, '--other', '../tools/other.py'],
                       'cwd': cwd, 'pinned_files': [{'path': str(check), 'scope': 'verifier_only'}]}
            action, _ = host.complete_verifier_commands(self.action([command]), self.draft, self.workspace)
            done = action['verifier_commands'][0]
            self.assertEqual(done['argv'][2], str(check))
            # Only the pinned file is respelled; an unpinned operand stays as written.
            self.assertEqual(done['argv'][4], '../tools/other.py')
            self.assertEqual(done['pinned_files'], [{
                'path': str(check), 'scope': 'verifier_only', 'sha256': digest(check)}])

    def test_unpinnable_or_malformed_commands_stay_for_the_callers_typed_refusal(self):
        # The worker's own new test, run directly, is its work product: nothing to pin.
        own = dict(self.minimal(), argv=[str(self.interpreter), 'pkg/test_new.py'])
        action, _ = host.complete_verifier_commands(self.action([own]), self.draft, self.workspace)
        self.assertEqual(action['verifier_commands'][0]['pinned_files'], [])
        # An existing program outside the workspace is pinnable (verifier_only); leave it unpinned
        # for the caller's typed refusal instead of inventing a scope.
        program = self.workspace.parent / 'external.py'
        program.write_text('print(1)\n')
        outside = dict(self.minimal(), argv=[str(self.interpreter), str(program), 'pkg/test_new.py'])
        action, _ = host.complete_verifier_commands(self.action([outside]), self.draft, self.workspace)
        self.assertNotIn('pinned_files', action['verifier_commands'][0])
        for malformed in ({'argv': 'python -m unittest'}, {'argv': []}, 'python', {'argv': ['', 'x']}):
            action, repairs = host.complete_verifier_commands(self.action([malformed]), self.draft, self.workspace)
            self.assertEqual(action['verifier_commands'], [malformed])
            self.assertEqual(repairs, [])
        traversal = dict(self.minimal(), argv=[str(self.interpreter), '-m', 'unittest', '../workspace/pkg/test_old.py'])
        action, _ = host.complete_verifier_commands(self.action([traversal]), self.draft, self.workspace)
        self.assertEqual(action['verifier_commands'][0]['argv'][-1], '../workspace/pkg/test_old.py')

    def test_relative_operands_resolve_from_the_command_cwd(self):
        # V430 (2026-10-24): node --test src/x.test.mjs run in ui/dashboard/frontend named files
        # under that directory; resolving them from the workspace root left the command unpinned.
        frontend = self.workspace / 'ui/frontend'
        (frontend / 'src').mkdir(parents=True)
        (frontend / 'src/hook.mjs').write_text('export {}\n')
        (frontend / 'src/a.test.mjs').write_text('import "node:test"\n')
        node = self.interpreter.parent / 'node'
        node.write_bytes(b'\xcf\xfa\xed\xfe fake node')
        command = {'argv': [str(node), '--import', './src/hook.mjs', '--test', 'src/a.test.mjs'],
                   'cwd': 'ui/frontend'}
        action = dict(self.action([command]), read_scopes=['ui/frontend/src/hook.mjs', 'ui/frontend/src/a.test.mjs'])
        done, _ = host.complete_verifier_commands(action, self.draft, self.workspace)
        [verifier] = done['verifier_commands']
        hook, test = str(frontend / 'src/hook.mjs'), str(frontend / 'src/a.test.mjs')
        self.assertEqual(verifier['argv'], [str(node), '--import', hook, '--test', test])
        self.assertEqual(verifier['cwd'], str(frontend))
        self.assertEqual([pin['path'] for pin in verifier['pinned_files']], [hook, test])
        # A cwd outside the workspace names nothing the workspace root would resolve.
        stray = dict(command, cwd=str(self.workspace.parent))
        done, _ = host.complete_verifier_commands(dict(action, verifier_commands=[stray]), self.draft, self.workspace)
        self.assertEqual(done['verifier_commands'][0]['argv'][2], './src/hook.mjs')

    def test_explicit_pins_follow_the_write_and_workspace_boundaries(self):
        # V430 (2026-10-24): coordinators pinned their own tests verifier_only inside the workspace
        # and the stdlib unittest entry without a scope outside it.
        old, new = str(self.workspace / 'pkg/test_old.py'), str(self.workspace / 'pkg/test_new.py')
        stdlib = self.workspace.parent / 'lib/unittest/__main__.py'
        stdlib.parent.mkdir(parents=True)
        stdlib.write_text('pass\n')
        command = {'argv': [str(self.interpreter), '-B', str(stdlib), old, new],
                   'pinned_files': [{'path': new, 'scope': 'verifier_only'},
                                    {'path': old, 'scope': 'verifier_only'}, {'path': str(stdlib)}]}
        action, repairs = host.complete_verifier_commands(self.action([command]), self.draft, self.workspace)
        self.assertEqual(action['verifier_commands'][0]['pinned_files'], [
            {'path': old, 'sha256': digest(old)},
            {'path': str(stdlib), 'sha256': digest(stdlib), 'scope': 'verifier_only'}])
        self.assertTrue({'pinned_files.work_product', 'pinned_files.scope'} <= set(repairs[0]['fields']))
        (self.workspace / 'checks').mkdir()
        (self.workspace / 'checks/run.py').write_text('pass\n')
        run = str(self.workspace / 'checks/run.py')
        command = {'argv': [str(self.interpreter), '-B', run], 'pinned_files': [{'path': run}]}
        action, repairs = host.complete_verifier_commands(self.action([command]), self.draft, self.workspace)
        self.assertEqual(action['read_scopes'][-1], 'checks/run.py')
        self.assertIn({'path': 'action.read_scopes', 'rule': 'verifier_pins_to_read_scopes',
                       'paths': ['checks/run.py']}, repairs)

    def test_entry_files_outside_every_read_scope_become_reads_only_when_none_is_covered(self):
        (self.workspace / 'checks').mkdir()
        (self.workspace / 'checks/run.py').write_text('pass\n')
        run, old = str(self.workspace / 'checks/run.py'), str(self.workspace / 'pkg/test_old.py')
        action, _ = host.complete_verifier_commands(
            self.action([{'argv': [str(self.interpreter), '-B', run]}]), self.draft, self.workspace)
        self.assertEqual(action['verifier_commands'][0]['pinned_files'], [{'path': run, 'sha256': digest(run)}])
        self.assertEqual(action['read_scopes'][-1], 'checks/run.py')
        # With a covered entry the declared reads stay exactly as declared.
        action, _ = host.complete_verifier_commands(
            self.action([{'argv': [str(self.interpreter), '-B', old, run]}]), self.draft, self.workspace)
        self.assertEqual([pin['path'] for pin in action['verifier_commands'][0]['pinned_files']], [old])
        self.assertNotIn('checks/run.py', action['read_scopes'])

    def test_named_scratch_inside_the_artifact_root_is_allocated(self):
        named = self.artifact / 'checks/scratch'
        command = dict(self.minimal(), scratch_root=str(named))
        action, repairs = host.complete_verifier_commands(self.action([command]), self.draft, self.workspace)
        self.assertEqual(action['verifier_commands'][0]['scratch_root'], str(named))
        self.assertTrue(named.is_dir())
        self.assertIn('scratch_root_allocated', repairs[0]['fields'])
        # A link inside the artifact root is never followed to create a directory elsewhere.
        elsewhere = self.workspace.parent / 'elsewhere'
        elsewhere.mkdir()
        (self.artifact / 'linked').symlink_to(elsewhere, target_is_directory=True)
        linked = dict(self.minimal(), scratch_root=str(self.artifact / 'linked/scratch'))
        action, _ = host.complete_verifier_commands(self.action([linked]), self.draft, self.workspace)
        self.assertFalse((elsewhere / 'scratch').exists())

    def test_bare_program_name_resolves_like_a_shell_and_is_pinned(self):
        self.interpreter.chmod(0o755)
        command = {'argv': ['python3.11', '-B', '-m', 'unittest', 'test_old']}
        with unittest.mock.patch.dict('os.environ', {'PATH': str(self.interpreter.parent)}):
            action, repairs = host.complete_verifier_commands(self.action([command]), self.draft, self.workspace)
        [done] = action['verifier_commands']
        self.assertEqual(done['argv'][0], str(self.interpreter))
        self.assertEqual(done['executable_sha256'], digest(self.interpreter))
        self.assertIn('argv', repairs[0]['fields'])
        with unittest.mock.patch.dict('os.environ', {'PATH': '/nonexistent'}):
            action, _ = host.complete_verifier_commands(self.action([command]), self.draft, self.workspace)
        self.assertEqual(action['verifier_commands'][0]['argv'][0], 'python3.11')

    # V312 (2026-10-22): the runtime admits an unpinned stdlib unittest runner, so a worker can run
    # the tests it writes by module name, the way the acceptance harness loads them.
    def test_module_name_unittest_runner_gets_an_empty_pin_list(self):
        command = {'argv': [str(self.interpreter), '-B', '-m', 'unittest', '-v', 'test_old', 'test_new'],
                   'python_import_roots': ['pkg']}
        action, repairs = host.complete_verifier_commands(self.action([command]), self.draft, self.workspace)
        [done] = action['verifier_commands']
        self.assertEqual(done['pinned_files'], [])
        self.assertIn('pinned_files', repairs[0]['fields'])
        # V313: pytest collects files the worker writes, so it pins none either.
        pytest = dict(command, argv=[str(self.interpreter), '-B', '-m', 'pytest', '-q', 'pkg/test_new.py'])
        action, _ = host.complete_verifier_commands(self.action([pytest]), self.draft, self.workspace)
        self.assertEqual(action['verifier_commands'][0]['pinned_files'], [])
        for argv in ([str(self.interpreter), '-I', '-m', 'unittest', 'test_old'],
                     [str(self.interpreter), '-m', 'pytest_extra', 'test_old'],
                     [str(self.interpreter), 'test_old']):
            action, _ = host.complete_verifier_commands(self.action([dict(command, argv=argv)]), self.draft,
                                                        self.workspace)
            self.assertNotIn('pinned_files', action['verifier_commands'][0], argv)

    def test_symlinked_virtualenv_interpreter_resolves_and_adds_site_packages(self):
        venv = self.workspace / '.venv'
        (venv / 'bin').mkdir(parents=True)
        (venv / 'pyvenv.cfg').write_text('home = %s\n' % self.interpreter.parent)
        site = venv / 'lib/python3.11/site-packages'
        site.mkdir(parents=True)
        (venv / 'bin/python').symlink_to(self.interpreter)
        command = {'argv': [str(venv / 'bin/python'), '-B', '-m', 'unittest', 'test_old'],
                   'python_import_roots': ['pkg']}
        action, repairs = host.complete_verifier_commands(self.action([command]), self.draft, self.workspace)
        [done] = action['verifier_commands']
        self.assertEqual(done['argv'][0], str(self.interpreter))
        self.assertEqual(done['executable_sha256'], digest(self.interpreter))
        self.assertEqual(done['python_import_roots'], [str(self.workspace / 'pkg'), str(site)])
        self.assertTrue({'argv', 'python_import_roots'} <= set(repairs[0]['fields']))
        isolated = dict(command, argv=[str(venv / 'bin/python'), '-I', '-m', 'unittest', 'test_old'])
        action, _ = host.complete_verifier_commands(self.action([isolated]), self.draft, self.workspace)
        self.assertEqual(action['verifier_commands'][0]['argv'][0], str(self.interpreter))
        self.assertEqual(action['verifier_commands'][0]['python_import_roots'], [str(self.workspace / 'pkg')])
        plain_link = self.workspace / 'python3.11'
        plain_link.symlink_to(self.interpreter)
        action, _ = host.complete_verifier_commands(
            self.action([dict(command, argv=[str(plain_link), '-B', '-m', 'unittest', 'test_old'])]),
            self.draft, self.workspace)
        self.assertEqual(action['verifier_commands'][0]['argv'][0], str(self.interpreter))
        self.assertEqual(action['verifier_commands'][0]['python_import_roots'], [str(self.workspace / 'pkg')])

    def test_prepare_action_applies_completion_before_request_identity(self):
        import ast

        tree = ast.parse(Path(host.__file__).read_text())
        prepare = next(node for node in tree.body
                       if isinstance(node, ast.FunctionDef) and node.name == 'prepare_action')
        calls = {name: [node.lineno for node in ast.walk(prepare)
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == name]
                 for name in ('inline_external_reads', 'complete_verifier_commands')}
        identities = [node.lineno for node in ast.walk(prepare)
                      if isinstance(node, ast.Assign) and any(
                          isinstance(target, ast.Name) and target.id == 'normalized_envelope'
                          for target in node.targets)]
        self.assertLess(max(calls['inline_external_reads']),
                        min(calls['complete_verifier_commands']))
        self.assertLess(max(calls['complete_verifier_commands']), min(identities))


if __name__ == '__main__':
    unittest.main()

