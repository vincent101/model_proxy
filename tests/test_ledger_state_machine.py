"""Ledger state machine unit tests. Fake adapter only; no native write claims."""
import copy
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import platform_tmp  # noqa: E402  (同目录测试基建)
from core import server
from core.config_snapshot import ConfigSnapshot, ConfigSnapshotError, FileMarker
from core.discovery import PreflightError


def initial_data(n=100):
    value = server.UsageTotalsStore._empty_data()
    value['total']['requests'] = n
    value['total']['ok'] = n
    return value


ACC = {'status': 200, 'supply': 'fake', 'route': 'fake', 'strategy': 'fake'}
MARKER = FileMarker(1, 2, 3, 4, 5)


class FakeAdapter:
    """In-memory failure injector, NOT a filesystem or platform adapter."""
    def __init__(self, data=None):
        self.disk = copy.deepcopy(data)
        self.identity = 2 if data is not None else None
        self.parent = 1
        self.protected = True
        self.mode = 'ok'
        self.calls = 0
        self.next_id = 10
        self.bad_observation = False

    def _observation(self):
        if self.bad_observation or not self.protected:
            raise RuntimeError('secret exception must not be logged')
        return server.LedgerObservation(copy.deepcopy(self.disk), (self.parent, self.identity, self.protected))

    def bind_startup(self, path, snapshot):
        observed = self._observation()
        expected_data = None if snapshot is None else snapshot.config
        expected_identity = None if snapshot is None else snapshot.marker.inode
        if observed.data != expected_data or self.identity != expected_identity:
            raise ValueError('startup changed')
        return observed

    def observe(self, path):
        return self._observation()

    def matches(self, observed, prior):
        return observed.evidence == prior.evidence and observed.data == prior.data

    def commit(self, path, baseline, data):
        self.calls += 1
        if not self.matches(self._observation(), baseline):
            return server.LedgerCommitResult('NOT_APPLIED')
        if self.mode == 'pending':
            return server.LedgerCommitResult('NOT_APPLIED')
        self.next_id += 1
        proposed = server.LedgerObservation(copy.deepcopy(data), (self.parent, self.next_id, True))
        if self.mode == 'exception':
            raise RuntimeError('secret')
        if self.mode == 'unknown_b':
            return server.LedgerCommitResult('UNKNOWN', proposed)
        self.disk = copy.deepcopy(data)
        self.identity = self.next_id
        if self.mode == 'unknown_c':
            return server.LedgerCommitResult('UNKNOWN', proposed)
        return server.LedgerCommitResult('CONFIRMED', proposed, self._observation())


def make_store(n=100):
    initial = initial_data(n)
    adapter = FakeAdapter(initial)
    snapshot = ConfigSnapshot(copy.deepcopy(initial), MARKER)
    store = server.UsageTotalsStore.from_verified_snapshot(Path('/fake/ledger'), snapshot, adapter=adapter)
    return store, adapter


