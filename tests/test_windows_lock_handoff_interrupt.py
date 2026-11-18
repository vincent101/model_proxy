"""Windows lock handoff/close races with recycled synthetic HANDLEs and FDs."""
import errno
import unittest
from unittest.mock import patch

from core import discovery, platform_lock as locks, platform_windows as win
from core.runtime_paths import PATH_KEYS
from tests import test_windows_discovery as fixtures


class FakeLockHandoffInterruptTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WindowsDiscoveryLockTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fs, self.crt = self.fixture.fs, self.fixture.crt
        self.root, self.state = self.fixture.ROOT, self.fixture.STATE
        self.target = self.state + r'\anchor.lock'
        self.paths = {key: self.state + '\\' + key for key in PATH_KEYS}
        self.recycled = ('unrelated-fake-object', self.fs.obj(123456))

    def snapshot(self):
        return discovery.inspect_runtime_identities(self.root, self.paths)

    def inject_raw_close_after_adopt_failure(self, interruption):
        original_close = self.fs.close
        state = {'handle': None}
        def adopt(handle):
            state['handle'] = handle
            raise OSError(errno.EMFILE, 'synthetic adopt failure')
        def close(handle):
            if handle == state['handle']:
                self.fs.close_calls.append(handle)
                del self.fs.handles[handle]       # fake OS has released it
                self.fs.handles[handle] = self.recycled  # same number, new object
                raise interruption
            return original_close(handle)
        self.crt.adopt = adopt
        return state, close

    def test_interrupt_during_raw_close_never_closes_recycled_handle(self):
        for create in (False, True):
            with self.subTest(create=create):
                if not create:
                    self.fs.objects[self.target] = self.fs.obj(71)
                state, close = self.inject_raw_close_after_adopt_failure(KeyboardInterrupt())
                with patch.object(self.fs, 'close', side_effect=close):
                    with self.assertRaises(KeyboardInterrupt) as caught:
                        locks.acquire_lock(self.snapshot(), 'anchor.lock', create=create)
                handle = state['handle']
                self.assertIs(caught.exception.created, create)
                self.assertIs(caught.exception.release_unknown, True)
                self.assertIs(self.fs.handles[handle], self.recycled)
                self.assertEqual(self.fs.close_calls.count(handle), 1)
                self.assertFalse(self.crt.close_calls)
                self.assertTrue(locks._windows_release_unknown)
                with self.assertRaises(locks.LockError) as blocked:
                    locks.acquire_lock(self.snapshot(), 'anchor.lock', create=False)
                self.assertEqual(blocked.exception.code, 'lock_release_unknown')
                # Only synthetic in-memory state is reset between cases.
                self.fs.handles.pop(handle)
                self.fs.objects.pop(self.target)
                locks._windows_release_unknown = False

    def test_raw_close_oserror_is_unknown_once_and_retains_created_flag(self):
        state, close = self.inject_raw_close_after_adopt_failure(OSError(errno.EIO, 'synthetic'))
        with patch.object(self.fs, 'close', side_effect=close):
            with self.assertRaises(locks.LockError) as caught:
                locks.acquire_lock(self.snapshot(), 'anchor.lock', create=True)
        handle = state['handle']
        self.assertEqual(caught.exception.code, 'lock_release_unknown')
        self.assertIs(caught.exception.created, True)
        self.assertIs(caught.exception.release_unknown, True)
        self.assertIs(self.fs.handles[handle], self.recycled)
        self.assertEqual(self.fs.close_calls.count(handle), 1)
        self.assertTrue(locks._windows_release_unknown)

    def test_object_owner_is_invalid_before_interrupted_raw_close(self):
        self.fs.objects[self.target] = self.fs.obj(71)
        raw = self.fs.open_file(self.target, False)
        owned = win.WindowsFileHandle(raw, self.fs, created=True)
        state, close = self.inject_raw_close_after_adopt_failure(KeyboardInterrupt())
        with patch.object(self.fs, 'close', side_effect=close):
            with self.assertRaises(KeyboardInterrupt) as caught:
                win.lock_private_object(owned, fd_backend=self.crt, acl_backend=self.fixture.acl)
            self.assertTrue(owned.closed)
            owned.close()  # no second native CloseHandle on the recycled value
        self.assertEqual(state['handle'], raw)
        self.assertIs(caught.exception.created, True)
        self.assertIs(caught.exception.release_unknown, True)
        self.assertIs(self.fs.handles[raw], self.recycled)
        self.assertEqual(self.fs.close_calls.count(raw), 1)

    def test_system_exit_during_raw_close_preserves_status_without_retry(self):
        state, close = self.inject_raw_close_after_adopt_failure(SystemExit(23))
        with patch.object(self.fs, 'close', side_effect=close):
            with self.assertRaises(SystemExit) as caught:
                locks.acquire_lock(self.snapshot(), 'anchor.lock', create=True)
        self.assertEqual(caught.exception.code, 23)
        self.assertTrue(caught.exception.created)
        self.assertTrue(caught.exception.release_unknown)
        self.assertIs(self.fs.handles[state['handle']], self.recycled)
        self.assertEqual(self.fs.close_calls.count(state['handle']), 1)
        self.assertTrue(locks._windows_release_unknown)

    def test_interruption_before_raw_close_remains_unknown_without_retry(self):
        state = {'handle': None}
        original_close = self.fs.close
        def adopt(handle):
            state['handle'] = handle
            raise OSError(errno.EMFILE, 'synthetic adopt failure')
        def close(handle):
            if handle == state['handle']:
                self.fs.close_calls.append(handle)
                raise KeyboardInterrupt()  # whether the OS closed it is unknowable
            return original_close(handle)
        self.crt.adopt = adopt
        with patch.object(self.fs, 'close', side_effect=close):
            with self.assertRaises(KeyboardInterrupt) as caught:
                locks.acquire_lock(self.snapshot(), 'anchor.lock', create=True)
        self.assertTrue(caught.exception.created)
        self.assertTrue(caught.exception.release_unknown)
        self.assertEqual(self.fs.close_calls.count(state['handle']), 1)
        self.assertIn(state['handle'], self.fs.handles)
        self.assertTrue(locks._windows_release_unknown)

    def test_reused_fd_with_other_handle_is_never_closed(self):
        reused = {}
        def interrupt_after_reuse(fd):
            reused['fd'] = fd
            reused['original'] = self.crt.mapping[fd]
            self.fs.next_handle += 1
            reused['other'] = self.fs.next_handle
            self.fs.handles[reused['other']] = self.recycled
            self.crt.mapping[fd] = reused['other']
            raise KeyboardInterrupt()
        self.crt.make_noninheritable = interrupt_after_reuse
        with self.assertRaises(KeyboardInterrupt) as caught:
            locks.acquire_lock(self.snapshot(), 'anchor.lock', create=True)
        self.assertTrue(caught.exception.created)
        self.assertTrue(caught.exception.release_unknown)
        self.assertFalse(self.crt.close_calls)
        self.assertEqual(self.crt.mapping[reused['fd']], reused['other'])
        self.assertIs(self.fs.handles[reused['other']], self.recycled)
        self.assertIn(reused['original'], self.fs.handles)
        self.assertTrue(locks._windows_release_unknown)

    def test_interrupt_after_fd_close_preserves_recycled_fd_and_handle(self):
        def fail_noninheritable(fd):
            raise OSError(errno.EIO, 'synthetic inheritable check')
        def close_fd(fd):
            self.crt.close_calls.append(fd)
            handle = self.crt.mapping.pop(fd)
            self.fs.close(handle)                 # fake OS has released both
            self.fs.handles[handle] = self.recycled
            self.crt.mapping[fd] = handle         # both numbers reused
            raise KeyboardInterrupt()
        self.crt.make_noninheritable = fail_noninheritable
        self.crt.close = close_fd
        with self.assertRaises(KeyboardInterrupt) as caught:
            locks.acquire_lock(self.snapshot(), 'anchor.lock', create=True)
        self.assertIs(caught.exception.created, True)
        self.assertIs(caught.exception.release_unknown, True)
        self.assertEqual(len(self.crt.close_calls), 1)
        fd = self.crt.close_calls[0]
        handle = self.crt.mapping[fd]
        self.assertIs(self.fs.handles[handle], self.recycled)
        self.assertEqual(self.fs.close_calls.count(handle), 1)
        self.assertTrue(locks._windows_release_unknown)

    def test_interrupt_during_open_with_unreturned_handle_is_unknown(self):
        self.fs.objects[self.target] = self.fs.obj(71)
        original = self.fs.open_file
        unreturned = []
        def interrupted(path, create):
            handle = original(path, create)
            unreturned.append(handle)
            raise KeyboardInterrupt()
        self.fs.open_file = interrupted
        with self.assertRaises(KeyboardInterrupt) as caught:
            locks.acquire_lock(self.snapshot(), 'anchor.lock', create=False)
        self.assertIs(caught.exception.created, False)
        self.assertIs(caught.exception.release_unknown, True)
        self.assertEqual(len(unreturned), 1)
        self.assertIn(unreturned[0], self.fs.handles)
        self.assertEqual(self.fs.close_calls.count(unreturned[0]), 0)
        self.assertTrue(locks._windows_release_unknown)

    def test_interrupt_after_known_cleanup_preserves_created_and_no_unknown(self):
        def interrupt_noninheritable(fd):
            raise KeyboardInterrupt()
        self.crt.make_noninheritable = interrupt_noninheritable
        with self.assertRaises(KeyboardInterrupt) as caught:
            locks.acquire_lock(self.snapshot(), 'anchor.lock', create=True)
        self.assertIs(caught.exception.created, True)
        self.assertIs(caught.exception.release_unknown, False)
        self.assertFalse(self.fs.handles)
        self.assertEqual(len(self.crt.close_calls), 1)
        self.assertFalse(locks._windows_release_unknown)


if __name__ == '__main__':
    unittest.main()
