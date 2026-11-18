"""Offline state-machine tests: fake files/evidence, not platform security tests."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.commands import (CommandContext, SessionOverridesSidecar, SidecarSnapshot,
                           SidecarWriteError, handle_route_command)
from core.server import extract_route_candidates


class FakeSecureIO:
    """Only simulates the adapter contract; never use as a security adapter."""
    def __init__(self):
        self.mode = 'ok'
        self.unavailable = False
        self.commits = 0

    def capture(self, path):
        if self.unavailable:
            raise PermissionError('synthetic denial')
        parent = path.parent.stat()
        try:
            st = path.stat()
            content = path.read_bytes()
            ident = (parent.st_dev, parent.st_ino, st.st_dev, st.st_ino, st.st_mode)
            return SidecarSnapshot(content, ident, st.st_mtime)
        except FileNotFoundError:
            return SidecarSnapshot(None, (parent.st_dev, parent.st_ino, 'absent'))

    def commit(self, path, baseline, content):
        self.commits += 1
        now = self.capture(path)
        if not SessionOverridesSidecar._same_snapshot(now, baseline):
            raise SidecarWriteError('NOT_REPLACED')
        if self.mode == 'before':
            raise SidecarWriteError('NOT_REPLACED')
        if self.mode == 'unknown_before':
            raise SidecarWriteError('UNKNOWN')
        tmp = path.parent / 'synthetic-stage'
        tmp.write_bytes(content)
        os.replace(tmp, path)
        candidate = self.capture(path)
        if self.mode == 'unknown_after':
            raise SidecarWriteError('UNKNOWN', candidate)
        if self.mode == 'unknown_no_evidence':
            raise SidecarWriteError('UNKNOWN')
        if self.mode == 'unclassified_after':
            raise OSError('synthetic postcondition failure')
        return candidate


class TestSidecarUnknown(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'session_overrides.json'
        self.io = FakeSecureIO()
        self.store = SessionOverridesSidecar(self.path, secure_io=self.io)
        self.store.apply_command('cc', 'session', 'set', 'B')
        self.original = self.path.read_bytes()
        self.store.touch('cc', 'session')
        self.before_mem = self.store._mem_last_seen.copy()

    def values(self):
        return self.store.get_overrides_for('cc')

    def command(self, route='C'):
        return handle_route_command(CommandContext(
            route, 'cc', 'session', {'route_pool': [{'route_id': 'B', 'weight': 1}]},
            {'B': {'id': 'B'}, 'C': {'id': 'C'}}, self.store, 'B'))

    def unknown(self, mode='unknown_after'):
        self.io.mode = mode
        result = self.command()
        self.assertFalse(result.wrote)
        self.assertIn('未知', result.receipt_text)
        self.assertEqual(self.values(), {'session': 'B'})
        self.assertEqual(self.store._mem_last_seen, self.before_mem)
        return result

    def test_not_replaced_preserves_disk_memory_and_receipt(self):
        self.io.mode = 'before'
        result = self.command()
        self.assertFalse(result.wrote)
        self.assertIn('未提交', result.receipt_text)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.values(), {'session': 'B'})
        self.assertEqual(self.store._mem_last_seen, self.before_mem)
        self.io.mode = 'ok'
        self.assertTrue(self.command().wrote)
        self.assertEqual(self.values(), {'session': 'C'})

    def test_real_replace_unknown_hot_reload_and_next_command_to_c(self):
        self.unknown()
        self.assertEqual(json.loads(self.path.read_bytes())['cc']['session']['route_id'], 'C')
        count = self.io.commits
        for _ in range(3):
            self.store.maybe_reload()
            self.assertEqual(self.values(), {'session': 'B'})
        self.assertEqual(self.io.commits, count)
        self.store.touch('cc', 'session')
        touched = self.store._mem_last_seen['cc', 'session']
        self.assertTrue(self.store.recover())
        self.assertEqual(self.values(), {'session': 'C'})
        self.assertEqual(self.store._mem_last_seen['cc', 'session'], touched)
        self.io.mode = 'ok'
        self.assertTrue(self.command('B').wrote)
        self.assertEqual(self.values(), {'session': 'B'})

    def test_next_command_verifies_c_before_writing(self):
        self.unknown()
        self.io.mode = 'ok'
        self.assertTrue(self.command('B').wrote)
        self.assertEqual(self.values(), {'session': 'B'})
        self.assertEqual(json.loads(self.path.read_bytes())['cc']['session']['route_id'], 'B')

    def test_unknown_with_actual_b_revalidated_before_next_command(self):
        self.unknown('unknown_before')
        self.io.mode = 'ok'
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertTrue(self.command().wrote)
        self.assertEqual(self.values(), {'session': 'C'})

    def test_third_content_is_not_loaded_or_overwritten(self):
        self.unknown()
        third = b'{"cc":{"session":"third"}}'
        self.path.write_bytes(third)
        self.store.maybe_reload()
        count = self.io.commits
        self.assertFalse(self.store.recover())
        self.assertFalse(self.command().wrote)
        self.assertEqual(self.io.commits, count)
        self.assertEqual(self.path.read_bytes(), third)
        self.assertEqual(self.values(), {'session': 'B'})

    def test_same_contents_new_identity_is_not_proof(self):
        self.unknown()
        content = self.path.read_bytes()
        other = self.path.parent / 'other'
        other.write_bytes(content)
        os.replace(other, self.path)
        self.assertFalse(self.store.recover())
        self.assertEqual(self.values(), {'session': 'B'})
        self.assertFalse(self.command().wrote)
        self.assertEqual(self.path.read_bytes(), content)

    def test_unknown_without_candidate_proof_cannot_accept_c(self):
        self.unknown('unknown_no_evidence')
        self.assertFalse(self.store.recover())
        self.assertFalse(self.command().wrote)
        self.assertEqual(self.values(), {'session': 'B'})

    def test_unclassified_post_replace_exception_is_unknown(self):
        self.unknown('unclassified_after')
        self.assertFalse(self.store.recover())
        self.assertEqual(self.values(), {'session': 'B'})

    def test_permission_error_blocks_recovery_and_write(self):
        self.unknown()
        content = self.path.read_bytes()
        count = self.io.commits
        self.io.unavailable = True
        self.store.maybe_reload()
        self.assertFalse(self.store.recover())
        self.assertFalse(self.command().wrote)
        self.assertEqual(self.io.commits, count)
        self.assertEqual(self.path.read_bytes(), content)
        self.assertEqual(self.values(), {'session': 'B'})
        self.io.unavailable = False
        self.assertTrue(self.store.recover())
        self.assertEqual(self.values(), {'session': 'C'})

    def test_changed_protection_evidence_does_not_resolve_unknown(self):
        self.unknown()
        content = self.path.read_bytes()
        mode = self.path.stat().st_mode & 0o777
        self.path.chmod(0o600 if mode != 0o600 else 0o644)
        self.assertFalse(self.store.recover())
        self.assertFalse(self.command().wrote)
        self.assertEqual(self.values(), {'session': 'B'})
        self.assertEqual(self.path.read_bytes(), content)

    def test_hot_path_stat_permission_error_does_not_interrupt_requests(self):
        with patch.object(Path, 'stat', side_effect=PermissionError('synthetic denial')):
            self.store.maybe_reload()
            self.assertEqual(self.values(), {'session': 'B'})
        self.assertTrue(self.store.recover())
        self.assertEqual(self.values(), {'session': 'B'})

    def test_reset_unknown_c_clears_last_seen_even_if_touched_after(self):
        self.io.mode = 'unknown_after'
        with self.assertRaises(SidecarWriteError):
            self.store.apply_command('cc', 'session', 'reset')
        self.store.touch('cc', 'session')
        self.assertTrue(self.store.recover())
        self.assertEqual(self.values(), {})
        self.assertNotIn(('cc', 'session'), self.store._mem_last_seen)

    def test_failed_cleanup_does_not_remove_memory_or_disk(self):
        data = json.loads(self.path.read_bytes())
        data['cc']['old'] = {'route_id': 'B', 'last_seen': '2000-01-01T00:00:00Z'}
        self.path.write_text(json.dumps(data))
        self.assertTrue(self.store.recover())
        self.store.touch('cc', 'old')
        # Make the touch old too, so TTL cleanup will select the entry.
        self.store._mem_last_seen['cc', 'old'] = 1.0
        before_data = self.path.read_bytes()
        before_mem = self.store._mem_last_seen.copy()
        self.io.mode = 'before'
        with self.assertRaises(SidecarWriteError):
            self.store.apply_command('cc', 'session', 'set', 'C')
        self.assertEqual(self.path.read_bytes(), before_data)
        self.assertEqual(self.store._mem_last_seen, before_mem)
        self.assertIn('old', self.values())
        self.io.mode = 'ok'
        result = self.store.apply_command('cc', 'session', 'set', 'C')
        self.assertIn(('cc', 'old'), result['cleaned'])
        self.assertNotIn(('cc', 'old'), self.store._mem_last_seen)

    def test_config_symlink_keeps_sidecar_in_original_parent(self):
        root = self.path.parent
        original = root / 'config-original'
        target_dir = root / 'config-target'
        original.mkdir()
        target_dir.mkdir()
        target = target_dir / 'business.json'
        target.write_text('{}')
        config_link = original / 'business.json'
        config_link.symlink_to(target)
        sidecar = original / 'session_overrides.json'
        store = SessionOverridesSidecar(config_link.parent / sidecar.name,
                                        secure_io=FakeSecureIO())
        store.apply_command('cc', 'session', 'set', 'B')
        self.assertTrue(sidecar.exists())
        self.assertFalse((target_dir / sidecar.name).exists())
        self.assertEqual(target.read_text(), '{}')

    def test_restart_verifies_current_content_no_knowledge_of_previous_outcome(self):
        self.unknown()
        restarted = SessionOverridesSidecar(self.path, secure_io=self.io)
        self.assertEqual(restarted.get_overrides_for('cc'), {'session': 'C'})
        self.assertIsNone(restarted._unknown)
        self.assertEqual(restarted._mem_last_seen, {})

    def test_restart_unavailable_falls_back_and_never_overwrites(self):
        self.unknown()
        content = self.path.read_bytes()
        self.io.unavailable = True
        restarted = SessionOverridesSidecar(self.path, secure_io=self.io)
        self.assertEqual(restarted.get_overrides_for('cc'), {})
        strategy = {'route_pool': [{'route_id': 'base', 'weight': 1}],
                    'dispatch': {'session_overrides': restarted.get_overrides_for('cc')}}
        self.assertEqual(extract_route_candidates(strategy, 'session', {'base': {'id': 'base'}})[0]['id'], 'base')
        with self.assertRaises(SidecarWriteError):
            restarted.apply_command('cc', 'session', 'set', 'B')
        self.assertEqual(self.path.read_bytes(), content)
        self.io.unavailable = False
        self.assertTrue(restarted.recover())
        self.assertEqual(restarted.get_overrides_for('cc'), {'session': 'C'})

    def test_restart_corrupt_blocks_write_then_recovers_validated_content(self):
        self.path.write_text('{broken', encoding='utf-8')
        restarted = SessionOverridesSidecar(self.path, secure_io=self.io)
        self.assertEqual(restarted.get_overrides_for('cc'), {})
        with self.assertRaises(SidecarWriteError):
            restarted.apply_command('cc', 'session', 'set', 'C')
        self.assertEqual(self.path.read_text(), '{broken')
        self.path.write_bytes(self.original)
        self.assertTrue(restarted.recover())
        self.assertEqual(restarted.get_overrides_for('cc'), {'session': 'B'})

    def test_legacy_replace_then_exception_never_hot_loads_unverified_c(self):
        store = SessionOverridesSidecar(self.path)
        original_replace = os.replace
        def replace_then_error(source, target):
            original_replace(source, target)
            raise OSError('synthetic unknown')
        with patch('core.commands.os.replace', side_effect=replace_then_error):
            with self.assertRaises(SidecarWriteError) as caught:
                store.apply_command('cc', 'session', 'set', 'C')
        self.assertEqual(caught.exception.state, 'UNKNOWN')
        store.maybe_reload()
        self.assertEqual(store.get_overrides_for('cc'), {'session': 'B'})
        self.assertFalse(store.recover())
        self.assertEqual(json.loads(self.path.read_bytes())['cc']['session']['route_id'], 'C')


if __name__ == '__main__':
    unittest.main()
