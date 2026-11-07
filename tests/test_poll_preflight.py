"""Setup stays bounded and cannot confuse review readiness with poll readiness."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/poll-review/scripts/preflight.py'
SPEC = importlib.util.spec_from_file_location('poll_preflight', SCRIPT)
p = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p)


def packet():
    return {'schema_version': 1, 'root': 'root-example', 'helper': 'helper-example',
            'observed_parent': 'root-example', 'effects_reconciled': True,
            'context': {'observed_bytes': 1000, 'limit_bytes': 65536, 'batches': 0, 'batch_limit': 2},
            'allowance': {'total': 6, 'consumed': 2},
            'capabilities': {k: {'available': True, 'verified_by': 'parent',
                                'evidence': 'selected host observation'} for k in p.REVIEW + p.POLL}}


class PreflightTests(unittest.TestCase):
    def test_ready_preserves_consumption_without_mutating_packet(self):
        data = packet(); before = deepcopy(data)
        result = p.check(data)
        self.assertEqual(result['status'], 'READY')
        self.assertEqual(result['returns_remaining'], 4)
        self.assertFalse(result['live_delivery_verified'])
        self.assertEqual(data, before)

    def test_missing_timer_allows_review_but_never_poll(self):
        data = packet(); del data['capabilities']['timer_completion']
        result = p.check(data)
        self.assertEqual(result['status'], 'REVIEW_ONLY')
        self.assertFalse(result['poll_ready'])

    def test_foreign_parent_and_same_identity_are_rejected(self):
        for field, value in [('observed_parent', 'old-root'), ('helper', 'root-example')]:
            data = packet(); data[field] = value
            with self.assertRaises(ValueError): p.check(data)

    def test_context_ceiling_and_unreconciled_effects_stop_all_assignments(self):
        for field, value in [('observed_bytes', 65536), ('batches', 2)]:
            data = packet(); data['context'][field] = value
            self.assertEqual(p.check(data)['status'], 'NOT_READY')
        data = packet(); data['effects_reconciled'] = False
        self.assertEqual(p.check(data)['status'], 'NOT_READY')

    def test_exhausted_budget_does_not_block_review_or_reset_returns(self):
        data = packet(); data['allowance']['consumed'] = 6
        result = p.check(data)
        self.assertEqual(result['status'], 'REVIEW_ONLY')
        self.assertEqual(result['returns_remaining'], 0)

    def test_missing_binding_or_unsubstantiated_capability_is_not_ready(self):
        data = packet(); del data['capabilities']['native_binding']
        self.assertEqual(p.check(data)['status'], 'NOT_READY')
        data = packet(); data['capabilities']['timer_completion']['evidence'] = ''
        with self.assertRaises(ValueError): p.check(data)
        data = packet(); data['allowance']['consumed'] = True
        with self.assertRaises(ValueError): p.check(data)

    def test_cli_bounds_input_and_runs_from_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'evidence.json'
            for raw, code in [(json.dumps(packet()), 0), ('x' * (p.MAX_BYTES + 1), 2), ('[]', 2)]:
                file.write_text(raw)
                result = subprocess.run([sys.executable, '-I', str(SCRIPT), '--evidence', str(file)],
                                        cwd=directory, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertLess(len(result.stdout + result.stderr), 1500)
            self.assertEqual([x.name for x in Path(directory).iterdir()], ['evidence.json'])


if __name__ == '__main__': unittest.main()
