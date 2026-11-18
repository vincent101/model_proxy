"""Conservative read-only Windows owner/DACL evidence.

The caller supplies a *held native file/directory HANDLE* opened with
READ_CONTROL and FILE_READ_ATTRIBUTES, without following the final reparse
point; directories require backup semantics. Keep it open through the later
operation. Parent chains, path-to-handle binding, file contents, SACL/MIC,
privileged users, and filesystem trust are outside this module's proof.

For private objects only the effective token user, LOCAL SYSTEM, and BUILTIN
Administrators are permitted. Ancestor directories additionally permit BUILTIN
Users, Authenticated Users and Everyone, but only read/traverse rights. Unknown
ACE types, all deny ACEs, and other allowed principals (including CREATOR OWNER)
fail closed, including inherit-only ACEs. Inherited ACEs retain their original ordering/flags. A real
AccessCheck against the effective token must independently grant the requested
rights; evaluating one token never proves that other tokens lack access.
SYSTEM and Administrators are trusted if present; neither is a required
explicit grantee. AccessCheck separately confirms the effective user's rights,
including via safe ancestor groups. Two identical observations detect changes
between reads, not change-and-revert or subsequent
races. The injection hook is for tests, never security evidence. Native Windows
behavior has NOT been tested.
"""
from __future__ import annotations

import ctypes
import os
import struct
from dataclasses import dataclass, field
from typing import Any

DWORD = ctypes.c_uint32
WORD = ctypes.c_uint16
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p

# File GENERIC_MAPPING values from the local Windows-compatible Go bindings.
FILE_ALL_ACCESS = 0x001F01FF
FILE_GENERIC_READ = 0x00120089
FILE_GENERIC_WRITE = 0x00120116
FILE_GENERIC_EXECUTE = 0x001200A0
GENERIC_BITS = 0xF0000000
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
INHERIT_ONLY_ACE = 0x08
INHERITED_ACE = 0x10
_SYSTEM = bytes.fromhex("010100000000000512000000")
_ADMINISTRATORS = bytes.fromhex("01020000000000052000000020020000")
_BUILTIN_USERS = bytes.fromhex("01020000000000052000000021020000")
_AUTHENTICATED_USERS = bytes.fromhex("01010000000000050b000000")
_EVERYONE = bytes.fromhex("010100000000000100000000")
# List/read attributes/read EA/traverse, READ_CONTROL, SYNCHRONIZE only.
_ANCESTOR_PUBLIC_READ = FILE_GENERIC_READ | FILE_GENERIC_EXECUTE


class ACLVerificationError(RuntimeError):
    """Sanitized failure; never includes SID, path, or Windows error text."""
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Ace:
    ace_type: int
    flags: int
    mask: int
    sid: bytes = field(repr=False)
    raw: bytes = field(repr=False)


@dataclass(frozen=True)
class Observation:
    """A complete, ordered observation from one handle and one effective token."""
    identity: tuple[int, int, int, int]
    owner: bytes = field(repr=False)
    group: bytes = field(repr=False)
    user: bytes = field(repr=False)
    token_source: str
    token_statistics: bytes = field(repr=False)
    control: int
    revision: int
    dacl_present: bool
    dacl_null: bool
    acl_header: bytes = field(repr=False)
    aces: tuple[Ace, ...] = field(repr=False)
    desired: int
    granted: int
    access_granted: bool
    privilege_count: int


@dataclass(frozen=True)
class ACLEvidence:
    """Bound to the still-held HANDLE; not a path or reusable authorization."""
    handle: int = field(repr=False)
    kind: str
    required_access: int
    observation: Observation = field(repr=False)
    policy: str = "private"


def _valid_sid(sid: bytes) -> bool:
    return (isinstance(sid, bytes) and len(sid) >= 8 and sid[0] == 1
            and sid[1] <= 15 and len(sid) == 8 + 4 * sid[1])


