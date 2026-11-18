"""Windows snapshots and finite protected-file writes.

Reads and writes fail closed on unverified ACLs or protection changes. The
last check is not a CAS: non-cooperating same-user changes can race it. Native
Windows behavior has not been validated. No persistent freeze or rollback is
provided.
"""
from __future__ import annotations

import inspect
import os
from dataclasses import dataclass, field

from core import windows_acl, windows_paths
from core.discovery import _WindowsPathMonitor, _windows_path_backend
from core.platform_windows import (
    GuardedFileRead, WindowsFileHandle, acl_protection, file_identity,
    read_guarded_file,
)


class SecureSnapshotError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SecureWriteError(RuntimeError):
    """Sanitized phase and artifact information for a failed write.

    UNKNOWN can follow a successful target postcheck if a later transaction
    step fails; target_was_verified records that historical observation, not
    current target protection or a successful transaction receipt.
    """
    def __init__(self, code: str, *, state="NOT_REPLACED", backup=None, staging=None,
                 backup_status="NONE", release_unknown=False, target_state=None,
                 creation_unknown=False, backup_was_verified=False, candidate=None,
                 target_was_verified=False):
        super().__init__(code)
        self.code, self.state = code, state
        self.target_state = state if target_state is None else target_state
        self.backup, self.staging = backup, staging
        self.backup_status, self.release_unknown = backup_status, release_unknown
        self.creation_unknown = creation_unknown
        self.backup_was_verified = backup_was_verified
        # Historical observation only; never a successful transaction receipt.
        self.target_was_verified = target_was_verified
        # Observation of the candidate before the native call, not proof that
        # the target was replaced or that its marker will remain unchanged.
        self.candidate = candidate

    @property
    def backup_verified(self):
        return self.backup_status == "VERIFIED"


# Only fixed, non-sensitive public codes may cross the writer boundary. An
# arbitrary exception's message, code object, and chain are never propagated.
_RELEASE_CODES = frozenset({
    "release_unknown", "close_failed", "handle_close_failed", "resource_release_failed",
})
_WRITE_CODES = frozenset({
    "access_invalid", "access_unverified", "ace_flags_unsupported", "ace_invalid",
    "ace_type_unsupported", "ancestor_access_unsafe", "ancestor_alias", "artifact_preflight_failed",
    "artifact_preflight_required", "authorized_root_changed", "authorized_root_missing",
    "backup_changed", "backup_unverified", "backend_unavailable", "boundary_mismatch",
    "boundary_not_directory", "boundary_unavailable", "candidate_boundary_unverified",
    "candidate_identity_unverified", "candidate_not_authorized", "candidate_parent_changed",
    "candidate_parent_missing", "candidate_parent_unverified", "candidate_path_unverified",
    "candidate_protection_unverified", "candidate_readback_unverified", "candidate_role_unverified",
    "candidate_unverified", "commit_alias_unverified", "commit_api_unverified",
    "commit_path_unverified", "commit_unknown", "commit_volume_or_identity_unverified",
    "creation_policy_unverified", "creation_protection_unverified", "creation_unknown",
    "dacl_empty", "dacl_invalid", "dacl_missing_or_null", "dacl_unavailable",
    "descriptor_unavailable", "evidence_changed", "evidence_invalid", "evidence_unavailable",
    "fixed_hardlink", "handle_changed", "handle_invalid", "handle_unverified", "identity_unknown",
    "invalid_arguments", "invalid_component", "invalid_limit", "invalid_path", "invalid_type",
    "kind_invalid", "lease_closed", "lease_invalid", "observation_changed", "observation_unsafe",
    "observation_unavailable", "open_unavailable", "root_unavailable",
    "outside_authorized_root", "owner_untrusted", "parent_missing", "path_changed",
    "platform_unavailable", "platform_unverified", "policy_invalid", "postcheck_failed",
    "preflight_failed", "principal_untrusted", "protection_changed", "sid_invalid",
    "snapshot_invalid", "stage_changed", "stage_unverified", "target_changed",
    "target_parent_changed", "target_unverified", "token_unknown", "volume_changed",
    "windows_secure_write_unverified", "write_unverified",
})
_MISSING = object()


