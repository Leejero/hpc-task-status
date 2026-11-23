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

    def write_recent_log(self, lines, name='run_v6.log'):
        """Write a clock-only log whose only event happened ~30 minutes ago."""
        log = self.job / name
        now = dt.datetime.now(collector.SHANGHAI)
        moment = (now - dt.timedelta(minutes=30)).replace(microsecond=0)
        clock = moment.strftime('%H:%M:%S')
        log.write_text(''.join(line.format(clock=clock) + '\n' for line in lines), encoding='utf-8')
        stamp = now.timestamp()
        os.utime(log, (stamp, stamp))
        return log, moment

    def write_old_log(self, name, line, moment):
        log = self.job / name
        log.write_text(line + '\n', encoding='utf-8')
        stamp = moment.timestamp()
        os.utime(log, (stamp, stamp))
        return log

    def valid_public(self, updated, record_time):
        return {
            'schema_version': 2, 'updated_at': updated, 'timezone': 'Asia/Shanghai',
            'collector_status': 'ok', 'job_active': False, 'total': 2,
            'counts': {'success': 1, 'warning': 0, 'failed': 0, 'running': 0, 'pending': 1, 'unknown': 0},
            'scope': ['slide-001'], 'running_tasks': [], 'issues': [],
            'recent_success': [{'name': 'slide-001/1.90', 'group': '2H', 'status': 'success',
                                 'phase': 'scf', 'detail': '计算成功', 'time': record_time}],
        }

    def test_multiday_log_reconstructs_calendar_dates(self):
        self.write_multiday_log()
        events = collector.read_events(self.job)
        self.assertEqual(events[('slide-000', 'd-2.10')]['SCF']['date'], '2026-11-05')
        self.assertEqual(events[('slide-000', 'd-3.00')]['RELAX']['date'], '2026-11-06')
        self.assertEqual(events[('slide-001', 'd-1.50')]['SCF']['date'], '2026-11-07')
        self.assertEqual(collector.iso_time(collector.event_timestamp(events[('slide-000', 'd-2.10')]['SCF'])), '2026-11-05T15:26:44+08:00')

    def test_recent_success_has_real_name_time_and_no_full_task_list(self):
        self.write('relax', 'slide-000', '2.10', 'reached required accuracy')
        self.write('scf', 'slide-000', '2.10', 'EDIFF is reached', '-736.123')
        self.write_recent_log(['[{clock}] SCF   ok  slide-000/d-2.10 E=-.73657876E+03'])
        scope_dir = self.job / 'relax' / 'slide-000' / 'd-9.99' / 'f1_1' / 'norm'
        data = self.collect({str(scope_dir)})
        self.assertEqual(data['counts']['success'], 1)
        self.assertEqual(data['recent_success'][0]['name'], 'slide-000/2.10')
        public = collector.public_data(data)
        self.assertNotIn('tasks', public)
        self.assertNotIn('history', public)
        self.assertEqual(public['total'], 2)
        publisher.validate(public)
        self.assertNotIn(str(self.job), json.dumps(public))

    def test_old_completion_is_kept_locally_but_never_published(self):
        self.write('relax', 'slide-001', '1.90', 'reached required accuracy')
        self.write('scf', 'slide-001', '1.90', 'EDIFF is reached', '-736.123')
        old = dt.datetime(2026, 11, 5, 13, 6, 44, tzinfo=collector.SHANGHAI)
        self.write_old_log('run.log', '[13:06:44] SCF   ok  slide-001/d-1.90 E=-.73657876E+03', old)
        data = self.collect(set())
        public = collector.public_data(data)
        self.assertEqual(public['scope'], ['slide-001'])
        self.assertEqual(data['counts']['success'], 1)
        self.assertEqual(public['recent_success'], [])
        self.assertEqual(public['issues'], [])
        self.assertEqual(len(data['history']), 1)
        self.assertEqual(data['history'][0]['time'], '2026-11-05T13:06:44+08:00')
        publisher.validate(public)

    def test_current_run_v6_log_overrides_old_inherited_archive_log(self):
        """Real incident: root uses run_v6.log while inherit holds a 16-day-old run.log."""
        inherit = self.root / 'fb24_old' / '2H'
        inherit.mkdir(parents=True)
        old = dt.datetime(2026, 11, 5, 13, 6, 44, tzinfo=collector.SHANGHAI)
        archive = inherit / 'run.log'
        archive.write_text('[13:06:44] SCF   ok  slide-001/d-1.90 E=-1\n', encoding='utf-8')
        os.utime(archive, (old.timestamp(), old.timestamp()))
        config = self.root / 'tasks_inherit.json'
        config.write_text(json.dumps({'jobs': [{'root': str(self.job), 'inherit': str(inherit)}]}), encoding='utf-8')
        self.write_recent_log(['[{clock}] SCF   ok  slide-010/d-2.10 E=-1'], name='run_v6.log')
        data = collector.collect(config, active=False, active_directories=set())
        public = collector.public_data(data)
        self.assertEqual([path.name for path in collector.run_logs(self.job)], ['run_v6.log'])
        self.assertEqual(public['scope'], ['slide-010'])
        self.assertEqual(public['recent_success'], [])
        self.assertEqual(public['issues'], [])
        publisher.validate(public)

    def test_inherited_archive_log_alone_cannot_define_scope(self):
        inherit = self.root / 'fb24_old' / '2H'
        inherit.mkdir(parents=True)
        old = dt.datetime(2026, 11, 5, 13, 6, 44, tzinfo=collector.SHANGHAI)
        archive = inherit / 'run.log'
        archive.write_text('[13:06:44] SCF   ok  slide-001/d-1.90 E=-1\n', encoding='utf-8')
        os.utime(archive, (old.timestamp(), old.timestamp()))
        config = self.root / 'tasks_inherit.json'
        config.write_text(json.dumps({'jobs': [{'root': str(self.job), 'inherit': str(inherit)}]}), encoding='utf-8')
        self.write('relax', 'slide-000', '2.10', 'reached required accuracy')
        self.write('scf', 'slide-000', '2.10', 'EDIFF is reached', '-736.123')
        data = collector.collect(config, active=False, active_directories=set())
        public = collector.public_data(data)
        self.assertEqual(public['scope'], ['slide-000'])
        self.assertEqual([record['name'] for record in public['recent_success']], ['slide-000/2.10'])
        publisher.validate(public)

    def test_publisher_rejects_expired_completion_record(self):
        now = dt.datetime.now(collector.SHANGHAI)
        fresh = self.valid_public(now.isoformat(timespec='seconds'), now.isoformat(timespec='seconds'))
        publisher.validate(fresh)
        expired = self.valid_public(now.isoformat(timespec='seconds'), '2026-11-05T13:06:44+08:00')
        with self.assertRaises(ValueError):
            publisher.validate(expired)
        stale_snapshot = self.valid_public('2026-11-05T16:00:00+08:00', '2026-11-05T13:06:44+08:00')
        with self.assertRaises(ValueError):
            publisher.validate(stale_snapshot)

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

    def test_recent_unconverged_is_warning_not_success(self):
        self.write('relax', 'slide-000', '2.10', 'unfinished')
        self.write_recent_log(['[{clock}] RELAX FAIL slide-000/d-2.10 (rc=0)'])
        data = self.collect()
        self.assertEqual(data['issues'][0]['status'], 'warning')
        self.assertEqual(data['issues'][0]['time'][:4], str(dt.datetime.now(collector.SHANGHAI).year))

    def test_nonzero_exit_is_failure(self):
        self.write('relax', 'slide-000', '2.10', 'unfinished')
        os.utime(self.job / 'relax' / 'slide-000' / 'd-2.10' / 'f1_1' / 'norm' / 'OUTCAR', (time.time() - 3600, time.time() - 3600))
        self.write_recent_log(['[{clock}] RELAX FAIL slide-000/d-2.10 (rc=1)'])
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
        self.write_recent_log(['[{clock}] SCF   ok  slide-001/d-1.90 E=-.73657876E+03'])
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