def _parse_ace(raw: bytes) -> Ace:
    if not isinstance(raw, bytes) or len(raw) < 8 or len(raw) % 4:
        raise ACLVerificationError("ace_invalid")
    ace_type, flags, size, mask = struct.unpack_from("<BBHI", raw)
    if size != len(raw):
        raise ACLVerificationError("ace_invalid")
    # Deny, object-specific, callback, conditional and unknown ACEs are not
    # reordered or ignored; conservative rejection avoids mis-evaluation.
    if ace_type != 0:
        raise ACLVerificationError("ace_type_unsupported")
    if flags & ~0x1F:
        raise ACLVerificationError("ace_flags_unsupported")
    sid = raw[8:]
    if not _valid_sid(sid):
        raise ACLVerificationError("sid_invalid")
    return Ace(ace_type, flags, mask, sid, raw)


def _validate_identity(identity: tuple[int, int, int, int], kind: str) -> None:
    if (not isinstance(identity, tuple) or len(identity) != 4
            or any(not isinstance(x, int) or isinstance(x, bool) or x < 0 for x in identity)):
        raise ACLVerificationError("identity_unknown")
    _volume, index, links, attributes = identity
    if not index or attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        raise ACLVerificationError("identity_unknown")
    if kind == "file" and (attributes & FILE_ATTRIBUTE_DIRECTORY or links != 1):
        raise ACLVerificationError("identity_unknown")
    if kind == "directory" and (links < 1 or not attributes & FILE_ATTRIBUTE_DIRECTORY):
        raise ACLVerificationError("identity_unknown")


def _validate(observation: Observation, kind: str, required_access: int, policy: str) -> None:
    if not isinstance(observation, Observation):
        raise ACLVerificationError("evidence_invalid")
    if kind not in ("file", "directory"):
        raise ACLVerificationError("kind_invalid")
    if policy not in ("private", "ancestor") or (policy == "ancestor" and kind != "directory"):
        raise ACLVerificationError("policy_invalid")
    if (not isinstance(required_access, int) or isinstance(required_access, bool)
            or required_access <= 0 or required_access > 0xFFFFFFFF
            or required_access & ~(FILE_ALL_ACCESS | GENERIC_BITS)):
        raise ACLVerificationError("access_invalid")
    _validate_identity(observation.identity, kind)
    if not all(_valid_sid(x) for x in (observation.owner, observation.group, observation.user)):
        raise ACLVerificationError("sid_invalid")
    if observation.token_source not in ("thread", "process") or not observation.token_statistics:
        raise ACLVerificationError("token_unknown")
    if observation.owner not in (observation.user, _SYSTEM, _ADMINISTRATORS):
        raise ACLVerificationError("owner_untrusted")
    if observation.dacl_present is not True or observation.dacl_null is not False:
        raise ACLVerificationError("dacl_missing_or_null")
    if not isinstance(observation.aces, tuple) or not observation.aces:
        raise ACLVerificationError("dacl_empty")
    # Verify all entries in order, including INHERIT_ONLY. We do not claim
    # inheritance from this directory is safe without verifying each child.
    allowed = {observation.user, _SYSTEM, _ADMINISTRATORS}
    for ace in observation.aces:
        if not isinstance(ace, Ace) or _parse_ace(ace.raw) != ace:
            raise ACLVerificationError("ace_invalid")
        # Everyone must never acquire trusted-user rights, even if an
        # unexpected effective token reports the same SID.
        if ace.sid == _EVERYONE:
            if policy != "ancestor":
                raise ACLVerificationError("principal_untrusted")
            if _map_mask(ace.mask) & ~_ANCESTOR_PUBLIC_READ:
                raise ACLVerificationError("ancestor_access_unsafe")
        elif ace.sid not in allowed:
            if policy != "ancestor" or ace.sid not in (_BUILTIN_USERS, _AUTHENTICATED_USERS):
                raise ACLVerificationError("principal_untrusted")
            # Check even INHERIT_ONLY: it may grant rights to children.
            if _map_mask(ace.mask) & ~_ANCESTOR_PUBLIC_READ:
                raise ACLVerificationError("ancestor_access_unsafe")
    if (not isinstance(observation.desired, int) or not observation.desired
            or observation.desired & ~FILE_ALL_ACCESS
            or not isinstance(observation.granted, int)
            or observation.access_granted is not True
            or observation.granted & observation.desired != observation.desired
            or observation.privilege_count != 0):
        raise ACLVerificationError("access_unverified")
    # A backend must return the exact standard file mapping, not a weakened
    # or substituted desired mask. This deterministic mapping is also checked
    # independently of MapGenericMask in the native backend.
    if observation.desired != _map_mask(required_access):
        raise ACLVerificationError("access_unverified")
    # AccessCheck above is authoritative for the effective user. SYSTEM and
    # Administrators are trusted if present, not mandatory grantees.


