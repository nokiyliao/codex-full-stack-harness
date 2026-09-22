import importlib.util
from pathlib import Path
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import json

path=Path(__file__).parents[1]/'scripts/nokiy_ablation_benchmark.py'
spec=importlib.util.spec_from_file_location('ablation_driver',path)
driver=importlib.util.module_from_spec(spec);spec.loader.exec_module(driver)


class AblationTests(unittest.TestCase):
    def test_only_context_flags_differ(self):
        base=['exec','--json','--sandbox','--task-context-capsule','context.json',
              '--jspace-contract','jspace.json','-a','direct','-m','codex/gpt-6-astra']
        clean=driver.select_argv(base,'clean');full=driver.select_argv(base,'full')
        self.assertEqual(clean,['exec','--json','--sandbox','-a','direct','-m','codex/gpt-6-astra','--log'])
        self.assertEqual(full,base+['--log'])
        self.assertEqual(base[3],'--task-context-capsule')

    def test_unsafe_or_unmatched_mode_rejected(self):
        for argv in (['exec','-a','direct'],['exec','--sandbox','-a','balanced'],
                     ['exec','--sandbox','-a','direct','--no-sandbox']):
            with self.assertRaises(AssertionError):driver.select_argv(argv,'full')

    def test_same_policy_protects_real_source(self):
        result=subprocess.run(['/usr/bin/sandbox-exec','-p',driver.sandbox_policy(),
                               str(driver.PYTHON),'-B',str(path),'probe'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('"readonly":true',result.stdout)

    def test_unfenced_entry_rejected(self):
        result=subprocess.run([str(driver.PYTHON),'-B',str(path),'probe'],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('BENCHMARK_READONLY_SANDBOX_MISSING',result.stderr)

    def test_supervisor_bytes_are_json_serializable(self):
        result=SimpleNamespace(stdout=b'{"final_text":"ok"}',stderr=b'\xff',
                               scope={'engine_reaped':True},failure=None,returncode=0)
        with patch('codex_collaboration_harness.graph_process.supervise',return_value=result), \
             patch.object(driver.signal,'signal'):
            outcome=driver.supervise(Path('/unused/spec.json'),123)
        self.assertEqual(outcome['result']['final_text'],'ok')
        self.assertIsInstance(outcome['stderr'],str)
        json.dumps(outcome)