class TestLedgerStateMachine(unittest.TestCase):
    def test_unknown_c_reconciles_without_double_count(self):
        store, adapter = make_store()
        adapter.mode = 'unknown_c'
        store.record(ACC, 1)
        self.assertEqual((store.persistence_state, adapter.disk['total']['requests']), ('UNKNOWN', 101))
        adapter.bad_observation = True
        store.record(ACC, 1)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(store._data['total']['requests'], 102)
        adapter.bad_observation = False
        adapter.mode = 'ok'
        store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'CLEAN')
        self.assertEqual(adapter.disk['total']['requests'], 103)
        self.assertEqual(adapter.calls, 2)

    def test_unknown_b_reconciles_all_uncommitted(self):
        store, adapter = make_store()
        adapter.mode = 'unknown_b'
        store.record(ACC, 1)
        adapter.bad_observation = True
        store.record(ACC, 1)
        self.assertEqual(adapter.disk['total']['requests'], 100)
        adapter.bad_observation = False
        adapter.mode = 'ok'
        store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'CLEAN')
        self.assertEqual(adapter.disk['total']['requests'], 103)

    def test_unknown_third_content_or_identity_never_overwrites(self):
        for changed in ('content', 'identity', 'parent', 'protection'):
            with self.subTest(changed=changed):
                store, adapter = make_store()
                adapter.mode = 'unknown_c'
                store.record(ACC, 1)
                if changed == 'content':
                    adapter.disk['total']['requests'] = 500
                elif changed == 'identity':
                    adapter.identity = 888
                elif changed == 'parent':
                    adapter.parent = 999
                else:
                    adapter.protected = False
                before = copy.deepcopy(adapter.disk)
                store.record(ACC, 1)
                store.record(ACC, 1)
                self.assertEqual(store.persistence_state, 'UNKNOWN')
                self.assertEqual(store._data['total']['requests'], 103)
                self.assertEqual(adapter.disk, before)
                self.assertEqual(adapter.calls, 1)

    def test_pending_revalidates_b_then_commits_once(self):
        store, adapter = make_store()
        adapter.mode = 'pending'
        store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'PENDING')
        self.assertEqual(adapter.disk['total']['requests'], 100)
        adapter.mode = 'ok'
        store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'CLEAN')
        self.assertEqual(adapter.disk['total']['requests'], 102)

    def test_pending_changed_parent_refuses_commit(self):
        store, adapter = make_store()
        adapter.mode = 'pending'
        store.record(ACC, 1)
        adapter.parent = 987
        adapter.mode = 'ok'
        store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'UNKNOWN')
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(adapter.disk['total']['requests'], 100)

    def test_unclassified_commit_exception_is_unknown_not_pending(self):
        store, adapter = make_store()
        adapter.mode = 'exception'
        store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'UNKNOWN')
        adapter.mode = 'ok'
        store.record(ACC, 1)
        self.assertEqual(adapter.disk['total']['requests'], 102)

    def test_postcheck_failure_is_unknown_and_reconciled(self):
        store, adapter = make_store()
        original = adapter.commit
        def incomplete(*args):
            result = original(*args)
            return server.LedgerCommitResult('CONFIRMED', result.candidate, None)
        adapter.commit = incomplete
        store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'UNKNOWN')
        adapter.commit = original
        store.record(ACC, 1)
        self.assertEqual(adapter.disk['total']['requests'], 102)
        self.assertEqual(store.persistence_state, 'CLEAN')

    def test_snapshot_construction_failure_is_pending_without_commit(self):
        class BadCopy:
            def __deepcopy__(self, memo):
                raise RuntimeError('secret')
        store, adapter = make_store()
        store._data['fake_extension'] = BadCopy()
        store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'PENDING')
        self.assertEqual(store._data['total']['requests'], 101)
        self.assertEqual(adapter.calls, 0)
        self.assertEqual(adapter.disk['total']['requests'], 100)

    def test_adapter_mutating_candidate_cannot_claim_wrong_data_confirmed(self):
        store, adapter = make_store()
        original = adapter.commit
        def corrupt(path, baseline, data):
            data['total']['requests'] += 10
            return original(path, baseline, data)
        adapter.commit = corrupt
        store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'UNKNOWN')
        self.assertEqual(store._data['total']['requests'], 101)
        self.assertEqual(adapter.disk['total']['requests'], 111)
        store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'UNKNOWN')
        self.assertEqual(adapter.calls, 1)

    def test_missing_candidate_proof_cannot_accept_content_alone(self):
        store, adapter = make_store()
        original = adapter.commit
        def incomplete(*args):
            original(*args)
            return server.LedgerCommitResult('UNKNOWN')
        adapter.commit = incomplete
        store.record(ACC, 1)
        store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'UNKNOWN')
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(adapter.disk['total']['requests'], 101)

    def test_default_adapter_is_disabled_and_does_not_touch_file(self):
        with platform_tmp.TemporaryDirectory() as directory:
            path = Path(directory) / 'totals.json'
            content = json.dumps(initial_data())
            path.write_text(content)
            snap = server.read_stable_config(path, reject_duplicate_keys=True)
            store = server.UsageTotalsStore.from_verified_snapshot(path, snap)
            store.record(ACC, 1)
            self.assertEqual(store.persistence_state, 'PENDING')
            self.assertEqual(store._data['total']['requests'], 101)
            self.assertEqual(path.read_text(), content)

    def test_first_verified_absence_and_concurrent_requests(self):
        adapter = FakeAdapter()
        store = server.UsageTotalsStore.from_verified_snapshot(Path('/fake/ledger'), None, adapter=adapter)
        threads = [threading.Thread(target=store.record, args=(ACC, 1)) for _ in range(30)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(store.persistence_state, 'CLEAN')
        self.assertEqual(adapter.disk['total']['requests'], 30)

    def test_restart_uses_only_actual_baseline_not_lost_increments(self):
        for mode, expected in [('unknown_b', 100), ('unknown_c', 101)]:
            store, adapter = make_store()
            adapter.mode = mode
            store.record(ACC, 1)
            adapter.bad_observation = True
            store.record(ACC, 1)
            # Fake verified restart; no access to the former process state.
            adapter.bad_observation = False
            adapter.mode = 'ok'
            restart = ConfigSnapshot(copy.deepcopy(adapter.disk), FileMarker(1, adapter.identity, 3, 4, 5))
            fresh = server.UsageTotalsStore.from_verified_snapshot(Path('/fake/ledger'), restart, adapter=adapter)
            self.assertEqual(fresh._data['total']['requests'], expected)
            fresh.record(ACC, 1)
            self.assertEqual(adapter.disk['total']['requests'], expected + 1)

    def test_forward_wrapper_continues_with_unknown_statistics(self):
        store, adapter = make_store()
        adapter.mode = 'unknown_c'
        handler = object.__new__(server.ModelProxyHandler)
        handler.server = SimpleNamespace(stream_health_store=None)
        replies = []
        def fake_forward(method):
            replies.append((method, 'fake response'))
            handler._acc['status'] = 200
            handler._acc['response_committed'] = 1
            handler._acc['stream_integrity'] = 'valid'
        handler._forward = fake_forward
        with patch.object(server, 'usage_totals', store):
            handler._forward_logged('POST')
            adapter.bad_observation = True
            handler._forward_logged('POST')
        self.assertEqual(replies, [('POST', 'fake response'), ('POST', 'fake response')])
        self.assertEqual(store._data['total']['requests'], 102)
        self.assertEqual(store.persistence_state, 'UNKNOWN')

    def test_recovery_is_logged_without_ledger_content(self):
        store, adapter = make_store()
        adapter.mode = 'pending'
        with self.assertLogs(server.log, level='INFO') as logs:
            store.record(ACC, 1)
            adapter.mode = 'ok'
            store.record(ACC, 1)
        self.assertTrue(any('recovered' in line for line in logs.output))
        self.assertFalse(any('supply=' in line for line in logs.output))
        self.assertEqual(store.persistence_state, 'CLEAN')

    def test_warnings_are_rate_limited_across_state_transitions(self):
        store, adapter = make_store()
        adapter.mode = 'pending'
        with patch.object(server.time, 'monotonic', return_value=10), self.assertLogs(server.log, level='WARNING') as logs:
            store.record(ACC, 1)
            adapter.parent = 888
            store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'UNKNOWN')
        self.assertEqual(len(logs.output), 1)

    def test_warnings_are_sanitized_and_rate_limited(self):
        store, adapter = make_store()
        adapter.mode = 'unknown_c'
        with patch.object(server.time, 'monotonic', return_value=10), self.assertLogs(server.log, level='WARNING') as logs:
            store.record(ACC, 1)
            adapter.bad_observation = True
            for _ in range(5):
                store.record(ACC, 1)
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn('secret', logs.output[0])
        self.assertIn('UNKNOWN', logs.output[0])


