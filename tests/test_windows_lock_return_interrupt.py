"""Instruction-window CRT transfer tests; all handles/FDs are fake objects."""
import dis
import errno
import sys
import unittest
from unittest.mock import patch

from core import discovery, platform_lock as locks, platform_windows as win
from core.runtime_paths import PATH_KEYS
from tests import test_windows_discovery as fixtures


def _transfer_offsets():
    instructions = list(dis.get_instructions(win.lock_private_object))
    index = next(i for i, item in enumerate(instructions)
                 if item.argval == '_transfer_to_fd'
                 and item.opname in ('LOAD_ATTR', 'LOAD_METHOD'))
    unpack = next(i for i in range(index + 1, len(instructions))
                  if instructions[i].opname == 'UNPACK_SEQUENCE')
    stores = []
    for i in range(unpack + 1, len(instructions)):
        item = instructions[i]
        if item.opname.startswith('STORE_FAST'):
            stores.append(i)
            # Python 3.14 may combine both stores into one instruction.
            if item.argval == 'fd' or ('raw' in item.argrepr and 'fd' in item.argrepr):
                break
    if not stores or stores[-1] + 1 >= len(instructions):
        raise AssertionError('unrecognized transfer bytecode')
    offsets = {
        'returned_before_unpack': instructions[unpack].offset,
        'before_first_store': instructions[stores[0]].offset,
        'assigned': instructions[stores[-1] + 1].offset,
    }
    if (len(stores) > 1 and instructions[stores[0]].argval == 'raw'
            and instructions[stores[-1]].argval == 'fd'):
        offsets['raw_assigned_fd_missing'] = instructions[stores[0] + 1].offset
    return offsets


class TransferReturnInterruptTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WindowsDiscoveryLockTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fs, self.crt = self.fixture.fs, self.fixture.crt
        self.root, self.state = self.fixture.ROOT, self.fixture.STATE
        self.target = self.state + r'\anchor.lock'
        self.paths = {key: self.state + '\\' + key for key in PATH_KEYS}

    def _exercise(self, *, create, point, expected_unknown, interruption=KeyboardInterrupt):
        offsets = _transfer_offsets()
        if point not in offsets:
            self.skipTest('interpreter combines raw/fd assignment into one opcode')
        if not create:
            self.fs.objects[self.target] = self.fs.obj(71)
        snapshot = discovery.inspect_runtime_identities(self.root, self.paths)
        expected_offset = offsets[point]
        captured = {}
        seen = []
        previous = sys.gettrace()
        def trace(frame, event, arg):
            if frame.f_code is win.lock_private_object.__code__:
                frame.f_trace_opcodes = True
                if event == 'opcode':
                    seen.append(frame.f_lasti)
                if event == 'opcode' and frame.f_lasti == expected_offset:
                    captured['raw'] = frame.f_locals.get('raw')
                    captured['fd'] = frame.f_locals.get('fd')
                    captured['closed'] = frame.f_locals['owned'].closed
                    captured['mapping'] = dict(self.crt.mapping)
                    # Do not intercept cleanup or the test runner.
                    sys.settrace(None)
                    raise interruption()
            return trace
        try:
            sys.settrace(trace)
            with self.assertRaises(interruption) as caught:
                locks.acquire_lock(snapshot, 'anchor.lock', create=create)
        except AssertionError:
            # 只有「断点没被走到」需要现场：给出真实执行到的偏移与断点邻近指令，
            # 避免在别的 Python 版本/分支上只看到一句 not raised。
            listing = [(item.offset, item.opname, item.argrepr)
                       for item in dis.get_instructions(win.lock_private_object)]
            near = [item for item in listing
                    if expected_offset - 24 <= item[0] <= expected_offset + 24]
            raise AssertionError(
                f'interruption not raised; point={point} offset={expected_offset} '
                f'create={create} seen={seen[-60:]} near={near}') from None
        finally:
            sys.settrace(previous)
        self.assertTrue(captured, 'opcode interruption did not execute')
        self.assertTrue(captured['closed'])
        self.assertEqual(len(captured['mapping']), 1)
        original_fd, original_handle = next(iter(captured['mapping'].items()))
        self.assertIs(caught.exception.created, create)
        self.assertIs(caught.exception.release_unknown, expected_unknown)
        self.assertIs(locks._windows_release_unknown, expected_unknown)
        if expected_unknown:
            self.assertIsNone(captured['fd'])
            self.assertEqual(self.crt.mapping[original_fd], original_handle)
            self.assertIn(original_handle, self.fs.handles)
            self.assertEqual(self.crt.close_calls, [])
            self.assertEqual(self.fs.close_calls.count(original_handle), 0)
            with self.assertRaises(locks.LockError) as blocked:
                locks.acquire_lock(snapshot, 'anchor.lock', create=False)
            self.assertEqual(blocked.exception.code, 'lock_release_unknown')
        else:
            self.assertEqual(captured['fd'], original_fd)
            self.assertEqual(captured['raw'], original_handle)
            self.assertEqual(self.crt.close_calls, [original_fd])
            self.assertNotIn(original_fd, self.crt.mapping)
            self.assertNotIn(original_handle, self.fs.handles)

    def test_owner_transfer_racing_with_failed_adopt_cleanup_is_unknown(self):
        self.fs.objects[self.target] = self.fs.obj(71)
        raw = self.fs.open_file(self.target, False)
        owned = win.WindowsFileHandle(raw, self.fs, created=True)
        original_adopt = self.crt.adopt
        original_close_once = owned._close_once
        captured = []
        def fail_adopt(handle):
            raise OSError(errno.EMFILE, 'synthetic failed adopt')
        def other_transfer_before_cleanup():
            self.crt.adopt = original_adopt
            captured.append(owned._transfer_to_fd(self.crt))
            return original_close_once()
        self.crt.adopt = fail_adopt
        with patch.object(owned, '_close_once', side_effect=other_transfer_before_cleanup):
            with self.assertRaises(win.WindowsObjectLockError) as caught:
                win.lock_private_object(owned, fd_backend=self.crt, acl_backend=self.fixture.acl)
        self.assertEqual(caught.exception.code, 'lock_release_unknown')
        self.assertTrue(caught.exception.release_unknown)
        self.assertTrue(caught.exception.created)
        adopted_raw, fd = captured[0]
        self.assertEqual(adopted_raw, raw)
        self.assertEqual(self.crt.mapping[fd], raw)
        self.assertIn(raw, self.fs.handles)
        self.assertEqual(self.crt.close_calls, [])
        self.assertEqual(self.fs.close_calls.count(raw), 0)

    def test_preclosed_owner_is_rejected_without_fabricating_unknown(self):
        self.fs.objects[self.target] = self.fs.obj(71)
        raw = self.fs.open_file(self.target, False)
        owned = win.WindowsFileHandle(raw, self.fs, created=False)
        owned.close()
        with self.assertRaises(win.WindowsObjectLockError) as caught:
            win.lock_private_object(owned, fd_backend=self.crt, acl_backend=self.fixture.acl)
        self.assertEqual(caught.exception.code, 'handle_invalid')
        self.assertFalse(caught.exception.release_unknown)
        self.assertEqual(self.fs.close_calls.count(raw), 1)
        self.assertEqual(self.crt.close_calls, [])

    def test_new_lock_interrupted_immediately_after_transfer_return(self):
        self._exercise(create=True, point='returned_before_unpack', expected_unknown=True)

    def test_existing_lock_interrupted_immediately_after_transfer_return(self):
        self._exercise(create=False, point='returned_before_unpack', expected_unknown=True)

    def test_new_lock_interrupted_after_unpack_before_assignment(self):
        self._exercise(create=True, point='before_first_store', expected_unknown=True)

    def test_existing_lock_interrupted_after_unpack_before_assignment(self):
        self._exercise(create=False, point='before_first_store', expected_unknown=True)

    def test_new_lock_interrupted_after_raw_only_assignment(self):
        self._exercise(create=True, point='raw_assigned_fd_missing', expected_unknown=True)

    def test_existing_lock_interrupted_after_raw_only_assignment(self):
        self._exercise(create=False, point='raw_assigned_fd_missing', expected_unknown=True)

    def test_system_exit_after_transfer_return_remains_unknown(self):
        self._exercise(create=True, point='returned_before_unpack', expected_unknown=True,
                       interruption=SystemExit)

    def test_new_lock_interrupted_after_assignment_closes_known_fd(self):
        self._exercise(create=True, point='assigned', expected_unknown=False)

    def test_existing_lock_interrupted_after_assignment_closes_known_fd(self):
        self._exercise(create=False, point='assigned', expected_unknown=False)


if __name__ == '__main__':
    unittest.main()
