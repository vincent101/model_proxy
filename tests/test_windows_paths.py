"""In-memory protocol tests only: no real Windows paths, ACLs, HOME or network."""
from __future__ import annotations

import unittest
import struct
from dataclasses import FrozenInstanceError, dataclass, field, replace

from core import windows_acl as acl
from core.windows_paths import (NativeWin32, Observation, PathEvidenceError,
                                WindowsPathBackend, inspect, may_alias, may_contain)


def observation(number, kind="directory", **changes):
    base = Observation(volume=7, file_id=number.to_bytes(4, "big"),
                       kind=kind, links=1, reparse=False, local=True,
                       stable=True, protection=b"owner-dacl-token-access")
    return replace(base, **changes)


@dataclass
class Node:
    obs: Observation
    children: dict = field(default_factory=dict)
    ancestor_allowed: bool = True
    traverse_allowed: bool = True
    creation_parent_allowed: bool = True
    private_allowed: bool = True
    open_error: Exception | None = None
    observe_error: Exception | None = None

    def add(self, name, node):
        self.children[name.casefold()] = node
        return node


@dataclass(eq=False)
class Handle:
    node: Node
    closed: bool = False


class MemoryBackend:
    """Only a deliberately simplified model of the documented protocol."""
    def __init__(self):
        self.root = Node(observation(1))
        self.parent = self.root.add("Safe", Node(observation(2)))
        self.target = self.parent.add("data.json", Node(observation(3, "file")))
        self.handles = []
        self.policies = []
        self.on_observe = None
        self.root_error = None
        self.close_error = False

    def _handle(self, node):
        handle = Handle(node)
        self.handles.append(handle)
        return handle

    def open_root(self, drive):
        if self.root_error:
            raise self.root_error
        if drive != "C:":
            raise OSError("fake drive")
        return self._handle(self.root)

    def open_child(self, parent, name):
        if parent.closed:
            raise OSError("closed")
        node = parent.node.children.get(name.casefold())
        if node is None:
            raise FileNotFoundError("fake missing")
        if node.open_error:
            raise node.open_error
        return self._handle(node)

    def observe(self, handle, policy, creation_kind=None):
        self.policies.append((handle.node, policy))
        if handle.closed:
            raise OSError("closed")
        if self.on_observe:
            self.on_observe(handle, policy)
        node = handle.node
        if node.observe_error:
            raise node.observe_error
        if not getattr(node, policy + "_allowed"):
            raise PermissionError("fake sensitive detail")
        return node.obs

    def close(self, handle):
        if handle.closed:
            raise RuntimeError("double close")
        handle.closed = True
        if self.close_error:
            raise OSError("fake close error")

    def assert_closed(self, case):
        case.assertTrue(all(h.closed for h in self.handles))