def _map_mask(mask: int) -> int:
    result = mask & ~GENERIC_BITS
    if mask & 0x80000000:
        result |= FILE_GENERIC_READ
    if mask & 0x40000000:
        result |= FILE_GENERIC_WRITE
    if mask & 0x20000000:
        result |= FILE_GENERIC_EXECUTE
    if mask & 0x10000000:
        result |= FILE_ALL_ACCESS
    return result


def read_stable_evidence(handle: int, *, kind: str, required_access: int,
                         policy: str = "private", backend: Any = None) -> ACLEvidence:
    """Read twice on the same held HANDLE and reject unknown or changed evidence.

    ``required_access`` is an explicit file/directory access mask chosen by the
    integration layer. The native backend obtains the calling thread's effective
    token, or its process token only when the thread has no token. The caller
    must validate parents and bind the handle to the intended path separately.
    """
    if not isinstance(handle, int) or isinstance(handle, bool) or handle in (0, -1, ctypes.c_void_p(-1).value):
        raise ACLVerificationError("handle_invalid")
    if kind not in ("file", "directory"):
        raise ACLVerificationError("kind_invalid")
    if policy not in ("private", "ancestor") or (policy == "ancestor" and kind != "directory"):
        raise ACLVerificationError("policy_invalid")
    if (not isinstance(required_access, int) or isinstance(required_access, bool)
            or required_access <= 0 or required_access > 0xFFFFFFFF
            or required_access & ~(FILE_ALL_ACCESS | GENERIC_BITS)):
        raise ACLVerificationError("access_invalid")
    try:
        api = _NativeBackend() if backend is None else backend
        before = api.read_observation(handle, kind, required_access)
        _validate(before, kind, required_access, policy)
        after = api.read_observation(handle, kind, required_access)
        _validate(after, kind, required_access, policy)
    except ACLVerificationError:
        raise
    except Exception:
        raise ACLVerificationError("evidence_unavailable") from None
    if before != after:
        raise ACLVerificationError("evidence_changed")
    return ACLEvidence(handle, kind, required_access, after, policy)


def verify_unchanged(handle: int, evidence: ACLEvidence, *, backend: Any = None) -> None:
    """Re-read on the still-held same object before use; raises on any change."""
    if not isinstance(evidence, ACLEvidence):
        raise ACLVerificationError("evidence_invalid")
    if handle != evidence.handle:
        raise ACLVerificationError("handle_changed")
    if read_stable_evidence(handle, kind=evidence.kind, policy=evidence.policy,
                            required_access=evidence.required_access, backend=backend) != evidence:
        raise ACLVerificationError("evidence_changed")


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", DWORD), ("high", DWORD)]


class _FILEINFO(ctypes.Structure):
    _fields_ = [("attributes", DWORD), ("creation", _FILETIME), ("access", _FILETIME),
                ("write", _FILETIME), ("volume", DWORD), ("size_high", DWORD),
                ("size_low", DWORD), ("links", DWORD), ("index_high", DWORD), ("index_low", DWORD)]


class _ACL_SIZE_INFORMATION(ctypes.Structure):
    _fields_ = [("count", DWORD), ("in_use", DWORD), ("free", DWORD)]


class _GENERIC_MAPPING(ctypes.Structure):
    _fields_ = [("read", DWORD), ("write", DWORD), ("execute", DWORD), ("all", DWORD)]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("sid", ctypes.c_void_p), ("attributes", DWORD)]