def _safe_exception_field(exc, name):
    """Inspect metadata without executing exception-defined code or descriptors.

    A dynamic attribute hook or descriptor means the field is unobservable;
    it must not be invoked, since it could raise, block, or leak secrets.
    """
    try:
        getter = inspect.getattr_static(type(exc), "__getattribute__")
        dynamic = ((getter is not BaseException.__getattribute__ and
                    getter is not object.__getattribute__) or
                   inspect.getattr_static(type(exc), "__getattr__", _MISSING) is not _MISSING)
        if dynamic:
            # Static values can be masked by a dynamic accessor. In particular,
            # a stored False is not evidence of a definite creation or release.
            return _MISSING, True
        return inspect.getattr_static(exc, name, _MISSING), False
    except BaseException:
        return _MISSING, True


def _write_exception_info(exc, *, include_created=False):
    """Safely reduce untrusted exception metadata to fixed, non-sensitive facts."""
    release, release_bad = _safe_exception_field(exc, "release_unknown")
    code, code_bad = _safe_exception_field(exc, "code")
    # Unexpected field types are not trusted, including objects with hostile
    # truth/equality methods; do not invoke those methods.
    if release is not _MISSING and release is not None and type(release) is not bool:
        release_bad = True
    if code is not _MISSING and code is not None and type(code) is not str:
        code_bad = True
    unknown = release_bad or code_bad or release is True
    if type(code) is str and code in _RELEASE_CODES:
        unknown = True
    safe_code = code if type(code) is str and code in _WRITE_CODES else None
    created = None
    if include_created:
        value, bad = _safe_exception_field(exc, "created")
        if value is True or value is False:
            created = value
        elif value is not _MISSING and value is not None:
            bad = True
        unknown = unknown or bad
    return safe_code, unknown, created


class _ArtifactPreflightFailure(Exception):
    """Internal wrapper; inspect the callback exception only after finish."""
    def __init__(self, original):
        super().__init__()
        self.original = original


def _raise_write_error(error):
    """Do not attach even an active caller exception to the public error."""
    try:
        raise error from None
    except SecureWriteError as sanitized:
        sanitized.__context__ = None
        raise


@dataclass(frozen=True, repr=False)
class WriteOutcome:
    """Last verified target and backup observations, not a future guarantee."""
    state: str
    backup: str | None = field(repr=False)
    backup_was_verified: bool
    backup_status: str

    @property
    def backup_verified(self) -> bool:
        return self.backup_status == "VERIFIED"


@dataclass(frozen=True, repr=False)
class IdentityObservation:
    """Historical parent/file/protection evidence, not a write authorization."""
    exists: bool
    parent: windows_paths.Observation = field(repr=False)
    file_identity: object | None = field(repr=False)
    file_protection: tuple | None = field(repr=False)
    marker: tuple | None = field(repr=False)


