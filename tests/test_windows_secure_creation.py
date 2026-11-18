"""Synthetic filesystem tests for reusable creation primitives, not native proof."""
import unittest
from core import platform_windows as win
from core.windows_paths import WindowsPathBackend
from tests.test_windows_secure_files_writer import WritingFS
from tests.test_windows_adapter import VirtualACL
from tests.test_windows_discovery import PathWinBridge


class PrivateCreation(unittest.TestCase):
    ROOT = r'C:\fixture\project'
    def setUp(self):
        self.fs = WritingFS()
        self.acl = VirtualACL(self.fs)
        self.paths = WindowsPathBackend(win32=PathWinBridge(self.fs), acl_backend=self.acl)
        self.seq = 9010
        self.unsafe = False
        def mkdir(path, *, user_sid):
            if path in self.fs.objects:
                raise OSError('exists')
            self.seq += 1
            self.fs.objects[path] = self.fs.obj(self.seq, directory=True)
            self.fs.objects[path]['unsafe_group'] = self.unsafe
        def create(path, *, user_sid):
            handle = self.fs.open_file(path, True)
            self.seq += 1
            self.fs.handles[handle][1]['index'] = self.seq
            self.fs.handles[handle][1]['unsafe_group'] = self.unsafe
            return handle
        self.fs.create_private_directory = mkdir
        self.fs.open_private_candidate = create

    def args(self):
        return dict(backend=self.fs, acl_backend=self.acl, paths_backend=self.paths)

    def test_create_directory_and_verify_existing(self):
        path = self.ROOT + r'\runtime'
        result = win.ensure_private_directory(path, **self.args())
        self.assertTrue(result.created)
        self.assertFalse(win.ensure_private_directory(path, **self.args()).created)
        self.assertFalse(self.fs.handles)

    def test_unsafe_existing_directory_never_repaired(self):
        path = self.ROOT + r'\runtime'
        self.fs.objects[path] = self.fs.obj(900, directory=True)
        self.fs.objects[path]['unsafe_group'] = True
        before = dict(self.fs.objects[path])
        with self.assertRaises(win.WindowsPrivateCreationError) as caught:
            win.ensure_private_directory(path, **self.args())
        self.assertFalse(caught.exception.created)
        self.assertEqual(self.fs.objects[path], before)
        self.assertFalse(self.fs.handles)

    def test_unsafe_created_directory_reports_partial_creation(self):
        self.unsafe = True
        with self.assertRaises(win.WindowsPrivateCreationError) as caught:
            win.ensure_private_directory(self.ROOT + r'\runtime', **self.args())
        self.assertTrue(caught.exception.created)
        self.assertFalse(self.fs.handles)

    def test_empty_file_create_new_and_never_overwrite(self):
        path = self.ROOT + r'\log'
        self.assertTrue(win.create_private_empty_file(path, **self.args()).created)
        self.assertEqual(self.fs.objects[path].get('data', b''), b'')
        with self.assertRaises(win.WindowsPrivateCreationError) as caught:
            win.create_private_empty_file(path, **self.args())
        self.assertEqual(caught.exception.code, 'already_exists')
        self.assertFalse(caught.exception.created)
        self.assertFalse(self.fs.handles)

    def test_missing_parent_and_unknown_create(self):
        with self.assertRaises(win.WindowsPrivateCreationError) as caught:
            win.create_private_empty_file(self.ROOT + r'\missing\log', **self.args())
        self.assertFalse(caught.exception.created)
        self.fs.open_private_candidate = lambda *a, **kw: (_ for _ in ()).throw(OSError('unknown'))
        with self.assertRaises(win.WindowsPrivateCreationError) as caught:
            win.create_private_empty_file(self.ROOT + r'\log', **self.args())
        self.assertIsNone(caught.exception.created)
        self.assertFalse(self.fs.handles)


if __name__ == '__main__':
    unittest.main()