class PathTests(unittest.TestCase):
    def setUp(self):
        self.fs = MemoryBackend()

    def check_error(self, code, action):
        with self.assertRaises(PathEvidenceError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn("fake sensitive detail", str(caught.exception))

    def test_success_and_handle_lifetime(self):
        with inspect(r"C:\Safe\data.json", backend=self.fs, single_link=True) as lease:
            self.assertTrue(lease.evidence.exists)
            self.assertTrue(lease.evidence.direct_parent_observed)
            self.assertEqual(len(lease.evidence.components), 3)
            self.assertIs(lease.borrowed_component_handle(2).node, self.fs.target)
            self.check_error("invalid_component", lambda: lease.borrowed_component_handle(-1))
            with self.assertRaises(FrozenInstanceError):
                lease.evidence.components[-1].observation.kind = "directory"
            lease.revalidate()
            self.assertFalse(any(h.closed for h in self.fs.handles[:3]))
            self.assertTrue(all(h.closed for h in self.fs.handles[3:]))
        self.fs.assert_closed(self)
        self.check_error("lease_closed", lease.revalidate)
        self.check_error("lease_closed", lambda: lease.borrowed_component_handle(0))
        lease.close()  # idempotent

    def test_existing_shared_read_only_ancestor_is_allowed(self):
        # Model the backend having verified the read-only/traverse Users ACE;
        # this does not test or implement the Windows ACL parser.
        self.fs.parent.creation_parent_allowed = False
        with inspect(r"C:\Safe\data.json", backend=self.fs) as lease:
            lease.revalidate()
            self.assertIn((self.fs.parent, "ancestor"), self.fs.policies)
            self.assertNotIn((self.fs.parent, "creation_parent"), self.fs.policies)

    def test_private_file_and_private_directory_policy(self):
        self.fs.target.private_allowed = False
        self.check_error("observation_unavailable",
                         lambda: inspect(r"C:\Safe\data.json", backend=self.fs))
        self.fs.parent.private_allowed = False
        self.check_error("observation_unavailable",
                         lambda: inspect(r"C:\Safe", backend=self.fs, kind="directory"))
        with inspect(r"C:\Safe", backend=self.fs, kind="directory", private=False) as lease:
            lease.revalidate()
        self.fs.assert_closed(self)

    def test_missing_target_checks_direct_parent(self):
        with inspect(r"C:\Safe\new.json", backend=self.fs) as lease:
            self.assertEqual(lease.evidence.missing, ("new.json",))
            self.assertTrue(lease.evidence.direct_parent_observed)
            self.assertEqual(lease.evidence.components[-1].policy, "creation_parent")
            self.assertEqual(lease.evidence.components[-1].creation_kind, "file")
            lease.revalidate()
        self.fs.parent.creation_parent_allowed = False
        self.check_error("observation_unavailable",
                         lambda: inspect(r"C:\Safe\new.json", backend=self.fs))
        self.fs.assert_closed(self)

    def test_multi_missing_does_not_claim_final_direct_parent(self):
        with inspect(r"C:\Safe\newdir\new.json", backend=self.fs) as lease:
            self.assertEqual(lease.evidence.missing, ("newdir", "new.json"))
            self.assertFalse(lease.evidence.direct_parent_observed)
            self.assertEqual(lease.evidence.components[-1].policy, "creation_parent")
            self.assertEqual(lease.evidence.components[-1].creation_kind, "directory")
            lease.revalidate()
        self.fs.parent.creation_parent_allowed = False
        self.check_error("observation_unavailable",
                         lambda: inspect(r"C:\Safe\newdir\new.json", backend=self.fs))

    def test_missing_created_or_parent_deleted_is_changed(self):
        with inspect(r"C:\Safe\new.json", backend=self.fs) as lease:
            self.fs.parent.add("new.json", Node(observation(4, "file")))
            self.check_error("path_changed", lease.revalidate)
        with inspect(r"C:\Safe\data.json", backend=self.fs) as lease:
            del self.fs.root.children["safe"]
            self.check_error("path_changed", lease.revalidate)
        self.fs.assert_closed(self)

    def test_parent_replaced_and_reused_file_id_with_changed_acl(self):
        for reuse in (False, True):
            with self.subTest(reuse=reuse):
                fs = MemoryBackend()
                with inspect(r"C:\Safe\data.json", backend=fs) as lease:
                    newobs = (replace(fs.parent.obs, protection=b"new-acl")
                              if reuse else observation(10))
                    newparent = Node(newobs)
                    newparent.add("data.json", fs.target)
                    fs.root.add("Safe", newparent)
                    self.check_error("path_changed", lease.revalidate)
                fs.assert_closed(self)

    def test_protection_and_identity_change_on_held_handle(self):
        for change in ({"protection": b"different"}, {"file_id": b"another-id"}):
            with self.subTest(change=change):
                fs = MemoryBackend()
                with inspect(r"C:\Safe\data.json", backend=fs) as lease:
                    fs.parent.obs = replace(fs.parent.obs, **change)
                    self.check_error("observation_changed", lease.revalidate)
                fs.assert_closed(self)

    def test_change_during_second_walk_is_detected(self):
        with inspect(r"C:\Safe\data.json", backend=self.fs) as lease:
            count = 0

            def mutate(handle, policy):
                nonlocal count
                if handle.node is self.fs.target:
                    count += 1
                    if count == 2:
                        self.fs.parent.obs = replace(self.fs.parent.obs, protection=b"changed")
            self.fs.on_observe = mutate
            self.check_error("observation_changed", lease.revalidate)
        self.fs.assert_closed(self)

    def test_case_and_hardlink_identity_alias(self):
        self.fs.target.obs = replace(self.fs.target.obs, links=2)
        self.fs.parent.add("alias.json", self.fs.target)
        with (inspect(r"C:\SAFE\DATA.JSON", backend=self.fs) as a,
              inspect(r"c:\safe\data.json", backend=self.fs) as b,
              inspect(r"C:\Safe\alias.json", backend=self.fs) as c):
            for lease in (a, b, c):
                lease.revalidate()
            self.assertTrue(may_alias(a, b))
            self.assertTrue(may_alias(a, c))
        self.check_error("fixed_hardlink",
                         lambda: inspect(r"C:\Safe\data.json", backend=self.fs, single_link=True))
        self.fs.assert_closed(self)

    def test_missing_alias_and_containment(self):
        with (inspect(r"C:\Safe\Dir", backend=self.fs, kind="directory") as p,
              inspect(r"C:\safe\DIR\file", backend=self.fs) as c,
              inspect(r"C:\safe\dir", backend=self.fs, kind="directory") as same,
              inspect(r"C:\safe\other", backend=self.fs, kind="directory") as other,
              inspect(r"C:\safe\é", backend=self.fs, kind="directory") as unicode_name,
              inspect(r"C:\safe\中文", backend=self.fs, kind="directory") as unicode_unknown,
              inspect(r"C:\safe\LONGNA~1", backend=self.fs, kind="directory") as short,
              inspect(r"C:\safe\LongName", backend=self.fs, kind="directory") as long):
            for lease in (p, c, same, other, unicode_name, unicode_unknown, short, long):
                lease.revalidate()
            self.assertTrue(may_contain(p, c))
            self.assertFalse(may_contain(c, p))
            self.assertTrue(may_alias(p, same))
            self.assertFalse(may_alias(p, other))
            self.assertTrue(may_alias(unicode_name, unicode_unknown))
            self.assertTrue(may_alias(short, long))

    def test_bad_observations_fail_closed(self):
        for changes in ({"reparse": True}, {"local": False}, {"local": None},
                        {"stable": False}, {"stable": None}, {"file_id": b""},
                        {"protection": b""}, {"links": 0},
                        {"volume": -1}, {"links": True}):
            with self.subTest(changes=changes):
                fs = MemoryBackend()
                fs.parent.obs = replace(fs.parent.obs, **changes)
                self.check_error("observation_unsafe",
                                 lambda: inspect(r"C:\Safe\data.json", backend=fs))
                fs.assert_closed(self)

    def test_reparse_root_target_volume_and_type(self):
        cases = (
            ("root_reparse", "observation_unsafe"),
            ("target_reparse", "observation_unsafe"),
            ("volume", "volume_changed"),
            ("parent_file", "invalid_type"),
            ("target_dir", "invalid_type"),
            ("loop", "ancestor_alias"),
        )
        for change, code in cases:
            with self.subTest(change=change):
                fs = MemoryBackend()
                if change == "root_reparse":
                    fs.root.obs = replace(fs.root.obs, reparse=True)
                elif change == "target_reparse":
                    fs.target.obs = replace(fs.target.obs, reparse=True)
                elif change == "volume":
                    fs.parent.obs = replace(fs.parent.obs, volume=8)
                elif change == "parent_file":
                    fs.parent.obs = replace(fs.parent.obs, kind="file")
                elif change == "target_dir":
                    fs.target.obs = replace(fs.target.obs, kind="directory")
                else:
                    fs.parent.obs = fs.root.obs
                self.check_error(code, lambda: inspect(r"C:\Safe\data.json", backend=fs))
                fs.assert_closed(self)

    def test_permission_and_unknown_errors_are_not_missing(self):
        for step in ("root", "child", "observation", "acl"):
            with self.subTest(step=step):
                fs = MemoryBackend()
                if step == "root":
                    fs.root_error = PermissionError("fake sensitive detail")
                    code = "root_unavailable"
                elif step == "child":
                    fs.target.open_error = PermissionError("fake sensitive detail")
                    code = "open_unavailable"
                elif step == "observation":
                    fs.target.observe_error = OSError("fake sensitive detail")
                    code = "observation_unavailable"
                else:
                    fs.parent.ancestor_allowed = False
                    code = "observation_unavailable"
                self.check_error(code, lambda: inspect(r"C:\Safe\data.json", backend=fs))
                fs.assert_closed(self)

    def test_revalidation_fails_if_permissions_or_link_count_change(self):
        with inspect(r"C:\Safe\new.json", backend=self.fs) as lease:
            self.fs.parent.creation_parent_allowed = False
            self.check_error("observation_unavailable", lease.revalidate)
        self.fs.parent.creation_parent_allowed = True
        with inspect(r"C:\Safe\data.json", backend=self.fs, single_link=True) as lease:
            self.fs.target.obs = replace(self.fs.target.obs, links=2)
            self.check_error("observation_changed", lease.revalidate)
        self.fs.assert_closed(self)

    def test_close_failure_is_not_silently_accepted(self):
        lease = inspect(r"C:\Safe\data.json", backend=self.fs)
        self.fs.close_error = True
        self.check_error("close_failed", lease.close)
        self.fs.assert_closed(self)
        self.check_error("lease_closed", lease.revalidate)

    def test_keyboard_interrupt_on_close_still_closes_other_handles_once(self):
        lease = inspect(r"C:\Safe\data.json", backend=self.fs)
        held = tuple(lease._handles)
        interrupted = held[-1]
        attempts = []
        original_close = self.fs.close
        def close_once(handle):
            attempts.append(handle)
            if handle is interrupted:
                raise KeyboardInterrupt('fake sensitive OS detail')
            original_close(handle)
        self.fs.close = close_once
        with self.assertRaises(KeyboardInterrupt) as caught:
            lease.close()
        self.assertEqual(str(caught.exception), '')
        self.assertEqual(caught.exception.code, 'close_failed')
        self.assertTrue(caught.exception.release_unknown)
        self.assertEqual(lease._handles, [])
        self.assertEqual(len(attempts), len(held))
        self.assertTrue(all(attempts.count(handle) == 1 for handle in held))
        self.assertTrue(all(handle.closed for handle in held if handle is not interrupted))
        # The interrupted handle may already have been closed by the OS: no retry.
        lease.close()
        self.assertEqual(len(attempts), len(held))

    def test_inspect_preserves_interrupt_if_final_lease_close_also_fails(self):
        fs = MemoryBackend()
        original_close = fs.close
        attempts = []
        def close_once(handle):
            attempts.append(handle)
            original_close(handle)
            # The revalidation walk has opened handles 3..5; the original
            # lease owns handles 0..2. Both closes may already have occurred.
            if handle is fs.handles[5]:
                raise KeyboardInterrupt('fake interrupt detail')
            if handle is fs.handles[2]:
                raise OSError('fake close detail')
        fs.close = close_once
        with self.assertRaises(KeyboardInterrupt) as caught:
            inspect(r"C:\Safe\data.json", backend=fs)
        self.assertTrue(caught.exception.release_unknown)
        self.assertIsInstance(caught.exception.__cause__, PathEvidenceError)
        self.assertEqual(caught.exception.__cause__.code, 'close_failed')
        self.assertEqual(len(fs.handles), 6)
        self.assertEqual(len(attempts), 6)
        self.assertTrue(all(attempts.count(handle) == 1 for handle in fs.handles))
        self.assertTrue(all(handle.closed for handle in fs.handles))
        self.assertNotIn('fake close detail', str(caught.exception.__cause__))

    def test_release_error_codes_survive_observe_and_open_without_backend_text(self):
        class BackendFailure(RuntimeError):
            def __init__(self, code):
                super().__init__('fake secret C:\\private\\file')
                self.code = code
        for code in ('release_unknown', 'resource_release_failed',
                     'handle_close_failed', 'close_failed'):
            for phase in ('root', 'child', 'observe'):
                with self.subTest(code=code, phase=phase):
                    fs = MemoryBackend()
                    error = BackendFailure(code)
                    if phase == 'root': fs.root_error = error
                    elif phase == 'child': fs.target.open_error = error
                    else: fs.target.observe_error = error
                    with self.assertRaises(PathEvidenceError) as caught:
                        inspect(r"C:\Safe\data.json", backend=fs)
                    self.assertEqual(caught.exception.code, code)
                    self.assertNotIn('private', str(caught.exception))
                    fs.assert_closed(self)

    def test_missing_error_with_release_code_is_not_treated_as_absence(self):
        error = FileNotFoundError('fake sensitive path')
        error.code = 'resource_release_failed'
        self.fs.target.open_error = error
        with self.assertRaises(PathEvidenceError) as caught:
            inspect(r"C:\Safe\data.json", backend=self.fs)
        self.assertEqual(caught.exception.code, 'resource_release_failed')
        self.assertNotIn('sensitive', str(caught.exception))
        self.fs.assert_closed(self)

    def test_unrecognized_backend_codes_are_sanitized(self):
        for phase in ('root', 'child', 'observe'):
            with self.subTest(phase=phase):
                fs = MemoryBackend()
                error = PathEvidenceError('C:\\sensitive\\data')
                if phase == 'root': fs.root_error = error
                elif phase == 'child': fs.target.open_error = error
                else: fs.target.observe_error = error
                with self.assertRaises(PathEvidenceError) as caught:
                    inspect(r"C:\Safe\data.json", backend=fs)
                expected = {'root': 'root_unavailable', 'child': 'open_unavailable',
                            'observe': 'observation_unavailable'}[phase]
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn('sensitive', str(caught.exception))
                fs.assert_closed(self)

    def test_invalid_names_rejected_before_backend(self):
        for path in (r"C:foo", r"\foo", r"\\server\share\file", r"\\?\C:\file",
                     "C:\\", r"C:\foo\..\bar", r"C:\foo\.\bar", r"C:\a\\b",
                     r"C:\CON.txt", r"C:\COM¹", r"C:\x. ", r"C:\a:stream",
                     "C:\\bad\x00name", r"C:\a/b", r"C:\bad?"):
            with self.subTest(path=path):
                self.check_error("invalid_path", lambda: inspect(path, backend=self.fs))
        self.assertEqual(self.fs.handles, [])

    def test_invalid_arguments_fail_before_backend(self):
        for kwargs in ({"kind": "socket"}, {"private": None}, {"private": False},
                       {"single_link": True, "kind": "directory"}, {"single_link": 1}):
            with self.subTest(kwargs=kwargs):
                self.check_error("invalid_arguments",
                                 lambda: inspect(r"C:\Safe\data.json", backend=self.fs, **kwargs))
        self.assertEqual(self.fs.handles, [])

    def test_evidence_cannot_be_reused_after_close_or_failure(self):
        a = inspect(r"C:\Safe\data.json", backend=self.fs)
        b = inspect(r"C:\Safe\data.json", backend=self.fs)
        self.check_error("lease_invalid", lambda: may_alias(a.evidence, b.evidence))
        a.close()
        self.check_error("lease_invalid", lambda: may_alias(a, b))
        self.fs.target.obs = replace(self.fs.target.obs, protection=b"changed")
        self.check_error("observation_changed", b.revalidate)
        self.check_error("lease_invalid", b.revalidate)
        self.check_error("lease_invalid", lambda: b.borrowed_component_handle(0))
        b.close()
        self.fs.assert_closed(self)

    def test_alias_check_revalidates_before_using_live_evidence(self):
        with (inspect(r"C:\Safe\data.json", backend=self.fs) as a,
              inspect(r"C:\Safe\data.json", backend=self.fs) as b):
            self.fs.target.obs = replace(self.fs.target.obs, protection=b"changed")
            self.check_error("observation_changed", lambda: may_alias(a, b))
            self.check_error("lease_invalid", lambda: may_alias(a, b))
        self.fs.assert_closed(self)


class FakeWin32:
    """Fake CreateFile/file info/volume/ACL primitives; no real path access."""
    USER = bytes.fromhex("01020000000000051500000039050000")

    def __init__(self):
        self.fs = MemoryBackend()
        self.volume = (3, "NTFS", 7, r"\Device\HarddiskVolume4")
        self.next_handle = 100
        self.live = {}
        self.open_calls = []
        self.close_calls = []
        self.after_open = None
        self.on_missing = None
        self.on_acl = None
        self.acl_checks = []
        self.open_error = {}
        self.close_failure = False

    def volume_info(self, drive):
        if drive != "C:":
            raise OSError("unknown")
        return self.volume

    def open_existing(self, path, access, share, flags):
        self.open_calls.append((path, access, share, flags))
        if path.casefold() in self.open_error:
            raise self.open_error[path.casefold()]
        node = self.fs.root
        for name in filter(None, path[3:].split("\\")):
            node = node.children.get(name.casefold())
            if node is None:
                if self.on_missing:
                    self.on_missing(path)
                raise FileNotFoundError("fake")
        self.next_handle += 1
        raw = self.next_handle
        self.live[raw] = node
        if self.after_open:
            self.after_open(path, node)
        return raw

    def close(self, raw):
        if raw not in self.live:
            raise AssertionError("duplicate or unknown close")
        self.close_calls.append(raw)
        del self.live[raw]
        if self.close_failure:
            raise OSError("fake close")

    def file_info(self, raw):
        obj = self.live[raw].obs
        flags = (0x10 if obj.kind == "directory" else 0) | (0x400 if obj.reparse else 0)
        return obj.volume, int.from_bytes(obj.file_id, "big"), obj.links, flags

    def read_observation(self, raw, kind, required):
        node = self.live[raw]
        self.acl_checks.append((node, required))
        if node.observe_error:
            raise node.observe_error
        if self.on_acl:
            self.on_acl(raw, node)
        entries = []
        for sid in (self.USER, acl._SYSTEM, acl._ADMINISTRATORS):
            raw_ace = struct.pack("<BBHI", 0, 0, 8 + len(sid), acl.FILE_ALL_ACCESS) + sid
            entries.append(acl._parse_ace(raw_ace))
        mask = getattr(node, "public_mask", 0)
        if mask:
            sid = getattr(node, "public_sid", acl._BUILTIN_USERS)
            flags = getattr(node, "public_flags", 0)
            raw_ace = struct.pack("<BBHI", 0, flags, 8 + len(sid), mask) + sid
            entries.append(acl._parse_ace(raw_ace))
        total = 8 + sum(len(a.raw) for a in entries)
        owner = getattr(node, "owner", self.USER)
        allowed = getattr(node, "allowed_access", acl.FILE_ALL_ACCESS)
        granted = required & allowed
        return acl.Observation(
            self.file_info(raw), owner, acl._ADMINISTRATORS, self.USER, "process",
            getattr(node, "token", b"fake token"), 0x9004, 1, True, False,
            struct.pack("<BBHHH", 2, 0, total, len(entries), 0), tuple(entries),
            required, granted, granted == required, 0)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.io = FakeWin32()
        self.backend = WindowsPathBackend(win32=self.io, acl_backend=self.io)

    def error(self, call):
        with self.assertRaises(PathEvidenceError):
            call()
        self.assertEqual(self.io.live, {})
        self.assertEqual(len(self.io.close_calls), len(set(self.io.close_calls)))

    def test_adapter_success_flags_acl_and_reopen(self):
        self.io.fs.root.public_mask = acl.FILE_GENERIC_READ | acl.FILE_GENERIC_EXECUTE
        self.io.fs.parent.public_mask = acl.FILE_GENERIC_READ | acl.FILE_GENERIC_EXECUTE
        with inspect(r"C:\Safe\data.json", backend=self.backend) as lease:
            self.assertTrue(lease.evidence.exists)
            self.assertEqual(lease.evidence.components[-1].observation.file_id,
                             (3).to_bytes(8, "big"))
            lease.revalidate()
            self.assertEqual(len(self.io.live), 3)
            self.assertGreater(len(self.io.open_calls), 6)
            for _, access, share, flags in self.io.open_calls:
                self.assertEqual(access, 0x20081)
                self.assertEqual(share, 3)
                self.assertEqual(flags, 0x02200000)
        self.assertEqual(self.io.live, {})
        self.assertEqual(len(self.io.close_calls), len(set(self.io.close_calls)))

    def test_missing_creation_parent_and_private_acl(self):
        with inspect(r"C:\Safe\missing", backend=self.backend) as lease:
            self.assertTrue(lease.evidence.direct_parent_observed)
            self.assertEqual(lease.evidence.components[-1].policy, "creation_parent")
            lease.revalidate()
        self.io.fs.parent.public_mask = 0x2  # untrusted FILE_ADD_FILE
        self.error(lambda: inspect(r"C:\Safe\missing", backend=self.backend))
        self.io.fs.parent.public_mask = 0
        self.io.fs.target.public_mask = acl.FILE_GENERIC_READ
        self.error(lambda: inspect(r"C:\Safe\data.json", backend=self.backend))

    def test_creation_access_is_checked_for_the_first_missing_component(self):
        # Traverse/list/read are allowed but FILE_ADD_FILE is denied.
        self.io.fs.parent.allowed_access = acl.FILE_ALL_ACCESS & ~0x2
        self.error(lambda: inspect(r"C:\Safe\missing", backend=self.backend))
        # A missing intermediate directory needs FILE_ADD_SUBDIRECTORY instead.
        with inspect(r"C:\Safe\newdir\child", backend=self.backend) as lease:
            self.assertEqual(lease.evidence.components[-1].creation_kind, "directory")
        self.io.fs.parent.allowed_access = acl.FILE_ALL_ACCESS & ~0x4
        self.error(lambda: inspect(r"C:\Safe\newdir\child", backend=self.backend))
        with inspect(r"C:\Safe\missing", backend=self.backend) as lease:
            self.assertEqual(lease.evidence.components[-1].creation_kind, "file")
        self.assertEqual(self.io.live, {})

    def test_everyone_ancestor_and_creation_parent_keep_separate_creation_checks(self):
        for node in (self.io.fs.root, self.io.fs.parent):
            node.public_sid = acl._EVERYONE
            node.public_mask = 0x1200A9
            node.public_flags = 0x13
        for path, expected_kind, create_bit in (
                (r"C:\Safe\missing", "file", 0x2),
                (r"C:\Safe\newdir\child", "directory", 0x4)):
            with self.subTest(kind=expected_kind):
                self.io.acl_checks.clear()
                with inspect(path, backend=self.backend) as lease:
                    self.assertEqual(lease.evidence.components[-1].policy, "creation_parent")
                    self.assertEqual(lease.evidence.components[-1].creation_kind, expected_kind)
                    lease.revalidate()
                masks = [mask for node, mask in self.io.acl_checks if node is self.io.fs.parent]
                self.assertIn(0x200A1, masks)
                self.assertIn(0x200A1 | create_bit, masks)
                self.io.fs.parent.allowed_access = acl.FILE_ALL_ACCESS & ~create_bit
                self.error(lambda: inspect(path, backend=self.backend))
                self.io.fs.parent.allowed_access = acl.FILE_ALL_ACCESS
        self.assertEqual(self.io.live, {})

    def test_everyone_creation_parent_unsafe_and_private_targets_still_reject(self):
        self.io.fs.parent.public_sid = acl._EVERYONE
        for bit in (0x2, 0x4, 0x10, 0x40, 0x100, 0x10000, 0x40000,
                    0x80000, 0x02000000, 0x40000000, 0x10000000):
            with self.subTest(bit=hex(bit)):
                self.io.fs.parent.public_mask = 0x1200A9 | bit
                self.error(lambda: inspect(r"C:\Safe\missing", backend=self.backend))
        self.io.fs.parent.public_mask = 0x1200A9
        self.error(lambda: inspect(r"C:\Safe", backend=self.backend, kind="directory"))
        self.io.fs.target.public_sid = acl._EVERYONE
        self.io.fs.target.public_mask = 0x1200A9
        self.error(lambda: inspect(r"C:\Safe\data.json", backend=self.backend))

    def test_parent_swap_during_absolute_open_is_detected_and_child_closed(self):
        def replace_parent(path, _):
            if path.lower().endswith(r"\safe\data.json"):
                replacement = Node(observation(40))
                replacement.add("data.json", self.io.fs.target)
                self.io.fs.root.add("Safe", replacement)
                self.io.after_open = None
        self.io.after_open = replace_parent
        self.error(lambda: inspect(r"C:\Safe\data.json", backend=self.backend))

    def test_deep_ancestor_swap_is_detected_by_overall_revalidation(self):
        middle = self.io.fs.parent.add('inner', Node(observation(50)))
        middle.add('data.json', self.io.fs.target)
        def replace_ancestor(path, _):
            if path.lower().endswith(r"\safe\inner\data.json"):
                replacement = Node(observation(40))
                # Immediate parent stays the same; the full rewalk must still
                # notice that an earlier ancestor changed.
                replacement.add('inner', middle)
                self.io.fs.root.add('Safe', replacement)
                self.io.after_open = None
        self.io.after_open = replace_ancestor
        self.error(lambda: inspect(r"C:\Safe\inner\data.json", backend=self.backend))

    def test_parent_dangerous_acl_after_child_open_rejects_and_closes(self):
        def make_unsafe(path, _):
            if path.lower().endswith(r"\safe\data.json"):
                self.io.fs.parent.public_sid = acl._EVERYONE
                self.io.fs.parent.public_mask = 0x2  # FILE_ADD_FILE
                self.io.after_open = None
        self.io.after_open = make_unsafe
        self.error(lambda: inspect(r"C:\Safe\data.json", backend=self.backend))

    def test_acl_reads_scale_linearly_with_path_length(self):
        for depth in (0, 4, 8, 12):
            with self.subTest(depth=depth):
                io = FakeWin32()
                parent = io.fs.parent
                parts = []
                for index in range(depth):
                    name = 'd' + str(index)
                    parts.append(name)
                    parent = parent.add(name, Node(observation(10 + index)))
                parent.add('target', Node(observation(80, 'file')))
                path = 'C:\\Safe\\' + '\\'.join(parts + ['target'])
                backend = WindowsPathBackend(win32=io, acl_backend=io)
                with inspect(path, backend=backend, kind='file', single_link=True) as lease:
                    components = len(lease.evidence.components)
                    inspect_reads = len(io.acl_checks)
                    lease.revalidate()
                    revalidate_reads = len(io.acl_checks) - inspect_reads
                    # Counts are actual policy/backend reads in this fake,
                    # not claims about native Windows performance.
                    self.assertLessEqual(inspect_reads, 24 * components + 16)
                    self.assertLessEqual(revalidate_reads, 14 * components + 8)
                self.assertEqual(io.live, {})

    def test_revalidation_rejects_new_untrusted_private_or_ancestor_rights(self):
        for role in ('private_target', 'ancestor_write'):
            with self.subTest(role=role):
                io = FakeWin32()
                backend = WindowsPathBackend(win32=io, acl_backend=io)
                with inspect(r"C:\Safe\data.json", backend=backend) as lease:
                    node = io.fs.target if role == 'private_target' else io.fs.parent
                    node.public_sid = acl._EVERYONE
                    node.public_mask = 0x1200A9 if role == 'private_target' else 0x2
                    with self.assertRaises(PathEvidenceError):
                        lease.revalidate()
                    with self.assertRaises(PathEvidenceError) as invalid:
                        lease.borrowed_component_handle(0)
                    self.assertEqual(invalid.exception.code, 'lease_invalid')
                self.assertEqual(io.live, {})
                self.assertEqual(len(io.close_calls), len(set(io.close_calls)))

    def test_parent_swap_during_missing_result_is_not_treated_as_absence(self):
        def replace_parent(path):
            if path.lower().endswith(r"\safe\missing"):
                self.io.fs.root.add("Safe", Node(observation(40)))
                self.io.on_missing = None
        self.io.on_missing = replace_parent
        self.error(lambda: inspect(r"C:\Safe\missing", backend=self.backend))

    def test_parent_disappearance_during_binding_is_not_child_absence(self):
        def remove_parent(path):
            if path.lower().endswith(r"\safe\missing"):
                del self.io.fs.root.children["safe"]
                self.io.on_missing = None
        self.io.on_missing = remove_parent
        with self.assertRaises(PathEvidenceError) as caught:
            inspect(r"C:\Safe\missing", backend=self.backend)
        self.assertEqual(caught.exception.code, "open_unavailable")
        self.assertEqual(self.io.live, {})

    def test_different_file_id_replacement_on_revalidate(self):
        with inspect(r"C:\Safe\data.json", backend=self.backend) as lease:
            replacement = Node(observation(40))
            replacement.add("data.json", self.io.fs.target)
            self.io.fs.root.add("Safe", replacement)
            with self.assertRaises(PathEvidenceError):
                lease.revalidate()
            with self.assertRaises(PathEvidenceError):
                lease.borrowed_component_handle(0)
        self.assertEqual(self.io.live, {})

    def test_same_id_same_security_aba_is_not_claimed_detectable(self):
        # This deliberately violates normal no-delete-sharing assumptions.
        # It documents that an adversarial same-ID/same-ACL ABA cannot be
        # distinguished by these observations, not an OS security property.
        with inspect(r"C:\Safe\data.json", backend=self.backend) as lease:
            replacement = Node(self.io.fs.parent.obs)
            replacement.add("data.json", self.io.fs.target)
            self.io.fs.root.add("Safe", replacement)
            lease.revalidate()
        self.assertEqual(self.io.live, {})

    def test_remote_unknown_subst_volume_and_mismatch_reject(self):
        for volume in ((4, "NTFS", 7, r"\Device\HarddiskVolume4"),
                       (3, "FAT32", 7, r"\Device\HarddiskVolume4"),
                       (3, "NTFS", 7, r"\??\C:\subdir"),
                       (3, "NTFS", 8, r"\Device\HarddiskVolume4"),
                       None):
            with self.subTest(volume=volume):
                self.io.volume = volume
                self.error(lambda: inspect(r"C:\Safe\data.json", backend=self.backend))

    def test_reparse_access_denial_unc_and_hardlink_fail_closed(self):
        self.io.fs.parent.obs = replace(self.io.fs.parent.obs, reparse=True)
        self.error(lambda: inspect(r"C:\Safe\data.json", backend=self.backend))
        self.io.fs.parent.obs = replace(self.io.fs.parent.obs, reparse=False)
        self.io.open_error[r"c:\safe\data.json"] = PermissionError("fake")
        self.error(lambda: inspect(r"C:\Safe\data.json", backend=self.backend))
        self.io.open_error.clear()
        self.io.fs.target.obs = replace(self.io.fs.target.obs, links=2)
        # Current windows_acl rejects file hardlinks for all file evidence.
        self.error(lambda: inspect(r"C:\Safe\data.json", backend=self.backend))
        prior = len(self.io.open_calls)
        self.error(lambda: inspect(r"\\server\share\file", backend=self.backend))
        self.assertEqual(len(self.io.open_calls), prior)

    def test_acl_changes_between_reads_and_unknown_owner_fail(self):
        count = 0
        def change_acl(raw, node):
            nonlocal count
            if node is self.io.fs.parent:
                count += 1
                if count == 2:
                    node.token = b"changed fake token"
        self.io.on_acl = change_acl
        self.error(lambda: inspect(r"C:\Safe\data.json", backend=self.backend))
        self.io.on_acl = None
        self.io.fs.parent.owner = bytes.fromhex("0102000000000005150000003a050000")
        self.error(lambda: inspect(r"C:\Safe\data.json", backend=self.backend))

    def test_native_acl_release_failure_is_not_reported_as_observation_error(self):
        def fail_release(raw, node):
            if node is self.io.fs.target:
                raise acl.ACLVerificationError('resource_release_failed')
        self.io.on_acl = fail_release
        with self.assertRaises(PathEvidenceError) as caught:
            inspect(r"C:\Safe\data.json", backend=self.backend)
        self.assertEqual(caught.exception.code, 'resource_release_failed')
        self.assertEqual(self.io.live, {})
        self.assertEqual(len(self.io.close_calls), len(set(self.io.close_calls)))

    def test_interrupted_child_cleanup_releases_remaining_handles(self):
        original_close = self.io.close
        def replace_parent(path, _):
            if path.lower().endswith(r"\safe\data.json"):
                self.io.fs.root.add('Safe', Node(observation(40)))
                self.io.after_open = None
        def close_once(raw):
            if self.io.live[raw] is self.io.fs.target:
                original_close(raw)
                raise KeyboardInterrupt('fake sensitive OS detail')
            original_close(raw)
        self.io.after_open = replace_parent
        self.io.close = close_once
        with self.assertRaises(KeyboardInterrupt) as caught:
            inspect(r"C:\Safe\data.json", backend=self.backend)
        self.assertEqual(str(caught.exception), '')
        self.assertEqual(caught.exception.code, 'close_failed')
        self.assertTrue(caught.exception.release_unknown)
        self.assertEqual(self.io.live, {})
        self.assertEqual(len(self.io.close_calls), len(set(self.io.close_calls)))

    def test_close_failure_does_not_double_close_any_handle(self):
        self.io.close_failure = True
        with self.assertRaises(PathEvidenceError) as caught:
            inspect(r"C:\Safe\data.json", backend=self.backend)
        self.assertEqual(caught.exception.code, 'close_failed')
        self.assertEqual(self.io.live, {})
        self.assertEqual(len(self.io.close_calls), len(set(self.io.close_calls)))

    def test_no_native_api_load_on_mac(self):
        import os
        if os.name != "nt":
            with self.assertRaises(OSError):
                NativeWin32()


if __name__ == "__main__":
    unittest.main()
