import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('collector', ROOT / 'server' / 'collect_status.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
push_spec = importlib.util.spec_from_file_location('publisher', ROOT / 'server' / 'push_status.py')
publisher = importlib.util.module_from_spec(push_spec)
push_spec.loader.exec_module(publisher)


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.job = self.root / 'private-calculation'
        (self.job / 'relax').mkdir(parents=True)
        (self.job / 'scf').mkdir(parents=True)
        (self.job / 'pairs.tsv').write_text('# slide\tdistance\nalpha-01\td-1.00\nalpha-02\td-2.00\n', encoding='utf-8')
        self.config = self.root / 'tasks.json'
        self.config.write_text(json.dumps({'jobs': [{'public_group': '任务组 A', 'root': str(self.job)}]}), encoding='utf-8')

    def make_stale(self, path):
        old = time.time() - 3600
        import os
        os.utime(path, (old, old))

    def tearDown(self):
        self.temp.cleanup()

    def write(self, kind, slide, name, text, energy=None):
        directory = self.job / kind / slide / ('d-' + name) / 'f1_1' / 'norm'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'OUTCAR').write_text(text, encoding='utf-8')
        if energy is not None:
            (directory / 'energy.txt').write_text(energy + '\n', encoding='ascii')
        return directory

    def test_converged_pair_succeeds(self):
        self.write('relax', 'alpha-01', '1.00', 'reached required accuracy')
        self.write('scf', 'alpha-01', '1.00', 'EDIFF is reached', energy='-736.123')
        data = collector.collect(self.config, active=False)
        self.assertEqual(data['tasks'][0]['status'], 'success')
        self.assertEqual(data['tasks'][1]['status'], 'pending')
        publisher.validate(data)
        encoded = json.dumps(data)
        for private in ('alpha-01', '1.00', str(self.job), 'private-calculation'):
            self.assertNotIn(private, encoded)

    def test_converged_relax_only_is_pending_scf(self):
        self.write('relax', 'alpha-01', '1.00', 'reached required accuracy')
        data = collector.collect(self.config, active=False)
        self.assertEqual(data['tasks'][0]['status'], 'pending')
        self.assertEqual(data['tasks'][0]['phase'], 'scf')

    def test_existing_unconverged_file_is_running_or_retry(self):
        self.write('relax', 'alpha-01', '1.00', 'unfinished electronic iteration')
        data = collector.collect(self.config, active=False)
        self.assertEqual(data['tasks'][0]['status'], 'running')

    def test_unconverged_completed_run_is_warning_not_failure(self):
        directory = self.write('relax', 'alpha-01', '1.00', 'finished but not converged')
        self.make_stale(directory / 'OUTCAR')
        (self.job / 'run.log').write_text('[12:00:00] RELAX FAIL alpha-01/d-1.00 rc=0\n', encoding='utf-8')
        data = collector.collect(self.config, active=False)
        self.assertEqual(data['tasks'][0]['status'], 'warning')

    def test_nonzero_exit_is_failure(self):
        directory = self.write('relax', 'alpha-01', '1.00', 'unfinished output')
        self.make_stale(directory / 'OUTCAR')
        (self.job / 'run.log').write_text('[12:00:00] RELAX FAIL alpha-01/d-1.00 rc=1\n', encoding='utf-8')
        data = collector.collect(self.config, active=False)
        self.assertEqual(data['tasks'][0]['status'], 'failed')

    def test_fatal_output_is_failure(self):
        self.write('relax', 'alpha-01', '1.00', 'ERROR: out of memory')
        data = collector.collect(self.config, active=False)
        self.assertEqual(data['tasks'][0]['status'], 'failed')

    def test_missing_energy_is_warning(self):
        self.write('relax', 'alpha-01', '1.00', 'reached required accuracy')
        self.write('scf', 'alpha-01', '1.00', 'EDIFF is reached')
        data = collector.collect(self.config, active=False)
        self.assertEqual(data['tasks'][0]['status'], 'warning')

    def test_variable_task_count_and_names_are_neutral(self):
        with (self.job / 'pairs.tsv').open('a', encoding='utf-8') as handle:
            handle.write('alpha-03\td-9.99\n')
        data = collector.collect(self.config, active=False)
        self.assertEqual(data['total'], 3)
        self.assertNotIn('9.99', json.dumps(data))

    def test_previous_unconverged_run_is_not_success(self):
        (self.job / 'run.log').write_text('[12:00:00] RELAX FAIL alpha-01/d-1.00 rc=0\n', encoding='utf-8')
        data = collector.collect(self.config, active=False)
        self.assertEqual(data['tasks'][0]['status'], 'warning')
        self.assertNotIn('rc=0', json.dumps(data))

    def test_rejects_non_neutral_group(self):
        self.config.write_text(json.dumps({'jobs': [{'public_group': 'private-project', 'root': str(self.job)}]}), encoding='utf-8')
        with self.assertRaises(ValueError):
            collector.collect(self.config, active=False)

    def test_publisher_rejects_unexpected_fields(self):
        data = collector.collect(self.config, active=False)
        data['tasks'][0]['path'] = '/private/path'
        with self.assertRaises(ValueError):
            publisher.validate(data)


if __name__ == '__main__':
    unittest.main()