def _sid_from_buffer(buffer, address: int, length: int) -> bytes:
    start = ctypes.addressof(buffer)
    if not address or address < start or address + 8 > start + length:
        raise ACLVerificationError("sid_invalid")
    prefix = ctypes.string_at(address, 8)
    size = 8 + 4 * prefix[1]
    if prefix[1] > 15 or address + size > start + length:
        raise ACLVerificationError("sid_invalid")
    sid = ctypes.string_at(address, size)
    if not _valid_sid(sid):
        raise ACLVerificationError("sid_invalid")
    return sid


class _NativeBackend:
    """Only created on Windows unless DLLs are explicitly injected for tests."""
    def __init__(self, *, advapi=None, kernel=None):
        if advapi is None or kernel is None:
            if os.name != "nt":
                raise ACLVerificationError("platform_unavailable")
            try:
                advapi = ctypes.WinDLL("advapi32", use_last_error=True)
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            except (OSError, AttributeError):
                raise ACLVerificationError("platform_unavailable") from None
        self.a, self.k = advapi, kernel
        self._bind()

    def _bind(self):
        p = ctypes.POINTER
        signatures = [
            (self.a.GetSecurityInfo, [HANDLE, DWORD, DWORD, p(ctypes.c_void_p), p(ctypes.c_void_p), p(ctypes.c_void_p), p(ctypes.c_void_p), p(ctypes.c_void_p)], DWORD),
            (self.a.GetSecurityDescriptorDacl, [ctypes.c_void_p, p(BOOL), p(ctypes.c_void_p), p(BOOL)], BOOL),
            (self.a.GetSecurityDescriptorControl, [ctypes.c_void_p, p(WORD), p(DWORD)], BOOL),
            (self.a.GetSecurityDescriptorLength, [ctypes.c_void_p], DWORD),
            (self.a.GetAclInformation, [ctypes.c_void_p, ctypes.c_void_p, DWORD, DWORD], BOOL),
            (self.a.GetAce, [ctypes.c_void_p, DWORD, p(ctypes.c_void_p)], BOOL),
            (self.a.OpenThreadToken, [HANDLE, DWORD, BOOL, p(HANDLE)], BOOL),
            (self.a.OpenProcessToken, [HANDLE, DWORD, p(HANDLE)], BOOL),
            (self.a.DuplicateTokenEx, [HANDLE, DWORD, ctypes.c_void_p, DWORD, DWORD, p(HANDLE)], BOOL),
            (self.a.GetTokenInformation, [HANDLE, DWORD, ctypes.c_void_p, DWORD, p(DWORD)], BOOL),
            (self.a.MapGenericMask, [p(DWORD), p(_GENERIC_MAPPING)], None),
            (self.a.AccessCheck, [ctypes.c_void_p, HANDLE, DWORD, p(_GENERIC_MAPPING), ctypes.c_void_p, p(DWORD), p(DWORD), p(BOOL)], BOOL),
            (self.k.GetFileInformationByHandle, [HANDLE, p(_FILEINFO)], BOOL),
            (self.k.GetFileType, [HANDLE], DWORD),
            (self.k.GetCurrentThread, [], HANDLE), (self.k.GetCurrentProcess, [], HANDLE),
            (self.k.CloseHandle, [HANDLE], BOOL), (self.k.LocalFree, [ctypes.c_void_p], ctypes.c_void_p),
        ]
        for fn, args, result in signatures:
            fn.argtypes, fn.restype = args, result

    def _identity(self, handle):
        # FILE_TYPE_DISK includes network shares; locality still requires a
        # separate integration-layer check. Pipes/devices/unknown are refused.
        if self.k.GetFileType(handle) != 1:
            raise ACLVerificationError("identity_unknown")
        data = _FILEINFO()
        if not self.k.GetFileInformationByHandle(handle, ctypes.byref(data)):
            raise ACLVerificationError("identity_unknown")
        return (int(data.volume), (int(data.index_high) << 32) | int(data.index_low),
                int(data.links), int(data.attributes))

    def _token_info(self, token, information):
        length = DWORD()
        # Both calls are bounded; an unavailable/erroring token never falls back.
        self.a.GetTokenInformation(token, information, None, 0, ctypes.byref(length))
        if not 0 < length.value <= 65536:
            raise ACLVerificationError("token_unknown")
        buf = ctypes.create_string_buffer(length.value)
        actual = DWORD(length.value)
        if not self.a.GetTokenInformation(token, information, buf, len(buf), ctypes.byref(actual)):
            raise ACLVerificationError("token_unknown")
        if not 0 < actual.value <= len(buf):
            raise ACLVerificationError("token_unknown")
        return buf, actual.value

    def _open_tokens(self):
        source, duplicate = HANDLE(), HANDLE()
        origin = "thread"
        if not self.a.OpenThreadToken(self.k.GetCurrentThread(), 0xA, True, ctypes.byref(source)):
            if ctypes.get_last_error() != 1008:  # ERROR_NO_TOKEN only
                raise ACLVerificationError("token_unknown")
            origin = "process"
            if not self.a.OpenProcessToken(self.k.GetCurrentProcess(), 0xA, ctypes.byref(source)):
                raise ACLVerificationError("token_unknown")
        if not source.value:
            raise ACLVerificationError("token_unknown")
        try:
            initial_stats, stats_len = self._token_info(source, 10)
            initial_stats = bytes(initial_stats.raw[:stats_len])
            # TOKEN_QUERY, SecurityImpersonation, TokenImpersonation
            if not self.a.DuplicateTokenEx(source, 0x8, None, 2, 2, ctypes.byref(duplicate)) or not duplicate.value:
                raise ACLVerificationError("token_unknown")
        except BaseException:
            failed = bool(duplicate.value and not self.k.CloseHandle(duplicate))
            if not self.k.CloseHandle(source):
                failed = True
            if failed:
                raise ACLVerificationError("handle_close_failed") from None
            raise
        return source, duplicate, origin, initial_stats

    def _sid_pointer(self, address, base, end):
        # GetSecurityInfo supplies a self-relative descriptor. Check both the
        # SID header and full SID remain inside its returned descriptor.
        if not address or address < base or address + 8 > end:
            raise ACLVerificationError("sid_invalid")
        prefix = ctypes.string_at(address, 8)
        length = 8 + 4 * prefix[1]
        if prefix[0] != 1 or prefix[1] > 15 or address + length > end:
            raise ACLVerificationError("sid_invalid")
        sid = ctypes.string_at(address, length)
        if not _valid_sid(sid):
            raise ACLVerificationError("sid_invalid")
        return sid

    def _aces(self, dacl, descriptor_base, descriptor_end):
        base = dacl.value
        if not base or base < descriptor_base or base + 8 > descriptor_end:
            raise ACLVerificationError("dacl_invalid")
        size = _ACL_SIZE_INFORMATION()
        if not self.a.GetAclInformation(dacl, ctypes.byref(size), ctypes.sizeof(size), 2):
            raise ACLVerificationError("dacl_unavailable")
        if (size.in_use < 8 or size.in_use + size.free > 65535
                or base + size.in_use + size.free > descriptor_end
                or size.count > (size.in_use - 8) // 4):
            raise ACLVerificationError("dacl_invalid")
        header = ctypes.string_at(base, 8)
        _, _, acl_size, ace_count, _ = struct.unpack("<BBHHH", header)
        if acl_size != size.in_use + size.free or ace_count != size.count:
            raise ACLVerificationError("dacl_invalid")
        expected = base + 8
        aces = []
        for index in range(size.count):
            ace = ctypes.c_void_p()
            if not self.a.GetAce(dacl, index, ctypes.byref(ace)):
                raise ACLVerificationError("dacl_unavailable")
            if ace.value != expected or expected + 4 > base + size.in_use:
                raise ACLVerificationError("ace_invalid")
            head = ctypes.string_at(expected, 4)
            ace_size = struct.unpack_from("<H", head, 2)[0]
            if ace_size < 8 or ace_size % 4 or expected + ace_size > base + size.in_use:
                raise ACLVerificationError("ace_invalid")
            aces.append(_parse_ace(ctypes.string_at(expected, ace_size)))
            expected += ace_size
        if expected != base + size.in_use:
            raise ACLVerificationError("dacl_invalid")
        return header, tuple(aces)

    def _access(self, descriptor, token, required):
        mapping = _GENERIC_MAPPING(FILE_GENERIC_READ, FILE_GENERIC_WRITE,
                                   FILE_GENERIC_EXECUTE, FILE_ALL_ACCESS)
        desired = DWORD(required)
        self.a.MapGenericMask(ctypes.byref(desired), ctypes.byref(mapping))
        if desired.value != _map_mask(required) or not desired.value or desired.value & ~FILE_ALL_ACCESS:
            raise ACLVerificationError("access_unverified")
        privilege = ctypes.create_string_buffer(1024)
        length, granted, status = DWORD(len(privilege)), DWORD(), BOOL()
        if not self.a.AccessCheck(descriptor, token, desired, ctypes.byref(mapping),
                                  privilege, ctypes.byref(length), ctypes.byref(granted), ctypes.byref(status)):
            raise ACLVerificationError("access_unverified")
        if length.value < 8 or length.value > len(privilege):
            raise ACLVerificationError("access_unverified")
        privilege_count = struct.unpack_from("<I", privilege.raw)[0]
        return desired.value, granted.value, bool(status.value), privilege_count

    def read_observation(self, handle, kind, required_access):
        identity_before = self._identity(handle)
        _validate_identity(identity_before, kind)
        source, duplicate, origin, initial_stats = self._open_tokens()
        sd = ctypes.c_void_p()
        try:
            stats, length = self._token_info(source, 10)  # TokenStatistics
            statistics = bytes(stats.raw[:length])
            if statistics != initial_stats:
                raise ACLVerificationError("evidence_changed")
            token_user, user_len = self._token_info(source, 1)  # TokenUser
            if user_len < ctypes.sizeof(_SID_AND_ATTRIBUTES):
                raise ACLVerificationError("token_unknown")
            token_struct = _SID_AND_ATTRIBUTES.from_buffer(token_user)
            user = _sid_from_buffer(token_user, token_struct.sid, user_len)
            owner, group, dacl = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
            # Group is required by AccessCheck; SD remains alive until after it.
            error = self.a.GetSecurityInfo(handle, 1, 0x7, ctypes.byref(owner), ctypes.byref(group),
                                           ctypes.byref(dacl), None, ctypes.byref(sd))
            if error or not sd.value:
                raise ACLVerificationError("descriptor_unavailable")
            control, revision = WORD(), DWORD()
            if not self.a.GetSecurityDescriptorControl(sd, ctypes.byref(control), ctypes.byref(revision)):
                raise ACLVerificationError("descriptor_unavailable")
            sd_length = self.a.GetSecurityDescriptorLength(sd)
            if ((control.value & 0x8004) != 0x8004 or revision.value != 1
                    or not 20 <= sd_length <= 1048576):
                raise ACLVerificationError("descriptor_unavailable")
            base, end = sd.value, sd.value + sd_length
            owner_sid = self._sid_pointer(owner.value, base, end)
            group_sid = self._sid_pointer(group.value, base, end)
            present, defaulted, descriptor_dacl = BOOL(), BOOL(), ctypes.c_void_p()
            if not self.a.GetSecurityDescriptorDacl(sd, ctypes.byref(present), ctypes.byref(descriptor_dacl), ctypes.byref(defaulted)):
                raise ACLVerificationError("dacl_unavailable")
            if not present.value or not descriptor_dacl.value or descriptor_dacl.value != dacl.value:
                raise ACLVerificationError("dacl_missing_or_null")
            header, aces = self._aces(dacl, base, end)
            desired, granted, permitted, privileges = self._access(sd, duplicate, required_access)
            stats_after, length_after = self._token_info(source, 10)
            if statistics != bytes(stats_after.raw[:length_after]) or identity_before != self._identity(handle):
                raise ACLVerificationError("evidence_changed")
            return Observation(identity_before, owner_sid, group_sid, user, origin, statistics,
                               int(control.value), int(revision.value), True, False, header, aces,
                               desired, granted, permitted, privileges)
        finally:
            failed = False
            if sd.value and self.k.LocalFree(sd):
                failed = True
            if not self.k.CloseHandle(duplicate):
                failed = True
            if not self.k.CloseHandle(source):
                failed = True
            if failed:
                raise ACLVerificationError("resource_release_failed") from None
