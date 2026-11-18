"""Fake-file regressions for writer interruption, cleanup and error boundaries."""
import contextlib
import errno
import os
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

from core.mac_config_path import capture_config, open_target_parent
from core.mac_config_writer import MacWriteError, WriteAuthorization, replace_config
from core.secure_files import (
    RuntimeWriteLocations, SecureWriteError, capture_private_directory,
    capture_target, mac_private_protection, secure_replace, secure_replace_runtime,
)


class LeakyError(Exception):
    def __init__(self, secret):
        super().__init__(secret)
        self.code = secret


class RaisingCodeError(Exception):
    @property
    def code(self):
        raise RuntimeError(self.args[0])


class HostileFlagsError(Exception):
    def __init__(self, secret, observed):
        super().__init__(secret)
        self.observed = observed

    def _fail(self):
        if not self.observed['closed']:
            self.observed['read_before_close'] = True
        raise RuntimeError(self.args[0])

    close_unknown = property(lambda self: self._fail())
    cleanup_unknown = property(lambda self: self._fail())
    marker_unknown = property(lambda self: self._fail())
    staging_name = property(lambda self: self._fail())


class HostileTruth:
    def __bool__(self):
        raise RuntimeError('FAKE-TRUTH-SECRET')


class ConfusingFlagsError(Exception):
    @property
    def close_unknown(self):
        raise AttributeError('FAKE-ATTRIBUTE-SECRET')

    @property
    def cleanup_unknown(self):
        return HostileTruth()


class InterruptedFlagsError(Exception):
    @property
    def close_unknown(self):
        raise KeyboardInterrupt('FAKE-FLAG-INTERRUPT')

    @property
    def cleanup_unknown(self):
        raise GeneratorExit('FAKE-FLAG-EXIT')


class HostileMacStateError(MacWriteError):
    def __getattribute__(self, name):
        if name == 'state':
            raise RuntimeError('FAKE-STATE-SECRET')
        return super().__getattribute__(name)


class HostileSecureStateError(SecureWriteError):
    def __getattribute__(self, name):
        if name == 'state':
            raise RuntimeError('FAKE-STATE-SECRET')
        return super().__getattribute__(name)


class HostileClassError(Exception):
    def __getattribute__(self, name):
        if name == '__class__':
            raise RuntimeError('FAKE-CLASS-SECRET')
        return super().__getattribute__(name)