class DiscoveryTest(unittest.TestCase):
    """All task trees are discovered dynamically; no folder is privileged."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / 'tasks.json'
        self.config.write_text(json.dumps({'discover': True, 'search_roots': [str(self.root)]}), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def collect(self, active_directories=None):
        return collector.collect(self.config, active=False, active_directories=active_directories or set())

    def make_phased(self, relative, rows):
        base = self.root / relative
        (base / 'relax').mkdir(parents=True, exist_ok=True)
        (base / 'scf').mkdir(parents=True, exist_ok=True)
        (base / 'pairs.tsv').write_text(
            '# slide\tdistance\tdirection\tstacking\n' +
            ''.join('\t'.join(row) + '\n' for row in rows), encoding='utf-8')
        return base

    def make_flat(self, relative, rows):
        base = self.root / relative
        base.mkdir(parents=True, exist_ok=True)
        (base / 'pairs.tsv').write_text(
            '# case\tslide\td_HCu\n' + ''.join('\t'.join(row) + '\n' for row in rows), encoding='utf-8')
        return base

    def assert_success(self, directory, phase):
        directory.mkdir(parents=True, exist_ok=True)
        marker = 'reached required accuracy' if phase == 'relax' else 'EDIFF is reached'
        (directory / 'OUTCAR').write_text(marker + '\nenergy  without entropy=  -123.456\n', encoding='utf-8')
        (directory / 'energy.txt').write_text('-123.456\n', encoding='ascii')
        return directory

    def test_only_current_batch_is_counted_with_readable_label(self):
        phased = self.make_phased('unexpected/deep/place', [('slide-000', 'd-2.10', 'f1_1', 'norm')])
        self.assert_success(phased / 'relax' / 'slide-000' / 'd-2.10' / 'f1_1' / 'norm', 'relax')
        self.assert_success(phased / 'scf' / 'slide-000' / 'd-2.10' / 'f1_1' / 'norm', 'scf')
        flat = self.make_flat('another/tree', [('2H', 's000', '3.80')])
        self.assert_success(flat / '2H' / 's000' / 'd-3.80', 'relax')
        # The live VASP process belongs to the flat tree; the other tree is an archive.
        data = self.collect({str(flat / '2H')})
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['counts']['success'], 1)
        self.assertEqual([record['name'] for record in data['history']], ['s000/3.80'])
        self.assertEqual(data['history'][0]['group'], '2H · tree')
        self.assertEqual(data['scope'], ['s000'])
        public = collector.public_data(data)
        publisher.validate(public)
        self.assertNotIn(str(self.root), json.dumps(public))

    def test_idle_gap_falls_back_to_newest_log_tree_not_archive(self):
        old = self.make_phased('fb24_old/2H', [('slide-000', 'd-2.10', 'f1_1', 'norm')])
        old_log = old / 'run.log'
        old_log.write_text('[15:26:44] SCF ok slide-000/d-2.10\n', encoding='utf-8')
        old_stamp = dt.datetime(2026, 11, 5, 15, 26, 44, tzinfo=collector.SHANGHAI).timestamp()
        os.utime(old_log, (old_stamp, old_stamp))
        current = self.make_flat('fb_pass_submit', [('2H', 's000', '3.80')])
        self.assert_success(current / '2H' / 's000' / 'd-3.80', 'relax')
        now = dt.datetime.now(collector.SHANGHAI)
        current_log = current / 'run_2H.log'
        current_log.write_text('[{}] OK s000 d-3.80\n'.format(now.strftime('%m-%d %H:%M')), encoding='utf-8')
        os.utime(current_log, (now.timestamp(), now.timestamp()))
        data = self.collect(set())
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['history'][0]['name'], 's000/3.80')
        self.assertEqual(data['history'][0]['group'], '2H · fb_pass_submit')
        publisher.validate(collector.public_data(data))

    def test_flat_log_failures_and_running_process(self):
        flat = self.make_flat('pass/submit', [('2H', 's000', '3.80'), ('2H', 's002', '2.00')])
        now = dt.datetime.now(collector.SHANGHAI)
        log = flat / 'run_2H.log'
        log.write_text('[{}] FAIL s002 d-2.00 rc=7\n'.format(now.strftime('%m-%d %H:%M')), encoding='utf-8')
        os.utime(log, (now.timestamp(), now.timestamp()))
        running = flat / '2H' / 's000' / 'd-3.80'
        running.mkdir(parents=True, exist_ok=True)
        (running / 'OUTCAR').write_text('still running', encoding='utf-8')
        data = self.collect({str(running)})
        public = collector.public_data(data)
        self.assertEqual(public['scope'], ['s000'])
        self.assertEqual(data['counts']['failed'], 1)
        self.assertEqual(data['counts']['running'], 1)
        self.assertEqual(public['running_tasks'][0]['name'], 's000/3.80')
        self.assertEqual(public['running_tasks'][0]['group'][:2], '2H')
        # s002 is a real failure but outside the active slide, so it stays in counts only.
        self.assertEqual(public['issues'], [])
        publisher.validate(public)

    def test_running_match_respects_case_label(self):
        flat = self.make_flat('pass/submit', [('2H', 's000', '3.80'), ('3C', 's000', '3.80')])
        running = flat / '2H' / 's000' / 'd-3.80'
        running.mkdir(parents=True, exist_ok=True)
        (running / 'OUTCAR').write_text('still running', encoding='utf-8')
        data = self.collect({str(running)})
        self.assertEqual(len(data['running_tasks']), 1)
        self.assertTrue(data['running_tasks'][0]['group'].startswith('2H'))
        publisher.validate(collector.public_data(data))

    def test_flat_event_dates_are_parsed_from_mm_dd_log(self):
        flat = self.make_flat('pass/submit', [('2H', 's000', '3.80')])
        now = dt.datetime.now(collector.SHANGHAI)
        log = flat / 'run_2H.log'
        log.write_text('[{}] FAIL s000 d-3.80 rc=7\n'.format(now.strftime('%m-%d %H:%M')), encoding='utf-8')
        os.utime(log, (now.timestamp(), now.timestamp()))
        events = collector.read_events(flat)
        self.assertIn(('s000', 'd-3.80'), events)
        event = events[('s000', 'd-3.80')]['RELAX']
        self.assertTrue(event['failed'])
        self.assertEqual(event['code'], '7')
        self.assertEqual(event['date'], now.date().isoformat())
        self.assertEqual(collector.last_event_slide(flat)[1], 's000')


if __name__ == '__main__':
    unittest.main()
