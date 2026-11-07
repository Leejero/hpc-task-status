import importlib.util
import json
import os
import tempfile
import datetime as dt
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
collector = load('collector', ROOT / 'server' / 'collect_status.py')
publisher = load('publisher', ROOT / 'server' / 'push_status.py')


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.job = self.root / '2H'
        (self.job / 'relax').mkdir(parents=True)
        (self.job / 'scf').mkdir(parents=True)
        (self.job / 'pairs.tsv').write_text('# slide\tdistance\nslide-000\td-2.10\nslide-001\td-1.90\n', encoding='utf-8')
        self.config = self.root / 'tasks.json'
        self.config.write_text(json.dumps({'jobs': [{'root': str(self.job)}]}), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def write(self, kind, slide, distance, text, energy=None):
        directory = self.job / kind / slide / ('d-' + distance) / 'f1_1' / 'norm'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'OUTCAR').write_text(text, encoding='utf-8')
        if energy:
            (directory / 'energy.txt').write_text(energy + '\n', encoding='ascii')
        return directory

    def collect(self, active_directories=None):
        return collector.collect(self.config, active=False, active_directories=active_directories)

    def stamp(self, directory, moment):
        timestamp = moment.timestamp()
        for path in [directory, *directory.iterdir()]:
            os.utime(path, (timestamp, timestamp))

    def write_multiday_log(self):
        log = self.job / 'run.log'
        lines = [
            '[15:01:32] RELAX ok  slide-000/d-2.10 (rc=0)',
            '[15:26:44] SCF   ok  slide-000/d-2.10 E=-.73657876E+03',
            '[23:36:52] SCF   ok  slide-000/d-1.70 E=-.73629365E+03',
            '[00:17:52] RELAX ok  slide-000/d-1.65 (rc=0)',
            '[22:00:07] RELAX FAIL slide-000/d-3.00 rc=0',
            '[00:31:03] RELAX ok  slide-001/d-2.10 (rc=0)',
            '[15:35:59] SCF   ok  slide-001/d-1.50 E=-.73596971E+03',
            '[16:07:32] === START 2H  NAT=100 NC=64 ===',
        ]
        log.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        anchor = dt.datetime(2026, 11, 7, 16, 7, 32, tzinfo=collector.SHANGHAI).timestamp()
        os.utime(log, (anchor, anchor))
        return log

    def test_multiday_log_reconstructs_calendar_dates(self):
        self.write_multiday_log()
        events = collector.read_events(self.job)
        self.assertEqual(events[('slide-000', 'd-2.10')]['SCF']['date'], '2026-11-05')
        self.assertEqual(events[('slide-000', 'd-3.00')]['RELAX']['date'], '2026-11-06')
        self.assertEqual(events[('slide-001', 'd-1.50')]['SCF']['date'], '2026-11-07')
        self.assertEqual(collector.iso_time(collector.event_timestamp(events[('slide-000', 'd-2.10')]['SCF'])), '2026-11-05T15:26:44+08:00')

    def test_success_has_real_name_time_and_no_full_task_list(self):
        directory = self.write('relax', 'slide-000', '2.10', 'reached required accuracy')
        scf = self.write('scf', 'slide-000', '2.10', 'EDIFF is reached', '-736.123')
        self.write_multiday_log()
        old = dt.datetime(2026, 11, 5, 15, 26, 44, tzinfo=collector.SHANGHAI)
        self.stamp(directory, old)
        self.stamp(scf, old)
        scope_dir = self.job / 'relax' / 'slide-000' / 'd-9.99' / 'f1_1' / 'norm'
        data = self.collect({str(scope_dir)})
        self.assertEqual(data['counts']['success'], 1)
        self.assertEqual(data['recent_success'][0]['name'], 'slide-000/2.10')
        self.assertEqual(data['recent_success'][0]['time'], '2026-11-05T15:26:44+08:00')
        public = collector.public_data(data)
        self.assertNotIn('tasks', public)
        self.assertNotIn('history', public)
        self.assertEqual(public['total'], 2)
        publisher.validate(public)
        self.assertNotIn(str(self.job), json.dumps(public))

    def test_pending_and_running_are_counts_only(self):
        directory = self.write('relax', 'slide-000', '2.10', 'unfinished')
        os.utime(directory / 'OUTCAR', (time.time() - 3600, time.time() - 3600))
        data = self.collect({str(directory)})
        public = collector.public_data(data)
        self.assertEqual(data['counts']['running'], 1)
        self.assertEqual(data['counts']['pending'], 1)
        self.assertEqual(public['issues'], [])
        self.assertEqual(public['recent_success'], [])
        self.assertEqual(len(public['running_tasks']), 1)

    def test_process_directory_identifies_running_relax(self):
        directory = self.write('relax', 'slide-000', '2.10', 'unfinished')
        data = self.collect({str(directory)})
        public = collector.public_data(data)
        self.assertEqual(public['running_tasks'], [{'name': 'slide-000/2.10', 'group': '2H', 'status': 'running', 'phase': 'relax', 'detail': '弛豫计算进行中'}])
        self.assertEqual(data['counts']['pending'], 1)
        publisher.validate(public)

    def test_process_directory_identifies_running_scf(self):
        self.write('relax', 'slide-000', '2.10', 'reached required accuracy')
        directory = self.write('scf', 'slide-000', '2.10', 'unfinished')
        data = self.collect({str(directory)})
        self.assertEqual(data['running_tasks'][0]['phase'], 'scf')
        self.assertEqual(data['counts']['running'], 1)

    def test_unconverged_is_warning_not_success(self):
        directory = self.write('relax', 'slide-000', '2.10', 'unfinished')
        log = self.job / 'run.log'
        log.write_text('[15:01:32] RELAX FAIL slide-000/d-2.10 (rc=0)\n', encoding='utf-8')
        old = dt.datetime(2026, 11, 6, 15, 1, 32, tzinfo=collector.SHANGHAI)
        self.stamp(directory, old)
        log_time = dt.datetime(2026, 11, 6, 16, 0, tzinfo=collector.SHANGHAI).timestamp()
        os.utime(log, (log_time, log_time))
        data = self.collect()
        self.assertEqual(data['issues'][0]['status'], 'warning')
        self.assertEqual(data['issues'][0]['time'], '2026-11-06T15:01:32+08:00')

    def test_nonzero_exit_is_failure(self):
        directory = self.write('relax', 'slide-000', '2.10', 'unfinished')
        os.utime(directory / 'OUTCAR', (time.time() - 3600, time.time() - 3600))
        (self.job / 'run.log').write_text('[15:01:32] RELAX FAIL slide-000/d-2.10 (rc=1)\n', encoding='utf-8')
        self.assertEqual(self.collect()['issues'][0]['status'], 'failed')

    def test_pending_does_not_require_calculation_directory(self):
        record = collector.evaluate(self.job, None, ['slide-001', 'd-1.90'], {}, {}, set(), False)
        self.assertEqual(record['status'], 'pending')
        self.assertEqual(record['sort_key'], 0)
        self.assertEqual(record['time'], '')

    def test_missing_energy_is_warning(self):
        self.write('relax', 'slide-000', '2.10', 'reached required accuracy')
        self.write('scf', 'slide-000', '2.10', 'EDIFF is reached')
        self.assertEqual(self.collect()['issues'][0]['status'], 'warning')

    def test_fatal_output_is_failure(self):
        self.write('relax', 'slide-000', '2.10', 'ERROR: out of memory')
        self.assertEqual(self.collect()['issues'][0]['status'], 'failed')

    def test_public_records_are_limited_to_current_slide(self):
        self.write('relax', 'slide-000', '2.10', 'reached required accuracy')
        self.write('scf', 'slide-000', '2.10', 'EDIFF is reached', '-736.123')
        self.write('relax', 'slide-001', '1.90', 'reached required accuracy')
        self.write('scf', 'slide-001', '1.90', 'EDIFF is reached', '-736.456')
        (self.job / 'run.log').write_text('[15:26:44] SCF ok slide-001/d-1.90\n', encoding='utf-8')
        data = self.collect(set())
        public = collector.public_data(data)
        self.assertEqual(public['scope'], ['slide-001'])
        self.assertEqual(data['counts']['success'], 2)
        self.assertEqual(len(data['history']), 2)
        self.assertEqual([record['name'] for record in public['recent_success']], ['slide-001/1.90'])
        publisher.validate(public)

    def test_removed_tasks_are_pruned_from_local_history(self):
        history = self.root / 'history.json'
        stale = {'name': 'slide-999/2.10', 'group': '2H', 'status': 'success', 'phase': 'scf', 'detail': '计算成功', 'time': '2026-11-05T15:26:44+08:00', 'sort_key': 1}
        history.write_text(json.dumps({'schema_version': 2, 'history': [stale]}), encoding='utf-8')
        data = collector.collect(self.config, active=False, active_directories=set(), history_path=str(history))
        self.assertTrue(all(record['name'] != stale['name'] for record in data['history']))

    def test_history_is_local_and_limited_in_public_file(self):
        for index in range(8):
            slide = 'slide-{:03d}'.format(index)
            with (self.job / 'pairs.tsv').open('a', encoding='utf-8') as handle:
                handle.write(slide + '\td-2.10\n')
            self.write('relax', slide, '2.10', 'reached required accuracy')
            self.write('scf', slide, '2.10', 'EDIFF is reached', '-736.123')
        data = self.collect()
        self.assertEqual(data['total'], 10)
        self.assertEqual(len(collector.public_data(data)['recent_success']), 5)
        self.assertEqual(len(data['history']), 8)
        with self.assertRaises(ValueError):
            publisher.validate({'schema_version': 1})


if __name__ == '__main__':
    unittest.main()
