"""Offline fake Win32 and policy tests; never touches a filesystem target."""
import ctypes
import struct
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from core import windows_acl as acl

USER = bytes.fromhex("01020000000000051500000039050000")
OTHER = bytes.fromhex("0102000000000005150000003a050000")
GROUP = acl._ADMINISTRATORS


def raw_ace(sid=USER, *, kind=0, flags=0, mask=acl.FILE_ALL_ACCESS):
    return struct.pack("<BBHI", kind, flags, 8 + len(sid), mask) + sid


def valid_observation(*, kind="file"):
    aces = tuple(acl._parse_ace(raw_ace(sid, flags=3)) for sid in (USER, acl._SYSTEM, GROUP))
    total = 8 + sum(len(a.raw) for a in aces)
    return acl.Observation(
        (1, 100, 1 if kind == "file" else 3, 0 if kind == "file" else 0x10),
        USER, GROUP, USER, "process", b"stable token statistics", 0x9004, 1,
        True, False, struct.pack("<BBHHH", 2, 0, total, len(aces), 0), aces,
        0x3, 0x3, True, 0)


class FakeBackend:
    def __init__(self, *items):
        self.items = list(items)
        self.handles = []

    def read_observation(self, handle, kind, required):
        self.handles.append((handle, kind, required))
        value = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(value, Exception):
            raise value
        return value