@unittest.skipUnless(os.name == 'posix', 'POSIX unit evidence fixture')
class TestUnavailableCapture(unittest.TestCase):
    def setUp(self):
        self.tmp = platform_tmp.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'fake-ledger.json'
        self.path.write_text('{invalid', encoding='utf-8')
        os.chmod(self.path, 0o600)
        st = self.path.stat()
        ident = SimpleNamespace(canonical=str(self.path), exists=True, device=st.st_dev,
                                inode=st.st_ino, link_count=1)
        self.snapshot = SimpleNamespace(as_dict=lambda: {'totals': ident})
        self.patcher1 = patch.object(server, 'verify_no_extended_acl', return_value=None)
        self.patcher2 = patch.object(server, 'verify_local_filesystem_fd', return_value=None)
        self.patcher1.start(); self.patcher2.start()
        self.addCleanup(self.patcher1.stop); self.addCleanup(self.patcher2.stop)

    def test_corrupt_stable_content_is_unavailable_not_none(self):
        captured = server._capture_migration_ledger(self.snapshot)
        self.assertIsInstance(captured, server.LedgerUnavailable)
        before = self.path.read_bytes()
        adapter = FakeAdapter(initial_data())
        store = server.UsageTotalsStore.from_verified_snapshot(self.path, captured, adapter=adapter)
        store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'UNAVAILABLE')
        self.assertEqual(store._data['total']['requests'], 1)
        self.assertEqual(adapter.calls, 0)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(server._capture_migration_ledger(self.snapshot), captured)

    def test_bad_schema_is_unavailable(self):
        self.path.write_text('{"version": 55}', encoding='utf-8')
        self.assertIsInstance(server._capture_migration_ledger(self.snapshot), server.LedgerUnavailable)

    def test_unproven_acl_and_io_remain_fail_closed(self):
        with patch.object(server, 'verify_no_extended_acl', side_effect=PreflightError('acl', 'failed')):
            with self.assertRaises(PreflightError):
                server._capture_migration_ledger(self.snapshot)
        with patch.object(server, 'read_stable_config', side_effect=ConfigSnapshotError('config_read_error')):
            with self.assertRaises(PreflightError):
                server._capture_migration_ledger(self.snapshot)

    def test_identity_drift_and_insecure_mode_remain_fail_closed(self):
        self.snapshot.as_dict()['totals'].inode += 1
        with self.assertRaises(PreflightError):
            server._capture_migration_ledger(self.snapshot)
        self.snapshot.as_dict()['totals'].inode -= 1
        os.chmod(self.path, 0o666)
        with self.assertRaises(PreflightError):
            server._capture_migration_ledger(self.snapshot)


