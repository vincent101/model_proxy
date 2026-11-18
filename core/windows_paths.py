"""Windows path-chain evidence, independent of any native adapter.

The absolute-path adapter checks each component and brackets each child open
with a check of its immediate held parent and its pathname. Revalidation walks
the whole chain again. None is an atomic relative-to-parent open or a guarantee
against same-user ABA or changes after the last check. Native Windows behaviour
has not been tested.

Keep a PathLease alive through revalidation and the dependent operation. Even
then, this is an observation, not a capability or a guarantee against
privileged/same-user actors, changes after the check, or change-and-revert.
"""
from __future__ import annotations

import ctypes
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Policy = Literal["traverse", "ancestor", "creation_parent", "private"]
Kind = Literal["directory", "file"]


class PathEvidenceError(RuntimeError):
    """Fail closed with a sanitized code (no paths, SIDs or OS messages)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Observation:
    """Immutable, policy-verified, handle-bound backend observation.

    Identity is meaningful only while the original handles remain held; no
    generation number or timestamp is claimed to prevent later file-ID reuse.
    For strict policies ``protection`` encodes owner/DACL/token/access-check
    inputs, not merely a boolean or ACL hash. The explicitly selected
    ``traverse`` policy records only handle-bound identity and successful
    opening, and makes no privacy or third-party access claim.
    ``local`` and ``stable`` must be
    positively established. Raw observations compare equal across the strict
    policies for the same unchanged object. A traverse observation deliberately
    differs from a strict one: compare related chains under the same boundary.
    """
    volume: int
    file_id: bytes
    kind: Kind
    links: int
    reparse: bool
    local: bool
    stable: bool
    protection: bytes = field(repr=False)


class Backend(Protocol):
    """Adapter obligations, not implemented by existing Win32 helpers.

    Open without following the final reparse point or sharing delete, never
    create/truncate/repair ACLs, and hold the observed ancestors. open_child
    may use a parent-relative open OR an absolute open bracketed by checks of
    the immediate held parent against its pathname. Each component is checked
    as walked; a lease revalidation rewalks the whole path. The absolute open
    is not atomic and cannot exclude same-user ABA or later changes. Only
    reliably established native absence may become FileNotFoundError; all
    uncertainty remains errors.
    Each returned handle is independently owned.

    observe performs stable handle-bound identity reads and verifies a
    supported local filesystem. For traverse only, successful opening provides
    the access needed to observe and bind that component; no owner/DACL
    whitelist or privacy conclusion is applied. The other policies require
    stable ACL reads and token AccessCheck and apply policy:
    ancestor allows Users/Authenticated Users/Everyone read/traverse only;
    creation_parent additionally forbids untrusted creation/deletion/rename;
    creation_parent also checks the effective token can create the specified
    first-missing file or directory. private requires trusted owner and
    private DACL. Unknown evidence fails.
    It returns the complete immutable observation or raises. close releases
    exactly one handle and raises on failure. There is no ACL setter.
    """
    def open_root(self, drive: str) -> Any: ...
    def open_child(self, parent: Any, name: str) -> Any: ...
    def observe(self, handle: Any, policy: Policy,
                creation_kind: Kind | None = None) -> Observation: ...
    def close(self, handle: Any) -> None: ...


# CreateFileW read-only flags. FILE_LIST_DIRECTORY and FILE_READ_DATA share
# bit 1; opening files with this conservative mask may deny some usable files.
_READ_ACCESS = 0x00020081
_DIR_CHECK_ACCESS = 0x000200A1
_SHARE_READ_WRITE = 0x3  # deliberately excludes FILE_SHARE_DELETE
_OPEN_FLAGS = 0x02000000 | 0x00200000  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _FileInfo(ctypes.Structure):
    _fields_ = [("attributes", ctypes.c_uint32), ("creation", _FileTime),
                ("access", _FileTime), ("write", _FileTime),
                ("volume", ctypes.c_uint32), ("size_high", ctypes.c_uint32),
                ("size_low", ctypes.c_uint32), ("links", ctypes.c_uint32),
                ("index_high", ctypes.c_uint32), ("index_low", ctypes.c_uint32)]


class NativeWin32:
    """Minimal injectable read-only Win32 primitives; untested on Windows.

    No WinDLL is loaded on import or on non-Windows. Native failures are
    sanitized; only ERROR_FILE_NOT_FOUND becomes FileNotFoundError.
    """
    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows unavailable")
        self._k = ctypes.WinDLL("kernel32", use_last_error=True)
        dword, handle = ctypes.c_uint32, ctypes.c_void_p
        self._k.CreateFileW.argtypes = [ctypes.c_wchar_p, dword, dword, handle,
                                       dword, dword, handle]
        self._k.CreateFileW.restype = handle
        self._k.CloseHandle.argtypes = [handle]
        self._k.CloseHandle.restype = ctypes.c_int32
        self._k.GetFileInformationByHandle.argtypes = [handle, ctypes.POINTER(_FileInfo)]
        self._k.GetFileInformationByHandle.restype = ctypes.c_int32
        self._k.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        self._k.GetDriveTypeW.restype = dword
        self._k.QueryDosDeviceW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, dword]
        self._k.QueryDosDeviceW.restype = dword
        self._k.GetVolumeInformationW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_wchar_p, dword, ctypes.POINTER(dword),
            ctypes.POINTER(dword), ctypes.POINTER(dword), ctypes.c_wchar_p, dword]
        self._k.GetVolumeInformationW.restype = ctypes.c_int32

    def open_existing(self, path: str, access: int, share: int, flags: int) -> int:
        raw = self._k.CreateFileW(path, access, share, None, 3, flags, None)
        if raw is None or raw == _INVALID_HANDLE:
            if ctypes.get_last_error() == 2:
                raise FileNotFoundError("not found")
            raise OSError("open unavailable")
        return raw

    def close(self, raw: int) -> None:
        if not self._k.CloseHandle(raw):
            raise OSError("close unavailable")

    def file_info(self, raw: int) -> tuple[int, int, int, int]:
        info = _FileInfo()
        if not self._k.GetFileInformationByHandle(raw, ctypes.byref(info)):
            raise OSError("identity unavailable")
        return (int(info.volume), (int(info.index_high) << 32) | int(info.index_low),
                int(info.links), int(info.attributes))

    def volume_info(self, drive: str) -> tuple[int, str, int, str]:
        root = drive + "\\"
        drive_type = int(self._k.GetDriveTypeW(root))
        if drive_type != 3:  # only DRIVE_FIXED; UNC/removable/remote are unsupported
            raise OSError("drive unavailable")
        device = ctypes.create_unicode_buffer(32768)
        if not self._k.QueryDosDeviceW(drive, device, len(device)):
            raise OSError("device unavailable")
        # Reject SUBST, mappings and unknown device namespaces. This deliberately
        # excludes other potentially local configurations until verified.
        if not re.fullmatch(r"\\Device\\HarddiskVolume[0-9]+", device.value, re.I):
            raise OSError("device unsupported")
        serial, max_component, flags = ctypes.c_uint32(), ctypes.c_uint32(), ctypes.c_uint32()
        fs = ctypes.create_unicode_buffer(128)
        if not self._k.GetVolumeInformationW(root, None, 0, ctypes.byref(serial),
                                             ctypes.byref(max_component), ctypes.byref(flags),
                                             fs, len(fs)):
            raise OSError("volume unavailable")
        if fs.value.upper() != "NTFS":
            raise OSError("filesystem unsupported")
        return drive_type, "NTFS", int(serial.value), device.value


@dataclass(eq=False)
class _PathHandle:
    raw: int = field(repr=False)
    path: str = field(repr=False)
    volume: tuple = field(repr=False)
    parent: _PathHandle | None = field(default=None, repr=False)
    baseline: Observation | None = field(default=None, repr=False)
    baseline_policy: Policy | None = field(default=None, repr=False)
    baseline_creation_kind: Kind | None = field(default=None, repr=False)
    closed: bool = False


class WindowsPathBackend:
    """Absolute-path, held-chain Win32 adapter; no service authorization.

    ``win32`` supplies open_existing/close/file_info/volume_info in the same
    form as NativeWin32. ``acl_backend`` is the injectable read-only backend
    accepted by windows_acl.read_stable_evidence. Both are test seams, not
    native security evidence. Unrecognized filesystems and mappings fail shut.
    The adapter and its leases are not thread-safe.
    """
    def __init__(self, *, win32: Any, acl_backend: Any = None):
        self._win32 = win32
        self._acl_backend = acl_backend

    def _volume(self, drive: str) -> tuple:
        info = self._win32.volume_info(drive)
        if (type(info) is not tuple or len(info) != 4 or info[0] != 3
                or info[1] != "NTFS" or type(info[2]) is not int or info[2] < 0
                or not isinstance(info[3], str)
                or not re.fullmatch(r"\\Device\\HarddiskVolume[0-9]+", info[3], re.I)):
            raise OSError("volume unsupported")
        return info

    def _open(self, path: str, volume: tuple, parent: _PathHandle | None) -> _PathHandle:
        raw = self._win32.open_existing(path, _READ_ACCESS, _SHARE_READ_WRITE, _OPEN_FLAGS)
        if type(raw) is not int or raw in (0, -1, _INVALID_HANDLE):
            raise OSError("handle invalid")
        return _PathHandle(raw, path, volume, parent)

    def open_root(self, drive: str) -> _PathHandle:
        volume = self._volume(drive)
        return self._open(drive + "\\", volume, None)

    def _check_parent(self, parent: _PathHandle) -> None:
        """Bind the immediate held parent to its current absolute pathname.

        Ancestors are checked as individual components during each walk, not
        recursively reopened for every child. This is not an atomic open and
        cannot prevent non-cooperating same-user changes after the last check.
        """
        if parent.closed or parent.baseline is None or parent.baseline_policy is None:
            raise OSError("parent unavailable")
        if self.observe(parent, parent.baseline_policy, parent.baseline_creation_kind) != parent.baseline:
            raise OSError("parent changed")
        temporary: list[_PathHandle] = []
        try:
            try:
                current = self._open(parent.path, parent.volume, None)
            except FileNotFoundError:
                # A missing parent is not proof that the child is absent.
                raise OSError("parent path unavailable") from None
            temporary.append(current)
            if self.observe(current, parent.baseline_policy, parent.baseline_creation_kind) != parent.baseline:
                raise OSError("parent path changed")
        finally:
            _close_all(self, temporary)

    def open_child(self, parent: _PathHandle, name: str) -> _PathHandle:
        if not isinstance(parent, _PathHandle) or parent.closed:
            raise OSError("parent unavailable")
        self._check_parent(parent)
        path = parent.path + ("" if parent.path.endswith("\\") else "\\") + name
        child = None
        try:
            try:
                child = self._open(path, parent.volume, parent)
            except FileNotFoundError:
                # A path-not-found result is meaningful only if the parent
                # chain still matches; otherwise it is an unknown race.
                self._check_parent(parent)
                raise
            self._check_parent(parent)
            return child
        except BaseException:
            if child is not None:
                try:
                    self.close(child)
                except BaseException as exc:
                    # The close may have succeeded even when its result was
                    # lost; never retry or misreport this as a path absence.
                    if not isinstance(exc, Exception):
                        raise _interrupted_close(exc) from None
                    raise PathEvidenceError("close_failed") from None
            raise

    def observe(self, handle: _PathHandle, policy: Policy,
                creation_kind: Kind | None = None) -> Observation:
        from core import windows_acl

        if not isinstance(handle, _PathHandle) or handle.closed:
            raise OSError("handle unavailable")
        if self._volume(handle.path[:2]) != handle.volume:
            raise OSError("volume changed")
        before = self._win32.file_info(handle.raw)
        if (type(before) is not tuple or len(before) != 4
                or any(type(x) is not int or x < 0 for x in before)
                or before[0] != handle.volume[2] or not before[1] or not before[2]
                or before[3] & 0x400):
            raise OSError("identity unavailable")
        kind: Kind = "directory" if before[3] & 0x10 else "file"
        if policy not in ("traverse", "ancestor", "creation_parent", "private"):
            raise OSError("policy unavailable")
        if ((policy == "creation_parent" and creation_kind not in ("file", "directory"))
                or (policy != "creation_parent" and creation_kind is not None)):
            raise OSError("creation kind unavailable")
        if policy != "private" and kind != "directory":
            raise OSError("invalid parent")
        if policy == "traverse":
            # The held handle was opened with the same read/identity access
            # used for path binding. Do not infer the effective rights of
            # anyone else from public or inherit-only ACEs. This is not a
            # privacy check and is available only above an explicit boundary.
            after = self._win32.file_info(handle.raw)
            if before != after:
                raise OSError("identity changed")
            obs = Observation(before[0], before[1].to_bytes(8, "big"), kind, before[2],
                              False, True, True, b"opened-identity-only")
            if handle.baseline is None:
                handle.baseline = obs
                handle.baseline_policy = policy
            return obs
        # The ancestor policy permits Users/Authenticated Users/Everyone
        # read/traverse only, including for a creation parent. It excludes
        # public create/delete/rename-related access, and independently checks
        # owner, every ACE and the effective token's requested access.
        acl_policy = "private" if policy == "private" else "ancestor"
        required = _DIR_CHECK_ACCESS if kind == "directory" else _READ_ACCESS
        if policy == "creation_parent":
            required |= 0x2 if creation_kind == "file" else 0x4
        proof = windows_acl.read_stable_evidence(
            handle.raw, kind=kind, required_access=required,
            policy=acl_policy, backend=self._acl_backend)
        after = self._win32.file_info(handle.raw)
        if before != after or proof.observation.identity != before:
            raise OSError("identity changed")
        a = proof.observation
        # Keep complete raw security evidence, not a policy boolean or hash.
        protection = repr((a.owner, a.group, a.user, a.token_source,
                           a.token_statistics, a.control, a.revision,
                           a.dacl_present, a.dacl_null, a.acl_header,
                           tuple((x.ace_type, x.flags, x.mask, x.sid, x.raw) for x in a.aces),
                           a.access_granted, a.privilege_count)).encode()
        obs = Observation(before[0], before[1].to_bytes(8, "big"), kind, before[2],
                          False, True, True, protection)
        if handle.baseline is None:
            handle.baseline = obs
            handle.baseline_policy = policy
            handle.baseline_creation_kind = creation_kind
        return obs

    def close(self, handle: _PathHandle) -> None:
        if not isinstance(handle, _PathHandle) or handle.closed:
            raise OSError("handle already closed")
        # One owner and one close attempt even if the native result is unknown.
        handle.closed = True
        self._win32.close(handle.raw)


@dataclass(frozen=True)
class ComponentEvidence:
    name: str = field(repr=False)
    observation: Observation = field(repr=False)
    policy: Policy
    creation_kind: Kind | None = None


@dataclass(frozen=True)
class PathEvidence:
    drive: str = field(repr=False)
    components: tuple[ComponentEvidence, ...] = field(repr=False)
    missing: tuple[str, ...] = field(repr=False)
    expected_kind: Kind
    private: bool
    single_link: bool
    boundary_depth: int | None = None

    @property
    def exists(self) -> bool:
        return not self.missing

    @property
    def direct_parent_observed(self) -> bool:
        # A longer missing suffix has an unobserved final direct parent. No
        # caller may create the final target until that parent is rechecked.
        return len(self.missing) <= 1


# Avoid ambiguous Win32 parsing (device names, ADS, drive-relative/UNC,
# extended/device namespace, trailing dot/space and normalization of '..').
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?$", re.I)
_DRIVE = re.compile(r"^[A-Za-z]:\\")
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _parse(path: str) -> tuple[str, tuple[str, ...]]:
    if not isinstance(path, str) or not _DRIVE.match(path):
        raise PathEvidenceError("invalid_path")
    parts = path[3:].split("\\")
    if (not parts or len(parts) > 127 or any(
        not p or p in (".", "..") or p[-1] in " ." or _BAD.search(p)
        or _RESERVED.match(p) for p in parts
    )):
        raise PathEvidenceError("invalid_path")
    return path[:2].upper(), tuple(parts)


def _valid_observation(value: Any) -> bool:
    return (type(value) is Observation
            and type(value.volume) is int and value.volume >= 0
            and type(value.file_id) is bytes and bool(value.file_id)
            and value.kind in ("file", "directory")
            and type(value.links) is int and value.links > 0
            and value.reparse is False and value.local is True and value.stable is True
            and type(value.protection) is bytes and bool(value.protection))


_RELEASE_FAILURE_CODES = frozenset((
    "release_unknown", "resource_release_failed", "handle_close_failed", "close_failed",
))


def _release_code(exc: Exception) -> str | None:
    # Never forward arbitrary backend text, paths or non-whitelisted codes.
    code = getattr(exc, "code", None)
    return code if type(code) is str and code in _RELEASE_FAILURE_CODES else None


def _interrupted_close(exc: BaseException) -> BaseException:
    # Preserve interruption semantics without forwarding backend message text.
    # SystemExit's .code is its exit status, so only KeyboardInterrupt gets a
    # fixed path error code; all interruptions record the uncertain release.
    if isinstance(exc, KeyboardInterrupt):
        exc = KeyboardInterrupt()
        exc.code = "close_failed"
    try:
        exc.release_unknown = True
    except BaseException:
        # An unusual control exception may forbid attributes; cleanup of the
        # remaining handles still takes priority over annotating it.
        pass
    return exc


def _observe(backend: Backend, handle: Any, policy: Policy,
             creation_kind: Kind | None = None) -> Observation:
    try:
        value = backend.observe(handle, policy, creation_kind)
    except Exception as exc:
        raise PathEvidenceError(_release_code(exc) or "observation_unavailable") from None
    if not _valid_observation(value):
        raise PathEvidenceError("observation_unsafe")
    return value


def _close_all(backend: Backend, handles: list[Any]) -> None:
    failed = False
    interrupted = None
    while handles:
        # Remove ownership before calling close; even an interruption can mean
        # the OS already released this particular handle, so never retry it.
        handle = handles.pop()
        try:
            backend.close(handle)
        except BaseException as exc:
            failed = True
            if not isinstance(exc, Exception) and interrupted is None:
                interrupted = _interrupted_close(exc)
    if interrupted is not None:
        raise interrupted from None
    if failed:
        raise PathEvidenceError("close_failed")


def _walk(backend: Backend, drive: str, parts: tuple[str, ...],
          kind: Kind, private: bool, single_link: bool,
          boundary_depth: int | None = None) -> tuple[PathEvidence, list[Any]]:
    handles: list[Any] = []
    records: list[ComponentEvidence] = []
    try:
        try:
            root = backend.open_root(drive)
        except Exception as exc:
            raise PathEvidenceError(_release_code(exc) or "root_unavailable") from None
        handles.append(root)
        root_policy: Policy = "traverse" if boundary_depth is not None else "ancestor"
        root_obs = _observe(backend, root, root_policy)
        if root_obs.kind != "directory":
            raise PathEvidenceError("invalid_type")
        records.append(ComponentEvidence(drive + "\\", root_obs, root_policy))
        seen = {(root_obs.volume, root_obs.file_id)}
        missing: tuple[str, ...] = ()
        for index, name in enumerate(parts):
            try:
                handle = backend.open_child(handles[-1], name)
            except FileNotFoundError as exc:
                # A release failure attached to a missing-file exception is
                # uncertainty, never evidence of absence.
                if _release_code(exc) is not None:
                    raise PathEvidenceError(_release_code(exc)) from None
                if boundary_depth is not None and index < boundary_depth:
                    raise PathEvidenceError("boundary_unavailable") from None
                missing = parts[index:]
                # The first absent entry could be created now. Its existing
                # parent must disallow untrusted creation even if the final
                # target's direct parent is still absent.
                old = records[-1]
                creation_kind: Kind = "directory" if index < len(parts) - 1 else kind
                parent_obs = _observe(backend, handles[-1], "creation_parent", creation_kind)
                if parent_obs != old.observation:
                    raise PathEvidenceError("observation_changed")
                records[-1] = ComponentEvidence(old.name, parent_obs, "creation_parent", creation_kind)
                break
            except Exception as exc:
                raise PathEvidenceError(_release_code(exc) or "open_unavailable") from None
            handles.append(handle)
            final = index == len(parts) - 1
            policy: Policy = ("traverse" if boundary_depth is not None and index + 1 < boundary_depth
                              else "private" if final and private else "ancestor")
            obs = _observe(backend, handle, policy)
            if obs.volume != root_obs.volume:
                raise PathEvidenceError("volume_changed")
            if obs.kind != (kind if final else "directory"):
                raise PathEvidenceError("invalid_type")
            if final and single_link and obs.links != 1:
                raise PathEvidenceError("fixed_hardlink")
            key = (obs.volume, obs.file_id)
            if key in seen:
                raise PathEvidenceError("ancestor_alias")
            seen.add(key)
            records.append(ComponentEvidence(name, obs, policy))
        return PathEvidence(drive, tuple(records), missing, kind, private, single_link,
                            boundary_depth), handles
    except BaseException:
        _close_all(backend, handles)
        raise


class PathLease:
    """Held original chain. Always use as a context manager and revalidate.

    Evidence is immutable but not actionable after close. Revalidation reads
    each original held object and does a second full walk, including absence
    and the creation-parent policy. Original handles remain held on success;
    failures must be treated as UNKNOWN. Leases are not thread-safe: callers
    must serialize revalidation, handle use and close.
    """
    def __init__(self, backend: Backend, evidence: PathEvidence, handles: list[Any]):
        self._backend = backend
        self.evidence = evidence
        self._handles = handles
        self._closed = False
        self._validated = False
        self._failed = False

    def __enter__(self) -> PathLease:
        if self._closed:
            raise PathEvidenceError("lease_closed")
        if not self._validated or self._failed:
            raise PathEvidenceError("lease_invalid")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            _close_all(self._backend, self._handles)

    def borrowed_component_handle(self, index: int) -> Any:
        """Return a still-held component handle, without transferring ownership.

        The caller must not close it and must not treat access as authorization.
        Index zero is the drive root; missing components have no handle.
        """
        if self._closed:
            raise PathEvidenceError("lease_closed")
        if not self._validated or self._failed:
            raise PathEvidenceError("lease_invalid")
        if type(index) is not int or not 0 <= index < len(self._handles):
            raise PathEvidenceError("invalid_component")
        return self._handles[index]

    def revalidate(self) -> None:
        if self._closed:
            raise PathEvidenceError("lease_closed")
        if self._failed:
            raise PathEvidenceError("lease_invalid")
        self._validated = False
        try:
            for handle, item in zip(self._handles, self.evidence.components):
                if _observe(self._backend, handle, item.policy, item.creation_kind) != item.observation:
                    raise PathEvidenceError("observation_changed")
            parts = tuple(item.name for item in self.evidence.components[1:]) + self.evidence.missing
            fresh, handles = _walk(self._backend, self.evidence.drive, parts,
                                   self.evidence.expected_kind, self.evidence.private,
                                   self.evidence.single_link, self.evidence.boundary_depth)
            try:
                if fresh != self.evidence:
                    raise PathEvidenceError("path_changed")
                # Changes after this point or change-and-revert remain possible.
                for handle, item in zip(self._handles, self.evidence.components):
                    if _observe(self._backend, handle, item.policy, item.creation_kind) != item.observation:
                        raise PathEvidenceError("observation_changed")
            finally:
                _close_all(self._backend, handles)
        except BaseException:
            self._failed = True
            raise
        self._validated = True


def inspect(path: str, *, backend: Backend, kind: Kind = "file",
            private: bool = True, single_link: bool = False,
            trusted_boundary: str | None = None) -> PathLease:
    """Observe an absolute drive path; backend and all handles are mandatory.

    Existing files always require private protection; private=False permits
    directory targets with ancestor policy, not unverified public files.
    Fixed files require single_link=True; other files retain link counts for
    physical alias comparison. An explicit trusted_boundary is an existing
    non-root directory on this exact path, selected internally by the caller:
    the project root or an explicitly selected external target's direct
    parent. Strict policies apply to it and below; only components above it
    use identity-only traversal. It is not a write authorization or a claim
    that any ancestor is private. Omission retains the original strict walk.
    """
    if (kind not in ("file", "directory") or type(private) is not bool
            or type(single_link) is not bool or (single_link and kind != "file")
            or (kind == "file" and not private)):
        raise PathEvidenceError("invalid_arguments")
    drive, parts = _parse(path)
    boundary_depth = None
    if trusted_boundary is not None:
        boundary_drive, boundary_parts = _parse(trusted_boundary)
        # A Windows directory can be case-sensitive. Folding names here could
        # turn an independently selected boundary into a different object.
        # Require the caller's selected path prefix verbatim; alias comparisons
        # for held objects and missing names remain separate and conservative.
        if (boundary_drive != drive or len(boundary_parts) > len(parts)
                or boundary_parts != parts[:len(boundary_parts)]):
            raise PathEvidenceError("boundary_mismatch")
        if len(boundary_parts) == len(parts) and kind != "directory":
            raise PathEvidenceError("boundary_not_directory")
        boundary_depth = len(boundary_parts)
    evidence, handles = _walk(backend, drive, parts, kind, private, single_link, boundary_depth)
    lease = PathLease(backend, evidence, handles)
    try:
        lease.revalidate()
        return lease
    except BaseException as primary:
        try:
            lease.close()
        except Exception as close_error:
            if isinstance(primary, KeyboardInterrupt):
                # Keep Ctrl-C semantics while retaining the later uncertain
                # close as an explicit cause. close() never retries handles.
                primary.release_unknown = True
                raise primary from close_error
            raise
        raise


def _may_alias(left: str, right: str) -> bool:
    # Unknown filesystem case/Unicode/8.3 equivalence is conservatively a
    # possible alias. Existing objects are compared using handle identity.
    def folded(s: str) -> str:
        return unicodedata.normalize("NFD", unicodedata.normalize("NFD", s).casefold())
    return (folded(left) == folded(right) or not (left.isascii() and right.isascii())
            or "~" in left or "~" in right)


def _prefix(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return len(left) <= len(right) and all(_may_alias(a, b) for a, b in zip(left, right))


def _live_evidence(lease: PathLease) -> PathEvidence:
    if not isinstance(lease, PathLease) or lease._closed or lease._failed or not lease._validated:
        raise PathEvidenceError("lease_invalid")
    return lease.evidence


def may_contain(parent: PathLease, child: PathLease) -> bool:
    """Conservative containment; true means possible, not proven alias.

    Compare any shared existing directory by physical identity, not spelling;
    then compare remaining (possibly missing) components conservatively. Call
    only with simultaneously held leases. Both are revalidated here. Evidence
    objects alone are rejected to prevent reuse after their handles close.
    """
    _live_evidence(parent)
    _live_evidence(child)
    parent.revalidate()
    if child is not parent:
        child.revalidate()
    parent_evidence, child_evidence = _live_evidence(parent), _live_evidence(child)
    for i, p in enumerate(parent_evidence.components):
        for j, c in enumerate(child_evidence.components):
            if ((p.observation.volume, p.observation.file_id)
                    != (c.observation.volume, c.observation.file_id)):
                continue
            ptail = tuple(x.name for x in parent_evidence.components[i + 1:]) + parent_evidence.missing
            ctail = tuple(x.name for x in child_evidence.components[j + 1:]) + child_evidence.missing
            if _prefix(ptail, ctail):
                return True
    return False


def may_alias(left: PathLease, right: PathLease) -> bool:
    """True for same existing identity or possibly equal unresolved names."""
    return may_contain(left, right) and may_contain(right, left)