class ACLPolicyTests(unittest.TestCase):
    def test_private_dacl_allowed_and_same_handle_read_twice(self):
        observation = valid_observation()
        backend = FakeBackend(observation)
        evidence = acl.read_stable_evidence(123, kind="file", required_access=3, backend=backend)
        self.assertEqual(backend.handles, [(123, "file", 3), (123, "file", 3)])
        self.assertEqual(evidence.observation.aces, observation.aces)
        self.assertNotIn(USER.hex(), repr(evidence))
        acl.verify_unchanged(123, evidence, backend=backend)
        self.assertEqual(len(backend.handles), 4)

    def test_directory_and_inherited_entries(self):
        observation = valid_observation(kind="directory")
        entries = list(observation.aces)
        entries[1] = acl._parse_ace(raw_ace(acl._SYSTEM, flags=0x13))
        observation = replace(observation, aces=tuple(entries))
        evidence = acl.read_stable_evidence(99, kind="directory", required_access=3,
                                            backend=FakeBackend(observation))
        self.assertEqual(evidence.observation.aces[1].flags, 0x13)

    def test_missing_null_and_empty_are_not_interchangeable_or_accepted(self):
        original = valid_observation()
        cases = ((replace(original, dacl_present=False), "dacl_missing_or_null"),
                 (replace(original, dacl_null=True), "dacl_missing_or_null"),
                 (replace(original, aces=()), "dacl_empty"))
        for item, code in cases:
            with self.subTest(code=code), self.assertRaises(acl.ACLVerificationError) as caught:
                acl.read_stable_evidence(123, kind="file", required_access=3, backend=FakeBackend(item))
            self.assertEqual(caught.exception.code, code)

    def test_deny_unknown_conditional_and_audit_flags_reject_without_reorder(self):
        original = valid_observation()
        for ace_type in (1, 5, 9, 255):
            raw = raw_ace(USER, kind=ace_type)
            for index in (0, len(original.aces)):
                unknown = acl.Ace(ace_type, 0, acl.FILE_ALL_ACCESS, USER, raw)
                entries = list(original.aces)
                entries.insert(index, unknown)
                with self.subTest(type=ace_type, index=index), self.assertRaises(acl.ACLVerificationError) as caught:
                    acl.read_stable_evidence(123, kind="file", required_access=3,
                                             backend=FakeBackend(replace(original, aces=tuple(entries))))
                self.assertEqual(caught.exception.code, "ace_type_unsupported")
        with self.assertRaises(acl.ACLVerificationError) as caught:
            acl._parse_ace(raw_ace(USER, flags=0x40))
        self.assertEqual(caught.exception.code, "ace_flags_unsupported")

    def test_trusted_grantees_optional_but_effective_access_required(self):
        original = valid_observation()
        for entries in ((original.aces[0],), (original.aces[1],),
                        tuple(acl._parse_ace(raw_ace(a.sid, flags=0x0B)) for a in original.aces)):
            with self.subTest(entries=entries):
                acl.read_stable_evidence(123, kind="file", required_access=3,
                                         backend=FakeBackend(replace(original, aces=entries)))
                with self.assertRaises(acl.ACLVerificationError) as caught:
                    acl.read_stable_evidence(123, kind="file", required_access=3,
                        backend=FakeBackend(replace(original, aces=entries, access_granted=False, granted=0)))
                self.assertEqual(caught.exception.code, "access_unverified")

    def test_split_ordinary_aces_accumulate_without_reordering(self):
        original = valid_observation()
        entries = []
        for sid in (USER, acl._SYSTEM, GROUP):
            entries.extend((acl._parse_ace(raw_ace(sid, mask=1)), acl._parse_ace(raw_ace(sid, mask=2))))
        observation = replace(original, aces=tuple(entries))
        evidence = acl.read_stable_evidence(123, kind="file", required_access=3, backend=FakeBackend(observation))
        self.assertEqual(evidence.observation.aces, tuple(entries))

    def test_ancestor_accepts_known_user_groups_read_and_traverse_only(self):
        base = valid_observation(kind="directory")
        for sid in (acl._BUILTIN_USERS, acl._AUTHENTICATED_USERS):
            for mask in (1, 0x20, 0x80000000, 0x20000000, 0xA0000000):
                entry = acl._parse_ace(raw_ace(sid, flags=0x13, mask=mask))
                observation = replace(base, aces=base.aces + (entry,))
                with self.subTest(sid=sid, mask=mask):
                    backend = FakeBackend(observation)
                    evidence = acl.read_stable_evidence(99, kind="directory", required_access=3,
                                                        policy="ancestor", backend=backend)
                    self.assertEqual(evidence.policy, "ancestor")
                    acl.verify_unchanged(99, evidence, backend=backend)

    def test_ancestor_user_may_access_via_safe_group_only_if_accesscheck_succeeds(self):
        base = valid_observation(kind="directory")
        group = acl._parse_ace(raw_ace(acl._BUILTIN_USERS, mask=acl.FILE_GENERIC_READ))
        item = replace(base, aces=base.aces[1:] + (group,), desired=1, granted=1)
        evidence = acl.read_stable_evidence(99, kind="directory", required_access=1,
                                            policy="ancestor", backend=FakeBackend(item))
        self.assertEqual(evidence.policy, "ancestor")
        denied = replace(item, access_granted=False, granted=0)
        with self.assertRaises(acl.ACLVerificationError):
            acl.read_stable_evidence(99, kind="directory", required_access=1,
                                     policy="ancestor", backend=FakeBackend(denied))

    def test_wine_style_root_accepts_readonly_everyone_private_rejects_it(self):
        # Reproduce the trustee classes, flags and masks observed on the
        # Whisky test bottle. No actual account SID or path is recorded.
        required = 0x200A1
        entries = tuple(acl._parse_ace(raw_ace(sid, flags=3, mask=mask))
                        for sid, mask in ((acl._SYSTEM, acl.FILE_ALL_ACCESS),
                                          (USER, acl.FILE_ALL_ACCESS),
                                          (acl._EVERYONE, 0x1200A9)))
        root = replace(valid_observation(kind="directory"), aces=entries,
                       desired=required, granted=required)
        self.assertEqual(acl._map_mask(entries[2].mask) & ~acl._ANCESTOR_PUBLIC_READ, 0)
        acl.read_stable_evidence(99, kind="directory", required_access=required,
                                 policy="ancestor", backend=FakeBackend(root))
        with self.assertRaises(acl.ACLVerificationError) as caught:
            acl.read_stable_evidence(99, kind="directory", required_access=required,
                                     policy="private", backend=FakeBackend(root))
        self.assertEqual(caught.exception.code, "principal_untrusted")
        admin = acl._parse_ace(raw_ace(acl._ADMINISTRATORS, flags=3))
        accepted = replace(root, aces=entries + (admin,))
        acl.read_stable_evidence(99, kind="directory", required_access=required,
                                 policy="ancestor", backend=FakeBackend(accepted))

    def test_everyone_ancestor_generic_mapping_and_all_access_bits(self):
        base = valid_observation(kind="directory")
        self.assertEqual(acl._ANCESTOR_PUBLIC_READ, 0x1200A9)
        for generic, expected in ((0x80000000, 0x120089),
                                  (0x20000000, 0x1200A0),
                                  (0xA0000000, 0x1200A9),
                                  (0x40000000, 0x120116),
                                  (0x10000000, 0x1F01FF)):
            self.assertEqual(acl._map_mask(generic), expected)
        allowed = {0, 0x1200A9, 0xA0000000, 0x80000000, 0x20000000}
        allowed.update(1 << bit for bit in range(28) if 0x1200A9 & (1 << bit))
        # Each individual bit, including unknown bits and GENERIC_{READ,
        # WRITE, EXECUTE, ALL}, is decided only after generic mapping.
        for mask in [0, 0x1200A9, 0xA0000000, *(1 << bit for bit in range(32))]:
            for flags in (0, 0x03, 0x08, 0x10, 0x1F):
                entry = acl._parse_ace(raw_ace(acl._EVERYONE, flags=flags, mask=mask))
                item = replace(base, aces=base.aces + (entry,))
                with self.subTest(mask=hex(mask), flags=hex(flags)):
                    if mask not in allowed:
                        with self.assertRaises(acl.ACLVerificationError) as caught:
                            acl.read_stable_evidence(99, kind="directory", required_access=3,
                                                     policy="ancestor", backend=FakeBackend(item))
                        self.assertEqual(caught.exception.code, "ancestor_access_unsafe")
                    else:
                        acl.read_stable_evidence(99, kind="directory", required_access=3,
                                                 policy="ancestor", backend=FakeBackend(item))
        with self.assertRaises(acl.ACLVerificationError) as caught:
            acl._parse_ace(raw_ace(acl._EVERYONE, flags=0x20, mask=1))
        self.assertEqual(caught.exception.code, "ace_flags_unsupported")

    def test_everyone_private_file_and_directory_always_rejected(self):
        for kind in ("file", "directory"):
            for flags in (0, 0x08, 0x10, 0x1F):
                base = valid_observation(kind=kind)
                everyone = acl._parse_ace(raw_ace(acl._EVERYONE, flags=flags, mask=1))
                item = replace(base, aces=base.aces + (everyone,))
                with self.subTest(kind=kind, flags=flags), self.assertRaises(acl.ACLVerificationError) as caught:
                    acl.read_stable_evidence(99, kind=kind, required_access=3,
                                             policy="private", backend=FakeBackend(item))
                self.assertEqual(caught.exception.code, "principal_untrusted")

    def test_everyone_never_gets_trusted_rights_from_token_sid_collision(self):
        base = valid_observation(kind="directory")
        entries = tuple(acl._parse_ace(raw_ace(sid)) for sid in
                        (acl._EVERYONE, acl._SYSTEM, acl._ADMINISTRATORS))
        item = replace(base, owner=acl._EVERYONE, user=acl._EVERYONE, aces=entries)
        for policy, expected in (("ancestor", "ancestor_access_unsafe"),
                                 ("private", "principal_untrusted")):
            with self.subTest(policy=policy), self.assertRaises(acl.ACLVerificationError) as caught:
                acl.read_stable_evidence(99, kind="directory", required_access=3,
                                         policy=policy, backend=FakeBackend(item))
            self.assertEqual(caught.exception.code, expected)

    def test_everyone_does_not_bypass_owner_other_principals_or_accesscheck(self):
        base = valid_observation(kind="directory")
        entry = acl._parse_ace(raw_ace(acl._EVERYONE, flags=3, mask=0x1200A9))
        base = replace(base, aces=base.aces + (entry,))
        other = acl._parse_ace(raw_ace(OTHER, mask=1))
        cases = (
            (replace(base, owner=OTHER), "owner_untrusted"),
            (replace(base, owner=acl._EVERYONE), "owner_untrusted"),
            (replace(base, aces=base.aces + (other,)), "principal_untrusted"),
            (replace(base, granted=0, access_granted=False), "access_unverified"),
            (replace(base, privilege_count=1), "access_unverified"),
        )
        for item, code in cases:
            with self.subTest(code=code), self.assertRaises(acl.ACLVerificationError) as caught:
                acl.read_stable_evidence(99, kind="directory", required_access=3,
                                         policy="ancestor", backend=FakeBackend(item))
            self.assertEqual(caught.exception.code, code)

    def test_ancestor_rejects_group_create_delete_change_acl_owner_or_unknown_bits(self):
        base = valid_observation(kind="directory")
        for sid in (acl._BUILTIN_USERS, acl._AUTHENTICATED_USERS):
            for mask in (0x2, 0x4, 0x10, 0x40, 0x100, 0x10000, 0x40000, 0x80000,
                         0x40000000, 0x10000000, 0x02000000):
                for flags in (0, 0x0B):
                    entry = acl._parse_ace(raw_ace(sid, flags=flags, mask=mask))
                    observation = replace(base, aces=base.aces + (entry,))
                    with self.subTest(sid=sid, mask=mask, flags=flags), self.assertRaises(acl.ACLVerificationError) as caught:
                        acl.read_stable_evidence(99, kind="directory", required_access=3,
                                                 policy="ancestor", backend=FakeBackend(observation))
                    self.assertEqual(caught.exception.code, "ancestor_access_unsafe")

    def test_group_policy_is_not_valid_for_private_file_or_unknown_group(self):
        base = valid_observation(kind="directory")
        for sid in (acl._BUILTIN_USERS, acl._AUTHENTICATED_USERS, OTHER):
            entry = acl._parse_ace(raw_ace(sid, mask=1))
            item = replace(base, aces=base.aces + (entry,))
            with self.subTest(sid=sid), self.assertRaises(acl.ACLVerificationError):
                acl.read_stable_evidence(99, kind="directory", required_access=3,
                                         policy="private" if sid != OTHER else "ancestor",
                                         backend=FakeBackend(item))
        with self.assertRaises(acl.ACLVerificationError) as caught:
            acl.read_stable_evidence(99, kind="file", required_access=3, policy="ancestor",
                                     backend=FakeBackend(valid_observation()))
        self.assertEqual(caught.exception.code, "policy_invalid")

    def test_unknown_user_or_group_is_rejected_even_if_inherit_only(self):
        for flags in (0, 0x08, 0x10):
            other = acl._parse_ace(raw_ace(OTHER, flags=flags))
            observation = replace(valid_observation(), aces=(other,))
            with self.subTest(flags=flags), self.assertRaises(acl.ACLVerificationError) as caught:
                acl.read_stable_evidence(123, kind="file", required_access=3, backend=FakeBackend(observation))
            self.assertEqual(caught.exception.code, "principal_untrusted")

    def test_inherit_only_ace_does_not_override_failed_access_check(self):
        entry = acl._parse_ace(raw_ace(USER, flags=0x0B))
        observation = replace(valid_observation(), aces=(entry,), access_granted=False, granted=0)
        with self.assertRaises(acl.ACLVerificationError) as caught:
            acl.read_stable_evidence(1, kind="file", required_access=3, backend=FakeBackend(observation))
        self.assertEqual(caught.exception.code, "access_unverified")

    def test_untrusted_owner_and_invalid_sids(self):
        for observation in (replace(valid_observation(), owner=OTHER),
                            replace(valid_observation(), user=b"bad"),
                            replace(valid_observation(), group=b"bad")):
            with self.subTest(observation=observation), self.assertRaises(acl.ACLVerificationError):
                acl.read_stable_evidence(123, kind="file", required_access=3, backend=FakeBackend(observation))

    def test_reparse_hardlink_file_directory_and_missing_identity(self):
        base = valid_observation()
        for identity in ((1, 100, 1, 0x400), (1, 100, 2, 0), (1, 100, 1, 0x10),
                         (1, 0, 1, 0), (1, 100, 0, 0)):
            with self.subTest(identity=identity), self.assertRaises(acl.ACLVerificationError):
                acl.read_stable_evidence(123, kind="file", required_access=3,
                                         backend=FakeBackend(replace(base, identity=identity)))

    def test_directory_missing_link_identity_is_rejected(self):
        item = replace(valid_observation(kind="directory"), identity=(1, 100, 0, 0x10))
        with self.assertRaises(acl.ACLVerificationError) as caught:
            acl.read_stable_evidence(123, kind="directory", required_access=3,
                                     policy="ancestor", backend=FakeBackend(item))
        self.assertEqual(caught.exception.code, "identity_unknown")

    def test_acl_order_owner_identity_token_or_rights_drift_detected(self):
        first = valid_observation()
        variants = (replace(first, aces=tuple(reversed(first.aces))),
                    replace(first, owner=acl._SYSTEM),
                    replace(first, identity=(1, 101, 1, 0)),
                    replace(first, token_statistics=b"changed"),
                    replace(first, control=first.control ^ 0x1000))
        for second in variants:
            with self.subTest(second=second), self.assertRaises(acl.ACLVerificationError) as caught:
                acl.read_stable_evidence(123, kind="file", required_access=3,
                                         backend=FakeBackend(first, second))
            self.assertEqual(caught.exception.code, "evidence_changed")

    def test_reread_detects_change(self):
        first = valid_observation()
        evidence = acl.read_stable_evidence(123, kind="file", required_access=3, backend=FakeBackend(first))
        changed = replace(first, identity=(1, 101, 1, 0))
        with self.assertRaises(acl.ACLVerificationError) as caught:
            acl.verify_unchanged(123, evidence, backend=FakeBackend(changed))
        self.assertEqual(caught.exception.code, "evidence_changed")

    def test_reread_rejects_a_different_handle_even_for_same_identity(self):
        backend = FakeBackend(valid_observation())
        evidence = acl.read_stable_evidence(123, kind="file", required_access=3, backend=backend)
        with self.assertRaises(acl.ACLVerificationError) as caught:
            acl.verify_unchanged(124, evidence, backend=backend)
        self.assertEqual(caught.exception.code, "handle_changed")
        self.assertEqual(len(backend.handles), 2)

    def test_insufficient_rights_weak_mapping_and_privilege_dependency_reject(self):
        for changes in ({"access_granted": False}, {"granted": 1}, {"desired": 1},
                        {"privilege_count": 1}, {"token_statistics": b""}, {"token_source": "unknown"}):
            with self.subTest(changes=changes), self.assertRaises(acl.ACLVerificationError):
                acl.read_stable_evidence(123, kind="file", required_access=3,
                                         backend=FakeBackend(replace(valid_observation(), **changes)))

    def test_invalid_input_never_invokes_backend(self):
        backend = FakeBackend(valid_observation())
        cases = ((0, "file", 3), (-1, "file", 3), (123, "other", 3),
                 (123, "file", 0), (123, "file", 0x01000000), (123, "file", True))
        for handle, kind, access in cases:
            with self.subTest(handle=handle, kind=kind, access=access), self.assertRaises(acl.ACLVerificationError):
                acl.read_stable_evidence(handle, kind=kind, required_access=access, backend=backend)
        self.assertEqual(backend.handles, [])

    def test_backend_error_is_sanitized_and_does_not_retry(self):
        backend = FakeBackend(RuntimeError("FAKE_SECRET"))
        with self.assertRaises(acl.ACLVerificationError) as caught:
            acl.read_stable_evidence(123, kind="file", required_access=3, backend=backend)
        self.assertEqual(caught.exception.code, "evidence_unavailable")
        self.assertNotIn("FAKE_SECRET", str(caught.exception))
        self.assertEqual(len(backend.handles), 1)


