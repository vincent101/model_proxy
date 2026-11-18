"""Finite fake-data tests; no service, real config, credentials, or network.

The permissive test guard is deliberately NOT a production authorization.
Native Darwin tests use only a new disposable private fixture directory.
"""
import json
import os
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

from core.commands import SessionOverridesSidecar, SidecarSnapshot, SidecarWriteError
from core import secure_files
from core.sidecar_secure_io import SidecarIOError, SidecarSecureIO, _win_candidate, _win_snapshot


@unittest.skipUnless(sys.platform == 'darwin', 'native Darwin filesystem required')
class DarwinSidecarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sidecar-fixture-', dir='/private/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / '.runtime' / 'tmp').mkdir(parents=True, mode=0o700)
        (self.root / 'config').mkdir(mode=0o700)
        self.path = self.root / 'config' / 'session_overrides.json'
        self.calls = []
        def test_only_guard(target, actual, stage, artifact):
            self.calls.append((target, actual, stage, artifact))
            return True
        self.io = SidecarSecureIO(project_root=self.root, target=self.path, guard=test_only_guard)

    def test_real_absence_create_and_replace(self):
        absent = self.io.capture(self.path)
        self.assertIsNone(absent.contents)
        created = self.io.commit(self.path, absent, b'{"a": {}}')
        self.assertEqual(self.io.capture(self.path), created)
        changed = self.io.commit(self.path, created, b'{"b": {}}')
        self.assertEqual(self.io.capture(self.path), changed)
        self.assertNotEqual(created.identity, changed.identity)
        self.assertEqual(self.path.read_bytes(), b'{"b": {}}')
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(list((self.root / '.runtime' / 'tmp').iterdir()), [])
        self.assertGreater(len(self.calls), 4)

    def test_real_sidecar_state_and_exact_original_config_parent(self):
        other = self.root / 'other'
        other.mkdir(mode=0o700)
        config = other / 'business.json'
        config.write_text('{}')
        (self.path.parent / 'business.json').symlink_to(config)
        store = SessionOverridesSidecar(self.path, secure_io=self.io)
        store.apply_command('fake-client', 'fake-session', 'set', 'route-B')
        self.assertEqual(store.get_overrides_for('fake-client'), {'fake-session': 'route-B'})
        self.assertTrue(self.path.is_file())
        self.assertFalse((other / self.path.name).exists())
        self.assertEqual(config.read_text(), '{}')

    def test_changed_baseline_blocks_without_overwrite(self):
        baseline = self.io.capture(self.path)
        self.path.write_bytes(b'{"external": {}}')
        self.path.chmod(0o600)
        with self.assertRaises(SidecarWriteError) as cm:
            self.io.commit(self.path, baseline, b'{}')
        self.assertEqual(cm.exception.state, 'NOT_REPLACED')
        self.assertEqual(self.path.read_bytes(), b'{"external": {}}')

    def test_hardlink_rejected_and_verified_target_symlink_preserved(self):
        other = self.root / 'other.json'
        other.write_bytes(b'{"fake": {}}')
        other.chmod(0o600)
        os.link(other, self.path)
        with self.assertRaises(SidecarIOError):
            self.io.capture(self.path)
        self.path.unlink()
        self.path.symlink_to(other)
        captured = self.io.capture(self.path)
        self.assertEqual(captured.contents, b'{"fake": {}}')
        self.assertEqual(self.io.commit(self.path, captured, b'{}').contents, b'{}')
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(other.read_bytes(), b'{}')

    def test_denied_guard_does_not_create(self):
        baseline = self.io.capture(self.path)
        self.io.guard = lambda *_: False
        with self.assertRaises(SidecarWriteError) as cm:
            self.io.commit(self.path, baseline, b'{}')
        self.assertEqual(cm.exception.state, 'NOT_REPLACED')
        self.assertFalse(self.path.exists())

    def test_unknown_after_real_commit_recovers_candidate(self):
        store = SessionOverridesSidecar(self.path, secure_io=self.io)
        store.apply_command('fake-client', 'fake-session', 'set', 'B')
        real_writer = secure_files.secure_replace_runtime
        def unknown_after_real_commit(*args, **kwargs):
            result = real_writer(*args, **kwargs)
            raise secure_files.SecureWriteError('postcheck_failed', state='POSTCHECK_FAILED',
                                                candidate=result.candidate)
        with patch.object(secure_files, 'secure_replace_runtime', side_effect=unknown_after_real_commit):
            with self.assertRaises(SidecarWriteError) as cm:
                store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        store.maybe_reload()
        self.assertEqual(store.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertEqual(json.loads(self.path.read_bytes())['fake-client']['fake-session']['route_id'], 'C')
        self.assertTrue(store.recover())
        self.assertEqual(store.get_overrides_for('fake-client')['fake-session'], 'C')

    def test_unknown_precommit_recovers_only_baseline(self):
        store = SessionOverridesSidecar(self.path, secure_io=self.io)
        store.apply_command('fake-client', 'fake-session', 'set', 'B')
        with patch.object(secure_files, 'secure_replace_runtime',
                          side_effect=secure_files.SecureWriteError('unknown', state='UNKNOWN')):
            with self.assertRaises(SidecarWriteError):
                store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertTrue(store.recover())
        self.assertEqual(store.get_overrides_for('fake-client')['fake-session'], 'B')

    def test_equal_candidate_content_with_other_identity_stays_isolated(self):
        store = SessionOverridesSidecar(self.path, secure_io=self.io)
        store.apply_command('fake-client', 'fake-session', 'set', 'B')
        real_writer = secure_files.secure_replace_runtime
        def unknown(*args, **kwargs):
            result = real_writer(*args, **kwargs)
            raise secure_files.SecureWriteError('postcheck_failed', state='POSTCHECK_FAILED',
                                                candidate=result.candidate)
        with patch.object(secure_files, 'secure_replace_runtime', side_effect=unknown):
            with self.assertRaises(SidecarWriteError):
                store.apply_command('fake-client', 'fake-session', 'set', 'C')
        replacement = self.root / 'config' / 'third.json'
        replacement.write_bytes(self.path.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, self.path)
        self.assertFalse(store.recover())
        self.assertEqual(store.get_overrides_for('fake-client')['fake-session'], 'B')
        with self.assertRaises(SidecarWriteError):
            store.apply_command('fake-client', 'fake-session', 'set', 'B')

    def test_cleanup_and_release_uncertainty_are_unknown_not_candidate(self):
        baseline = self.io.capture(self.path)
        for attr in ('close_unknown', 'cleanup_unknown'):
            with self.subTest(attr=attr), patch.object(
                secure_files, 'secure_replace_runtime',
                side_effect=secure_files.SecureWriteError('uncertain', state='NOT_REPLACED', **{attr: True})):
                with self.assertRaises(SidecarWriteError) as cm:
                    self.io.commit(self.path, baseline, b'{}')
                self.assertEqual(cm.exception.state, 'UNKNOWN')
                self.assertTrue(cm.exception.release_unknown)
                self.assertTrue(getattr(cm.exception, attr))
                self.assertIsNone(cm.exception.candidate)

    def test_post_success_unverified_capture_is_unknown_with_stage_evidence(self):
        baseline = self.io.capture(self.path)
        original = self.io.capture
        with patch.object(self.io, 'capture', side_effect=SidecarIOError('close_failed')):
            with self.assertRaises(SidecarWriteError) as cm:
                self.io.commit(self.path, baseline, b'{}')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertIsNotNone(cm.exception.candidate)
        self.assertEqual(original(self.path).identity, cm.exception.candidate.identity)

    def test_unclassified_failure_after_writer_entry_is_unknown(self):
        baseline = self.io.capture(self.path)
        with patch.object(secure_files, 'secure_replace_runtime', side_effect=RuntimeError('fake private')):
            with self.assertRaises(SidecarWriteError) as cm:
                self.io.commit(self.path, baseline, b'{}')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertIsNone(cm.exception.candidate)
        self.assertNotIn('private', str(cm.exception))

    def test_external_parent_and_runtime_namespaces_both_require_guard(self):
        with tempfile.TemporaryDirectory(prefix='sidecar-external-', dir='/private/tmp') as d:
            target = Path(d).resolve() / 'session_overrides.json'
            checked = []
            def guard(target_path, actual, stage, artifact):
                checked.append(stage)
                return stage != str(target.parent)
            adapter = SidecarSecureIO(project_root=self.root, target=target, guard=guard,
                                      allow_external_target_staging=True)
            with self.assertRaises(SidecarIOError):
                adapter.capture(target)
            self.assertIn(str(target.parent), checked)
            self.assertFalse(target.exists())

    def test_parent_symlink_and_dotdot_keep_original_path_evidence(self):
        inner = self.path.parent / 'inner'
        inner.mkdir(mode=0o700)
        linked = self.root / 'linked'
        linked.symlink_to(inner, target_is_directory=True)
        source = str(linked) + '/../session_overrides.json'
        io = SidecarSecureIO(project_root=self.root, target=source, guard=lambda *_: True)
        captured = io.capture(source)
        self.assertIsNone(captured.contents)
        final = io.commit(source, captured, b'{}')
        self.assertEqual(final.contents, b'{}')
        self.assertTrue(linked.is_symlink())
        self.assertEqual(self.path.read_bytes(), b'{}')
        self.assertFalse((self.root / 'session_overrides.json').exists())

    def test_directory_link_replacement_before_commit_never_overwrites(self):
        link = self.root / 'config-link'
        link.symlink_to('config', target_is_directory=True)
        source = link / self.path.name
        io = SidecarSecureIO(project_root=self.root, target=source, guard=lambda *_: True)
        baseline = io.capture(source)
        other = self.root / 'other'
        other.mkdir(mode=0o700)
        link.unlink()
        link.symlink_to(other, target_is_directory=True)
        with self.assertRaises(SidecarWriteError) as cm:
            io.commit(source, baseline, b'{}')
        self.assertEqual(cm.exception.state, 'NOT_REPLACED')
        self.assertFalse(self.path.exists())
        self.assertFalse((other / self.path.name).exists())

    def test_directory_link_swap_during_writer_is_detected_before_replace(self):
        link = self.root / 'config-link'
        link.symlink_to('config', target_is_directory=True)
        source = link / self.path.name
        self.path.write_bytes(b'{"fake": {}}')
        self.path.chmod(0o600)
        other = self.root / 'other'
        other.mkdir(mode=0o700)
        io = SidecarSecureIO(project_root=self.root, target=source, guard=lambda *_: True)
        baseline = io.capture(source)
        verify = io._mac_revalidate_binding
        calls = 0
        def swap_then_verify(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                link.unlink()
                link.symlink_to(other, target_is_directory=True)
            return verify(*args)
        with patch.object(io, '_mac_revalidate_binding', side_effect=swap_then_verify):
            with self.assertRaises(SidecarWriteError) as cm:
                io.commit(source, baseline, b'{}')
        self.assertEqual(cm.exception.state, 'NOT_REPLACED')
        self.assertEqual(self.path.read_bytes(), b'{"fake": {}}')
        self.assertFalse((other / self.path.name).exists())
        self.assertEqual(list((self.root / '.runtime' / 'tmp').iterdir()), [])

    def test_postcommit_link_swap_is_unknown_and_cannot_publish_c(self):
        link = self.root / 'config-link'
        link.symlink_to('config', target_is_directory=True)
        source = link / self.path.name
        other = self.root / 'other'
        other.mkdir(mode=0o700)
        other_sidecar = other / self.path.name
        other_sidecar.write_bytes(b'{}')
        other_sidecar.chmod(0o600)
        io = SidecarSecureIO(project_root=self.root, target=source, guard=lambda *_: True)
        store = SessionOverridesSidecar(source, secure_io=io)
        store.apply_command('fake-client', 'fake-session', 'set', 'B')
        real_replace = os.replace
        def replace_then_swap(*args, **kwargs):
            real_replace(*args, **kwargs)
            link.unlink()
            link.symlink_to(other, target_is_directory=True)
        with patch.object(secure_files.os, 'replace', side_effect=replace_then_swap):
            with self.assertRaises(SidecarWriteError) as cm:
                store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertEqual(json.loads(self.path.read_bytes())['fake-client']['fake-session']['route_id'], 'C')
        self.assertEqual(other_sidecar.read_bytes(), b'{}')
        self.assertEqual(store.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertFalse(store.recover())
        # Re-creating the old textual link does not recreate its inode evidence.
        link.unlink()
        link.symlink_to('config', target_is_directory=True)
        self.assertFalse(store.recover())
        self.assertEqual(store.get_overrides_for('fake-client')['fake-session'], 'B')

    def test_unknown_recovery_for_unchanged_link_chain_uses_candidate(self):
        link = self.root / 'config-link'
        link.symlink_to('config', target_is_directory=True)
        source = link / self.path.name
        io = SidecarSecureIO(project_root=self.root, target=source, guard=lambda *_: True)
        store = SessionOverridesSidecar(source, secure_io=io)
        store.apply_command('fake-client', 'fake-session', 'set', 'B')
        writer = secure_files.secure_replace_runtime
        def after_commit(*args, **kwargs):
            outcome = writer(*args, **kwargs)
            raise secure_files.SecureWriteError('postcheck_failed', state='POSTCHECK_FAILED',
                                                candidate=outcome.candidate)
        with patch.object(secure_files, 'secure_replace_runtime', side_effect=after_commit):
            with self.assertRaises(SidecarWriteError):
                store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertTrue(store.recover())
        self.assertEqual(store.get_overrides_for('fake-client')['fake-session'], 'C')
        self.assertTrue(link.is_symlink())

    def test_guard_receives_actual_target_and_refuses_before_file_read(self):
        link = self.root / 'config-link'
        link.symlink_to('config', target_is_directory=True)
        source = link / self.path.name
        self.path.write_bytes(b'{"fake": {}}')
        self.path.chmod(0o600)
        seen = []
        def reject(original, actual, stage, artifact):
            seen.append((original, actual))
            return False
        io = SidecarSecureIO(project_root=self.root, target=source, guard=reject)
        with patch.object(secure_files, 'capture_target', wraps=secure_files.capture_target) as reader:
            with self.assertRaises(SidecarIOError):
                io.capture(source)
        reader.assert_not_called()
        self.assertEqual(seen[0], (str(source), str(self.path)))

    def test_dangling_and_loop_links_fail_without_creating_target(self):
        link = self.root / 'config-link'
        source = link / self.path.name
        io = SidecarSecureIO(project_root=self.root, target=source, guard=lambda *_: True)
        link.symlink_to('missing', target_is_directory=True)
        with self.assertRaises(SidecarIOError):
            io.capture(source)
        link.unlink()
        link.symlink_to('config-link', target_is_directory=True)
        with self.assertRaises(SidecarIOError):
            io.capture(source)
        self.assertFalse(self.path.exists())
        self.path.symlink_to('missing-sidecar.json')
        with self.assertRaises(SidecarIOError):
            self.io.capture(self.path)
        self.assertFalse((self.path.parent / 'missing-sidecar.json').exists())

    def test_guard_release_uncertainty_is_unknown(self):
        class GuardReleaseError(RuntimeError):
            release_unknown = True
        baseline = self.io.capture(self.path)
        self.io.guard = lambda *_: (_ for _ in ()).throw(GuardReleaseError())
        with self.assertRaises(SidecarWriteError) as cm:
            self.io.commit(self.path, baseline, b'{}')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertTrue(cm.exception.release_unknown)
        self.assertIsNone(cm.exception.candidate)
        self.assertFalse(self.path.exists())

    def test_relative_and_absolute_directory_links_preserve_config_source_parent(self):
        config = self.path.parent / 'business.json'
        remote = self.root / 'business-real'
        remote.mkdir(mode=0o700)
        (remote / 'business.json').write_bytes(b'{}')
        config.symlink_to(remote / 'business.json')
        for idx, text in enumerate(('config', str(self.path.parent))):
            with self.subTest(link=text):
                link = self.root / ('parent-' + str(idx))
                link.symlink_to(text, target_is_directory=True)
                source = link / self.path.name
                io = SidecarSecureIO(project_root=self.root, target=source, guard=lambda *_: True)
                baseline = io.capture(source)
                result = io.commit(source, baseline, ('{"fake%d": {}}' % idx).encode())
                self.assertEqual(io.capture(source), result)
                self.assertTrue(config.is_symlink())
                self.assertFalse((remote / self.path.name).exists())
                self.assertEqual((remote / 'business.json').read_bytes(), b'{}')


    def test_link_to_external_target_uses_actual_parent_and_runtime_staging(self):
        with tempfile.TemporaryDirectory(prefix='sidecar-ext-', dir='/private/tmp') as d:
            outside = Path(d).resolve()
            link = self.root / 'external-link'
            link.symlink_to(outside, target_is_directory=True)
            source = link / self.path.name
            actual = outside / self.path.name
            seen = []
            def fixture_guard(original, resolved, stage, artifact):
                seen.append((original, resolved, stage, artifact))
                return True
            io = SidecarSecureIO(project_root=self.root, target=source, guard=fixture_guard)
            baseline = io.capture(source)
            result = io.commit(source, baseline, b'{}')
            self.assertEqual(actual.read_bytes(), b'{}')
            self.assertEqual(io.capture(source), result)
            self.assertTrue(link.is_symlink())
            self.assertTrue(all(entry[1] == str(actual) for entry in seen))
            self.assertTrue(all(entry[2] == str(self.root / '.runtime' / 'tmp') for entry in seen))
            self.assertEqual(list(outside.glob('.model-proxy-*')), [])

    def test_external_cross_volume_exception_requires_explicit_opt_in(self):
        with tempfile.TemporaryDirectory(prefix='sidecar-ext-', dir='/private/tmp') as d:
            actual = Path(d).resolve() / self.path.name
            seen = []
            def fixture_guard(original, resolved, stage, artifact):
                seen.append(stage)
                return True
            denied = SidecarSecureIO(project_root=self.root, target=actual, guard=fixture_guard)
            baseline = denied.capture(actual)
            with patch.object(secure_files, '_same_volume', return_value=False):
                with self.assertRaises(SidecarWriteError) as cm:
                    denied.commit(actual, baseline, b'{}')
            self.assertEqual(cm.exception.state, 'NOT_REPLACED')
            self.assertFalse(actual.exists())
            allowed = SidecarSecureIO(project_root=self.root, target=actual, guard=fixture_guard,
                                      allow_external_target_staging=True)
            baseline = allowed.capture(actual)
            with patch.object(secure_files, '_same_volume', return_value=False):
                outcome = allowed.commit(actual, baseline, b'{}')
            self.assertEqual(outcome.contents, b'{}')
            self.assertIn(str(actual.parent), seen)
            self.assertEqual(list(actual.parent.glob('.model-proxy-*')), [])

    def test_release_unknown_during_writer_guard_is_not_not_replaced(self):
        class GuardReleaseError(RuntimeError):
            release_unknown = True
        baseline = self.io.capture(self.path)
        verify = self.io._mac_revalidate_binding
        def failed(binding, external):
            self.io.guard = lambda *_: (_ for _ in ()).throw(GuardReleaseError())
            return verify(binding, external)
        with patch.object(self.io, '_mac_revalidate_binding', side_effect=failed):
            with self.assertRaises(SidecarWriteError) as cm:
                self.io.commit(self.path, baseline, b'{}')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertTrue(cm.exception.release_unknown)
        self.assertFalse(self.path.exists())


    def test_final_symlink_changed_to_other_valid_file_rejects_stale_baseline(self):
        first = self.root / 'first.json'
        second = self.root / 'second.json'
        for target in (first, second):
            target.write_bytes(b'{}')
            target.chmod(0o600)
        self.path.symlink_to(first)
        baseline = self.io.capture(self.path)
        self.path.unlink()
        self.path.symlink_to(second)
        with self.assertRaises(SidecarWriteError) as cm:
            self.io.commit(self.path, baseline, b'{"fake": {}}')
        self.assertEqual(cm.exception.state, 'NOT_REPLACED')
        self.assertEqual(first.read_bytes(), b'{}')
        self.assertEqual(second.read_bytes(), b'{}')
        self.assertTrue(self.path.is_symlink())

    def test_writable_source_parent_is_not_authorized(self):
        link = self.root / 'config-link'
        link.symlink_to('config', target_is_directory=True)
        source = link / self.path.name
        io = SidecarSecureIO(project_root=self.root, target=source, guard=lambda *_: True)
        baseline = io.capture(source)
        self.path.parent.chmod(0o777)
        try:
            with self.assertRaises(SidecarWriteError) as cm:
                io.commit(source, baseline, b'{}')
            self.assertEqual(cm.exception.state, 'NOT_REPLACED')
            self.assertFalse(self.path.exists())
        finally:
            self.path.parent.chmod(0o700)


    def test_relative_source_preserves_link_dotdot_at_bound_start_cwd(self):
        inner = self.path.parent / 'inner'
        inner.mkdir(mode=0o700)
        link = self.root / 'shortcut'
        link.symlink_to('config/inner', target_is_directory=True)
        source = 'shortcut/../session_overrides.json'
        # No process-wide chdir: emulate the configured startup cwd only here.
        with patch('os.getcwd', return_value=str(self.root)):
            io = SidecarSecureIO(project_root=self.root, target=source, guard=lambda *_: True)
            baseline = io.capture(source)
            committed = io.commit(source, baseline, b'{}')
            self.assertEqual(io.capture(source), committed)
        self.assertEqual(self.path.read_bytes(), b'{}')
        self.assertFalse((self.root / self.path.name).exists())
        self.assertTrue(link.is_symlink())

    def test_guard_can_deny_final_link_to_sensitive_actual_target(self):
        fake_config = self.root / 'business.json'
        fake_config.write_bytes(b'{"config": "fake"}')
        fake_config.chmod(0o600)
        self.path.symlink_to(fake_config)
        def refuse_config(original, actual, stage, artifact):
            return actual != str(fake_config)
        io = SidecarSecureIO(project_root=self.root, target=self.path, guard=refuse_config)
        with patch.object(secure_files, 'capture_target', wraps=secure_files.capture_target) as reader:
            with self.assertRaises(SidecarIOError):
                io.capture(self.path)
        reader.assert_not_called()
        self.assertEqual(fake_config.read_bytes(), b'{"config": "fake"}')
        self.assertTrue(self.path.is_symlink())


    def test_unknown_before_replace_recovers_b_through_original_link(self):
        link = self.root / 'config-link'
        link.symlink_to('config', target_is_directory=True)
        source = link / self.path.name
        io = SidecarSecureIO(project_root=self.root, target=source, guard=lambda *_: True)
        store = SessionOverridesSidecar(source, secure_io=io)
        store.apply_command('fake-client', 'fake-session', 'set', 'B')
        with patch.object(secure_files.os, 'replace', side_effect=OSError('synthetic syscall failure')):
            with self.assertRaises(SidecarWriteError) as cm:
                store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertTrue(store.recover())
        self.assertEqual(store.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertTrue(link.is_symlink())
        self.assertEqual(list((self.root / '.runtime' / 'tmp').iterdir()), [])

    def test_unknown_after_first_create_recovers_c_through_original_link(self):
        link = self.root / 'config-link'
        link.symlink_to('config', target_is_directory=True)
        source = link / self.path.name
        io = SidecarSecureIO(project_root=self.root, target=source, guard=lambda *_: True)
        store = SessionOverridesSidecar(source, secure_io=io)
        writer = secure_files.secure_replace_runtime
        def after_commit(*args, **kwargs):
            outcome = writer(*args, **kwargs)
            raise secure_files.SecureWriteError('postcheck_failed', state='POSTCHECK_FAILED',
                                                candidate=outcome.candidate)
        with patch.object(secure_files, 'secure_replace_runtime', side_effect=after_commit):
            with self.assertRaises(SidecarWriteError) as cm:
                store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertEqual(store.get_overrides_for('fake-client'), {})
        self.assertTrue(store.recover())
        self.assertEqual(store.get_overrides_for('fake-client')['fake-session'], 'C')
        self.assertTrue(link.is_symlink())


    def test_same_bytes_with_new_mtime_do_not_reuse_baseline(self):
        self.path.write_bytes(b'{}')
        self.path.chmod(0o600)
        baseline = self.io.capture(self.path)
        before = self.path.stat()
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        self.assertEqual(self.path.read_bytes(), b'{}')
        with self.assertRaises(SidecarWriteError) as cm:
            self.io.commit(self.path, baseline, b'{"fake": {}}')
        self.assertEqual(cm.exception.state, 'NOT_REPLACED')
        self.assertEqual(self.path.read_bytes(), b'{}')


    def test_capture_keeps_guard_resource_release_error_without_secrets(self):
        class GuardError(RuntimeError):
            code = 'resource_release_failed'
        self.io.guard = lambda *_: (_ for _ in ()).throw(GuardError('fake-private'))
        with self.assertRaises(SidecarIOError) as cm:
            self.io.capture(self.path)
        self.assertTrue(cm.exception.release_unknown)
        self.assertFalse(cm.exception.close_unknown)
        self.assertNotIn('fake-private', ''.join(traceback.format_exception(cm.exception)))

    def test_capture_propagates_code_only_descriptor_close_uncertainty(self):
        from core.mac_config_path import MacConfigError
        with patch.object(self.io, '_mac_capture', side_effect=MacConfigError('fd_close_unknown')):
            with self.assertRaises(SidecarIOError) as cm:
                self.io.capture(self.path)
        self.assertTrue(cm.exception.close_unknown)
        self.assertTrue(cm.exception.release_unknown)
        self.assertEqual(str(cm.exception), 'capture_unverified')

    def test_capture_preserves_real_descriptor_close_uncertainty(self):
        from core import mac_config_path as mac
        original = mac._close_fd
        failed = []
        def close_then_fail(fd):
            original(fd)
            if not failed:
                failed.append(True)
                error = mac.MacConfigError('fd_close_unknown')
                error.close_unknown = True
                raise error
        with patch.object(mac, '_close_fd', side_effect=close_then_fail):
            with self.assertRaises(SidecarIOError) as cm:
                self.io.capture(self.path)
        self.assertTrue(cm.exception.close_unknown)
        self.assertTrue(cm.exception.release_unknown)
        self.assertEqual(str(cm.exception), 'capture_unverified')
        self.assertFalse(self.path.exists())

    def test_postcommit_mac_capture_release_uncertainty_is_reported(self):
        from core import mac_config_path as mac
        baseline = self.io.capture(self.path)
        capture, close = self.io.capture, mac._close_fd
        failed = []
        def close_then_fail(fd):
            close(fd)
            if not failed:
                failed.append(True)
                error = mac.MacConfigError('fd_close_unknown')
                error.close_unknown = True
                raise error
        def capture_with_failed_close(path):
            with patch.object(mac, '_close_fd', side_effect=close_then_fail):
                return capture(path)
        with patch.object(self.io, 'capture', side_effect=capture_with_failed_close):
            with self.assertRaises(SidecarWriteError) as cm:
                self.io.commit(self.path, baseline, b'{}')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertTrue(cm.exception.close_unknown)
        self.assertTrue(cm.exception.release_unknown)
        self.assertIsNotNone(cm.exception.candidate)
        self.assertEqual(self.path.read_bytes(), b'{}')

    def test_interrupt_after_real_mac_writer_keeps_store_isolated(self):
        store = SessionOverridesSidecar(self.path, secure_io=self.io)
        store.apply_command('fake-client', 'fake-session', 'set', 'B')
        writer = secure_files.secure_replace_runtime
        def commit_then_interrupt(*args, **kwargs):
            writer(*args, **kwargs)
            raise KeyboardInterrupt('fake-secret-in-interruption')
        with patch.object(secure_files, 'secure_replace_runtime', side_effect=commit_then_interrupt):
            with self.assertRaises(SidecarWriteError) as cm:
                store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertNotIn('fake-secret-in-interruption', ''.join(traceback.format_exception(cm.exception)))
        self.assertEqual(json.loads(self.path.read_bytes())['fake-client']['fake-session']['route_id'], 'C')
        store.maybe_reload()
        self.assertEqual(store.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertIsNotNone(store._unknown)
        self.assertFalse(store.recover())  # no staged candidate evidence survived the interruption

    def test_interrupt_in_postcommit_mac_capture_is_unknown_with_candidate(self):
        baseline = self.io.capture(self.path)
        with patch.object(self.io, 'capture', side_effect=KeyboardInterrupt('fake-private')):
            with self.assertRaises(SidecarWriteError) as cm:
                self.io.commit(self.path, baseline, b'{}')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertIsNotNone(cm.exception.candidate)
        self.assertEqual(self.path.read_bytes(), b'{}')
        self.assertNotIn('fake-private', ''.join(traceback.format_exception(cm.exception)))

    def test_interruption_during_mac_candidate_classification_is_unknown(self):
        from core import sidecar_secure_io as adapter
        baseline = self.io.capture(self.path)
        with patch.object(adapter, '_mac_candidate', side_effect=KeyboardInterrupt('fake-private')):
            with self.assertRaises(SidecarWriteError) as cm:
                self.io.commit(self.path, baseline, b'{}')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertIsNone(cm.exception.candidate)
        self.assertEqual(self.path.read_bytes(), b'{}')
        self.assertNotIn('fake-private', ''.join(traceback.format_exception(cm.exception)))



class EvidenceTests(unittest.TestCase):
    def test_guard_is_required_and_unknown_platform_fails_closed(self):
        with self.assertRaises(SidecarIOError):
            SidecarSecureIO(project_root='/fake', target='/fake/target', guard=None)
        io = SidecarSecureIO(project_root='/fake', target='/fake/target', guard=lambda *_: True)
        with patch('core.sidecar_secure_io.sys.platform', 'linux'):
            with self.assertRaises(SidecarIOError):
                io.capture('/fake/target')
            with self.assertRaises(SidecarWriteError) as cm:
                io.commit('/fake/target', SidecarSnapshot(None, ('fake',)), b'{}')
            self.assertEqual(cm.exception.state, 'NOT_REPLACED')

    def test_windows_marker_units_and_candidate_identity_use_parent_proof(self):
        from types import SimpleNamespace
        from core.windows_secure_files import IdentityObservation
        from core.platform_windows import FileIdentity
        file_id = FileIdentity(7, 9, 1, 0)
        obs = SimpleNamespace(file_identity=file_id, file_protection=('acl',),
                              marker=(7, 9, 11644473601000000000, 1, 2))
        parent = SimpleNamespace(name='fixture', observation='parent', policy='creation_parent', creation_kind='file')
        ev = SimpleNamespace(drive='C:', components=(parent,))
        value = SimpleNamespace(_target_lease=SimpleNamespace(evidence=ev), exists=False,
                                identity_observation=obs, path='C:\\fake', contents=b'{}')
        snapshot = _win_snapshot(value)
        self.assertEqual(snapshot.mtime, 1.0)
        stage = IdentityObservation(True, 'parent', file_id, ('acl',), obs.marker)
        candidate = _win_candidate(value.path, snapshot.identity[2], 'parent', stage, b'{}')
        self.assertEqual(candidate.identity, snapshot.identity)
        self.assertEqual(candidate.mtime, 1.0)
        self.assertIsNone(_win_candidate(value.path, snapshot.identity[2], 'other-parent', stage, b'{}'))
        self.assertIsNone(_win_candidate(value.path, snapshot.identity[2], 'parent', stage, b'bad'))


class SyntheticWindowsProductTests(unittest.TestCase):
    """Real adapter + product writer against an in-memory Win32 fixture only."""
    ROOT = r'C:\fixture\project'
    TARGET = ROOT + r'\session_overrides.json'
    STAGE = ROOT + r'\.runtime\tmp'

    def setUp(self):
        import ntpath
        from core import discovery, platform_windows as win, windows_acl as acl
        from core import windows_secure_files as secure
        from core.windows_paths import WindowsPathBackend
        from tests.test_windows_adapter import VirtualACL
        from tests.test_windows_discovery import PathWinBridge
        from tests.test_windows_same_volume_commit import MultiVolumeFS
        from core import sidecar_secure_io as adapter

        self.fs = MultiVolumeFS()
        self.fs.objects[self.STAGE] = self.fs.obj(31, directory=True)
        self.acl = VirtualACL(self.fs)
        paths = WindowsPathBackend(win32=PathWinBridge(self.fs), acl_backend=self.acl)
        self.checks = []
        self.io = object.__new__(SidecarSecureIO)
        self.io.root, self.io.target, self.io.staging = self.ROOT, self.TARGET, self.STAGE
        self.io.boundary, self.io.external, self.io.max_bytes = self.ROOT, False, 1024
        self.io.allow_external_target_staging = False
        self.io.guard = lambda *args: self.checks.append(args) or True
        for item in (
            patch.object(os, 'name', 'nt'),
            patch.object(self.io, '_platform', return_value='windows'),
            patch.object(os, 'path', ntpath),
            patch.object(adapter, '_absolute', side_effect=lambda p: ntpath.abspath(os.fspath(p))),
            patch.object(secure, '_windows_path_backend', return_value=paths),
            patch.object(discovery, '_windows_path_backend', return_value=paths),
            patch.object(win, '_api', side_effect=lambda backend=None: backend if backend is not None else self.fs),
            patch.object(acl, '_NativeBackend', return_value=self.acl),
        ):
            item.start()
            self.addCleanup(item.stop)


    def _realistic_windows_marker(self):
        """100 ns units at a current-scale FILETIME; fake in-memory Win32."""
        write = self.fs.write_all_and_flush
        def precise(handle, data):
            write(handle, data)
            item = self.fs.handles[handle][1]
            item['write'] = 13_400_000_000_000_000_000
            item['change'] = 13_400_000_000_000_000_000
        self.fs.write_all_and_flush = precise

    def test_exact_windows_marker_rejects_100ns_baseline_drift(self):
        self._realistic_windows_marker()
        baseline = self.io.commit(self.TARGET, self.io.capture(self.TARGET), b'{}')
        for field in ('write', 'change'):
            with self.subTest(field=field):
                self.fs.objects[self.TARGET][field] += 100
                observed = self.io.capture(self.TARGET)
                self.assertEqual(observed.mtime, baseline.mtime)  # float cannot encode the drift
                self.assertEqual(observed.contents, baseline.contents)
                self.assertNotEqual(observed.identity, baseline.identity)
                with self.assertRaises(SidecarWriteError) as cm:
                    self.io.commit(self.TARGET, baseline, b'{"fake": {}}')
                self.assertEqual(cm.exception.state, 'NOT_REPLACED')
                self.fs.objects[self.TARGET][field] -= 100
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'{}')
        self.assertFalse(self.fs.handles)

    def _unknown_c_without_resource_uncertainty(self):
        """Inject UNKNOWN after a verified fake-Win32 adapter return, not a release error."""
        self._realistic_windows_marker()
        store = SessionOverridesSidecar(self.TARGET, secure_io=self.io)
        store.apply_command('fake-client', 'fake-session', 'set', 'B')
        commit = self.io.commit
        def commit_then_unknown(*args):
            candidate = commit(*args)
            raise SidecarWriteError('UNKNOWN', candidate)
        with patch.object(self.io, 'commit', side_effect=commit_then_unknown):
            with self.assertRaises(SidecarWriteError) as cm:
                store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertFalse(cm.exception.release_unknown)
        self.assertFalse(cm.exception.close_unknown)
        self.assertFalse(cm.exception.cleanup_unknown)
        self.assertFalse(store._resource_unknown)
        candidate = store._unknown[1]
        self.assertIsNotNone(candidate)
        self.assertEqual(store.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertTrue(SessionOverridesSidecar._same_snapshot(self.io.capture(self.TARGET), candidate))
        return store, candidate

    def test_unknown_recovery_refuses_c_with_100ns_marker_change(self):
        store, candidate = self._unknown_c_without_resource_uncertainty()
        for field, marker_index in (('write', 2), ('change', 3)):
            with self.subTest(field=field):
                self.fs.objects[self.TARGET][field] += 100
                observed = self.io.capture(self.TARGET)
                self.assertEqual(observed.contents, candidate.contents)
                self.assertEqual(observed.mtime, candidate.mtime)  # float loses this 100 ns
                self.assertEqual(observed.identity[:-1], candidate.identity[:-1])
                self.assertEqual(observed.identity[-1][marker_index],
                                 candidate.identity[-1][marker_index] + 100)
                self.assertNotEqual(observed.identity, candidate.identity)
                self.assertFalse(store._resource_unknown)
                self.assertFalse(store.recover())
                self.assertEqual(store.get_overrides_for('fake-client')['fake-session'], 'B')
                with patch.object(self.io, 'commit', wraps=self.io.commit) as retry:
                    with self.assertRaises(SidecarWriteError) as blocked:
                        store.apply_command('fake-client', 'fake-session', 'set', 'B')
                self.assertEqual(blocked.exception.state, 'UNKNOWN')
                retry.assert_not_called()
                self.assertEqual(store._unknown[1], candidate)
                self.fs.objects[self.TARGET][field] -= 100
        exact_c = self.io.capture(self.TARGET)
        self.assertTrue(SessionOverridesSidecar._same_snapshot(exact_c, candidate))
        self.assertFalse(store._resource_unknown)
        self.assertTrue(store.recover())
        self.assertEqual(store.get_overrides_for('fake-client')['fake-session'], 'C')
        self.assertIsNone(store._unknown)
        self.assertFalse(self.fs.handles)

    def test_unknown_without_resource_flags_rejects_same_bytes_different_file_identity(self):
        store, candidate = self._unknown_c_without_resource_uncertainty()
        original = self.fs.objects[self.TARGET]
        # In-memory Win32 fixture models a distinct replacement object with the
        # same bytes, timestamps and ACL, but a different real file index.
        self.fs.objects[self.TARGET] = dict(original, index=original['index'] + 10_000)
        observed = self.io.capture(self.TARGET)
        self.assertEqual(observed.contents, candidate.contents)
        self.assertEqual(observed.mtime, candidate.mtime)
        self.assertNotEqual(observed.identity[3], candidate.identity[3])
        self.assertEqual(observed.identity[4], candidate.identity[4])
        self.assertFalse(store._resource_unknown)
        self.assertFalse(store.recover())
        with patch.object(self.io, 'commit', wraps=self.io.commit) as retry:
            with self.assertRaises(SidecarWriteError) as blocked:
                store.apply_command('fake-client', 'fake-session', 'set', 'B')
        self.assertEqual(blocked.exception.state, 'UNKNOWN')
        retry.assert_not_called()
        self.assertEqual(store.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertEqual(store._unknown[1], candidate)
        self.assertFalse(self.fs.handles)

    def test_final_capture_rejects_exact_marker_drift_after_verified_writer(self):
        self._realistic_windows_marker()
        baseline = self.io.capture(self.TARGET)
        capture = self.io.capture
        def changed(path):
            self.fs.objects[self.TARGET]['change'] += 100
            return capture(path)
        with patch.object(self.io, 'capture', side_effect=changed):
            with self.assertRaises(SidecarWriteError) as cm:
                self.io.commit(self.TARGET, baseline, b'{}')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertIsNotNone(cm.exception.candidate)
        observed = self.io.capture(self.TARGET)
        self.assertEqual(observed.contents, cm.exception.candidate.contents)
        self.assertEqual(observed.mtime, cm.exception.candidate.mtime)
        self.assertNotEqual(observed.identity, cm.exception.candidate.identity)
        self.assertFalse(self.fs.handles)

    def test_windows_interruption_during_error_candidate_mapping_is_unknown(self):
        from core import sidecar_secure_io as adapter
        store = SessionOverridesSidecar(self.TARGET, secure_io=self.io)
        store.apply_command('fake-client', 'fake-session', 'set', 'B')
        replace = self.fs.replace_existing
        def committed_then_unknown(target, source):
            replace(target, source)
            raise OSError('fake failed syscall')
        self.fs.replace_existing = committed_then_unknown
        with patch.object(adapter, '_win_candidate', side_effect=KeyboardInterrupt('fake-private')):
            with self.assertRaises(SidecarWriteError) as cm:
                store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertEqual(store.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertIsNotNone(store._unknown)
        self.assertNotIn('fake-private', ''.join(traceback.format_exception(cm.exception)))
        self.assertFalse(self.fs.handles)

    def test_windows_writer_postcheck_rejects_changed_stage_marker(self):
        from core.windows_secure_native import ProductWriteBackend
        self._realistic_windows_marker()
        baseline = self.io.capture(self.TARGET)
        postcheck = ProductWriteBackend.postcheck
        def mutated(backend):
            self.fs.objects[self.TARGET]['change'] += 100
            return postcheck(backend)
        with patch.object(ProductWriteBackend, 'postcheck', mutated):
            with self.assertRaises(SidecarWriteError) as cm:
                self.io.commit(self.TARGET, baseline, b'{}')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertIsNotNone(cm.exception.candidate)
        self.assertNotEqual(self.io.capture(self.TARGET).identity, cm.exception.candidate.identity)
        self.assertFalse(self.fs.handles)

    def test_windows_capture_keeps_release_unknown_flag(self):
        from core.windows_secure_files import TargetSnapshot, SecureSnapshotError
        close = TargetSnapshot.close
        failed = []
        def close_then_fail(snapshot):
            close(snapshot)
            if not failed:
                failed.append(True)
                raise SecureSnapshotError('release_unknown')
        with patch.object(TargetSnapshot, 'close', close_then_fail):
            with self.assertRaises(SidecarIOError) as cm:
                self.io.capture(self.TARGET)
        self.assertTrue(cm.exception.release_unknown)
        self.assertEqual(str(cm.exception), 'capture_unverified')
        self.assertFalse(self.fs.handles)

    def test_windows_postcommit_capture_keeps_release_unknown_flag(self):
        from core.windows_secure_files import TargetSnapshot, SecureSnapshotError
        baseline = self.io.capture(self.TARGET)
        capture, close = self.io.capture, TargetSnapshot.close
        failed = []
        def close_then_fail(snapshot):
            close(snapshot)
            if not failed:
                failed.append(True)
                raise SecureSnapshotError('release_unknown')
        def capture_with_failed_close(path):
            with patch.object(TargetSnapshot, 'close', close_then_fail):
                return capture(path)
        with patch.object(self.io, 'capture', side_effect=capture_with_failed_close):
            with self.assertRaises(SidecarWriteError) as cm:
                self.io.commit(self.TARGET, baseline, b'{}')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertTrue(cm.exception.release_unknown)
        self.assertIsNotNone(cm.exception.candidate)
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'{}')
        self.assertFalse(self.fs.handles)

    def test_windows_interrupt_after_writer_keeps_store_isolated(self):
        from core import windows_secure_files as secure
        store = SessionOverridesSidecar(self.TARGET, secure_io=self.io)
        store.apply_command('fake-client', 'fake-session', 'set', 'B')
        write = secure._finite_write
        def committed_then_interrupt(*args, **kwargs):
            write(*args, **kwargs)
            raise KeyboardInterrupt('fake-private')
        with patch.object(secure, '_finite_write', side_effect=committed_then_interrupt):
            with self.assertRaises(SidecarWriteError) as cm:
                store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertNotIn('fake-private', ''.join(traceback.format_exception(cm.exception)))
        self.assertEqual(json.loads(self.fs.objects[self.TARGET]['data'])['fake-client']['fake-session']['route_id'], 'C')
        self.assertIsNotNone(store._unknown)
        self.assertEqual(store.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertFalse(store.recover())
        self.assertFalse(self.fs.handles)

    def test_interrupt_in_windows_postcommit_capture_is_unknown(self):
        baseline = self.io.capture(self.TARGET)
        with patch.object(self.io, 'capture', side_effect=KeyboardInterrupt('fake-private')):
            with self.assertRaises(SidecarWriteError) as cm:
                self.io.commit(self.TARGET, baseline, b'{}')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertIsNotNone(cm.exception.candidate)
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'{}')
        self.assertNotIn('fake-private', ''.join(traceback.format_exception(cm.exception)))
        self.assertFalse(self.fs.handles)

    def test_windows_same_bytes_with_changed_write_time_rejects_baseline(self):
        first = self.io.capture(self.TARGET)
        baseline = self.io.commit(self.TARGET, first, b'{}')
        self.fs.objects[self.TARGET]['write'] += 100_000_000
        with self.assertRaises(SidecarWriteError) as cm:
            self.io.commit(self.TARGET, baseline, b'{"fake": {}}')
        self.assertEqual(cm.exception.state, 'NOT_REPLACED')
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'{}')
        self.assertFalse(self.fs.handles)

    def test_windows_guard_release_unknown_is_not_not_replaced(self):
        class GuardReleaseError(RuntimeError):
            close_unknown = True
        baseline = self.io.capture(self.TARGET)
        self.io.guard = lambda *_: (_ for _ in ()).throw(GuardReleaseError())
        with self.assertRaises(SidecarWriteError) as cm:
            self.io.commit(self.TARGET, baseline, b'{}')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertTrue(cm.exception.release_unknown)
        self.assertFalse(self.fs.handles)

    def test_ambiguous_windows_root_spelling_is_not_external_authority(self):
        with self.assertRaises(SidecarIOError):
            SidecarSecureIO(project_root=self.ROOT,
                            target=r'C:\fixture\PROJECT\session_overrides.json',
                            guard=lambda *_: True)

    def test_actual_windows_product_orchestration_new_and_replace(self):
        absent = self.io.capture(self.TARGET)
        self.assertIsNone(absent.contents)
        created = self.io.commit(self.TARGET, absent, b'{}')
        self.assertEqual(self.io.capture(self.TARGET).identity, created.identity)
        changed = self.io.commit(self.TARGET, created, b'{"fake": {}}')
        self.assertEqual(self.io.capture(self.TARGET).identity, changed.identity)
        self.assertEqual([event[0] for event in self.fs.commits], ['move', 'replace'])
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'{"fake": {}}')
        self.assertTrue(any(item[3] is not None for item in self.checks))
        self.assertFalse(self.fs.handles)

    def test_commit_error_with_remaining_stage_is_unknown(self):
        baseline = self.io.capture(self.TARGET)
        def fail_move(*args):
            raise OSError('synthetic failure')
        self.fs.move_new = fail_move
        with self.assertRaises(SidecarWriteError) as cm:
            self.io.commit(self.TARGET, baseline, b'{}')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertTrue(cm.exception.cleanup_unknown)
        self.assertIn(cm.exception.staging, self.fs.objects)
        self.assertIsNotNone(cm.exception.candidate)
        self.assertFalse(self.fs.handles)

    def test_unknown_after_native_move_retains_precommit_candidate(self):
        store = SessionOverridesSidecar(self.TARGET, secure_io=self.io)
        original = self.fs.move_new
        def moved_then_error(source, target):
            original(source, target)
            raise OSError('synthetic outcome unknown')
        self.fs.move_new = moved_then_error
        with self.assertRaises(SidecarWriteError) as cm:
            store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertTrue(cm.exception.release_unknown)
        self.assertTrue(cm.exception.cleanup_unknown)
        self.assertTrue(store._resource_unknown.release_unknown)
        self.assertTrue(store._resource_unknown.cleanup_unknown)
        self.assertEqual(store.get_overrides_for('fake-client'), {})  # verified absent B
        candidate = store._unknown[1]
        self.assertIsNotNone(candidate)
        # C may be an exact target match, but it does not verify the source
        # artifact's cleanup or handle release.
        self.assertTrue(SessionOverridesSidecar._same_snapshot(self.io.capture(self.TARGET), candidate))
        before_bytes = self.fs.objects[self.TARGET]['data']
        before_commits = list(self.fs.commits)
        with patch.object(self.io, 'commit', wraps=self.io.commit) as retry:
            store.maybe_reload()
            self.assertFalse(store.recover())
            with self.assertRaises(SidecarWriteError) as blocked:
                store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(blocked.exception.state, 'UNKNOWN')
        self.assertTrue(blocked.exception.release_unknown)
        self.assertTrue(blocked.exception.cleanup_unknown)
        self.assertEqual(store.get_overrides_for('fake-client'), {})
        self.assertEqual(self.fs.objects[self.TARGET]['data'], before_bytes)
        self.assertEqual(self.fs.commits, before_commits)
        retry.assert_not_called()
        self.assertFalse(self.fs.handles)

    def test_different_volume_external_target_uses_its_parent(self):
        self.io.target = r'D:\backups\session_overrides.json'
        self.io.boundary = r'D:\backups'
        self.io.external = True
        baseline = self.io.capture(self.io.target)
        with self.assertRaises(SidecarWriteError) as cm:
            self.io.commit(self.io.target, baseline, b'{}')
        self.assertEqual(cm.exception.state, 'NOT_REPLACED')
        self.assertNotIn(self.io.target, self.fs.objects)
        self.io.allow_external_target_staging = True
        final = self.io.commit(self.io.target, baseline, b'{}')
        self.assertEqual(final.contents, b'{}')
        self.assertTrue(self.fs.commits[0][2].startswith(r'D:\backups\.model-proxy-stage-'))
        self.assertFalse(self.fs.handles)

    def test_windows_release_unknown_carries_only_real_stage_evidence(self):
        from core.windows_secure_native import ProductWriteBackend, CandidateError
        baseline = self.io.capture(self.TARGET)
        finish = ProductWriteBackend.finish
        def uncertain(backend):
            finish(backend)
            raise CandidateError('release_unknown', release_unknown=True)
        with patch.object(ProductWriteBackend, 'finish', uncertain):
            with self.assertRaises(SidecarWriteError) as cm:
                self.io.commit(self.TARGET, baseline, b'{}')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertTrue(cm.exception.release_unknown)
        self.assertIsNotNone(cm.exception.candidate)
        self.assertEqual(self.io.capture(self.TARGET).identity, cm.exception.candidate.identity)
        self.assertFalse(self.fs.handles)


if __name__ == '__main__':
    unittest.main()