class TestWindowsCaptureMockOnly(unittest.TestCase):
    def test_guarded_corrupt_content_degrades_and_identity_drift_refuses(self):
        identity = SimpleNamespace(canonical=r'C:\fake\ledger.json', exists=True,
                                   link_count=1, device=7, inode=9)
        preflight = SimpleNamespace(as_dict=lambda: {'totals': identity})
        raw = b'{bad'
        guarded = SimpleNamespace(marker=(7, 9, 1, 1, len(raw)), data=raw)
        with patch.object(server, 'Path', return_value=identity.canonical), \
             patch.object(server.os, 'name', 'nt'), \
             patch('core.platform_windows.read_guarded_file', return_value=guarded):
            captured = server._capture_migration_ledger(preflight)
        self.assertIsInstance(captured, server.LedgerUnavailable)
        identity.inode = 10
        with patch.object(server, 'Path', return_value=identity.canonical), \
             patch.object(server.os, 'name', 'nt'), \
             patch('core.platform_windows.read_guarded_file', return_value=guarded):
            with self.assertRaises(PreflightError):
                server._capture_migration_ledger(preflight)


class TestStartupGuardMockOnly(unittest.TestCase):
    def test_role_conflict_cannot_be_degraded_to_unavailable(self):
        config = ConfigSnapshot({}, MARKER)
        paths = ConfigSnapshot({}, MARKER)
        store = SimpleNamespace(_lock=threading.Lock(), _probe_marker=MARKER, _config={})
        guard = (config, paths, os.environ.get('MODEL_PROXY_PORT'), server.LedgerUnavailable(MARKER))
        with patch.object(server.sys, 'platform', 'linux'), \
             patch.object(server, '_read_business_config', return_value=config), \
             patch.object(server, 'read_stable_config', return_value=paths), \
             patch.object(server, 'resolve_listen', return_value='listen'), \
             patch.object(server, 'resolve_runtime_paths', return_value=server._runtime_paths), \
             patch.object(server, 'verify_held_instance_locks'), \
             patch.object(server, 'inspect_runtime_identities', side_effect=PreflightError('role_conflict', 'failed')), \
             patch.object(server, '_capture_migration_ledger') as capture:
            with self.assertRaises(PreflightError) as caught:
                server._verify_migration_guard(guard, Path('/fake/config'), store, 'listen', None, None, None)
        self.assertEqual(caught.exception.code, 'role_conflict')
        capture.assert_not_called()

    def test_lock_failure_cannot_be_degraded_to_unavailable(self):
        config = ConfigSnapshot({}, MARKER)
        paths = ConfigSnapshot({}, MARKER)
        store = SimpleNamespace(_lock=threading.Lock(), _probe_marker=MARKER, _config={})
        unavailable = server.LedgerUnavailable(MARKER)
        guard = (config, paths, os.environ.get('MODEL_PROXY_PORT'), unavailable)
        with patch.object(server.sys, 'platform', 'linux'), \
             patch.object(server, '_read_business_config', return_value=config), \
             patch.object(server, 'read_stable_config', return_value=paths), \
             patch.object(server, 'resolve_listen', return_value='listen'), \
             patch.object(server, 'resolve_runtime_paths', return_value=server._runtime_paths), \
             patch.object(server, 'verify_held_instance_locks', side_effect=PreflightError('locks_unverified', 'failed')), \
             patch.object(server, '_capture_migration_ledger') as capture:
            with self.assertRaises(PreflightError) as caught:
                server._verify_migration_guard(guard, Path('/fake/config'), store, 'listen', None, None, None)
        self.assertEqual(caught.exception.code, 'locks_unverified')
        capture.assert_not_called()


if __name__ == '__main__':
    unittest.main()
