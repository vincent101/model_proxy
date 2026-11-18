"""Fake-only trust-boundary tests; this is not native Windows evidence."""
from __future__ import annotations

import unittest
from dataclasses import replace

from core import windows_acl as acl
from core.windows_paths import PathEvidenceError, WindowsPathBackend, inspect, may_alias, may_contain
from test_windows_paths import FakeWin32, MemoryBackend, Node, observation


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.io = FakeWin32()
        self.root = self.io.fs.root
        self.shared = self.root.add("Shared", Node(observation(10)))
        self.work = self.shared.add("Work", Node(observation(11)))
        self.project = self.work.add("Project", Node(observation(12)))
        self.parent = self.project.add("config", Node(observation(13)))
        self.target = self.parent.add("secret.json", Node(observation(14, "file")))
        self.boundary = r"C:\Shared\Work\Project"
        self.file = self.boundary + r"\config\secret.json"
        self.backend = WindowsPathBackend(win32=self.io, acl_backend=self.io)
        # Deliberately model both reported diagnostic shapes, without
        # interpreting these ACEs as the effective rights of other users.
        self.root.owner = bytes.fromhex("0102000000000005150000003a050000")
        self.shared.public_mask = acl.FILE_ALL_ACCESS
        self.shared.public_sid = acl._EVERYONE
        self.shared.public_flags = acl.INHERIT_ONLY_ACE | acl.INHERITED_ACE
        self.work.public_mask = acl.FILE_GENERIC_WRITE
        self.work.public_flags = acl.INHERITED_ACE

    def inspect(self, path=None, **kw):
        return inspect(path or self.file, backend=self.backend,
                       trusted_boundary=self.boundary, **kw)

    def error(self, action, code=None):
        with self.assertRaises(PathEvidenceError) as caught:
            action()
        if code is not None:
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.io.live, {})
        self.assertEqual(len(self.io.close_calls), len(set(self.io.close_calls)))

    def test_only_explicit_boundary_skips_high_ancestor_acl(self):
        self.error(lambda: inspect(self.file, backend=self.backend))
        self.io.acl_checks.clear()
        with self.inspect(single_link=True) as lease:
            self.assertEqual([x.policy for x in lease.evidence.components],
                             ["traverse", "traverse", "traverse",
                              "ancestor", "ancestor", "private"])
            lease.revalidate()
            nodes = [node for node, _ in self.io.acl_checks]
            for node in (self.root, self.shared, self.work):
                self.assertNotIn(node, nodes)
            for node in (self.project, self.parent, self.target):
                self.assertIn(node, nodes)
        self.assertFalse(self.io.live)

    def test_boundary_and_direct_parent_and_target_stay_strict(self):
        for node in (self.project, self.parent):
            with self.subTest(node=node.obs.file_id):
                node.public_mask = acl.FILE_GENERIC_WRITE
                self.error(self.inspect)
                del node.public_mask
        self.target.public_mask = acl.FILE_GENERIC_READ
        self.error(self.inspect)
        del self.target.public_mask
        self.target.allowed_access = 0
        self.error(self.inspect)

    def test_missing_target_checks_effective_create_and_parent_acl(self):
        missing = self.boundary + r"\config\new.json"
        with self.inspect(missing) as lease:
            self.assertEqual(lease.evidence.missing, ("new.json",))
            self.assertEqual(lease.evidence.components[-1].policy, "creation_parent")
            self.assertEqual(lease.evidence.components[-1].creation_kind, "file")
        self.parent.allowed_access = acl.FILE_ALL_ACCESS & ~0x2
        self.error(lambda: self.inspect(missing))
        del self.parent.allowed_access
        self.parent.public_mask = 0x2
        self.error(lambda: self.inspect(missing))

    def test_project_entry_and_first_runtime_directory(self):
        with self.inspect(self.boundary, kind="directory", private=False) as project:
            self.assertTrue(project.evidence.exists)
            self.assertEqual(project.evidence.components[-1].policy, "ancestor")
        with self.inspect(self.boundary + r"\.runtime", kind="directory") as runtime:
            self.assertEqual(runtime.evidence.missing, (".runtime",))
            self.assertEqual(runtime.evidence.components[-1].creation_kind, "directory")
            self.assertTrue(runtime.evidence.direct_parent_observed)
        self.project.allowed_access = acl.FILE_ALL_ACCESS & ~0x4
        self.error(lambda: self.inspect(self.boundary + r"\.runtime", kind="directory"))

    def test_related_chains_with_same_boundary_have_comparable_observations(self):
        with self.inspect(self.boundary, kind="directory", private=False) as entry:
            with self.inspect() as target:
                prefix = target.evidence.components[:len(entry.evidence.components)]
                self.assertEqual([x.observation for x in entry.evidence.components],
                                 [x.observation for x in prefix])
        self.assertFalse(self.io.live)

    def test_explicit_external_direct_parent_boundary(self):
        external_parent = r"C:\Shared\Work"
        external = external_parent + r"\external.json"
        self.work.public_mask = acl.FILE_GENERIC_WRITE
        self.error(lambda: inspect(external, backend=self.backend,
                                   trusted_boundary=external_parent))
        del self.work.public_mask
        with inspect(external, backend=self.backend,
                     trusted_boundary=external_parent) as lease:
            self.assertEqual(lease.evidence.components[-1].policy, "creation_parent")
            self.assertTrue(lease.evidence.direct_parent_observed)
        self.assertFalse(self.io.live)

    def test_boundary_not_path_prefix_or_not_existing_fails(self):
        for boundary in (r"C:\Shared\Other", r"C:\Shared\Work\Project\other",
                         r"D:\Shared\Work", r"\\server\share", "C:\\"):
            with self.subTest(boundary=boundary):
                self.error(lambda: inspect(self.file, backend=self.backend,
                                           trusted_boundary=boundary))
        missing_boundary = r"C:\Shared\Work\Absent"
        self.error(lambda: inspect(missing_boundary + r"\child", backend=self.backend,
                                   trusted_boundary=missing_boundary),
                   "boundary_unavailable")
        self.error(lambda: inspect(self.file, backend=self.backend,
                                   trusted_boundary=self.file), "boundary_not_directory")
        self.error(lambda: inspect(r"\\host\share\file", backend=self.backend,
                                   trusted_boundary=self.boundary), "invalid_path")

    def test_boundary_spelling_is_exact_for_case_sensitive_directories(self):
        with inspect("c:" + self.file[2:], backend=self.backend,
                     trusted_boundary=self.boundary) as lease:
            self.assertTrue(lease.evidence.exists)
        for boundary in (r"C:\Shared\Work\PROJECT", r"C:\Shared\Work\PROJEC~1",
                         r"C:\Shared\Work\Projeçt"):
            self.error(lambda: inspect(self.file, backend=self.backend,
                                       trusted_boundary=boundary), "boundary_mismatch")
        # Unicode casefold is not used to authorize a different boundary spelling.
        self.error(lambda: inspect(r"C:\Shared\Work\Straße\x", backend=self.backend,
                                   trusted_boundary=r"C:\Shared\Work\STRASSE"),
                   "boundary_mismatch")

    def test_boundary_must_be_directory_reparse_and_hardlink_rejected(self):
        self.shared.obs = replace(self.shared.obs, reparse=True)
        self.error(self.inspect)
        self.shared.obs = replace(self.shared.obs, reparse=False)
        self.project.obs = replace(self.project.obs, reparse=True)
        self.error(self.inspect)
        self.project.obs = replace(self.project.obs, reparse=False, kind="file")
        self.error(self.inspect)
        self.project.obs = replace(self.project.obs, kind="directory")
        self.target.obs = replace(self.target.obs, links=2)
        self.error(lambda: self.inspect(single_link=True))
        self.target.obs = replace(self.target.obs, links=1, reparse=True)
        self.error(self.inspect)

    def test_identity_parent_or_target_replacement_fails_revalidation(self):
        for which in ("ancestor", "parent", "target"):
            with self.subTest(which=which):
                self.setUp()
                lease = self.inspect()
                if which == "ancestor":
                    replacement = Node(observation(30))
                    replacement.add("Work", self.work)
                    self.root.add("Shared", replacement)
                elif which == "parent":
                    replacement = Node(observation(31))
                    replacement.add("secret.json", self.target)
                    self.project.add("config", replacement)
                else:
                    self.parent.add("secret.json", Node(observation(32, "file")))
                with self.assertRaises(PathEvidenceError):
                    lease.revalidate()
                lease.close()
                self.assertEqual(self.io.live, {})

    def test_parent_swap_during_open_and_unknown_close_are_not_absence(self):
        def swap(path, _node):
            if path.lower() == self.file.lower():
                self.io.after_open = None
                self.project.add("config", Node(observation(55)))
        self.io.after_open = swap
        self.error(self.inspect)
        self.setUp()
        self.io.close_failure = True
        self.error(self.inspect, "close_failed")

    def test_unknown_open_and_identity_never_become_missing(self):
        self.io.open_error[self.file.lower()] = PermissionError("fake")
        self.error(self.inspect, "open_unavailable")
        self.io.open_error.clear()
        self.io.open_error[r"c:\shared"] = PermissionError("fake")
        self.error(self.inspect, "open_unavailable")
        self.io.open_error.clear()
        self.shared.obs = replace(self.shared.obs, file_id=b"")
        self.error(self.inspect, "observation_unavailable")

    def test_missing_name_aliases_remain_conservative(self):
        def lease(name):
            return self.inspect(self.boundary + "\\config\\" + name)
        for left, right, expected in (
            ("abc", "ABC", True), ("abc", "xyz", False),
            ("longname", "LONGNA~1", True), ("é", "other", True),
        ):
            with self.subTest(left=left, right=right):
                with lease(left) as first, lease(right) as second:
                    self.assertEqual(may_alias(first, second), expected)
        self.assertFalse(self.io.live)

    def test_existing_aliases_compare_physical_identity(self):
        # Fake protocol models hardlink identity; the actual adapter retains
        # its stricter existing single-link ACL rule.
        fs = MemoryBackend()
        fs.root.ancestor_allowed = False
        fs.parent.add("alias", fs.target)
        boundary = r"C:\Safe"
        with inspect(r"C:\Safe\data.json", backend=fs, trusted_boundary=boundary) as a:
            with inspect(r"C:\Safe\alias", backend=fs, trusted_boundary=boundary) as b:
                self.assertTrue(may_alias(a, b))
                self.assertTrue(may_contain(a, b))
        fs.assert_closed(self)


if __name__ == "__main__":
    unittest.main()