class MacWriterInterruptions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['MP_TEST_ROOT'])
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config'
        self.runtime = self.root / '.runtime'
        self.stage = self.runtime / 'tmp'
        self.backup = self.runtime / 'backups'
        for path in (self.config, self.runtime, self.stage, self.backup):
            path.mkdir(mode=0o700)
        self.target = self.config / 'business.json'
        self.target.write_bytes(b'fake-original')
        self.target.chmod(0o600)
        self.alias = self.root / 'link'
        self.alias.symlink_to('config/business.json')

    def locations(self, *, backup=True):
        return RuntimeWriteLocations(
            capture_private_directory(self.stage, purpose='staging'),
            capture_private_directory(self.backup, purpose='backup') if backup else None,
        )

    def config_snapshot(self, *, legacy=False):
        snapshot = capture_config(str(self.alias))
        with open_target_parent(snapshot) as (fd, _):
            parent = os.fstat(fd)
            auth = WriteAuthorization(snapshot.target_path, (parent.st_dev, parent.st_ino),
                                      snapshot.final_identity[:2], legacy)
        return snapshot, auth

    def secure_snapshot(self):
        return capture_target(self.target, authorized_root=self.config,
                              protection_reader=mac_private_protection, max_bytes=1024,
                              trusted_boundary=self.config)

    def write_config(self, snapshot, auth, *, locations):
        return replace_config(snapshot, b'fake-new', auth, preflight=lambda _: True,
                              before_replace=lambda: None, locations=locations)

    @staticmethod
    def assert_scrubbed(error, secret):
        text = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
        assert secret not in str(error) and secret not in text, text

    def test_replace_and_cleanup_interruptions_preserve_state_and_path(self):
        for variant in ('config-legacy', 'config-runtime', 'secure-legacy', 'secure-runtime'):
            with self.subTest(variant=variant):
                snapshot, auth = self.config_snapshot(legacy=variant == 'config-legacy')
                target = self.secure_snapshot()
                locations = self.locations(backup=variant != 'secure-runtime')
                real_unlink = os.unlink

                def interrupted_unlink(name, *args, **kwargs):
                    if isinstance(name, str) and name.startswith(
                            ('.model-proxy-config-', '.model-proxy-install-', '.model-proxy-runtime-')):
                        raise KeyboardInterrupt('fake cleanup interruption')
                    return real_unlink(name, *args, **kwargs)

                with patch('os.replace', side_effect=KeyboardInterrupt('fake commit interruption')):
                    with patch('os.unlink', side_effect=interrupted_unlink):
                        with self.assertRaises((MacWriteError, SecureWriteError)) as caught:
                            if variant.startswith('config'):
                                self.write_config(snapshot, auth,
                                                  locations=None if variant == 'config-legacy' else locations)
                            elif variant == 'secure-legacy':
                                secure_replace(target, b'fake-new', max_bytes=1024)
                            else:
                                secure_replace_runtime(target, b'fake-new', max_bytes=1024,
                                                       locations=locations, make_backup=False)
                error = caught.exception
                self.assertEqual(error.state, 'UNKNOWN')
                self.assertTrue(error.cleanup_unknown)
                residual = getattr(error, 'staging_name', None) or getattr(error, 'staging', None)
                self.assertIsNotNone(residual)
                residual = Path(residual)
                if not residual.is_absolute():
                    residual = self.config / residual
                self.assertEqual(residual.read_bytes(), b'fake-new')
                self.assertEqual(self.target.read_bytes(), b'fake-original')
                residual.unlink()

    def test_open_private_interrupted_close_is_not_retried_after_fd_reuse(self):
        for variant in ('config', 'secure'):
            with self.subTest(variant=variant):
                snapshot, auth = self.config_snapshot()
                target = self.secure_snapshot()
                locations = self.locations(backup=variant == 'config')
                sentinel = self.root / ('sentinel-' + variant)
                sentinel.write_bytes(b'fake-sentinel')
                actual_open, actual_close = os.open, os.close
                seen = {'fd': None, 'sentinel_fd': None, 'attempts': 0}

                def watched_open(name, flags, *args, **kwargs):
                    fd = actual_open(name, flags, *args, **kwargs)
                    if (isinstance(name, str) and name.startswith(
                            ('.model-proxy-config-', '.model-proxy-runtime-'))
                            and flags & os.O_EXCL):
                        seen['fd'] = fd
                    return fd

                def interrupted_close(fd):
                    if fd == seen['fd']:
                        seen['attempts'] += 1
                        if seen['attempts'] == 1:
                            actual_close(fd)
                            seen['sentinel_fd'] = actual_open(sentinel, os.O_RDONLY)
                            if seen['sentinel_fd'] != fd:
                                os.dup2(seen['sentinel_fd'], fd)
                                actual_close(seen['sentinel_fd'])
                                seen['sentinel_fd'] = fd
                            raise KeyboardInterrupt('fake close interruption')
                        raise AssertionError('uncertain descriptor was closed twice')
                    return actual_close(fd)

                try:
                    with patch('os.open', side_effect=watched_open), patch('os.close', side_effect=interrupted_close):
                        with self.assertRaises((MacWriteError, SecureWriteError)) as caught:
                            if variant == 'config':
                                self.write_config(snapshot, auth, locations=locations)
                            else:
                                secure_replace_runtime(target, b'fake-new', max_bytes=1024,
                                                       locations=locations, make_backup=False)
                    self.assertEqual(seen['sentinel_fd'], seen['fd'])
                    self.assertEqual(seen['attempts'], 1)
                    self.assertTrue(caught.exception.close_unknown)
                    self.assertFalse(caught.exception.cleanup_unknown)
                    self.assertEqual(caught.exception.state, 'NOT_REPLACED')
                    self.assertEqual(os.read(seen['sentinel_fd'], 20), b'fake-sentinel')
                    self.assertEqual(self.target.read_bytes(), b'fake-original')
                finally:
                    if seen['sentinel_fd'] is not None:
                        actual_close(seen['sentinel_fd'])

    def test_backup_close_and_cleanup_interruptions_report_backup(self):
        for variant in ('config-legacy', 'config-runtime', 'secure-legacy', 'secure-runtime'):
            with self.subTest(variant=variant):
                snapshot, auth = self.config_snapshot(legacy=variant == 'config-legacy')
                target = self.secure_snapshot()
                locations = self.locations()
                actual_open, actual_close, actual_unlink = os.open, os.close, os.unlink
                seen = {'fd': None, 'closes': 0}

                def watched_open(name, flags, *args, **kwargs):
                    fd = actual_open(name, flags, *args, **kwargs)
                    if isinstance(name, str) and '.bak.' in name and flags & os.O_EXCL:
                        seen['fd'] = fd
                    return fd

                def interrupted_close(fd):
                    if fd == seen['fd']:
                        seen['closes'] += 1
                        actual_close(fd)
                        raise KeyboardInterrupt('fake close interruption')
                    return actual_close(fd)

                def interrupted_unlink(name, *args, **kwargs):
                    if isinstance(name, str) and '.bak.' in name:
                        raise GeneratorExit('fake cleanup interruption')
                    return actual_unlink(name, *args, **kwargs)

                with patch('os.open', side_effect=watched_open), \
                     patch('os.close', side_effect=interrupted_close), \
                     patch('os.unlink', side_effect=interrupted_unlink):
                    with self.assertRaises((MacWriteError, SecureWriteError)) as caught:
                        if variant.startswith('config'):
                            self.write_config(snapshot, auth,
                                              locations=None if variant == 'config-legacy' else locations)
                        elif variant == 'secure-legacy':
                            secure_replace(target, b'fake-new', max_bytes=1024)
                        else:
                            secure_replace_runtime(target, b'fake-new', max_bytes=1024,
                                                   locations=locations)
                error = caught.exception
                self.assertEqual(error.state, 'NOT_REPLACED')
                self.assertTrue(error.close_unknown and error.cleanup_unknown)
                self.assertEqual(seen['closes'], 1)
                residual = getattr(error, 'backup_name', None) or getattr(error, 'backup', None)
                self.assertIsNotNone(residual)
                residual = Path(residual)
                if not residual.is_absolute():
                    residual = self.config / residual
                self.assertEqual(residual.read_bytes(), b'fake-original')
                self.assertEqual(self.target.read_bytes(), b'fake-original')
                residual.unlink()

    def test_interrupted_exclusive_open_reports_unknown_name_and_descriptor(self):
        for variant in ('config', 'secure'):
            with self.subTest(variant=variant):
                snapshot, auth = self.config_snapshot()
                target = self.secure_snapshot()
                locations = self.locations(backup=variant == 'config')
                actual_open, actual_close = os.open, os.close

                def interrupted_open(name, flags, *args, **kwargs):
                    fd = actual_open(name, flags, *args, **kwargs)
                    selected = (isinstance(name, str) and flags & os.O_EXCL and
                                ('.bak.' in name if variant == 'config'
                                 else name.startswith('.model-proxy-runtime-')))
                    if selected:
                        # Only the fake knows its descriptor; the writer does not.
                        actual_close(fd)
                        raise KeyboardInterrupt('fake open interruption')
                    return fd

                with patch('os.open', side_effect=interrupted_open):
                    with self.assertRaises((MacWriteError, SecureWriteError)) as caught:
                        if variant == 'config':
                            self.write_config(snapshot, auth, locations=locations)
                        else:
                            secure_replace_runtime(target, b'fake-new', max_bytes=1024,
                                                   locations=locations, make_backup=False)
                error = caught.exception
                self.assertEqual(error.state, 'NOT_REPLACED')
                self.assertTrue(error.close_unknown and error.cleanup_unknown)
                residual = (getattr(error, 'backup_name', None) if variant == 'config'
                            else getattr(error, 'staging', None))
                self.assertIsNotNone(residual)
                self.assertTrue(Path(residual).is_file())
                self.assertEqual(self.target.read_bytes(), b'fake-original')
                Path(residual).unlink()

    def test_interrupted_cleanup_stat_reports_owned_staging_path(self):
        for variant in ('config-legacy', 'config-runtime', 'secure-legacy', 'secure-runtime'):
            with self.subTest(variant=variant):
                snapshot, auth = self.config_snapshot(legacy=variant == 'config-legacy')
                target = self.secure_snapshot()
                locations = self.locations(backup=variant != 'secure-runtime')
                actual_stat = os.stat
                interrupted = {'commit': False}

                def interrupted_replace(*args, **kwargs):
                    interrupted['commit'] = True
                    raise KeyboardInterrupt('fake commit interruption')

                def interrupted_stat(name, *args, **kwargs):
                    if (interrupted['commit'] and isinstance(name, str)
                            and name.startswith(('.model-proxy-config-', '.model-proxy-install-',
                                                 '.model-proxy-runtime-'))):
                        raise GeneratorExit('fake cleanup stat interruption')
                    return actual_stat(name, *args, **kwargs)

                with patch('os.replace', side_effect=interrupted_replace), \
                     patch('os.stat', side_effect=interrupted_stat):
                    with self.assertRaises((MacWriteError, SecureWriteError)) as caught:
                        if variant.startswith('config'):
                            self.write_config(snapshot, auth,
                                              locations=None if variant == 'config-legacy' else locations)
                        elif variant == 'secure-legacy':
                            secure_replace(target, b'fake-new', max_bytes=1024)
                        else:
                            secure_replace_runtime(target, b'fake-new', max_bytes=1024,
                                                   locations=locations, make_backup=False)
                error = caught.exception
                self.assertEqual(error.state, 'UNKNOWN')
                self.assertTrue(error.cleanup_unknown)
                residual = getattr(error, 'staging_name', None) or getattr(error, 'staging', None)
                self.assertIsNotNone(residual)
                residual = Path(residual)
                if not residual.is_absolute():
                    residual = self.config / residual
                self.assertEqual(residual.read_bytes(), b'fake-new')
                self.assertEqual(self.target.read_bytes(), b'fake-original')
                residual.unlink()

    def test_all_held_directories_attempt_close_after_commit(self):
        for variant in ('config', 'secure'):
            with self.subTest(variant=variant):
                self.target.write_bytes(b'fake-original')
                snapshot, auth = self.config_snapshot()
                target = self.secure_snapshot()
                locations = self.locations()
                identities = {(p.stat().st_dev, p.stat().st_ino) for p in (self.stage, self.backup)}
                actual_close = os.close
                attempts = []

                def interrupted_close(fd):
                    info = os.fstat(fd)
                    identity = (info.st_dev, info.st_ino)
                    if identity in identities:
                        attempts.append(identity)
                        actual_close(fd)
                        raise KeyboardInterrupt('fake directory close interruption')
                    return actual_close(fd)

                with patch('os.close', side_effect=interrupted_close):
                    with self.assertRaises((MacWriteError, SecureWriteError)) as caught:
                        if variant == 'config':
                            self.write_config(snapshot, auth, locations=locations)
                        else:
                            secure_replace_runtime(target, b'fake-new', max_bytes=1024,
                                                   locations=locations)
                self.assertEqual(set(attempts), identities)
                self.assertEqual(len(attempts), 2)
                self.assertTrue(caught.exception.close_unknown)
                self.assertEqual(caught.exception.state, 'POSTCHECK_FAILED')
                self.assertEqual(self.target.read_bytes(), b'fake-new')
                if variant == 'secure':
                    self.assertTrue(caught.exception.candidate.matches(self.secure_snapshot()))

    def test_config_parent_close_interruption_after_commit_is_not_retried(self):
        snapshot, auth = self.config_snapshot()
        locations = self.locations()
        sentinel = self.root / 'parent-close-sentinel'
        sentinel.write_bytes(b'fake-sentinel')
        actual_close, actual_open = os.close, os.open
        seen = {'fd': None, 'replacement': None, 'closes': 0}

        @contextlib.contextmanager
        def watched_parent(config):
            scope = open_target_parent(config)
            fd, name = scope.__enter__()
            seen['fd'] = fd

            def interrupted_close(closing_fd):
                if closing_fd == fd:
                    seen['closes'] += 1
                    actual_close(fd)
                    seen['replacement'] = actual_open(sentinel, os.O_RDONLY)
                    if seen['replacement'] != fd:
                        os.dup2(seen['replacement'], fd)
                        actual_close(seen['replacement'])
                        seen['replacement'] = fd
                    raise KeyboardInterrupt('fake parent close interruption')
                return actual_close(closing_fd)

            try:
                yield fd, name
            finally:
                with patch('os.close', side_effect=interrupted_close):
                    scope.__exit__(None, None, None)

        try:
            with patch('core.mac_config_writer.open_target_parent', watched_parent):
                with self.assertRaises(MacWriteError) as caught:
                    self.write_config(snapshot, auth, locations=locations)
            self.assertEqual(caught.exception.state, 'POSTCHECK_FAILED')
            self.assertTrue(caught.exception.close_unknown)
            self.assertEqual(seen['closes'], 1)
            self.assertEqual(seen['replacement'], seen['fd'])
            self.assertEqual(os.read(seen['replacement'], 20), b'fake-sentinel')
            self.assertEqual(self.target.read_bytes(), b'fake-new')
            self.assertTrue(self.alias.is_symlink())
        finally:
            if seen['replacement'] is not None:
                actual_close(seen['replacement'])

    def test_cleanup_and_directory_close_interruptions_preserve_both_unknowns(self):
        for variant in ('config', 'secure'):
            with self.subTest(variant=variant):
                snapshot, auth = self.config_snapshot()
                target = self.secure_snapshot()
                locations = self.locations(backup=variant == 'config')
                stage_info = self.stage.stat()
                stage_id = (stage_info.st_dev, stage_info.st_ino)
                actual_unlink, actual_close = os.unlink, os.close
                observed = {'cleanup': False, 'directory_closes': 0}
                secret = 'FAKE-COMMIT-SECRET-' + variant

                def interrupted_unlink(name, *args, **kwargs):
                    if isinstance(name, str) and name.startswith(
                            ('.model-proxy-config-', '.model-proxy-runtime-')):
                        observed['cleanup'] = True
                        raise GeneratorExit('fake cleanup interruption')
                    return actual_unlink(name, *args, **kwargs)

                def interrupted_close(fd):
                    info = os.fstat(fd)
                    if observed['cleanup'] and (info.st_dev, info.st_ino) == stage_id:
                        observed['directory_closes'] += 1
                        actual_close(fd)
                        raise KeyboardInterrupt('fake directory close interruption')
                    return actual_close(fd)

                with patch('os.replace', side_effect=LeakyError(secret)), \
                     patch('os.unlink', side_effect=interrupted_unlink), \
                     patch('os.close', side_effect=interrupted_close):
                    with self.assertRaises((MacWriteError, SecureWriteError)) as caught:
                        if variant == 'config':
                            self.write_config(snapshot, auth, locations=locations)
                        else:
                            secure_replace_runtime(target, b'fake-new', max_bytes=1024,
                                                   locations=locations, make_backup=False)
                error = caught.exception
                self.assertEqual(error.state, 'UNKNOWN')
                self.assertTrue(error.close_unknown and error.cleanup_unknown)
                self.assertEqual(observed['directory_closes'], 1)
                residual = getattr(error, 'staging_name', None) or getattr(error, 'staging', None)
                self.assertIsNotNone(residual)
                self.assertEqual(Path(residual).read_bytes(), b'fake-new')
                self.assertEqual(self.target.read_bytes(), b'fake-original')
                self.assert_scrubbed(error, secret)
                Path(residual).unlink()

    def test_untrusted_error_code_and_traceback_are_sanitized_after_replace(self):
        for variant in ('config-legacy', 'config-runtime', 'secure-legacy', 'secure-runtime'):
            with self.subTest(variant=variant):
                self.target.write_bytes(b'fake-original')
                snapshot, auth = self.config_snapshot(legacy=variant == 'config-legacy')
                target = self.secure_snapshot()
                locations = self.locations(backup=variant != 'secure-runtime')
                secret = 'FAKE-SECRET-' + variant
                actual_replace = os.replace

                def replaced_then_error(*args, **kwargs):
                    actual_replace(*args, **kwargs)
                    raise LeakyError(secret)

                with patch('os.replace', side_effect=replaced_then_error):
                    with self.assertRaises((MacWriteError, SecureWriteError)) as caught:
                        if variant.startswith('config'):
                            self.write_config(snapshot, auth,
                                              locations=None if variant == 'config-legacy' else locations)
                        elif variant == 'secure-legacy':
                            secure_replace(target, b'fake-new', max_bytes=1024)
                        else:
                            secure_replace_runtime(target, b'fake-new', max_bytes=1024,
                                                   locations=locations, make_backup=False)
                self.assertEqual(caught.exception.state, 'UNKNOWN')
                self.assertEqual(self.target.read_bytes(), b'fake-new')
                self.assert_scrubbed(caught.exception, secret)
                if variant == 'secure-runtime':
                    self.assertIsNotNone(caught.exception.candidate)

    def test_postcommit_failure_keeps_known_replacement_and_safe_error(self):
        for variant in ('config', 'secure'):
            with self.subTest(variant=variant):
                self.target.write_bytes(b'fake-original')
                snapshot, auth = self.config_snapshot()
                target = self.secure_snapshot()
                locations = self.locations(backup=variant == 'config')
                secret = 'FAKE-POSTCHECK-SECRET-' + variant
                if variant == 'config':
                    with patch('core.mac_config_writer.capture_config',
                               side_effect=MacWriteError(secret, state='UNKNOWN', marker_unknown=True)):
                        with self.assertRaises(MacWriteError) as caught:
                            self.write_config(snapshot, auth, locations=locations)
                    self.assertTrue(caught.exception.marker_unknown)
                else:
                    calls = 0

                    def guard():
                        nonlocal calls
                        calls += 1
                        if calls == 4:
                            raise SecureWriteError(secret, state='UNKNOWN', close_unknown=True)

                    with self.assertRaises(SecureWriteError) as caught:
                        secure_replace_runtime(target, b'fake-new', max_bytes=1024,
                                               locations=locations, make_backup=False,
                                               boundary_guard=guard)
                    self.assertTrue(caught.exception.close_unknown)
                    self.assertTrue(caught.exception.candidate.matches(self.secure_snapshot()))
                self.assertEqual(caught.exception.state, 'POSTCHECK_FAILED')
                self.assertEqual(self.target.read_bytes(), b'fake-new')
                self.assert_scrubbed(caught.exception, secret)

    def test_exception_code_accessor_failure_is_also_sanitized(self):
        for variant in ('config', 'secure'):
            with self.subTest(variant=variant):
                snapshot, auth = self.config_snapshot()
                target = self.secure_snapshot()
                locations = self.locations(backup=variant == 'config')
                secret = 'FAKE-ACCESSOR-SECRET-' + variant
                with patch('os.replace', side_effect=RaisingCodeError(secret)):
                    with self.assertRaises((MacWriteError, SecureWriteError)) as caught:
                        if variant == 'config':
                            self.write_config(snapshot, auth, locations=locations)
                        else:
                            secure_replace_runtime(target, b'fake-new', max_bytes=1024,
                                                   locations=locations, make_backup=False)
                self.assertEqual(caught.exception.state, 'UNKNOWN')
                self.assertEqual(self.target.read_bytes(), b'fake-original')
                self.assert_scrubbed(caught.exception, secret)

    def test_hostile_error_properties_do_not_prevent_closing_owned_fd(self):
        for variant in ('config-legacy', 'config-runtime', 'secure-legacy', 'secure-runtime'):
            with self.subTest(variant=variant):
                snapshot, auth = self.config_snapshot(legacy=variant == 'config-legacy')
                target = self.secure_snapshot()
                locations = self.locations(backup=variant != 'secure-runtime')
                secret = 'FAKE-HOSTILE-FLAGS-' + variant
                observed = {'fd': None, 'closes': 0, 'closed': False, 'read_before_close': False}
                actual_open, actual_close = os.open, os.close

                def watch_open(name, flags, *args, **kwargs):
                    fd = actual_open(name, flags, *args, **kwargs)
                    if isinstance(name, str) and flags & os.O_EXCL:
                        observed['fd'] = fd
                    return fd

                def watch_close(fd):
                    if fd == observed['fd']:
                        observed['closes'] += 1
                        observed['closed'] = True
                    return actual_close(fd)

                try:
                    with patch('os.open', side_effect=watch_open), \
                         patch('os.close', side_effect=watch_close), \
                         patch('os.write', side_effect=HostileFlagsError(secret, observed)):
                        with self.assertRaises((MacWriteError, SecureWriteError)) as caught:
                            if variant.startswith('config'):
                                self.write_config(snapshot, auth,
                                                  locations=None if variant == 'config-legacy' else locations)
                            elif variant == 'secure-legacy':
                                secure_replace(target, b'fake-new', max_bytes=1024)
                            else:
                                secure_replace_runtime(target, b'fake-new', max_bytes=1024,
                                                       locations=locations, make_backup=False)
                    self.assertEqual(observed['closes'], 1)
                    self.assertFalse(observed['read_before_close'])
                    self.assertEqual(caught.exception.state, 'NOT_REPLACED')
                    self.assertTrue(caught.exception.close_unknown)
                    self.assertTrue(caught.exception.cleanup_unknown)
                    self.assertEqual(self.target.read_bytes(), b'fake-original')
                    self.assert_scrubbed(caught.exception, secret)
                finally:
                    if observed['fd'] is not None and not observed['closed']:
                        actual_close(observed['fd'])

    def test_hostile_callback_metadata_does_not_escape_boundary(self):
        for variant in ('config-legacy', 'config-runtime', 'secure-legacy', 'secure-runtime'):
            with self.subTest(variant=variant):
                snapshot, auth = self.config_snapshot(legacy=variant == 'config-legacy')
                target = self.secure_snapshot()
                locations = self.locations(backup=variant != 'secure-runtime')
                secret = 'FAKE-CALLBACK-FLAGS-' + variant
                observed = {'closed': True, 'read_before_close': False}
                with patch('os.replace', side_effect=HostileFlagsError(secret, observed)):
                    with self.assertRaises((MacWriteError, SecureWriteError)) as caught:
                        if variant.startswith('config'):
                            self.write_config(snapshot, auth,
                                              locations=None if variant == 'config-legacy' else locations)
                        elif variant == 'secure-legacy':
                            secure_replace(target, b'fake-new', max_bytes=1024)
                        else:
                            secure_replace_runtime(target, b'fake-new', max_bytes=1024,
                                                   locations=locations, make_backup=False)
                self.assertEqual(caught.exception.state, 'UNKNOWN')
                self.assertTrue(caught.exception.close_unknown)
                self.assertTrue(caught.exception.cleanup_unknown)
                self.assertEqual(self.target.read_bytes(), b'fake-original')
                self.assert_scrubbed(caught.exception, secret)

    def test_attributeerror_and_hostile_bool_become_unknown_without_leak(self):
        for variant in ('config', 'secure'):
            with self.subTest(variant=variant):
                snapshot, auth = self.config_snapshot()
                target = self.secure_snapshot()
                locations = self.locations(backup=variant == 'config')
                with patch('os.replace', side_effect=ConfusingFlagsError('fake')):
                    with self.assertRaises((MacWriteError, SecureWriteError)) as caught:
                        if variant == 'config':
                            self.write_config(snapshot, auth, locations=locations)
                        else:
                            secure_replace_runtime(target, b'fake-new', max_bytes=1024,
                                                   locations=locations, make_backup=False)
                error = caught.exception
                self.assertEqual(error.state, 'UNKNOWN')
                self.assertTrue(error.close_unknown and error.cleanup_unknown)
                self.assert_scrubbed(error, 'FAKE-ATTRIBUTE-SECRET')
                self.assert_scrubbed(error, 'FAKE-TRUTH-SECRET')
                self.assertEqual(self.target.read_bytes(), b'fake-original')

    def test_unreadable_exception_state_is_sanitized_and_unknown(self):
        snapshot, auth = self.config_snapshot()
        target = self.secure_snapshot()
        locations = self.locations()
        secret = 'FAKE-STATE-SECRET'

        def config_guard():
            raise HostileMacStateError('filesystem_or_backend_error')

        with self.assertRaises(MacWriteError) as caught:
            replace_config(snapshot, b'fake-new', auth, preflight=lambda _: True,
                           before_replace=config_guard, locations=locations)
        self.assertEqual(caught.exception.state, 'UNKNOWN')
        self.assert_scrubbed(caught.exception, secret)
        self.assertEqual(self.target.read_bytes(), b'fake-original')

        def secure_guard():
            raise HostileSecureStateError('filesystem_or_backend_error')

        with self.assertRaises(SecureWriteError) as caught:
            secure_replace_runtime(target, b'fake-new', max_bytes=1024,
                                   locations=self.locations(backup=False), make_backup=False,
                                   boundary_guard=secure_guard)
        self.assertEqual(caught.exception.state, 'UNKNOWN')
        self.assert_scrubbed(caught.exception, secret)
        self.assertEqual(self.target.read_bytes(), b'fake-original')

    def test_baseexception_from_error_flag_is_sanitized(self):
        for variant in ('config', 'secure'):
            with self.subTest(variant=variant):
                snapshot, auth = self.config_snapshot()
                target = self.secure_snapshot()
                locations = self.locations(backup=variant == 'config')
                with patch('os.replace', side_effect=InterruptedFlagsError('fake')):
                    with self.assertRaises((MacWriteError, SecureWriteError)) as caught:
                        if variant == 'config':
                            self.write_config(snapshot, auth, locations=locations)
                        else:
                            secure_replace_runtime(target, b'fake-new', max_bytes=1024,
                                                   locations=locations, make_backup=False)
                self.assertEqual(caught.exception.state, 'UNKNOWN')
                self.assertTrue(caught.exception.close_unknown and caught.exception.cleanup_unknown)
                self.assert_scrubbed(caught.exception, 'FAKE-FLAG-INTERRUPT')
                self.assert_scrubbed(caught.exception, 'FAKE-FLAG-EXIT')
                self.assertEqual(self.target.read_bytes(), b'fake-original')

    def test_hostile_exception_class_attribute_is_sanitized(self):
        for variant in ('config', 'secure'):
            with self.subTest(variant=variant):
                snapshot, auth = self.config_snapshot()
                target = self.secure_snapshot()
                locations = self.locations(backup=variant == 'config')

                def guard():
                    raise HostileClassError('fake error')

                with self.assertRaises((MacWriteError, SecureWriteError)) as caught:
                    if variant == 'config':
                        replace_config(snapshot, b'fake-new', auth, preflight=lambda _: True,
                                       before_replace=guard, locations=locations)
                    else:
                        secure_replace_runtime(target, b'fake-new', max_bytes=1024,
                                               locations=locations, make_backup=False,
                                               boundary_guard=guard)
                self.assertTrue(caught.exception.close_unknown)
                self.assertTrue(caught.exception.cleanup_unknown)
                self.assert_scrubbed(caught.exception, 'FAKE-CLASS-SECRET')
                self.assertEqual(self.target.read_bytes(), b'fake-original')

    def test_exclusive_open_oserror_after_creation_reports_unknown_resource(self):
        for variant in ('config-legacy', 'config-runtime', 'secure-legacy', 'secure-runtime'):
            for fake_errno in (errno.EIO, errno.EEXIST):
                with self.subTest(variant=variant, fake_errno=fake_errno):
                    snapshot, auth = self.config_snapshot(legacy=variant == 'config-legacy')
                    target = self.secure_snapshot()
                    locations = self.locations(backup=variant != 'secure-runtime')
                    secret = 'FAKE-OPEN-SECRET-' + variant
                    actual_open, actual_close = os.open, os.close
                    opened = []

                    def create_then_raise(name, flags, *args, **kwargs):
                        fd = actual_open(name, flags, *args, **kwargs)
                        if isinstance(name, str) and flags & os.O_EXCL:
                            opened.append(fd)
                            raise OSError(fake_errno, secret)
                        return fd

                    try:
                        with patch('os.open', side_effect=create_then_raise):
                            with self.assertRaises((MacWriteError, SecureWriteError)) as caught:
                                if variant.startswith('config'):
                                    self.write_config(snapshot, auth,
                                                      locations=None if variant == 'config-legacy' else locations)
                                elif variant == 'secure-legacy':
                                    secure_replace(target, b'fake-new', max_bytes=1024)
                                else:
                                    secure_replace_runtime(target, b'fake-new', max_bytes=1024,
                                                           locations=locations, make_backup=False)
                        error = caught.exception
                        self.assertEqual(error.state, 'NOT_REPLACED')
                        self.assertTrue(error.close_unknown)
                        self.assertTrue(error.cleanup_unknown)
                        residual = (getattr(error, 'backup_name', None) or
                                    getattr(error, 'backup', None) or getattr(error, 'staging', None))
                        self.assertIsNotNone(residual)
                        residual = Path(residual)
                        if not residual.is_absolute():
                            residual = self.config / residual
                        self.assertTrue(residual.exists())
                        self.assertEqual(len(opened), 1)
                        self.assertEqual(os.fstat(opened[0]).st_ino, residual.stat().st_ino)
                        self.assertEqual(self.target.read_bytes(), b'fake-original')
                        self.assert_scrubbed(error, secret)
                    finally:
                        for fd in opened:
                            actual_close(fd)
                        for parent in (self.config, self.stage, self.backup):
                            for path in parent.iterdir():
                                if path.name.endswith('.bak.' + 'fixed-fake-name') or (
                                        '.bak.' in path.name or path.name.startswith('.model-proxy-')):
                                    path.unlink()

    def test_existing_backup_collision_remains_definite_and_untouched(self):
        for variant in ('config-legacy', 'config-runtime', 'secure-legacy', 'secure-runtime'):
            with self.subTest(variant=variant):
                snapshot, auth = self.config_snapshot(legacy=variant == 'config-legacy')
                target = self.secure_snapshot()
                locations = self.locations()
                parent = self.config if variant.endswith('legacy') else self.backup
                existing = parent / (self.target.name + '.bak.fixed-fake-name')
                existing.write_bytes(b'fake foreign backup')
                existing.chmod(0o600)
                try:
                    with patch('secrets.token_hex', return_value='fixed-fake-name'):
                        with self.assertRaises((MacWriteError, SecureWriteError)) as caught:
                            if variant.startswith('config'):
                                self.write_config(snapshot, auth,
                                                  locations=None if variant == 'config-legacy' else locations)
                            elif variant == 'secure-legacy':
                                secure_replace(target, b'fake-new', max_bytes=1024)
                            else:
                                secure_replace_runtime(target, b'fake-new', max_bytes=1024,
                                                       locations=locations)
                    error = caught.exception
                    self.assertEqual(error.state, 'NOT_REPLACED')
                    self.assertFalse(error.close_unknown)
                    self.assertFalse(error.cleanup_unknown)
                    self.assertIsNone(getattr(error, 'backup_name', None) or getattr(error, 'backup', None))
                    self.assertEqual(existing.read_bytes(), b'fake foreign backup')
                    self.assertEqual(self.target.read_bytes(), b'fake-original')
                finally:
                    existing.unlink()

    def test_staging_open_oserror_reports_residue_and_keeps_verified_backup(self):
        for variant in ('config-legacy', 'config-runtime', 'secure-legacy', 'secure-runtime'):
            for fake_errno in (errno.EIO, errno.EEXIST):
                with self.subTest(variant=variant, fake_errno=fake_errno):
                    snapshot, auth = self.config_snapshot(legacy=variant == 'config-legacy')
                    target = self.secure_snapshot()
                    locations = self.locations()
                    actual_open, actual_close = os.open, os.close
                    opened = []

                    def create_then_raise(name, flags, *args, **kwargs):
                        fd = actual_open(name, flags, *args, **kwargs)
                        if (isinstance(name, str) and flags & os.O_EXCL and name.startswith(
                                ('.model-proxy-config-', '.model-proxy-install-', '.model-proxy-runtime-'))):
                            opened.append(fd)
                            raise OSError(fake_errno, 'FAKE-STAGING-OPEN-SECRET')
                        return fd

                    try:
                        with patch('os.open', side_effect=create_then_raise):
                            with self.assertRaises((MacWriteError, SecureWriteError)) as caught:
                                if variant.startswith('config'):
                                    self.write_config(snapshot, auth,
                                                      locations=None if variant == 'config-legacy' else locations)
                                elif variant == 'secure-legacy':
                                    secure_replace(target, b'fake-new', max_bytes=1024)
                                else:
                                    secure_replace_runtime(target, b'fake-new', max_bytes=1024,
                                                           locations=locations)
                        error = caught.exception
                        self.assertEqual(error.state, 'NOT_REPLACED')
                        self.assertTrue(error.close_unknown and error.cleanup_unknown)
                        staging = getattr(error, 'staging_name', None) or getattr(error, 'staging', None)
                        backup = getattr(error, 'backup_name', None) or getattr(error, 'backup', None)
                        self.assertIsNotNone(staging)
                        self.assertIsNotNone(backup)
                        staging, backup = Path(staging), Path(backup)
                        if not staging.is_absolute():
                            staging = self.config / staging
                        if not backup.is_absolute():
                            backup = self.config / backup
                        self.assertEqual(len(opened), 1)
                        self.assertEqual(os.fstat(opened[0]).st_ino, staging.stat().st_ino)
                        self.assertEqual(backup.read_bytes(), b'fake-original')
                        self.assertEqual(self.target.read_bytes(), b'fake-original')
                        self.assert_scrubbed(error, 'FAKE-STAGING-OPEN-SECRET')
                    finally:
                        for fd in opened:
                            actual_close(fd)
                        for parent in (self.config, self.stage, self.backup):
                            for path in parent.iterdir():
                                if '.bak.' in path.name or path.name.startswith('.model-proxy-'):
                                    path.unlink()

    def test_existing_staging_collision_is_definite_and_untouched(self):
        prefixes = {'config-legacy': '.model-proxy-config-',
                    'config-runtime': '.model-proxy-config-',
                    'secure-legacy': '.model-proxy-install-',
                    'secure-runtime': '.model-proxy-runtime-'}
        for variant, prefix in prefixes.items():
            with self.subTest(variant=variant):
                snapshot, auth = self.config_snapshot(legacy=variant == 'config-legacy')
                target = self.secure_snapshot()
                locations = self.locations()
                parent = self.config if variant.endswith('legacy') else self.stage
                existing = parent / (prefix + 'fixed-fake-name')
                existing.write_bytes(b'fake foreign stage')
                existing.chmod(0o600)
                try:
                    with patch('secrets.token_hex', return_value='fixed-fake-name'):
                        with self.assertRaises((MacWriteError, SecureWriteError)) as caught:
                            if variant.startswith('config'):
                                self.write_config(snapshot, auth,
                                                  locations=None if variant == 'config-legacy' else locations)
                            elif variant == 'secure-legacy':
                                secure_replace(target, b'fake-new', max_bytes=1024)
                            else:
                                secure_replace_runtime(target, b'fake-new', max_bytes=1024,
                                                       locations=locations)
                    error = caught.exception
                    self.assertEqual(error.state, 'NOT_REPLACED')
                    self.assertFalse(error.close_unknown or error.cleanup_unknown)
                    self.assertIsNone(getattr(error, 'staging_name', None) or getattr(error, 'staging', None))
                    backup = getattr(error, 'backup_name', None) or getattr(error, 'backup', None)
                    self.assertIsNotNone(backup)
                    backup = Path(backup)
                    if not backup.is_absolute():
                        backup = self.config / backup
                    self.assertEqual(backup.read_bytes(), b'fake-original')
                    self.assertEqual(existing.read_bytes(), b'fake foreign stage')
                    self.assertEqual(self.target.read_bytes(), b'fake-original')
                finally:
                    existing.unlink()
                    for directory in (self.config, self.backup):
                        for path in directory.iterdir():
                            if path.name.endswith('.bak.fixed-fake-name'):
                                path.unlink()

    def test_open_error_without_entry_keeps_absence_but_fd_is_unknown(self):
        for variant in ('config-legacy', 'config-runtime', 'secure-legacy', 'secure-runtime'):
            with self.subTest(variant=variant):
                snapshot, auth = self.config_snapshot(legacy=variant == 'config-legacy')
                target = self.secure_snapshot()
                locations = self.locations(backup=variant != 'secure-runtime')
                actual_open = os.open

                def reject_exclusive(name, flags, *args, **kwargs):
                    if isinstance(name, str) and flags & os.O_EXCL:
                        raise OSError(errno.EACCES, 'fake denied')
                    return actual_open(name, flags, *args, **kwargs)

                with patch('os.open', side_effect=reject_exclusive):
                    with self.assertRaises((MacWriteError, SecureWriteError)) as caught:
                        if variant.startswith('config'):
                            self.write_config(snapshot, auth,
                                              locations=None if variant == 'config-legacy' else locations)
                        elif variant == 'secure-legacy':
                            secure_replace(target, b'fake-new', max_bytes=1024)
                        else:
                            secure_replace_runtime(target, b'fake-new', max_bytes=1024,
                                                   locations=locations, make_backup=False)
                error = caught.exception
                self.assertEqual(error.state, 'NOT_REPLACED')
                self.assertTrue(error.close_unknown)
                self.assertFalse(error.cleanup_unknown)
                self.assertIsNone(getattr(error, 'backup_name', None) or getattr(error, 'backup', None))
                self.assertIsNone(getattr(error, 'staging_name', None) or getattr(error, 'staging', None))
                self.assertEqual(self.target.read_bytes(), b'fake-original')

    def test_unlinked_open_fd_cannot_be_inferred_from_name_absence(self):
        for variant in ('config', 'secure'):
            with self.subTest(variant=variant):
                snapshot, auth = self.config_snapshot()
                target = self.secure_snapshot()
                locations = self.locations(backup=variant == 'config')
                actual_open, actual_close, actual_unlink = os.open, os.close, os.unlink
                opened = []

                def create_unlink_then_raise(name, flags, *args, **kwargs):
                    fd = actual_open(name, flags, *args, **kwargs)
                    if isinstance(name, str) and flags & os.O_EXCL:
                        opened.append(fd)
                        actual_unlink(name, dir_fd=kwargs['dir_fd'])
                        raise OSError(errno.EIO, 'fake error')
                    return fd

                try:
                    with patch('os.open', side_effect=create_unlink_then_raise):
                        with self.assertRaises((MacWriteError, SecureWriteError)) as caught:
                            if variant == 'config':
                                self.write_config(snapshot, auth, locations=locations)
                            else:
                                secure_replace_runtime(target, b'fake-new', max_bytes=1024,
                                                       locations=locations, make_backup=False)
                    error = caught.exception
                    self.assertEqual(error.state, 'NOT_REPLACED')
                    self.assertTrue(error.close_unknown)
                    self.assertFalse(error.cleanup_unknown)
                    self.assertIsNone(getattr(error, 'backup_name', None) or getattr(error, 'backup', None))
                    self.assertIsNone(getattr(error, 'staging_name', None) or getattr(error, 'staging', None))
                    self.assertEqual(len(opened), 1)
                    self.assertEqual(os.fstat(opened[0]).st_nlink, 0)
                    self.assertEqual(self.target.read_bytes(), b'fake-original')
                finally:
                    for fd in opened:
                        actual_close(fd)

    def test_marker_unknown_survives_cleanup_and_close_interruptions(self):
        snapshot, auth = self.config_snapshot()
        locations = self.locations()
        info = self.stage.stat()
        stage_id = (info.st_dev, info.st_ino)
        actual_unlink, actual_close = os.unlink, os.close
        observed = {'cleanup': False}
        secret = 'FAKE-MARKER-SECRET'

        def before_replace():
            raise MacWriteError(secret, marker_unknown=True)

        def interrupted_unlink(name, *args, **kwargs):
            if isinstance(name, str) and name.startswith('.model-proxy-config-'):
                observed['cleanup'] = True
                raise KeyboardInterrupt('fake cleanup interruption')
            return actual_unlink(name, *args, **kwargs)

        def interrupted_close(fd):
            info = os.fstat(fd)
            if observed['cleanup'] and (info.st_dev, info.st_ino) == stage_id:
                actual_close(fd)
                raise KeyboardInterrupt('fake close interruption')
            return actual_close(fd)

        with patch('os.unlink', side_effect=interrupted_unlink), \
             patch('os.close', side_effect=interrupted_close):
            with self.assertRaises(MacWriteError) as caught:
                replace_config(snapshot, b'fake-new', auth, preflight=lambda _: True,
                               before_replace=before_replace, locations=locations)
        error = caught.exception
        self.assertEqual(error.state, 'NOT_REPLACED')
        self.assertTrue(error.marker_unknown and error.close_unknown and error.cleanup_unknown)
        self.assertEqual(self.target.read_bytes(), b'fake-original')
        self.assert_scrubbed(error, secret)
        self.assertEqual(Path(error.staging_name).read_bytes(), b'fake-new')
        Path(error.staging_name).unlink()

