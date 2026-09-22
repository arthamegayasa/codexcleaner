from __future__ import annotations

import contextlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('storage_guard', Path(__file__).resolve().parents[1] / 'scripts' / 'storage_guard.py')
G = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = G
SPEC.loader.exec_module(G)


@contextlib.contextmanager
def fixture_opener(path):
    """Portable fixture-only backend. Production CLI has no way to select it."""
    remove = []
    def disposition(value=True):
        remove[:] = [True] if value else []
    with path.open('rb') as stream:
        yield stream, disposition
    if remove:
        path.unlink()


class StorageGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='codexcleaner-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'disposable'
        self.source.mkdir()
        self.quarantine = self.base / 'quarantine'
        self.policy_path = self.base / 'policy.json'
        self.plan_path = self.base / 'plan.json'
        self.now = time.time()
        self.policy = {'version': 1, 'protected_roots': [], 'rules': [
            {'root': str(self.source), 'kind': 'cache', 'reason': 'Synthetic test cache', 'min_age_days': 7}]}
        self.save_policy()

    def save_policy(self):
        self.policy_path.write_text(json.dumps(self.policy), encoding='utf-8')

    def file(self, relative='old.bin', content=b'old synthetic cache', days=10):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        stamp = self.now - days * G.DAY
        os.utime(path, (stamp, stamp))
        return path

    def plan(self):
        plan = G.make_plan(self.policy_path, now=self.now)
        self.plan_path.write_text(json.dumps(plan), encoding='utf-8')
        return plan

    def apply(self, plan, **kw):
        return G.apply_plan(self.plan_path, self.policy_path, self.quarantine, plan['plan_id'],
                            now=self.now, process_probe=lambda: [], opener=fixture_opener, **kw)

    def test_empty_policy_is_noop(self):
        self.policy['rules'] = []
        self.save_policy()
        plan = self.plan()
        self.assertEqual(plan['entries'], [])
        self.assertEqual(self.apply(plan)['status'], 'no-op')
        self.assertFalse(self.quarantine.exists())

    def test_plan_does_not_change_sources_and_protects_state(self):
        old = self.file()
        for name in ('recent.bin',):
            self.file(name, days=1)
        for name in ('db.sqlite', 'db.sqlite-wal', 'auth.json', 'config.toml', 'main.py', 'lock.pid'):
            self.file(name)
        self.file('undisclosed.bin', b'SQLite format 3\x00some database')
        before = {str(p): p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        plan = self.plan()
        self.assertEqual([e['source'] for e in plan['entries']], [str(old)])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.source.rglob('*') if p.is_file()})
        self.assertFalse(self.quarantine.exists())

    def test_recent_descendant_keeps_entire_reviewed_temp(self):
        self.policy['rules'][0]['kind'] = 'reviewed_temp'
        self.save_policy()
        self.file()
        self.file('nested/new.bin', days=1)
        os.utime(self.source, (self.now - 30 * G.DAY,) * 2)
        self.assertEqual(self.plan()['entries'], [])

    def test_database_keeps_entire_reviewed_temp(self):
        self.policy['rules'][0]['kind'] = 'reviewed_temp'
        self.save_policy()
        self.file()
        self.file('data', b'SQLite format 3\x00contents')
        self.assertEqual(self.plan()['entries'], [])

    def test_project_root_and_protected_root_are_rejected(self):
        (self.source / 'package.json').write_text('{}')
        with self.assertRaises(G.SafetyError):
            self.plan()
        (self.source / 'package.json').unlink()
        self.policy['protected_roots'] = [str(self.source)]
        self.save_policy()
        with self.assertRaises(G.SafetyError):
            self.plan()

    def test_archived_state_and_outputs_are_never_candidates(self):
        for folder in ('archived_sessions', 'sessions', 'generated_images', 'visualizations', 'worktrees', 'codex-router'):
            self.file(folder + '/old.bin')
        self.assertEqual(self.plan()['entries'], [])

    def test_hardlinks_are_retained(self):
        original = self.file()
        os.link(original, self.source / 'linked.bin')
        self.assertEqual(self.plan()['entries'], [])

    def test_unsafe_ages_and_overlapping_rules_rejected(self):
        for age in (0, -1, 6, True, float('nan'), float('inf')):
            self.policy['rules'][0]['min_age_days'] = age
            self.save_policy()
            with self.assertRaises(G.SafetyError):
                self.plan()
        self.policy['rules'][0]['min_age_days'] = 7
        self.policy['rules'].append(dict(self.policy['rules'][0]))
        self.save_policy()
        with self.assertRaises(G.SafetyError):
            self.plan()

    def test_wrong_confirmation_tamper_and_expiry_refuse_before_write(self):
        source = self.file()
        plan = self.plan()
        with self.assertRaises(G.SafetyError):
            G.apply_plan(self.plan_path, self.policy_path, self.quarantine, 'wrong', process_probe=lambda: [])
        with self.assertRaises(G.SafetyError):
            G.apply_plan(self.plan_path, self.policy_path, self.quarantine, plan['plan_id'], now=self.now + 2 * G.DAY, process_probe=lambda: [])
        plan['entries'][0]['source'] = str(self.base / 'escape.bin')
        self.plan_path.write_text(json.dumps(plan))
        with self.assertRaises(G.SafetyError):
            self.apply(plan)
        self.assertTrue(source.exists())
        self.assertFalse(self.quarantine.exists())

    def test_policy_change_refuses(self):
        self.file()
        plan = self.plan()
        self.policy['rules'][0]['reason'] = 'Changed review'
        self.save_policy()
        with self.assertRaises(G.SafetyError):
            self.apply(plan)

    def test_process_guard_and_low_space_retain_sources(self):
        source = self.file()
        plan = self.plan()
        with self.assertRaises(G.SafetyError):
            G.apply_plan(self.plan_path, self.policy_path, self.quarantine, plan['plan_id'],
                         process_probe=lambda: [{'name': 'Codex.exe'}])
        with mock.patch.object(G.shutil, 'disk_usage', return_value=mock.Mock(free=0)):
            with self.assertRaises(G.SafetyError):
                self.apply(plan)
        self.assertTrue(source.exists())

    def test_metadata_or_contents_change_is_retained(self):
        source = self.file(content=b'aaaa')
        plan = self.plan()
        stamp = source.stat().st_mtime_ns
        source.write_bytes(b'bbbb')
        os.utime(source, ns=(stamp, stamp))
        report = self.apply(plan)
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(source.read_bytes(), b'bbbb')

    def test_new_file_invalidates_whole_temp_bundle(self):
        self.policy['rules'][0]['kind'] = 'reviewed_temp'
        self.save_policy()
        source = self.file()
        plan = self.plan()
        self.file('another-old.bin')
        report = self.apply(plan)
        self.assertEqual(report['entries'][0]['state'], 'skipped')
        self.assertTrue(source.exists())

    def test_round_trip_quarantine_restore_and_retention_purge(self):
        source = self.file()
        original = source.read_bytes()
        plan = self.plan()
        report = self.apply(plan)
        self.assertEqual(report['status'], 'completed')
        self.assertFalse(source.exists())
        manifest_path = Path(report['manifest_path'])
        backup = Path(report['entries'][0]['backup'])
        self.assertEqual(backup.read_bytes(), original)
        self.assertIn('observed_free_delta', report)
        with self.assertRaises(G.SafetyError):
            G.purge_manifest(manifest_path, report['run_id'], now=self.now + G.DAY, process_probe=lambda: [], opener=fixture_opener)
        restored = G.restore_manifest(manifest_path, report['run_id'], process_probe=lambda: [])
        self.assertEqual(restored['restore_status'], 'completed')
        self.assertEqual(source.read_bytes(), original)
        purged = G.purge_manifest(manifest_path, report['run_id'], now=self.now + 8 * G.DAY, process_probe=lambda: [], opener=fixture_opener)
        self.assertEqual(purged['entries'][0]['state'], 'purged')
        self.assertFalse(backup.exists())
        self.assertEqual(source.read_bytes(), original)

    def test_restore_never_overwrites_new_work(self):
        source = self.file()
        report = self.apply(self.plan())
        source.write_bytes(b'new project work')
        restored = G.restore_manifest(Path(report['manifest_path']), report['run_id'], process_probe=lambda: [])
        self.assertEqual(restored['restore_status'], 'partial')
        self.assertEqual(source.read_bytes(), b'new project work')

    def test_modified_quarantine_and_manifest_are_preserved(self):
        self.file()
        report = self.apply(self.plan())
        path = Path(report['manifest_path'])
        backup = Path(report['entries'][0]['backup'])
        backup.write_bytes(b'tampered quarantine')
        restored = G.restore_manifest(path, report['run_id'], process_probe=lambda: [])
        self.assertEqual(restored['restore_status'], 'partial')
        raw = json.loads(path.read_text())
        raw['entries'][0]['backup'] = str(self.base / 'unrelated.bin')
        path.write_text(json.dumps(raw))
        with self.assertRaises(G.SafetyError):
            G.purge_manifest(path, report['run_id'], now=self.now + 8 * G.DAY, process_probe=lambda: [], opener=fixture_opener)
        self.assertTrue(backup.exists())

    def test_copy_failure_retains_original(self):
        source = self.file()
        plan = self.plan()
        with mock.patch.object(G, '_copy_verified', side_effect=OSError('full disk')):
            report = self.apply(plan)
        self.assertEqual(report['status'], 'partial')
        self.assertTrue(source.exists())

    def test_manifest_is_durable_before_source_removal(self):
        source = self.file()
        plan = self.plan()
        original_writer = G.Journal.record
        def fail_when_copied(writer, index):
            if writer.manifest['entries'][index]['state'] == 'copied':
                raise OSError('journal unavailable')
            return original_writer(writer, index)
        with mock.patch.object(G.Journal, 'record', new=fail_when_copied):
            report = self.apply(plan)
        self.assertTrue(source.exists())
        self.assertEqual(report['status'], 'partial')

    def test_interrupted_checkpoint_can_be_restored_from_journal(self):
        source = self.file()
        plan = self.plan()
        with mock.patch.object(G.Journal, 'checkpoint', side_effect=OSError('interrupted final checkpoint')):
            with self.assertRaises(OSError):
                self.apply(plan)
        self.assertFalse(source.exists())
        manifest = next(self.quarantine.glob('*/manifest.json'))
        result = G.restore_manifest(manifest, manifest.parent.name, process_probe=lambda: [])
        self.assertEqual(result['restore_status'], 'completed')
        self.assertEqual(source.read_bytes(), b'old synthetic cache')

    def test_journal_fsync_failure_stops_apply_and_previous_files_remain_restorable(self):
        first = self.file('a.bin', b'first disposable file')
        second = self.file('b.bin', b'second disposable file')
        third = self.file('c.bin', b'third disposable file')
        plan = self.plan()
        original_record = G.Journal.record

        def fail_after_second_copy_is_appended(writer, index):
            if index == 1 and writer.manifest['entries'][index]['state'] == 'copied':
                with mock.patch.object(G.os, 'fsync', side_effect=OSError('fsync failed after append')):
                    return original_record(writer, index)
            return original_record(writer, index)

        with mock.patch.object(G.Journal, 'record', new=fail_after_second_copy_is_appended):
            with self.assertRaises(G.JournalError):
                self.apply(plan)
        self.assertFalse(first.exists())
        self.assertEqual(second.read_bytes(), b'second disposable file')
        self.assertEqual(third.read_bytes(), b'third disposable file')
        manifest = next(self.quarantine.glob('*/manifest.json'))
        events = [json.loads(line) for line in manifest.with_name('journal.jsonl').read_text().splitlines()]
        self.assertEqual([event['sequence'] for event in events], [1, 2, 3])
        self.assertFalse((manifest.parent / 'files' / '00000002.bin').exists())
        recovered = G.restore_manifest(manifest, manifest.parent.name, process_probe=lambda: [])
        self.assertEqual(first.read_bytes(), b'first disposable file')
        self.assertEqual(second.read_bytes(), b'second disposable file')
        self.assertEqual(third.read_bytes(), b'third disposable file')
        self.assertEqual(recovered['entries'][0]['state'], 'restored')

    def test_journal_append_failure_poison_prevents_writes_and_checkpoints(self):
        self.file()
        report = self.apply(self.plan())
        manifest = Path(report['manifest_path'])
        manifest_before = manifest.read_bytes()
        writer = G.Journal(manifest, G.load_manifest(manifest, report['run_id']))
        with mock.patch.object(G.os, 'fsync', side_effect=OSError('fsync failed after append')):
            with self.assertRaises(G.JournalError):
                writer.record(0)
        journal_after_failure = manifest.with_name('journal.jsonl').read_bytes()
        with self.assertRaises(G.JournalError):
            writer.record(0)
        with self.assertRaises(G.JournalError):
            writer.checkpoint()
        self.assertEqual(manifest.read_bytes(), manifest_before)
        self.assertEqual(manifest.with_name('journal.jsonl').read_bytes(), journal_after_failure)

    def test_unterminated_journal_tail_is_preserved_before_restore(self):
        source = self.file()
        report = self.apply(self.plan())
        manifest = Path(report['manifest_path'])
        journal = manifest.with_name('journal.jsonl')
        valid_prefix = journal.read_bytes()
        tail = b'{"sequence":3,"entry":{"state":"uncommitted'
        journal.write_bytes(valid_prefix + tail)
        files_before = set(manifest.parent.iterdir())
        loaded = G.load_manifest(manifest, report['run_id'])
        self.assertIn('_journal_tail', loaded)
        self.assertEqual(journal.read_bytes(), valid_prefix + tail)
        self.assertEqual(set(manifest.parent.iterdir()), files_before)
        result = G.restore_manifest(manifest, report['run_id'], process_probe=lambda: [])
        self.assertEqual(result['restore_status'], 'completed')
        self.assertEqual(source.read_bytes(), b'old synthetic cache')
        preserved = [p for p in set(manifest.parent.iterdir()) - files_before if p.is_file()]
        self.assertTrue(any(tail in p.read_bytes() for p in preserved), 'The uncommitted tail must have a recovery copy.')
        self.assertTrue(journal.read_bytes().startswith(valid_prefix))
        self.assertNotIn('_journal_tail', G.load_manifest(manifest, report['run_id']))

    def test_changed_journal_tail_is_not_truncated_using_stale_replay(self):
        self.file()
        report = self.apply(self.plan())
        manifest = Path(report['manifest_path'])
        journal = manifest.with_name('journal.jsonl')
        journal.write_bytes(journal.read_bytes() + b'{"sequence":3,"entry":')
        loaded = G.load_manifest(manifest, report['run_id'])
        changed = journal.read_bytes() + b'{"changed":true'
        journal.write_bytes(changed)
        manifest_before = manifest.read_bytes()
        with self.assertRaises((G.SafetyError, G.JournalError)):
            G.Journal(manifest, loaded)
        self.assertEqual(journal.read_bytes(), changed)
        self.assertEqual(manifest.read_bytes(), manifest_before)

    def test_newline_terminated_journal_corruption_is_never_discarded(self):
        source = self.file()
        report = self.apply(self.plan())
        manifest = Path(report['manifest_path'])
        journal = manifest.with_name('journal.jsonl')
        valid = journal.read_bytes()
        lines = valid.splitlines(keepends=True)
        corruptions = {
            'middle malformed record': lines[0] + b'not a JSON event\n' + b''.join(lines[1:]),
            'terminated malformed tail': valid + b'{"sequence":\n',
            'terminated invalid hash': valid + json.dumps({
                'sequence': 3, 'previous': json.loads(lines[-1])['hash'],
                'index': 0, 'entry': report['entries'][0], 'hash': 'invalid'
            }).encode('utf-8') + b'\n',
        }
        for label, corrupted in corruptions.items():
            with self.subTest(label=label):
                journal.write_bytes(corrupted)
                with self.assertRaises((G.SafetyError, G.JournalError, ValueError)):
                    G.restore_manifest(manifest, report['run_id'], process_probe=lambda: [])
                self.assertFalse(source.exists())
                self.assertEqual(Path(report['entries'][0]['backup']).read_bytes(), b'old synthetic cache')
                self.assertEqual(journal.read_bytes(), corrupted)

    def test_journal_truncation_before_checkpoint_is_never_recovered(self):
        source = self.file()
        report = self.apply(self.plan())
        manifest = Path(report['manifest_path'])
        journal = manifest.with_name('journal.jsonl')
        lines = journal.read_bytes().splitlines(keepends=True)
        self.assertGreaterEqual(len(lines), 2)
        for suffix in (b'', lines[-1][:len(lines[-1]) // 2]):
            with self.subTest(partial_tail=bool(suffix)):
                truncated = b''.join(lines[:-1]) + suffix
                journal.write_bytes(truncated)
                with self.assertRaises((G.SafetyError, G.JournalError, ValueError)):
                    G.restore_manifest(manifest, report['run_id'], process_probe=lambda: [])
                self.assertFalse(source.exists())
                self.assertEqual(Path(report['entries'][0]['backup']).read_bytes(), b'old synthetic cache')
                self.assertEqual(journal.read_bytes(), truncated)

    def test_bulk_quarantine_does_not_rewrite_full_manifest_per_file(self):
        for i in range(30):
            self.file(str(i) + '.bin')
        plan = self.plan()
        with mock.patch.object(G, 'write_json', wraps=G.write_json) as writer:
            report = self.apply(plan)
        self.assertEqual(report['status'], 'completed')
        self.assertLessEqual(writer.call_count, 3)

    def test_restore_hash_mismatch_never_publishes_original(self):
        source = self.file()
        report = self.apply(self.plan())
        with mock.patch.object(G, '_copy_verified', return_value='different hash'):
            result = G.restore_manifest(Path(report['manifest_path']), report['run_id'], process_probe=lambda: [])
        self.assertEqual(result['restore_status'], 'partial')
        self.assertFalse(source.exists())

    def test_ai_state_directories_are_protected(self):
        for relative in ('.opencodex/responses-state-spill', '.claude/projects', '.gemini/antigravity'):
            root = self.base / relative
            root.mkdir(parents=True)
            self.policy['rules'][0]['root'] = str(root)
            self.save_policy()
            with self.assertRaises(G.SafetyError):
                self.plan()

    def test_uncertain_backup_prevents_purge_completed_status(self):
        source = self.file()
        plan = self.plan()
        def cannot_delete(path):
            @contextlib.contextmanager
            def opened():
                with path.open('rb') as stream:
                    def fail(*args):
                        raise OSError('delete refused')
                    yield stream, fail
            return opened()
        report = G.apply_plan(self.plan_path, self.policy_path, self.quarantine, plan['plan_id'], now=self.now,
                              process_probe=lambda: [], opener=cannot_delete)
        self.assertEqual(report['status'], 'partial')
        result = G.purge_manifest(Path(report['manifest_path']), report['run_id'], now=self.now + 8 * G.DAY,
                                  process_probe=lambda: [], opener=fixture_opener)
        self.assertEqual(result['purge_status'], 'partial')
        self.assertTrue(result['purge_outstanding'])
        self.assertTrue(source.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows bundle locking')
    def test_reviewed_bundle_locks_and_copies_all_members(self):
        self.policy['rules'][0]['kind'] = 'reviewed_temp'
        self.save_policy()
        a, b = self.file('a.bin'), self.file('b.bin')
        plan = self.plan()
        report = G.apply_plan(self.plan_path, self.policy_path, self.quarantine, plan['plan_id'], now=self.now, process_probe=lambda: [])
        self.assertEqual(report['status'], 'completed', report)
        self.assertFalse(a.exists())
        self.assertFalse(b.exists())

    def test_second_bundle_disposition_failure_cancels_first_removal(self):
        self.policy['rules'][0]['kind'] = 'reviewed_temp'
        self.save_policy()
        first, second = self.file('a.bin'), self.file('b.bin')
        plan = self.plan()
        dispositions = []

        @contextlib.contextmanager
        def fail_second_disposition(path):
            remove_on_close = False

            def disposition(value=True):
                nonlocal remove_on_close
                dispositions.append((path, value))
                if path == second and value:
                    raise OSError('second disposition refused')
                remove_on_close = value

            try:
                with path.open('rb') as stream:
                    yield stream, disposition
            finally:
                if remove_on_close:
                    path.unlink()

        report = G.apply_plan(self.plan_path, self.policy_path, self.quarantine, plan['plan_id'],
                              now=self.now, process_probe=lambda: [], opener=fail_second_disposition)
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(dispositions, [(first, True), (second, True), (first, False)])
        self.assertEqual(first.read_bytes(), b'old synthetic cache')
        self.assertEqual(second.read_bytes(), b'old synthetic cache')
        self.assertTrue(all(entry['state'] == 'retained_with_copy' for entry in report['entries']))
        for entry in report['entries']:
            self.assertEqual(Path(entry['backup']).read_bytes(), b'old synthetic cache')

    @unittest.skipUnless(os.name == 'nt', 'NTFS alternate streams')
    def test_ads_in_second_temp_member_preserves_whole_bundle(self):
        self.policy['rules'][0]['kind'] = 'reviewed_temp'
        self.save_policy()
        a, b = self.file('a.bin'), self.file('b.bin')
        Path(str(b) + ':notes').write_text('keep')
        os.utime(b, (self.now - 10 * G.DAY,) * 2)
        plan = self.plan()
        self.assertEqual(len(plan['entries']), 2)
        report = self.apply(plan)
        self.assertEqual(report['status'], 'partial')
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows process inventory')
    def test_process_inventory_is_fail_closed(self):
        for stdout in ('', 'garbage', '[]'):
            with mock.patch.object(G.subprocess, 'run', return_value=mock.Mock(stdout=stdout)):
                with self.assertRaises(G.SafetyError):
                    G.active_processes()
        rows = [{'ProcessId': os.getpid(), 'Name': 'python.exe'}, {'ProcessId': 987654, 'Name': 'node.exe'}]
        with mock.patch.object(G.subprocess, 'run', return_value=mock.Mock(stdout=json.dumps(rows))):
            self.assertEqual(G.active_processes(), [{'pid': 987654, 'name': 'node.exe'}])

    @unittest.skipUnless(os.name == 'nt', 'Native exclusive Windows handle')
    def test_windows_exclusive_handle_round_trip(self):
        source = self.file()
        plan = self.plan()
        report = G.apply_plan(self.plan_path, self.policy_path, self.quarantine, plan['plan_id'],
                              now=self.now, process_probe=lambda: [])
        self.assertEqual(report['status'], 'completed', report)
        self.assertFalse(source.exists())
        restored = G.restore_manifest(Path(report['manifest_path']), report['run_id'], process_probe=lambda: [])
        self.assertEqual(restored['restore_status'], 'completed')
        result = G.purge_manifest(Path(report['manifest_path']), report['run_id'], now=self.now + 8 * G.DAY, process_probe=lambda: [])
        self.assertEqual(result['purge_status'], 'completed')
        self.assertTrue(source.exists())

    @unittest.skipUnless(os.name == 'nt', 'Native Windows locks')
    def test_locked_source_is_skipped(self):
        source = self.file()
        plan = self.plan()
        with G.exclusive_file(source):
            report = self.apply(plan)
        self.assertEqual(report['status'], 'partial')
        self.assertTrue(source.exists())

    @unittest.skipUnless(os.name == 'nt', 'NTFS alternate streams')
    def test_alternate_stream_is_not_lost(self):
        source = self.file()
        Path(str(source) + ':notes').write_text('must be preserved')
        os.utime(source, (self.now - 10 * G.DAY,) * 2)
        plan = self.plan()
        report = self.apply(plan)
        self.assertEqual(report['status'], 'partial')
        self.assertTrue(source.exists())
        self.assertEqual(Path(str(source) + ':notes').read_text(), 'must be preserved')

    @unittest.skipUnless(os.name == 'nt', 'Windows junctions')
    def test_junction_target_is_not_traversed(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'sentinel.bin').write_bytes(b'keep')
        junction = self.source / 'junction'
        q = lambda p: "'" + str(p).replace("'", "''") + "'"
        result = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
                                 'New-Item -ItemType Junction -Path ' + q(junction) + ' -Target ' + q(outside) + ' | Out-Null'],
                                capture_output=True, text=True)
        if result.returncode or not junction.exists():
            self.skipTest('Junction creation unavailable')
        self.addCleanup(lambda: os.rmdir(junction) if junction.exists() else None)
        self.assertEqual(self.plan()['entries'], [])
        self.assertEqual((outside / 'sentinel.bin').read_bytes(), b'keep')


if __name__ == '__main__':
    unittest.main()
