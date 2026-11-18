"""Isolated fake and native-Mac sidecar transaction interruption tests.

Fixture guards and FakeSecureIO are deliberately NOT production authorization.
No real config, sidecar, credentials, service, or network is used.
"""
from __future__ import annotations

import dis
import inspect
import json
import os
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

from core.commands import (CommandContext, SessionOverridesSidecar, SidecarSnapshot,
                           SidecarWriteError, handle_route_command)


class FakeSecureIO:
    """Small state-machine fixture, not a file-security implementation."""
    def capture(self, path):
        parent = path.parent.stat()
        try:
            info = path.stat()
            return SidecarSnapshot(path.read_bytes(),
                                   (parent.st_dev, parent.st_ino, info.st_dev, info.st_ino,
                                    info.st_mode, info.st_mtime_ns), info.st_mtime)
        except FileNotFoundError:
            return SidecarSnapshot(None, (parent.st_dev, parent.st_ino, 'absent'))

    def commit(self, path, baseline, content):
        if not SessionOverridesSidecar._same_snapshot(self.capture(path), baseline):
            raise SidecarWriteError('NOT_REPLACED')
        tmp = path.parent / 'fixture-sidecar-stage'
        tmp.write_bytes(content)
        os.replace(tmp, path)
        return self.capture(path)


def _line_for(text, *, occurrence=0):
    lines, start = inspect.getsourcelines(SessionOverridesSidecar.apply_command)
    matches = [start + i for i, line in enumerate(lines) if line.strip() == text]
    if len(matches) <= occurrence:
        raise AssertionError('transaction trace boundary not found')
    return matches[occurrence]


def _inject_on_line(store, line, *, action='set', interrupt=KeyboardInterrupt):
    """One deterministic exception in this thread; restore the trace afterward."""
    previous = sys.gettrace()
    fired = []
    def trace(frame, event, arg):
        if (frame.f_code is SessionOverridesSidecar.apply_command.__code__
                and event == 'line' and frame.f_lineno == line and not fired):
            fired.append(True)
            raise interrupt('fixture-secret-in-interruption')
        return trace
    try:
        sys.settrace(trace)
        store.apply_command('fake-client', 'fake-session', action, 'C' if action == 'set' else None)
    finally:
        sys.settrace(previous)
        if not fired:
            raise AssertionError('transaction trace boundary was not reached')


def _interrupt_after_commit_return(store):
    """Interrupt before STORE_FAST receives the already-returned snapshot."""
    instructions = list(dis.get_instructions(SessionOverridesSidecar.apply_command))
    commit = next(i for i, item in enumerate(instructions)
                  if item.opname in ('LOAD_ATTR', 'LOAD_METHOD') and item.argval == 'commit')
    offset = next(item.offset for item in instructions[commit + 1:]
                  if item.opname == 'STORE_FAST' and item.argval == 'committed')
    previous, fired = sys.gettrace(), []
    def trace(frame, event, arg):
        if frame.f_code is SessionOverridesSidecar.apply_command.__code__:
            frame.f_trace_opcodes = True
            if event == 'opcode' and frame.f_lasti == offset and not fired:
                fired.append(True)
                raise KeyboardInterrupt('fixture-secret-after-return')
        return trace
    try:
        sys.settrace(trace)
        store.apply_command('fake-client', 'fake-session', 'set', 'C')
    finally:
        sys.settrace(previous)
        if not fired:
            raise AssertionError('commit return opcode was not reached')


def _interrupt_during_recovery(store, text):
    lines, start = inspect.getsourcelines(SessionOverridesSidecar._recover_locked)
    matches = [start + i for i, line in enumerate(lines) if line.strip() == text]
    if len(matches) != 1:
        raise AssertionError('recovery trace boundary not unique')
    previous, fired = sys.gettrace(), []
    def trace(frame, event, arg):
        if (frame.f_code is SessionOverridesSidecar._recover_locked.__code__
                and event == 'line' and frame.f_lineno == matches[0] and not fired):
            fired.append(True)
            raise KeyboardInterrupt('fixture-secret-during-recovery')
        return trace
    try:
        sys.settrace(trace)
        store.recover()
    finally:
        sys.settrace(previous)
        if not fired:
            raise AssertionError('recovery trace boundary was not reached')


class FakeCommandInterruptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sidecar-command-fake-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'session_overrides.json'
        self.io = FakeSecureIO()
        self.store = SessionOverridesSidecar(self.path, secure_io=self.io)
        self.store.apply_command('fake-client', 'fake-session', 'set', 'B')
        self.store.touch('fake-client', 'fake-session')

    def _assert_quarantined_b(self, original_data, original_mem, original_baseline, error):
        self.assertEqual(error.state, 'UNKNOWN')
        self.assertNotIn('fixture-secret', ''.join(traceback.format_exception(error)))
        self.assertEqual(self.store._data, original_data)
        self.assertEqual(self.store._mem_last_seen, original_mem)
        self.assertEqual(self.store._baseline, original_baseline)
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertIsNotNone(self.store._unknown)
        self.store.maybe_reload()
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')

    def test_interrupts_across_return_verification_and_partial_publication(self):
        # Each scenario starts from its own B; all pauses happen after commit.
        targets = (
            ('commit_returned = True', 1),
            ('if (self._decode_snapshot(committed) != data', 0),
            ('self._data = data', 0),
            ('self._mem_last_seen = candidate_mem', 0),
            ('self._baseline = committed', 0),
            ('self._mtime = committed.mtime', 0),
            ('self._unknown = None', 0),
            ('log.info("sidecar.write action=%s", action)', 0),
            ('return {"cleaned": cleaned}', 0),
        )
        for index, (text, occurrence) in enumerate(targets):
            with self.subTest(boundary=text):
                if index:
                    self.store = SessionOverridesSidecar(self.path, secure_io=self.io)
                    self.store.apply_command('fake-client', 'fake-session', 'set', 'B')
                    self.store.touch('fake-client', 'fake-session')
                old_data = json.loads(json.dumps(self.store._data))
                old_mem = self.store._mem_last_seen.copy()
                baseline = self.store._baseline
                with self.assertRaises(SidecarWriteError) as cm:
                    _inject_on_line(self.store, _line_for(text, occurrence=occurrence))
                self._assert_quarantined_b(old_data, old_mem, baseline, cm.exception)
                self.assertEqual(json.loads(self.path.read_bytes())['fake-client']['fake-session']['route_id'], 'C')
                self.assertIsNotNone(self.store._unknown[1])
                self.assertTrue(self.store.recover())
                self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'C')

    def test_non_keyboard_baseexception_after_return_is_sanitized(self):
        old_data, old_mem, baseline = self.store._data, self.store._mem_last_seen.copy(), self.store._baseline
        with self.assertRaises(SidecarWriteError) as cm:
            _inject_on_line(self.store, _line_for('self._baseline = committed'), interrupt=SystemExit)
        self._assert_quarantined_b(old_data, old_mem, baseline, cm.exception)
        self.assertTrue(self.store.recover())
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'C')

    @unittest.expectedFailure
    def test_final_return_opcode_interrupt_should_be_unknown(self):
        # CPython unwinds the lock/try before RETURN_VALUE. A trace-injected
        # interruption there escapes this frame after C has been fully published.
        # The stricter UNKNOWN requirement needs a caller-side handoff boundary.
        code = SessionOverridesSidecar.apply_command.__code__
        offset = next(item.offset for item in dis.get_instructions(code)
                      if item.opname == 'RETURN_VALUE')
        previous, fired, error = sys.gettrace(), [], None
        def trace(frame, event, arg):
            if frame.f_code is code:
                frame.f_trace_opcodes = True
                if event == 'opcode' and frame.f_lasti == offset and not fired:
                    fired.append(True)
                    raise KeyboardInterrupt('fixture-secret-at-final-return')
            return trace
        try:
            sys.settrace(trace)
            try:
                self.store.apply_command('fake-client', 'fake-session', 'set', 'C')
            except BaseException as exc:
                error = exc
        finally:
            sys.settrace(previous)
        self.assertTrue(fired)
        self.assertIsInstance(error, SidecarWriteError)
        self.assertEqual(error.state, 'UNKNOWN')
        self.assertIsNotNone(self.store._unknown)

    def test_interrupt_after_adapter_return_before_assignment_is_quarantined(self):
        old_data, old_mem, baseline = self.store._data, self.store._mem_last_seen.copy(), self.store._baseline
        with self.assertRaises(SidecarWriteError) as cm:
            _interrupt_after_commit_return(self.store)
        self._assert_quarantined_b(old_data, old_mem, baseline, cm.exception)
        self.assertEqual(json.loads(self.path.read_bytes())['fake-client']['fake-session']['route_id'], 'C')
        self.assertIsNone(self.store._unknown[1])  # return value was never assigned
        self.assertFalse(self.store.recover())

    def test_quarantine_is_armed_before_calling_writer(self):
        original = self.io.commit
        def check_armed(*args):
            self.assertIsNotNone(self.store._unknown)
            self.assertEqual(self.store._data['fake-client']['fake-session']['route_id'], 'B')
            return original(*args)
        with patch.object(self.io, 'commit', side_effect=check_armed):
            self.store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertIsNone(self.store._unknown)
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'C')

    def test_route_handler_returns_unknown_not_success_after_decode_interrupt(self):
        decode = self.store._decode_snapshot
        def interrupted(snapshot):
            if snapshot.contents and b'"route_id": "C"' in snapshot.contents:
                raise KeyboardInterrupt('fixture-secret')
            return decode(snapshot)
        ctx = CommandContext('C', 'fake-client', 'fake-session',
                             {'route_pool': [{'route_id': 'B', 'weight': 1}]},
                             {'B': {'id': 'B'}, 'C': {'id': 'C'}}, self.store, 'B')
        with patch.object(self.store, '_decode_snapshot', side_effect=interrupted):
            result = handle_route_command(ctx)
        self.assertFalse(result.wrote)
        self.assertIn('未知', result.receipt_text)
        self.assertNotIn('fixture-secret', result.receipt_text)
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertTrue(self.store.recover())

    def test_legacy_interrupt_after_commit_remains_quarantined(self):
        from core import commands
        legacy = SessionOverridesSidecar(self.path)
        original = commands._atomic_write_json
        def committed_then_interrupted(*args):
            original(*args)
            raise KeyboardInterrupt('fixture-secret')
        with patch.object(commands, '_atomic_write_json', side_effect=committed_then_interrupted):
            with self.assertRaises(SidecarWriteError) as cm:
                legacy.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertTrue(cm.exception.legacy_unverifiable)
        legacy.maybe_reload()
        self.assertEqual(legacy.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertFalse(legacy.recover())

    def test_legacy_close_and_cleanup_flags_are_not_lost(self):
        from core import commands
        for mode in ('close', 'cleanup'):
            with self.subTest(mode=mode):
                legacy = SessionOverridesSidecar(self.path)
                real_close, real_unlink = os.close, os.unlink
                def close_then_fail(fd):
                    real_close(fd)
                    raise OSError('fixture-secret-from-close')
                def unlink_then_fail(path):
                    real_unlink(path)
                    raise OSError('fixture-secret-from-cleanup')
                other = (patch.object(commands.os, 'close', side_effect=close_then_fail)
                         if mode == 'close' else
                         patch.object(commands.os, 'unlink', side_effect=unlink_then_fail))
                with patch.object(commands.os, 'fdopen', side_effect=OSError('fixture-secret-open')), other:
                    with self.assertRaises(SidecarWriteError) as cm:
                        legacy.apply_command('fake-client', 'fake-session', 'set', 'C')
                self.assertEqual(cm.exception.state, 'UNKNOWN')
                self.assertTrue(cm.exception.legacy_unverifiable)
                self.assertEqual(cm.exception.close_unknown, mode == 'close')
                self.assertEqual(cm.exception.cleanup_unknown, mode == 'cleanup')
                self.assertEqual(cm.exception.release_unknown, mode == 'close')
                self.assertEqual(legacy.get_overrides_for('fake-client')['fake-session'], 'B')
                self.assertNotIn('fixture-secret', ''.join(traceback.format_exception(cm.exception)))

    def test_legacy_interruption_inside_replace_is_unknown(self):
        from core import commands
        legacy = SessionOverridesSidecar(self.path)
        replace = os.replace
        def replace_then_interrupt(*args):
            replace(*args)
            raise KeyboardInterrupt('fixture-secret')
        with patch.object(commands.os, 'replace', side_effect=replace_then_interrupt):
            with self.assertRaises(SidecarWriteError) as cm:
                legacy.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertTrue(cm.exception.legacy_unverifiable)
        self.assertIsNotNone(legacy._unknown)
        self.assertEqual(legacy.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertEqual(json.loads(self.path.read_bytes())['fake-client']['fake-session']['route_id'], 'C')
        self.assertNotIn('fixture-secret', ''.join(traceback.format_exception(cm.exception)))

    def test_legacy_interruption_before_replace_closes_and_cleans(self):
        from core import commands
        legacy = SessionOverridesSidecar(self.path)
        with patch.object(commands.json, 'dump', side_effect=KeyboardInterrupt('fixture-secret')):
            with self.assertRaises(SidecarWriteError) as cm:
                legacy.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'NOT_REPLACED')
        self.assertFalse(cm.exception.release_unknown)
        self.assertEqual(legacy.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.assertNotIn('fixture-secret', ''.join(traceback.format_exception(cm.exception)))

    def test_interrupt_before_adapter_returns_never_invents_candidate(self):
        commit = self.io.commit
        def after_real_fake_write(*args):
            commit(*args)
            raise KeyboardInterrupt('fixture-secret-from-adapter')
        old_data, old_mem, baseline = self.store._data, self.store._mem_last_seen.copy(), self.store._baseline
        with patch.object(self.io, 'commit', side_effect=after_real_fake_write):
            with self.assertRaises(SidecarWriteError) as cm:
                self.store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self._assert_quarantined_b(old_data, old_mem, baseline, cm.exception)
        self.assertIsNone(self.store._unknown[1])
        self.assertFalse(self.store.recover())

    def test_proved_b_can_recover_after_interruption_before_write(self):
        with patch.object(self.io, 'commit', side_effect=KeyboardInterrupt('fixture-secret')):
            with self.assertRaises(SidecarWriteError) as cm:
                self.store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertTrue(self.store.recover())
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')

    def test_third_identity_cannot_recover_after_interrupt(self):
        with self.assertRaises(SidecarWriteError):
            _inject_on_line(self.store, _line_for('self._mtime = committed.mtime'))
        third = self.path.parent / 'fixture-third'
        third.write_bytes(self.path.read_bytes())
        os.replace(third, self.path)
        self.assertFalse(self.store.recover())
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')
        with self.assertRaises(SidecarWriteError):
            self.store.apply_command('fake-client', 'fake-session', 'set', 'B')

    def test_reset_interruption_restores_b_then_recovery_removes_touch(self):
        old = self.store._mem_last_seen.copy()
        with self.assertRaises(SidecarWriteError):
            _inject_on_line(self.store, _line_for('self._baseline = committed'), action='reset')
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertEqual(self.store._mem_last_seen, old)
        self.store.touch('fake-client', 'fake-session')
        self.assertTrue(self.store.recover())
        self.assertEqual(self.store.get_overrides_for('fake-client'), {})
        self.assertNotIn(('fake-client', 'fake-session'), self.store._mem_last_seen)

    def test_release_close_cleanup_flags_survive_sanitized_rethrow(self):
        commit = self.io.commit
        def after_write(*args):
            candidate = commit(*args)
            try:
                raise RuntimeError('fixture-secret-from-platform')
            except RuntimeError as exc:
                raise SidecarWriteError('UNKNOWN', candidate, release_unknown=True,
                                        close_unknown=True, cleanup_unknown=True) from exc
        with patch.object(self.io, 'commit', side_effect=after_write):
            with self.assertRaises(SidecarWriteError) as cm:
                self.store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertTrue(cm.exception.release_unknown)
        self.assertTrue(cm.exception.close_unknown)
        self.assertTrue(cm.exception.cleanup_unknown)
        self.assertNotIn('fixture-secret', ''.join(traceback.format_exception(cm.exception)))
        self.assertFalse(self.store.recover())
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')
        with self.assertRaises(SidecarWriteError) as blocked:
            self.store.apply_command('fake-client', 'fake-session', 'set', 'B')
        self.assertTrue(blocked.exception.release_unknown)
        self.assertTrue(blocked.exception.close_unknown)
        self.assertTrue(blocked.exception.cleanup_unknown)

    def test_prewrite_capture_resource_failure_sticks_for_each_flag(self):
        for name in ('release_unknown', 'close_unknown', 'cleanup_unknown'):
            with self.subTest(flag=name):
                store = SessionOverridesSidecar(self.path, secure_io=self.io)
                baseline = store._baseline
                failure = RuntimeError('fixture-secret-capture')
                setattr(failure, name, True)
                with patch.object(self.io, 'capture', side_effect=failure), \
                     patch.object(self.io, 'commit', wraps=self.io.commit) as writer:
                    with self.assertRaises(SidecarWriteError) as cm:
                        store.apply_command('fake-client', 'fake-session', 'set', 'C')
                self.assertEqual(cm.exception.state, 'UNKNOWN')
                self.assertTrue(getattr(cm.exception, name))
                writer.assert_not_called()
                self.assertTrue(getattr(store._resource_unknown, name))
                self.assertEqual(store._baseline, baseline)
                self.assertEqual(store.get_overrides_for('fake-client')['fake-session'], 'B')
                self.assertNotIn('fixture-secret', ''.join(traceback.format_exception(cm.exception)))
                # A later successful fake capture could show B, but the store
                # must not treat that as proof of FD/handle/artifact cleanup.
                self.assertTrue(SessionOverridesSidecar._same_snapshot(self.io.capture(self.path), baseline))
                with patch.object(self.io, 'capture', wraps=self.io.capture) as capture, \
                     patch.object(self.io, 'commit', wraps=self.io.commit) as retry_writer:
                    store.maybe_reload()
                    self.assertFalse(store.recover())
                    with self.assertRaises(SidecarWriteError) as again:
                        store.apply_command('fake-client', 'fake-session', 'set', 'C')
                capture.assert_not_called()
                retry_writer.assert_not_called()
                self.assertEqual(again.exception.state, 'UNKNOWN')
                self.assertTrue(getattr(again.exception, name))
                self.assertEqual(store.get_overrides_for('fake-client')['fake-session'], 'B')

    def test_initial_capture_resource_failure_keeps_empty_state_and_blocks(self):
        failure = RuntimeError('fixture-secret-start')
        failure.release_unknown = True
        with patch.object(self.io, 'capture', side_effect=failure):
            store = SessionOverridesSidecar(self.path, secure_io=self.io)
        self.assertEqual(store.get_overrides_for('fake-client'), {})
        self.assertIsNone(store._baseline)
        with patch.object(self.io, 'capture', wraps=self.io.capture) as capture, \
             patch.object(self.io, 'commit', wraps=self.io.commit) as writer:
            store.maybe_reload()
            self.assertFalse(store.recover())
            with self.assertRaises(SidecarWriteError) as cm:
                store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertTrue(cm.exception.release_unknown)
        self.assertEqual(store.get_overrides_for('fake-client'), {})
        capture.assert_not_called()
        writer.assert_not_called()
        self.assertEqual(json.loads(self.path.read_bytes())['fake-client']['fake-session']['route_id'], 'B')

    def test_hot_reload_capture_resource_failure_latches_last_verified_b(self):
        external = {'fake-client': {'fake-session': {'route_id': 'C'}}}
        self.path.write_text(json.dumps(external))
        info = self.path.stat()
        os.utime(self.path, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
        failure = RuntimeError('fixture-secret-reload')
        failure.close_unknown = True
        with patch.object(self.io, 'capture', side_effect=failure):
            self.store.maybe_reload()
        self.assertTrue(self.store._resource_unknown.close_unknown)
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')
        with patch.object(self.io, 'capture', wraps=self.io.capture) as capture:
            self.store.maybe_reload()
            self.assertFalse(self.store.recover())
        capture.assert_not_called()
        with self.assertRaises(SidecarWriteError) as cm:
            self.store.apply_command('fake-client', 'fake-session', 'set', 'B')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertTrue(cm.exception.close_unknown)
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')

    def test_resource_unknown_blocks_even_when_target_proves_b_or_c(self):
        for disk_state in ('B', 'C'):
            for flag in ('release_unknown', 'close_unknown', 'cleanup_unknown'):
                with self.subTest(disk=disk_state, flag=flag), tempfile.TemporaryDirectory() as d:
                    path = Path(d) / 'session_overrides.json'
                    io = FakeSecureIO()
                    store = SessionOverridesSidecar(path, secure_io=io)
                    store.apply_command('fake-client', 'fake-session', 'set', 'B')
                    commit = io.commit
                    def fail(*args):
                        candidate = commit(*args) if disk_state == 'C' else None
                        raise SidecarWriteError('UNKNOWN' if disk_state == 'C' else 'NOT_REPLACED',
                                                candidate, **{flag: True})
                    with patch.object(io, 'commit', side_effect=fail):
                        with self.assertRaises(SidecarWriteError) as first:
                            store.apply_command('fake-client', 'fake-session', 'set', 'C')
                    self.assertEqual(first.exception.state, 'UNKNOWN')
                    self.assertTrue(getattr(first.exception, flag))
                    observed = io.capture(path)
                    expected = store._unknown[1 if disk_state == 'C' else 0]
                    self.assertTrue(SessionOverridesSidecar._same_snapshot(observed, expected))
                    with patch.object(io, 'capture', wraps=io.capture) as capture, \
                         patch.object(io, 'commit', wraps=io.commit) as writer:
                        store.maybe_reload()
                        self.assertFalse(store.recover())
                        with self.assertRaises(SidecarWriteError) as blocked:
                            store.apply_command('fake-client', 'fake-session', 'set', 'B')
                    capture.assert_not_called()
                    writer.assert_not_called()
                    self.assertTrue(getattr(blocked.exception, flag))
                    self.assertEqual(store.get_overrides_for('fake-client')['fake-session'], 'B')

    def test_code_only_capture_release_failure_is_latched(self):
        class CaptureFailure(RuntimeError):
            code = 'resource_release_failed'
        with patch.object(self.io, 'capture', side_effect=CaptureFailure('fixture-secret')):
            with self.assertRaises(SidecarWriteError) as cm:
                self.store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertTrue(cm.exception.release_unknown)
        self.assertFalse(self.store.recover())
        self.assertNotIn('fixture-secret', ''.join(traceback.format_exception(cm.exception)))

    def test_known_code_only_close_and_cleanup_failures_are_latched(self):
        class CaptureFailure(RuntimeError):
            def __init__(self, code):
                super().__init__('fixture-secret')
                self.code = code
        for code, flag in (('fd_close_unknown', 'close_unknown'),
                           ('staging_cleanup_unknown', 'cleanup_unknown')):
            with self.subTest(code=code):
                store = SessionOverridesSidecar(self.path, secure_io=self.io)
                with patch.object(self.io, 'capture', side_effect=CaptureFailure(code)):
                    with self.assertRaises(SidecarWriteError) as cm:
                        store.apply_command('fake-client', 'fake-session', 'set', 'C')
                self.assertEqual(cm.exception.state, 'UNKNOWN')
                self.assertTrue(getattr(cm.exception, flag))
                self.assertFalse(store.recover())
                self.assertNotIn('fixture-secret', ''.join(traceback.format_exception(cm.exception)))

    def test_resource_failure_receipt_does_not_promise_b_c_recovery(self):
        error = SidecarWriteError('NOT_REPLACED', cleanup_unknown=True)
        ctx = CommandContext('C', 'fake-client', 'fake-session',
                             {'route_pool': [{'route_id': 'B', 'weight': 1}]},
                             {'B': {'id': 'B'}, 'C': {'id': 'C'}}, self.store, 'B')
        with patch.object(self.io, 'commit', side_effect=error):
            result = handle_route_command(ctx)
        self.assertFalse(result.wrote)
        self.assertIn('资源', result.receipt_text)
        self.assertIn('不能解除', result.receipt_text)
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')

    def test_recovery_interrupts_never_partially_publish_c(self):
        targets = (
            'recovered_mem = old_mem.copy()',
            'publishing = True',
            'self._data = recovered_data',
            'self._mem_last_seen = recovered_mem',
            'self._baseline = observed',
            'self._mtime = observed.mtime',
            'self._unavailable = False',
            'self._unknown = None',
            'log.warning("sidecar: UNKNOWN resolved against verified snapshot")',
        )
        for index, target in enumerate(targets):
            with self.subTest(boundary=target):
                if index:
                    self.store = SessionOverridesSidecar(self.path, secure_io=self.io)
                    self.store.apply_command('fake-client', 'fake-session', 'set', 'B')
                with self.assertRaises(SidecarWriteError):
                    _inject_on_line(self.store, _line_for('self._baseline = committed'))
                self.store.touch('fake-client', 'fake-session')
                old_data = json.loads(json.dumps(self.store._data))
                old_mem, baseline, pending = self.store._mem_last_seen.copy(), self.store._baseline, self.store._unknown
                with self.assertRaises(SidecarWriteError) as cm:
                    _interrupt_during_recovery(self.store, target)
                self.assertEqual(cm.exception.state, 'UNKNOWN')
                self.assertNotIn('fixture-secret', ''.join(traceback.format_exception(cm.exception)))
                self.assertEqual(self.store._data, old_data)
                self.assertEqual(self.store._mem_last_seen, old_mem)
                self.assertEqual(self.store._baseline, baseline)
                self.assertEqual(self.store._unknown, pending)
                self.store.maybe_reload()
                self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')
                self.assertTrue(self.store.recover())
                self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'C')
                self.assertGreaterEqual(self.store._mem_last_seen['fake-client', 'fake-session'],
                                        old_mem['fake-client', 'fake-session'])

    def test_interrupted_b_recovery_retains_unknown_until_reverified(self):
        with patch.object(self.io, 'commit', side_effect=KeyboardInterrupt('fake')):
            with self.assertRaises(SidecarWriteError):
                self.store.apply_command('fake-client', 'fake-session', 'set', 'C')
        pending = self.store._unknown
        with self.assertRaises(SidecarWriteError) as cm:
            _interrupt_during_recovery(self.store, 'self._baseline = observed')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertEqual(self.store._unknown, pending)
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertTrue(self.store.recover())
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')

    def test_recovery_capture_release_unknown_is_not_lost(self):
        with self.assertRaises(SidecarWriteError):
            _inject_on_line(self.store, _line_for('self._mtime = committed.mtime'))
        class CaptureFailure(RuntimeError):
            release_unknown = True
            close_unknown = True
        with patch.object(self.io, 'capture', side_effect=CaptureFailure('fixture-secret')):
            with self.assertRaises(SidecarWriteError) as cm:
                self.store.recover()
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertTrue(cm.exception.release_unknown)
        self.assertTrue(cm.exception.close_unknown)
        self.assertIsNotNone(self.store._unknown)
        self.assertNotIn('fixture-secret', ''.join(traceback.format_exception(cm.exception)))
        self.assertFalse(self.store.recover())
        with self.assertRaises(SidecarWriteError) as blocked:
            self.store.apply_command('fake-client', 'fake-session', 'set', 'B')
        self.assertTrue(blocked.exception.release_unknown)
        self.assertTrue(blocked.exception.close_unknown)
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')

    def test_unclassified_error_keeps_flags_but_not_untrusted_candidate(self):
        commit = self.io.commit
        class Unclassified(RuntimeError):
            release_unknown = True
            close_unknown = True
        def write_then_throw(*args):
            candidate = commit(*args)
            error = Unclassified('fixture-secret')
            error.candidate = candidate
            raise error
        with patch.object(self.io, 'commit', side_effect=write_then_throw):
            with self.assertRaises(SidecarWriteError) as cm:
                self.store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertTrue(cm.exception.release_unknown)
        self.assertTrue(cm.exception.close_unknown)
        self.assertIsNone(self.store._unknown[1])
        self.assertFalse(self.store.recover())
        self.assertNotIn('fixture-secret', ''.join(traceback.format_exception(cm.exception)))

    def test_proven_not_replaced_clears_prearmed_quarantine(self):
        with patch.object(self.io, 'commit', side_effect=SidecarWriteError('NOT_REPLACED')):
            with self.assertRaises(SidecarWriteError) as cm:
                self.store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'NOT_REPLACED')
        self.assertIsNone(self.store._unknown)
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')

    def test_false_not_replaced_after_successful_commit_is_unknown(self):
        decode = self.store._decode_snapshot
        def wrong_classification(snapshot):
            if snapshot.contents and b'"route_id": "C"' in snapshot.contents:
                raise SidecarWriteError('NOT_REPLACED')
            return decode(snapshot)
        with patch.object(self.store, '_decode_snapshot', side_effect=wrong_classification):
            with self.assertRaises(SidecarWriteError) as cm:
                self.store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertTrue(self.store.recover())
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'C')