class Function:
    def __init__(self, fn):
        self.fn = fn
    def __call__(self, *args):
        return self.fn(*args)


def put(ptr, typ, value):
    ctypes.cast(ptr, ctypes.POINTER(typ))[0] = value


def number(value):
    return value.value if hasattr(value, "value") else value


class FakeWin32:
    """In-memory Win32-shaped API. No file, token, or DLL exists."""
    def __init__(self):
        self.mem = ctypes.create_string_buffer(512)
        self.base = ctypes.addressof(self.mem)
        self.owner = self.base + 64
        self.group = self.base + 96
        self.dacl = self.base + 128
        self.aces = [raw_ace(USER, flags=3), raw_ace(acl._SYSTEM, flags=3), raw_ace(GROUP, flags=3)]
        self._layout()
        self.free_calls = []
        self.close_calls = []
        self.thread_ok = True
        self.duplicate_ok = True
        self.info_ok = True
        self.file_type = 1
        self.attributes = 0
        self.links = 1
        self.access_ok = True
        self.access_granted = True
        self.present = True
        self.null = False
        self.free_ok = True
        self.control = 0x9004
        self.statistics = b"A" * 56
        self.ace_calls = []
        self.check_calls = []
        self.sizes = []
        names = ["GetSecurityInfo", "GetSecurityDescriptorDacl", "GetSecurityDescriptorControl",
                 "GetSecurityDescriptorLength", "GetAclInformation", "GetAce", "OpenThreadToken",
                 "OpenProcessToken", "DuplicateTokenEx", "GetTokenInformation", "MapGenericMask", "AccessCheck"]
        self.advapi = SimpleNamespace(**{name: Function(getattr(self, name)) for name in names})
        names = ["GetFileInformationByHandle", "GetFileType", "GetCurrentThread", "GetCurrentProcess", "CloseHandle", "LocalFree"]
        self.kernel = SimpleNamespace(**{name: Function(getattr(self, name)) for name in names})
        self.backend = acl._NativeBackend(advapi=self.advapi, kernel=self.kernel)

    def _layout(self):
        ctypes.memmove(self.owner, USER, len(USER))
        ctypes.memmove(self.group, GROUP, len(GROUP))
        data = b"".join(self.aces)
        header = struct.pack("<BBHHH", 2, 0, 8 + len(data), len(self.aces), 0)
        ctypes.memmove(self.dacl, header + data, len(header) + len(data))

    def GetFileType(self, handle): return self.file_type
    def GetCurrentThread(self): return 900
    def GetCurrentProcess(self): return 901
    def OpenThreadToken(self, thread, access, self_access, out):
        self.sizes.append(("thread", thread, access, self_access))
        if not self.thread_ok: return 0
        put(out, acl.HANDLE, 101)
        return 1
    def OpenProcessToken(self, process, access, out):
        self.sizes.append(("process", process, access))
        put(out, acl.HANDLE, 101)
        return 1
    def DuplicateTokenEx(self, source, access, attrs, level, token_type, out):
        self.sizes.append(("duplicate", number(source), access, level, token_type))
        if not self.duplicate_ok: return 0
        put(out, acl.HANDLE, 102)
        return 1
    def GetTokenInformation(self, token, kind, buf, buflen, returned):
        if kind == 1:
            length = ctypes.sizeof(acl._SID_AND_ATTRIBUTES) + len(USER)
            put(returned, acl.DWORD, length)
            if buf is None: return 0
            addr = ctypes.addressof(buf)
            data = acl._SID_AND_ATTRIBUTES.from_buffer(buf)
            data.sid = addr + ctypes.sizeof(data)
            ctypes.memmove(data.sid, USER, len(USER))
        elif kind == 10:
            put(returned, acl.DWORD, len(self.statistics))
            if buf is None: return 0
            ctypes.memmove(ctypes.addressof(buf), self.statistics, len(self.statistics))
        else:
            return 0
        return 1
    def GetFileInformationByHandle(self, handle, out):
        if not self.info_ok: return 0
        info = ctypes.cast(out, ctypes.POINTER(acl._FILEINFO)).contents
        info.volume, info.index_low, info.links, info.attributes = 1, 100, self.links, self.attributes
        return 1
    def GetSecurityInfo(self, handle, object_type, flags, owner, group, dacl, sacl, descriptor):
        self.sizes.append(("security", handle, object_type, flags))
        put(owner, ctypes.c_void_p, self.owner)
        put(group, ctypes.c_void_p, self.group)
        put(dacl, ctypes.c_void_p, self.dacl if not self.null else None)
        put(descriptor, ctypes.c_void_p, self.base)
        return 0
    def GetSecurityDescriptorLength(self, descriptor): return 512
    def GetSecurityDescriptorControl(self, descriptor, control, revision):
        put(control, acl.WORD, self.control)
        put(revision, acl.DWORD, 1)
        return 1
    def GetSecurityDescriptorDacl(self, descriptor, present, dacl, defaulted):
        put(present, acl.BOOL, self.present)
        put(dacl, ctypes.c_void_p, None if self.null else self.dacl)
        put(defaulted, acl.BOOL, False)
        return 1
    def GetAclInformation(self, dacl, out, size, kind):
        obj = ctypes.cast(out, ctypes.POINTER(acl._ACL_SIZE_INFORMATION)).contents
        obj.count, obj.in_use, obj.free = len(self.aces), 8 + sum(map(len, self.aces)), 0
        self.sizes.append(("acl_info", size, kind))
        return 1
    def GetAce(self, dacl, index, out):
        self.ace_calls.append(index)
        offset = sum(len(a) for a in self.aces[:index])
        put(out, ctypes.c_void_p, self.dacl + 8 + offset)
        return 1
    def MapGenericMask(self, mask, mapping):
        p = ctypes.cast(mask, ctypes.POINTER(acl.DWORD))
        p[0] = acl._map_mask(p[0])
    def AccessCheck(self, descriptor, token, desired, mapping, privileges, length, granted, status):
        self.check_calls.append((number(token), number(desired)))
        if not self.access_ok: return 0
        put(length, acl.DWORD, 8)
        put(granted, acl.DWORD, number(desired) if self.access_granted else 0)
        put(status, acl.BOOL, self.access_granted)
        return 1
    def CloseHandle(self, handle):
        self.close_calls.append(number(handle))
        return 1
    def LocalFree(self, descriptor):
        self.free_calls.append(number(descriptor))
        return None if self.free_ok else 1