@dataclass(repr=False, frozen=True, eq=False)
class TargetSnapshot:
    """Sensitive data and held path leases; use as a context manager.

    A failed revalidation permanently invalidates this observation. Closed
    leases cannot authorize future reads or writes. Not thread-safe.
    """
    path: str = field(repr=False)
    authorized_root: str = field(repr=False)
    exists: bool
    contents: bytes | None = field(repr=False)
    _read: GuardedFileRead | None = field(repr=False)
    _root_lease: windows_paths.PathLease = field(repr=False)
    _target_lease: windows_paths.PathLease = field(repr=False)
    _monitor: _WindowsPathMonitor = field(repr=False)
    _max_bytes: int = field(repr=False)
    _trusted_boundary: str | None = field(default=None, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _failed: bool = field(default=False, init=False, repr=False)

    def __enter__(self):
        if self._closed or self._failed:
            raise SecureSnapshotError("snapshot_invalid")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    @property
    def identity_observation(self) -> IdentityObservation:
        """Capture-time observation; revalidate before relying on this lease."""
        if self._closed or self._failed:
            raise SecureSnapshotError("snapshot_invalid")
        return _identity_observation(self, self._read)

    def close(self):
        if self._closed:
            return
        object.__setattr__(self, "_closed", True)
        failed = False
        for lease in (self._target_lease, self._root_lease):
            try:
                lease.close()
            except BaseException:
                failed = True
        if failed or self._monitor.release_unknown:
            raise SecureSnapshotError("release_unknown") from None

    def revalidate(self):
        """Re-read and compare after confirmation; repeat before each operation.

        This is not a CAS, a lock against writers, or authorization to write.
        In particular the retained no-delete-share target handle is not a
        demonstrated Windows replace mechanism.
        """
        if self._closed or self._failed:
            raise SecureSnapshotError("snapshot_invalid")
        try:
            self._root_lease.revalidate()
            self._target_lease.revalidate()
            if self.exists:
                current = _read_target(self.path, self._target_lease, self._max_bytes,
                                       self._trusted_boundary)
                if current != self._read:
                    raise SecureSnapshotError("target_changed")
            self._target_lease.revalidate()
            self._root_lease.revalidate()
            if self._monitor.release_unknown:
                raise SecureSnapshotError("release_unknown")
        except BaseException as exc:
            object.__setattr__(self, "_failed", True)
            if self._monitor.release_unknown or getattr(exc, "code", None) in (
                    "release_unknown", "close_failed", "handle_close_failed", "resource_release_failed"):
                raise SecureSnapshotError("release_unknown") from None
            if isinstance(exc, SecureSnapshotError):
                raise
            if isinstance(exc, windows_paths.PathEvidenceError) and exc.code in (
                    "path_changed", "observation_changed", "fixed_hardlink"):
                raise SecureSnapshotError("target_changed") from None
            raise SecureSnapshotError("target_unverified") from None


def _identity_observation(snapshot, read):
    parent = snapshot._target_lease.evidence.components[-2 if snapshot.exists else -1].observation
    return IdentityObservation(read is not None, parent,
                               read.identity if read is not None else None,
                               read.protection if read is not None else None,
                               read.marker if read is not None else None)


def _read_target(path, lease, max_bytes, trusted_boundary=None):
    """Bind a separately held, ACL-checked read to the still-held path lease."""
    options = {"trusted_boundary": trusted_boundary} if trusted_boundary is not None else {}
    result = read_guarded_file(path, with_data=True, max_bytes=max_bytes, **options)
    if not isinstance(result, GuardedFileRead) or not isinstance(result.data, bytes):
        raise SecureSnapshotError("target_unverified")
    obs = lease.evidence.components[-1].observation
    ident = result.identity
    marker = result.marker
    if (not isinstance(marker, tuple) or len(marker) != 5
            or any(type(x) is not int or x < 0 for x in marker)
            or marker[2] <= 0 or marker[3] <= 0 or marker[4] != len(result.data)
            or marker[4] > max_bytes or ident.volume_serial != obs.volume
            or ident.file_index != int.from_bytes(obs.file_id, "big")
            or ident.link_count != 1 or obs.links != 1
            or ident.attributes & (0x10 | 0x400)
            or marker[:2] != (ident.volume_serial, ident.file_index)
            or not result.protection):
        raise SecureSnapshotError("target_unverified")
    return result


def capture_target(target: str, *, authorized_root: str, max_bytes: int,
                   trusted_boundary: str | None = None) -> TargetSnapshot:
    """Capture an existing or directly absent target without creating anything.

    The authorization root must already exist and its exact path prefix and
    object observations must agree with the target chain. Reparse points and
    hardlinked targets are rejected rather than modified. Native Win32 behavior
    is untested; successful capture is not proof of safe write capability.
    Only an internal caller may select the project root or the authorized
    external target's direct parent as trusted_boundary. It changes traversal
    policy above that directory; it never grants target write authorization.
    Omitting it retains the strict original walk.
    """
    if type(max_bytes) is not int or max_bytes < 0:
        raise SecureSnapshotError("invalid_limit")
    try:
        drive, parts = windows_paths._parse(target)
        root_drive, root_parts = windows_paths._parse(authorized_root)
    except (windows_paths.PathEvidenceError, TypeError):
        raise SecureSnapshotError("invalid_path") from None
    # Deliberately exact spelling: do not assume Unicode/8.3 alias semantics.
    if drive != root_drive or len(parts) <= len(root_parts) or parts[:len(root_parts)] != root_parts:
        raise SecureSnapshotError("outside_authorized_root")
    if os.name != "nt":
        raise SecureSnapshotError("platform_unverified")
    root = target_lease = monitor = None
    try:
        monitor = _WindowsPathMonitor(_windows_path_backend())
        root = windows_paths.inspect(authorized_root, backend=monitor, kind="directory", private=False,
                                     trusted_boundary=trusted_boundary)
        if not root.evidence.exists:
            raise SecureSnapshotError("authorized_root_missing")
        target_lease = windows_paths.inspect(target, backend=monitor, kind="file", private=True,
                                             single_link=True, trusted_boundary=trusted_boundary)
        ev = target_lease.evidence
        if not ev.direct_parent_observed:
            raise SecureSnapshotError("parent_missing")
        root_items = root.evidence.components
        target_items = ev.components
        if (len(target_items) < len(root_items) or any(
                a.name != b.name or a.observation != b.observation
                for a, b in zip(root_items, target_items))):
            raise SecureSnapshotError("authorized_root_changed")
        root.revalidate()
        target_lease.revalidate()
        value = _read_target(target, target_lease, max_bytes, trusted_boundary) if ev.exists else None
        target_lease.revalidate()
        root.revalidate()
        if monitor.release_unknown:
            raise SecureSnapshotError("release_unknown")
        return TargetSnapshot(target, authorized_root, ev.exists,
                              value.data if value else None, value,
                              root, target_lease, monitor, max_bytes, trusted_boundary)
    except BaseException as exc:
        failed = False
        for lease in (target_lease, root):
            if lease is not None:
                try:
                    lease.close()
                except BaseException:
                    failed = True
        if failed or (monitor is not None and monitor.release_unknown) or getattr(exc, "code", None) in (
                "release_unknown", "close_failed", "handle_close_failed", "resource_release_failed"):
            raise SecureSnapshotError("release_unknown") from None
        if isinstance(exc, SecureSnapshotError):
            raise
        if isinstance(exc, windows_paths.PathEvidenceError):
            raise SecureSnapshotError(exc.code) from None
        raise SecureSnapshotError("target_unverified") from None


@dataclass(frozen=True, repr=False)
class EmptyHandleObservation:
    """An instantaneous handle observation, NOT authority to write or commit."""
    identity: object = field(repr=False)
    marker: tuple = field(repr=False)
    protection: tuple = field(repr=False)


def inspect_empty_handle(snapshot: TargetSnapshot, owned: WindowsFileHandle,
                         *, acl_backend=None) -> EmptyHandleObservation:
    """Read-only, conservative protection comparison for an empty new handle.

    Only existing targets are supported. Equality includes the original
    owner, group, full ordered DACL and descriptor control, including
    inheritance/protection bits; private policy and token access are checked
    separately. The caller retains the handle even on failure.

    This does NOT bind the handle to an authorized temporary pathname, prove
    CREATE_NEW, guarantee that it remains empty/protected, authorize writing,
    or prove closure/rename/backup safety. The experimental candidate
    adapter uses this check alongside additional path checks; it is not
    connected to the production writer.
    """
    if not isinstance(snapshot, TargetSnapshot) or not isinstance(owned, WindowsFileHandle):
        raise SecureSnapshotError("handle_unverified")
    if not snapshot.exists or snapshot._read is None:
        raise SecureSnapshotError("creation_protection_unverified")
    try:
        with owned._state_lock:
            if owned._handle is None or owned.created is not True:
                raise SecureSnapshotError("handle_unverified")
            snapshot.revalidate()
            handle, api = owned.handle, owned._backend
            ident = file_identity(handle, backend=api)
            original = snapshot._read.identity
            if (ident.volume_serial != original.volume_serial or
                    ident.file_index == original.file_index):
                raise SecureSnapshotError("handle_unverified")
            before = api.file_marker(handle)
            if (not isinstance(before, tuple) or len(before) != 5 or
                    any(type(x) is not int or x < 0 for x in before) or
                    before[:2] != (ident.volume_serial, ident.file_index) or
                    before[2] <= 0 or before[3] <= 0 or before[4] != 0):
                raise SecureSnapshotError("handle_unverified")
            proof = windows_acl.read_stable_evidence(
                handle, kind="file", policy="private",
                required_access=windows_acl.FILE_GENERIC_READ | windows_acl.FILE_GENERIC_WRITE,
                backend=acl_backend)
            if (proof.observation.identity !=
                    (ident.volume_serial, ident.file_index, ident.link_count, ident.attributes) or
                    acl_protection(proof) != snapshot._read.protection):
                raise SecureSnapshotError("protection_changed")
            windows_acl.verify_unchanged(handle, proof, backend=acl_backend)
            if file_identity(handle, backend=api) != ident or api.file_marker(handle) != before:
                raise SecureSnapshotError("handle_changed")
            snapshot.revalidate()
            return EmptyHandleObservation(ident, before, acl_protection(proof))
    except BaseException as exc:
        if getattr(exc, "code", None) in (
                "resource_release_failed", "handle_close_failed", "close_failed", "release_unknown"):
            object.__setattr__(snapshot, "_failed", True)
            raise SecureSnapshotError("release_unknown") from None
        if isinstance(exc, SecureSnapshotError):
            raise
        if isinstance(exc, Exception):
            raise SecureSnapshotError("handle_unverified") from None
        raise


def _finite_write(snapshot, new_bytes, backend, preflight, *, make_backup=True,
                  artifact_preflight=None):
    """One finite transaction; backend evidence must be actual OS observations.

    Never retry or roll back a commit. Artifact paths may remain on failure.
    No process-wide freeze or alias key is claimed.
    """
    backup = stage = None
    backup_status = "NONE"
    backup_was_verified = False
    backup_read = stage_read = None
    state = "NOT_REPLACED"
    creation_unknown = release_unknown = False
    target_was_verified = False
    failed = False
    error_code = None
    primary_error = backup_error = None

    def check_artifact(role, path):
        if artifact_preflight is None:
            return
        failure = None
        try:
            authorized = artifact_preflight(role, path) is True
        except BaseException as exc:
            failure = exc
            authorized = False
        if not authorized:
            # Preserve the untrusted exception without inspecting it while
            # handles remain open. Public output is sanitized after finish.
            raise _ArtifactPreflightFailure(failure) from None

    try:
        try:
            if preflight() is not True:
                raise SecureWriteError("preflight_failed")
            backend.revalidate()
            if snapshot.exists and make_backup:
                backup = backend.reserve("backup")
                backup_status = "UNVERIFIED"
                check_artifact("backup", backup)
                creation_unknown = True
                backup_read = backend.create_and_write(backup, snapshot.contents)
                creation_unknown = False
                backup_status = "VERIFIED"
                backup_was_verified = True
            stage = backend.reserve("stage")
            check_artifact("stage", stage)
            creation_unknown = True
            stage_read = backend.create_and_write(stage, new_bytes)
            creation_unknown = False
            # The checks are observations, not a CAS against another writer.
            if backend.inspect(stage) != stage_read:
                raise SecureWriteError("stage_changed")
            if backup is not None:
                try:
                    current = backend.inspect(backup)
                except BaseException:
                    backup_status = "UNKNOWN"
                    raise
                if current != backup_read:
                    backup_status = "CHANGED"
                    raise SecureWriteError("backup_changed")
            if preflight() is not True:
                raise SecureWriteError("preflight_failed")
            if backup is not None:
                check_artifact("backup", backup)
            check_artifact("stage", stage)
            backend.revalidate()
            backend.release_precommit()
            state = "UNKNOWN"  # native commit can fail after side effects
            backend.commit(stage)
            state = "REPLACED"
            result = backend.postcheck()
            if (result.identity != stage_read.identity or result.data != new_bytes or
                    result.protection != stage_read.protection):
                raise SecureWriteError("postcheck_failed")
            state = "VERIFIED"
            target_was_verified = True
        except BaseException as exc:
            failed = True
            primary_error = exc
            if state == "REPLACED":
                state = "POSTCHECK_FAILED"
        # A commit can take time; separately re-observe the backup.
        if backup_read is not None and backup_status == "VERIFIED":
            try:
                if backend.inspect(backup) != backup_read:
                    backup_status = "CHANGED"
            except BaseException as exc:
                backup_status = "UNKNOWN"
                backup_error = exc
            if backup_status != "VERIFIED" and not failed:
                failed = True
                error_code = "backup_changed" if backup_status == "CHANGED" else "backup_unverified"
    except BaseException:
        # Even an unforeseen failure while handling an exception cannot skip
        # finish or escape as a sensitive raw exception.
        failed = True
        release_unknown = True
        error_code = None
        if state == "REPLACED":
            state = "POSTCHECK_FAILED"
        if backup_status == "VERIFIED":
            backup_status = "UNKNOWN"
    finally:
        try:
            backend.finish()
        except BaseException:
            release_unknown = True

    # Only after all backend-owned resources were given their cleanup attempt
    # may untrusted exception metadata be inspected. Unreadable metadata remains
    # UNKNOWN and no original exception is exposed to the caller.
    try:
        if primary_error is not None:
            if type(primary_error) is _ArtifactPreflightFailure:
                error_code = "artifact_preflight_failed"
                if primary_error.original is not None:
                    _, unknown, _ = _write_exception_info(primary_error.original)
                    release_unknown = release_unknown or unknown
            else:
                error_code, unknown, created = _write_exception_info(
                    primary_error, include_created=creation_unknown)
                if creation_unknown and created is not None:
                    creation_unknown = False
                release_unknown = release_unknown or unknown
        if backup_error is not None:
            _, unknown, _ = _write_exception_info(backup_error)
            release_unknown = release_unknown or unknown
    except BaseException:
        # A failure in the sanitizer is itself unknown; cleanup has completed.
        failed = True
        release_unknown = True
        error_code = None

    if release_unknown and backup_status == "VERIFIED":
        backup_status = "UNKNOWN"
    if failed or release_unknown or creation_unknown:
        if state == "VERIFIED":
            # This historical target observation is not a transaction receipt.
            state = "UNKNOWN"
        code = error_code or ("commit_unknown" if state == "UNKNOWN" else "write_unverified")
        if release_unknown:
            code = "release_unknown"
        elif creation_unknown:
            code = "creation_unknown"
        candidate = None
        try:
            if isinstance(snapshot, TargetSnapshot) and isinstance(stage_read, GuardedFileRead):
                candidate = _identity_observation(snapshot, stage_read)
        except BaseException:
            # Cleanup already happened; do not expose a raw observation error.
            candidate = None
        _raise_write_error(SecureWriteError(code, state=state, target_state=state, backup=backup,
                                           staging=stage, backup_status=backup_status,
                                           release_unknown=release_unknown,
                                           creation_unknown=creation_unknown,
                                           backup_was_verified=backup_was_verified, candidate=candidate,
                                           target_was_verified=target_was_verified))
    return WriteOutcome("VERIFIED", backup, backup_status == "VERIFIED", backup_status)


def write_target(snapshot: TargetSnapshot, new_bytes: bytes, *, preflight=None,
                 stage_directory=None, backup_directory=None, make_backup=True,
                 artifact_preflight=None, stage_boundary=None, backup_boundary=None,
                 stage_at_target_parent=False):
    """Write one ordinary Windows file, preserving private security evidence.

    The snapshot is transferred only after the backend is constructed. The
    final recheck and native commit are not a CAS against other local writers.
    Explicit runtime artifact directories must already exist, be private and
    permit creation. ``stage_at_target_parent=True`` opts into the authorized
    external-target exception: the explicit stage directory must be the
    observed direct target parent, which uses the ancestor policy. This flag
    grants no authorization and does not establish an external or cross-volume
    case; the caller must independently establish both and authorize target
    writing and each artifact. A cross-volume stage is
    rejected; callers may explicitly choose the authorized target parent for
    the narrowly permitted external-target exception. Backups may cross
    volumes. Defaults retain the original target-parent behavior.
    Explicit directories require ``artifact_preflight(role, path)``: the
    caller must independently authorize each candidate's purpose and check
    collisions with every other protected role, including absent/ambiguous
    aliases. It runs before each creation and again before commit, and must
    return exactly True. An exception or any other result fails closed.
    Legacy calls without explicit directories may omit the callback; then only
    target and transaction-candidate alias checks are available.
    For explicitly supplied artifact directories, the internal caller may
    independently supply their project-root or authorized external-parent
    boundaries. These never grant write or artifact authorization. Omission
    keeps a strict walk for explicit directories.
    """
    if (not isinstance(snapshot, TargetSnapshot) or not isinstance(new_bytes, bytes)
            or len(new_bytes) > getattr(snapshot, "_max_bytes", -1)
            or not callable(preflight) or type(make_backup) is not bool
            or type(stage_at_target_parent) is not bool
            or (stage_at_target_parent and stage_directory is None)
            or (artifact_preflight is not None and not callable(artifact_preflight))
            or any(value is not None and not isinstance(value, str)
                   for value in (stage_directory, backup_directory, stage_boundary, backup_boundary))
            or (stage_directory is None and stage_boundary is not None)
            or (backup_directory is None and backup_boundary is not None)):
        _raise_write_error(SecureWriteError("windows_secure_write_unverified"))
    if (stage_directory is not None or backup_directory is not None) and artifact_preflight is None:
        _raise_write_error(SecureWriteError("artifact_preflight_required"))
    from core.windows_secure_native import ProductWriteBackend
    # The first preflight runs before backend construction, so ownership of the
    # snapshot remains with the caller on failure. Normalize every exception,
    # including SecureWriteError, and never retain its traceback or chain.
    failed = False
    error_code = None
    release_unknown = False
    try:
        authorized = preflight() is True
    except BaseException as exc:
        error_code, release_unknown, _ = _write_exception_info(exc)
        authorized = False
    if not authorized:
        _raise_write_error(SecureWriteError(
            "release_unknown" if release_unknown else (error_code or "preflight_failed"),
            release_unknown=release_unknown))
    options = {}
    if stage_directory is not None:
        options["stage_directory"] = stage_directory
    if stage_at_target_parent:
        options["stage_at_target_parent"] = True
    if backup_directory is not None:
        options["backup_directory"] = backup_directory
    if stage_boundary is not None:
        options["stage_boundary"] = stage_boundary
    if backup_boundary is not None:
        options["backup_boundary"] = backup_boundary
    try:
        backend = ProductWriteBackend(snapshot, **options)
    except BaseException as exc:
        error_code, release_unknown, _ = _write_exception_info(exc)
        failed = True
    if failed:
        _raise_write_error(SecureWriteError(
            "release_unknown" if release_unknown else (error_code or "backend_unavailable"),
            release_unknown=release_unknown))
    return _finite_write(snapshot, new_bytes, backend, preflight, make_backup=make_backup,
                         artifact_preflight=artifact_preflight)