@unittest.skipUnless(sys.platform == 'darwin', 'native Darwin filesystem required')
class NativeMacCommandInterruptTests(unittest.TestCase):
    def setUp(self):
        from core.sidecar_secure_io import SidecarSecureIO
        self.tmp = tempfile.TemporaryDirectory(prefix='sidecar-command-mac-', dir='/private/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / '.runtime' / 'tmp').mkdir(parents=True, mode=0o700)
        (self.root / 'config').mkdir(mode=0o700)
        self.link = self.root / 'fixture-config-link'
        self.link.symlink_to('config', target_is_directory=True)
        self.path = self.link / 'session_overrides.json'
        # Permissive fixture only, never production authorization.
        self.io = SidecarSecureIO(project_root=self.root, target=self.path, guard=lambda *_: True)
        self.store = SessionOverridesSidecar(self.path, secure_io=self.io)
        self.store.apply_command('fake-client', 'fake-session', 'set', 'B')

    def test_interruption_after_real_commit_during_decode_quarantines_then_recovers_c(self):
        decode = self.store._decode_snapshot
        def interrupted(snapshot):
            if snapshot.contents and b'"route_id": "C"' in snapshot.contents:
                raise KeyboardInterrupt('fixture-secret-from-decode')
            return decode(snapshot)
        with patch.object(self.store, '_decode_snapshot', side_effect=interrupted):
            with self.assertRaises(SidecarWriteError) as cm:
                self.store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertNotIn('fixture-secret', ''.join(traceback.format_exception(cm.exception)))
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertEqual(json.loads(self.path.read_bytes())['fake-client']['fake-session']['route_id'], 'C')
        self.store.maybe_reload()
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertTrue(self.store.recover())
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'C')
        self.assertTrue(self.link.is_symlink())

    def test_native_interrupt_before_return_value_assignment_quarantines(self):
        before = self.store._baseline
        with self.assertRaises(SidecarWriteError) as cm:
            _interrupt_after_commit_return(self.store)
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertEqual(self.store._baseline, before)
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertEqual(json.loads(self.path.read_bytes())['fake-client']['fake-session']['route_id'], 'C')
        self.assertIsNone(self.store._unknown[1])
        self.store.maybe_reload()
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertFalse(self.store.recover())
        self.assertTrue(self.link.is_symlink())

    def test_interruption_during_partial_mac_publish_keeps_b_and_exact_c_evidence(self):
        before = self.store._baseline
        before_mem = self.store._mem_last_seen.copy()
        with self.assertRaises(SidecarWriteError) as cm:
            _inject_on_line(self.store, _line_for('self._mtime = committed.mtime'))
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertEqual(self.store._baseline, before)
        self.assertEqual(self.store._mem_last_seen, before_mem)
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertEqual(self.store._unknown[1], self.io.capture(self.path))
        self.assertTrue(self.store.recover())
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'C')
        self.assertTrue(self.link.is_symlink())

    def test_native_interrupted_c_recovery_rolls_back_to_b(self):
        with self.assertRaises(SidecarWriteError):
            _inject_on_line(self.store, _line_for('self._mtime = committed.mtime'))
        pending = self.store._unknown
        old_mem = self.store._mem_last_seen.copy()
        with self.assertRaises(SidecarWriteError) as cm:
            _interrupt_during_recovery(self.store, 'self._mtime = observed.mtime')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertEqual(self.store._unknown, pending)
        self.assertEqual(self.store._mem_last_seen, old_mem)
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertTrue(self.store.recover())
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'C')

    def test_real_mac_prewrite_capture_close_unknown_latches(self):
        from core import mac_config_path as mac
        close = mac._close_fd
        failed = []
        baseline = self.store._baseline
        def close_then_fail(fd):
            close(fd)
            if not failed:
                failed.append(True)
                error = mac.MacConfigError('fd_close_unknown')
                error.close_unknown = True
                raise error
        with patch.object(mac, '_close_fd', side_effect=close_then_fail), \
             patch.object(self.io, 'commit', wraps=self.io.commit) as writer:
            with self.assertRaises(SidecarWriteError) as cm:
                self.store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertTrue(cm.exception.close_unknown)
        self.assertTrue(cm.exception.release_unknown)
        writer.assert_not_called()
        # The separately acquired clean target proves B, not the first close.
        self.assertTrue(SessionOverridesSidecar._same_snapshot(self.io.capture(self.path), baseline))
        with patch.object(self.io, 'capture', wraps=self.io.capture) as capture, \
             patch.object(self.io, 'commit', wraps=self.io.commit) as retry:
            self.store.maybe_reload()
            self.assertFalse(self.store.recover())
            with self.assertRaises(SidecarWriteError) as blocked:
                self.store.apply_command('fake-client', 'fake-session', 'set', 'C')
        capture.assert_not_called()
        retry.assert_not_called()
        self.assertTrue(blocked.exception.close_unknown)
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertTrue(self.link.is_symlink())

    def test_real_mac_failed_stage_cleanup_cannot_be_cleared_by_b(self):
        from core import secure_files
        unlink = os.unlink
        def refuse_stage(name, *args, **kwargs):
            if isinstance(name, str) and name.startswith('.model-proxy-runtime-'):
                raise OSError('fixture-secret-cleanup')
            return unlink(name, *args, **kwargs)
        baseline = self.store._baseline
        with patch.object(secure_files.os, 'replace', side_effect=OSError('fixture-secret-replace')), \
             patch.object(secure_files.os, 'unlink', side_effect=refuse_stage):
            with self.assertRaises(SidecarWriteError) as cm:
                self.store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertTrue(cm.exception.cleanup_unknown)
        stages = list((self.root / '.runtime' / 'tmp').iterdir())
        self.assertEqual(len(stages), 1)
        try:
            self.assertTrue(SessionOverridesSidecar._same_snapshot(self.io.capture(self.path), baseline))
            self.assertFalse(self.store.recover())
            with self.assertRaises(SidecarWriteError) as blocked:
                self.store.apply_command('fake-client', 'fake-session', 'set', 'C')
            self.assertTrue(blocked.exception.cleanup_unknown)
            self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')
            self.assertEqual(len(list((self.root / '.runtime' / 'tmp').iterdir())), 1)
            self.assertNotIn('fixture-secret', ''.join(traceback.format_exception(cm.exception)))
            # Deleting a fixture artifact outside the store is not a verified
            # resource-resolution protocol and must not silently clear the latch.
            stages[0].unlink()
            self.assertFalse(self.store.recover())
            with self.assertRaises(SidecarWriteError) as still_blocked:
                self.store.apply_command('fake-client', 'fake-session', 'set', 'C')
            self.assertTrue(still_blocked.exception.cleanup_unknown)
        finally:
            # Remove only the artifact created by this isolated fixture.
            if stages[0].exists():
                stages[0].unlink()

    def test_real_adapter_release_uncertainty_survives_command_rethrow(self):
        from core import secure_files
        from core.sidecar_secure_io import SidecarIOError
        failure = SidecarIOError('capture_unverified')
        failure.close_unknown = True
        failure.release_unknown = True
        writer = secure_files.secure_replace_runtime
        with patch.object(self.io, 'capture', wraps=self.io.capture) as capture:
            def set_failure_after_commit(*args, **kwargs):
                result = writer(*args, **kwargs)
                capture.side_effect = failure
                return result
            with patch.object(secure_files, 'secure_replace_runtime', side_effect=set_failure_after_commit):
                with self.assertRaises(SidecarWriteError) as cm:
                    self.store.apply_command('fake-client', 'fake-session', 'set', 'C')
        self.assertEqual(cm.exception.state, 'UNKNOWN')
        self.assertTrue(cm.exception.close_unknown)
        self.assertTrue(cm.exception.release_unknown)
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')
        self.assertFalse(self.store.recover())
        with self.assertRaises(SidecarWriteError) as blocked:
            self.store.apply_command('fake-client', 'fake-session', 'set', 'B')
        self.assertTrue(blocked.exception.release_unknown)
        self.assertTrue(blocked.exception.close_unknown)
        self.assertEqual(self.store.get_overrides_for('fake-client')['fake-session'], 'B')


if __name__ == '__main__':
    unittest.main()