class NativeCallContractTests(unittest.TestCase):
    def test_private_native_path_uses_token_accesscheck_and_frees_all(self):
        win = FakeWin32()
        evidence = acl.read_stable_evidence(333, kind="file", required_access=3, backend=win.backend)
        self.assertEqual(evidence.observation.user, USER)
        self.assertEqual(evidence.observation.identity, (1, 100, 1, 0))
        self.assertEqual(win.free_calls, [win.base, win.base])
        self.assertEqual(win.close_calls, [102, 101, 102, 101])
        self.assertEqual(win.ace_calls, [0, 1, 2, 0, 1, 2])
        self.assertEqual(win.check_calls, [(102, 3), (102, 3)])
        self.assertIn(("security", 333, 1, 7), win.sizes)
        self.assertIn(("duplicate", 101, 8, 2, 2), win.sizes)
        self.assertIn(("acl_info", 12, 2), win.sizes)

    def test_directory_handle_path_with_same_handle_and_no_disk_io(self):
        win = FakeWin32()
        win.attributes, win.links = acl.FILE_ATTRIBUTE_DIRECTORY, 3
        evidence = acl.read_stable_evidence(333, kind="directory", required_access=3, backend=win.backend)
        self.assertEqual(evidence.observation.identity, (1, 100, 3, 0x10))

    def test_non_disk_handle_rejected_before_token_or_descriptor(self):
        for kind in (0, 2, 3):
            win = FakeWin32()
            win.file_type = kind
            with self.subTest(kind=kind), self.assertRaises(acl.ACLVerificationError):
                acl.read_stable_evidence(333, kind="file", required_access=3, backend=win.backend)
            self.assertEqual(win.sizes, [])
            self.assertEqual(win.close_calls, [])
            self.assertEqual(win.free_calls, [])

    def test_reparse_rejected_before_opening_token_or_querying_descriptor(self):
        win = FakeWin32()
        win.attributes = acl.FILE_ATTRIBUTE_REPARSE_POINT
        with self.assertRaises(acl.ACLVerificationError):
            acl.read_stable_evidence(333, kind="file", required_access=3, backend=win.backend)
        self.assertEqual(win.sizes, [])
        self.assertEqual(win.close_calls, [])
        self.assertEqual(win.free_calls, [])

    def test_token_change_during_duplication_is_detected(self):
        win = FakeWin32()
        old = win.DuplicateTokenEx
        def change_token(*args):
            result = old(*args)
            win.statistics = b"B" * 56
            return result
        win.advapi.DuplicateTokenEx.fn = change_token
        with self.assertRaises(acl.ACLVerificationError) as caught:
            acl.read_stable_evidence(333, kind="file", required_access=3, backend=win.backend)
        self.assertEqual(caught.exception.code, "evidence_changed")
        self.assertEqual(win.close_calls, [102, 101])
        self.assertEqual(win.free_calls, [])

    def test_process_fallback_only_no_token(self):
        win = FakeWin32()
        win.thread_ok = False
        with patch.object(ctypes, "get_last_error", return_value=1008, create=True):
            evidence = acl.read_stable_evidence(333, kind="file", required_access=3, backend=win.backend)
        self.assertEqual(evidence.observation.token_source, "process")
        self.assertIn(("process", 901, 10), win.sizes)
        win = FakeWin32()
        win.thread_ok = False
        with patch.object(ctypes, "get_last_error", return_value=5, create=True):
            with self.assertRaises(acl.ACLVerificationError):
                acl.read_stable_evidence(333, kind="file", required_access=3, backend=win.backend)
        self.assertFalse(any(x[0] == "process" for x in win.sizes))

    def test_generic_access_is_mapped_before_accesscheck(self):
        win = FakeWin32()
        evidence = acl.read_stable_evidence(333, kind="file", required_access=0x80000000, backend=win.backend)
        self.assertEqual(evidence.observation.desired, acl.FILE_GENERIC_READ)
        self.assertEqual(win.check_calls, [(102, acl.FILE_GENERIC_READ)] * 2)

    def test_bad_dacl_and_access_errors_release_all_resources(self):
        for setting in ("null", "present", "access_ok", "access_granted"):
            win = FakeWin32()
            setattr(win, setting, setting == "null")
            with self.subTest(setting=setting), self.assertRaises(acl.ACLVerificationError):
                acl.read_stable_evidence(333, kind="file", required_access=3, backend=win.backend)
            self.assertEqual(win.close_calls, [102, 101])
            self.assertEqual(win.free_calls, [win.base])

    def test_file_identity_drift_during_one_read_fails_and_releases(self):
        win = FakeWin32()
        original = win.GetFileInformationByHandle
        calls = 0
        def changed(handle, out):
            nonlocal calls
            calls += 1
            result = original(handle, out)
            if calls == 2:
                ctypes.cast(out, ctypes.POINTER(acl._FILEINFO)).contents.index_low = 101
            return result
        win.kernel.GetFileInformationByHandle.fn = changed
        with self.assertRaises(acl.ACLVerificationError) as caught:
            acl.read_stable_evidence(333, kind="file", required_access=3, backend=win.backend)
        self.assertEqual(caught.exception.code, "evidence_changed")
        self.assertEqual(win.close_calls, [102, 101])
        self.assertEqual(win.free_calls, [win.base])

    def test_dacl_change_between_individually_safe_observations_is_detected(self):
        win = FakeWin32()
        original = win.GetSecurityInfo
        calls = 0
        def change_between_reads(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                win.aces[1] = raw_ace(acl._SYSTEM, flags=3, mask=3)
                win._layout()
            return original(*args)
        win.advapi.GetSecurityInfo.fn = change_between_reads
        with self.assertRaises(acl.ACLVerificationError) as caught:
            acl.read_stable_evidence(333, kind="file", required_access=3, backend=win.backend)
        self.assertEqual(caught.exception.code, "evidence_changed")
        self.assertEqual(win.close_calls, [102, 101, 102, 101])
        self.assertEqual(win.free_calls, [win.base, win.base])

    def test_descriptor_error_with_allocated_buffer_frees_it(self):
        win = FakeWin32()
        original = win.GetSecurityInfo
        def fail_after_allocation(*args):
            original(*args)
            return 5
        win.advapi.GetSecurityInfo.fn = fail_after_allocation
        with self.assertRaises(acl.ACLVerificationError):
            acl.read_stable_evidence(333, kind="file", required_access=3, backend=win.backend)
        self.assertEqual(win.close_calls, [102, 101])
        self.assertEqual(win.free_calls, [win.base])

    def test_out_of_descriptor_sid_and_invalid_control_rejected(self):
        for bad in ("owner", "control"):
            win = FakeWin32()
            if bad == "owner":
                win.owner = win.base + 511
            else:
                win.control = 0x9000  # SE_DACL_PRESENT missing
            with self.subTest(bad=bad), self.assertRaises(acl.ACLVerificationError):
                acl.read_stable_evidence(333, kind="file", required_access=3, backend=win.backend)
            self.assertEqual(win.check_calls, [])
            self.assertEqual(win.close_calls, [102, 101])
            self.assertEqual(win.free_calls, [win.base])

    def test_invalid_ace_pointer_rejected_before_dereference(self):
        win = FakeWin32()
        def bad_pointer(dacl, index, out):
            put(out, ctypes.c_void_p, win.base + 999999)
            return 1
        win.advapi.GetAce.fn = bad_pointer
        with self.assertRaises(acl.ACLVerificationError):
            acl.read_stable_evidence(333, kind="file", required_access=3, backend=win.backend)
        self.assertEqual(win.check_calls, [])
        self.assertEqual(win.close_calls, [102, 101])
        self.assertEqual(win.free_calls, [win.base])

    def test_duplicate_failure_closes_original_token(self):
        win = FakeWin32()
        win.duplicate_ok = False
        with self.assertRaises(acl.ACLVerificationError):
            acl.read_stable_evidence(333, kind="file", required_access=3, backend=win.backend)
        self.assertEqual(win.close_calls, [101])
        self.assertEqual(win.free_calls, [])

    def test_localfree_failure_reports_release_failure_and_closes_tokens(self):
        win = FakeWin32()
        win.free_ok = False
        with self.assertRaises(acl.ACLVerificationError) as caught:
            acl.read_stable_evidence(333, kind="file", required_access=3, backend=win.backend)
        self.assertEqual(caught.exception.code, "resource_release_failed")
        self.assertEqual(win.close_calls, [102, 101])

    def test_malformed_ace_and_unknown_type_fail_before_accesscheck(self):
        for raw in (raw_ace(USER, kind=1), b"\x00\x00\x04\x00"):
            win = FakeWin32()
            win.aces = [raw]
            win._layout()
            with self.subTest(raw=raw), self.assertRaises(acl.ACLVerificationError):
                acl.read_stable_evidence(333, kind="file", required_access=3, backend=win.backend)
            self.assertEqual(win.check_calls, [])
            self.assertEqual(win.close_calls, [102, 101])
            self.assertEqual(win.free_calls, [win.base])

    def test_non_windows_without_injection_is_rejected(self):
        with patch.object(acl.os, "name", "posix"):
            with self.assertRaises(acl.ACLVerificationError) as caught:
                acl.read_stable_evidence(333, kind="file", required_access=3)
        self.assertEqual(caught.exception.code, "platform_unavailable")


if __name__ == "__main__":
    unittest.main()
