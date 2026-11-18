"""Mac business-config replacement, against an explicitly authorized target.

A held directory descriptor and repeated checks detect observable drift; the
last check and replace are *not* a compare-and-swap. Pause non-cooperative
editors. The caller must independently authorize the exact target and the
chosen backup and staging locations, and supply a preflight which binds
runtime/sidecar conflict checks. Legacy calls use authorized siblings.
"""
from __future__ import annotations

import os
import secrets
import stat
import sys
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Callable

from core.mac_config_path import capture_config, verify_config, open_target_parent, _entry_meta
from core.secure_files import (_open_private, _mac_fd_protection, _same_volume,
                               RuntimeWriteLocations, open_private_directory,
                               verify_private_directory, _safe_write_error_code,
                               _safe_error_flag, _safe_error_state)


@dataclass(frozen=True, repr=False)
class WriteAuthorization:
    """Caller-approved exact resolved target and parent inode, not a read root.

    ``target_path`` must be selected by an actual authorization policy, *not*
    copied from an untrusted symlink merely because it could be read. The
    policy must also authorize the private backup and staging siblings.
    """
    target_path: str
    parent_identity: tuple[int, int]
    target_identity: tuple[int, int]
    allow_backup_and_staging: bool = False
    # Set only when the caller obtained explicit authorization for this exact
    # external target. It permits the narrow target-parent staging exception
    # when the real staging and target parent differ in volume; the writer
    # still enforces that the volumes actually differ.
    external_confirmed: bool = False


@dataclass(frozen=True, repr=False)
class WriteOutcome:
    state: str
    backup_name: str
    marker: object = field(repr=False)
    snapshot: object = field(repr=False)


class MacWriteError(RuntimeError):
    def __init__(self, code, *, state='NOT_REPLACED', backup_name=None,
                 staging_name=None, cleanup_unknown=False, close_unknown=False,
                 marker_unknown=False):
        super().__init__(code)
        self.code = code
        self.state = state
        self.backup_name = backup_name
        self.staging_name = staging_name
        self.cleanup_unknown = cleanup_unknown
        self.close_unknown = close_unknown
        self.marker_unknown = marker_unknown


def _authorize(snapshot, authorization, fd, name, *, legacy=True):
    if not isinstance(authorization, WriteAuthorization):
        raise MacWriteError('write_authorization_missing')
    if ((legacy and authorization.allow_backup_and_staging is not True)
            or not isinstance(authorization.target_path, str)
            or not authorization.target_path.startswith('/')
            or authorization.target_path != snapshot.target_path
            or name in ('', '.', '..') or '/' in name
            or os.path.basename(snapshot.target_path) != name
            or os.path.dirname(snapshot.target_path) == '/'):
        raise MacWriteError('target_outside_authorization')
    parent = os.fstat(fd)
    if ((parent.st_dev, parent.st_ino) != authorization.parent_identity
            or tuple(snapshot.final_identity[:2]) != authorization.target_identity):
        raise MacWriteError('authorization_identity_changed')
    if snapshot.final_identity[5] != 1:
        raise MacWriteError('hardlinks_not_preserved')


def validate_write_authorization(snapshot, authorization, *, locations=None):
    """Reject an unapproved or unprovable target before user editing starts."""
    verify_config(snapshot)
    with open_target_parent(snapshot) as (fd, name):
        _authorize(snapshot, authorization, fd, name, legacy=locations is None)
        if locations is not None:
            _validate_locations(locations)
            with open_private_directory(locations.staging, purpose='staging') as temp_fd:
                with open_private_directory(locations.backup, purpose='backup') as backup_fd:
                    _check_location_devices(locations, temp_fd, backup_fd, fd,
                                            external_confirmed=authorization.external_confirmed)
    verify_config(snapshot)


def _validate_locations(locations):
    if (not isinstance(locations, RuntimeWriteLocations)
            or type(locations.allow_target_staging) is not bool):
        raise MacWriteError('write_locations_invalid')


def _check_location_devices(locations, temp_fd, backup_fd, target_fd,
                            *, external_confirmed=False):
    temp, backup = os.fstat(temp_fd), os.fstat(backup_fd)
    if (temp.st_dev, temp.st_ino) == (backup.st_dev, backup.st_ino):
        raise MacWriteError('backup_staging_not_distinct')
    same = _same_volume(temp_fd, target_fd)
    # The narrow target-parent exception applies only to an explicitly
    # authorized external target, and only when the volumes really differ.
    if not same and not (locations.allow_target_staging is True and external_confirmed):
        raise MacWriteError('cross_volume_staging_not_authorized')
    return same


def _check_preflight(preflight, snapshot):
    if not callable(preflight) or preflight(snapshot) is not True:
        raise MacWriteError('write_preflight_unverified')


def _check_created(fd, name, identity, contents, proof, *, cross_volume=False):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    file_fd = os.open(name, flags, dir_fd=fd)
    try:
        info = os.fstat(file_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != identity):
            raise MacWriteError('staging_identity_changed')
        metadata = _entry_meta(info)
        actual = _mac_fd_protection(file_fd, directory=False)
        # Backups may be on a different filesystem; compare security metadata,
        # not filesystem identity. The backend separately verifies each FS.
        if ((actual[2:7] != proof[2:7] or actual[8:] != proof[8:])
                if cross_volume else actual[2:] != proof[2:]):
            raise MacWriteError('staging_protection_mismatch')
        data = bytearray()
        while len(data) <= len(contents):
            piece = os.read(file_fd, min(65536, len(contents) + 1 - len(data)))
            if not piece:
                break
            data.extend(piece)
        if bytes(data) != contents or _mac_fd_protection(file_fd, directory=False) != actual:
            raise MacWriteError('staging_changed')
        current = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if _entry_meta(os.fstat(file_fd)) != metadata or _entry_meta(current) != metadata:
            raise MacWriteError('staging_changed')
    finally:
        active = sys.exc_info()[1]
        try:
            os.close(file_fd)
        except BaseException:
            raise MacWriteError('staging_close_unknown', close_unknown=True,
                                cleanup_unknown=_safe_error_flag(active, 'cleanup_unknown'),
                                marker_unknown=_safe_error_flag(active, 'marker_unknown')) from None


def replace_config(snapshot, new_bytes: bytes, authorization: WriteAuthorization,
                   *, preflight: Callable, before_replace: Callable,
                   boundary_guard: Callable | None = None,
                   locations: RuntimeWriteLocations | None = None) -> WriteOutcome:
    """Back up and replace the actual target; no silent rollback on failure.

    ``preflight(snapshot)`` must recheck the real target against runtime,
    sidecar, fixed-state and other conflicts and return literal True.
    ``before_replace()`` records the caller's in-progress marker.
    ``boundary_guard()`` may recheck the caller's peer before each creation
    and replacement; it is not a lock or a compare-and-swap. None grants
    authorization. The supplied snapshot is captured before editing.

    With ``locations``, its staging and backup directories are separately
    authorized by the caller. Target-side staging is allowed only when the
    caller explicitly approves the applicable cross-volume exception.
    In that mode backup_name/staging_name are absolute paths; legacy calls
    keep their existing sibling basenames and authorization semantics.
    """
    state = 'NOT_REPLACED'
    backup = temp = None
    temp_identity = None
    temp_fd = runtime_temp_fd = None
    cleanup_unknown = close_unknown = marker_unknown = False
    try:
        limit = snapshot.max_bytes
        if (not isinstance(new_bytes, bytes)
                or (limit is not None and (type(limit) is not int or limit < 0
                                           or len(new_bytes) > limit))):
            raise MacWriteError('invalid_new_content')
        if not callable(before_replace):
            raise MacWriteError('commit_marker_unavailable')
        if boundary_guard is not None and not callable(boundary_guard):
            raise MacWriteError('boundary_guard_unavailable')
        def guard():
            if boundary_guard is not None:
                boundary_guard()
        verify_config(snapshot)
        _check_preflight(preflight, snapshot)
        with open_target_parent(snapshot) as (parent_fd, name), ExitStack() as dirs:
            try:
                _authorize(snapshot, authorization, parent_fd, name, legacy=locations is None)
                verify_config(snapshot)
                _check_preflight(preflight, snapshot)
                temp_fd = backup_fd = parent_fd
                runtime_temp_fd = None
                if locations is not None:
                    _validate_locations(locations)
                    runtime_temp_fd = dirs.enter_context(open_private_directory(
                        locations.staging, purpose='staging'))
                    backup_fd = dirs.enter_context(open_private_directory(
                        locations.backup, purpose='backup'))
                    same_volume = _check_location_devices(
                        locations, runtime_temp_fd, backup_fd, parent_fd,
                        external_confirmed=authorization.external_confirmed)
                    temp_fd = runtime_temp_fd if same_volume else parent_fd

                def verify_locations():
                    if locations is not None:
                        verify_private_directory(locations.staging, runtime_temp_fd)
                        verify_private_directory(locations.backup, backup_fd)
                        same = _check_location_devices(
                            locations, runtime_temp_fd, backup_fd, parent_fd,
                            external_confirmed=authorization.external_confirmed)
                        if (temp_fd == runtime_temp_fd) != same:
                            raise MacWriteError('staging_volume_changed')

                proof = snapshot.protection[1]
                mode, uid, gid = stat.S_IMODE(proof[2]), proof[3], proof[4]
                backup_entry = name + '.bak.' + secrets.token_hex(8)
                backup = (os.path.join(locations.backup.path, backup_entry)
                          if locations is not None else backup_entry)
                try:
                    guard()
                    verify_locations()
                    backup_id = _open_private(backup_fd, backup_entry, snapshot.data,
                                              mode, uid, gid, proof[-1])
                except BaseException as exc:
                    if not _safe_error_flag(exc, 'cleanup_unknown'):
                        backup = None
                    raise
                _check_created(backup_fd, backup_entry, backup_id, snapshot.data, proof,
                               cross_volume=locations is not None)
                temp = '.model-proxy-config-' + secrets.token_hex(12)
                guard()
                verify_locations()
                temp_identity = _open_private(temp_fd, temp, new_bytes,
                                               mode, uid, gid, proof[-1])
                _check_created(temp_fd, temp, temp_identity, new_bytes, proof)
                verify_config(snapshot)
                _authorize(snapshot, authorization, parent_fd, name, legacy=locations is None)
                _check_preflight(preflight, snapshot)
                verify_locations()
                _check_created(backup_fd, backup_entry, backup_id, snapshot.data, proof,
                               cross_volume=locations is not None)
                _check_created(temp_fd, temp, temp_identity, new_bytes, proof)
                before_replace()
                # Marker callbacks and preflight can themselves take time.
                verify_config(snapshot)
                _authorize(snapshot, authorization, parent_fd, name, legacy=locations is None)
                _check_preflight(preflight, snapshot)
                verify_locations()
                _check_created(backup_fd, backup_entry, backup_id, snapshot.data, proof,
                               cross_volume=locations is not None)
                _check_created(temp_fd, temp, temp_identity, new_bytes, proof)
                verify_config(snapshot)
                _authorize(snapshot, authorization, parent_fd, name, legacy=locations is None)
                verify_locations()
                guard()
                # Guard callbacks can themselves observe or alter paths.
                verify_config(snapshot)
                _authorize(snapshot, authorization, parent_fd, name, legacy=locations is None)
                verify_locations()
                _check_created(backup_fd, backup_entry, backup_id, snapshot.data, proof,
                               cross_volume=locations is not None)
                _check_created(temp_fd, temp, temp_identity, new_bytes, proof)
                state = 'UNKNOWN'  # syscall may have happened if it fails/interrupted
                os.replace(temp, name, src_dir_fd=temp_fd, dst_dir_fd=parent_fd)
                state = 'REPLACED'
                temp = None
                final = capture_config(snapshot.source_path, max_bytes=snapshot.max_bytes)
                if (final.target_path != snapshot.target_path or final.data != new_bytes
                        or tuple(final.final_identity[:2]) != temp_identity
                        or final.final_identity[5] != 1
                        or final.chain[:-1] != snapshot.chain[:-1]
                        or final.target_parent_protection != snapshot.target_parent_protection
                        or final.protection[1][2:] != proof[2:]):
                    raise MacWriteError('postcheck_failed')
                _check_created(parent_fd, name, temp_identity, new_bytes, proof)
                verify_locations()
                _check_created(backup_fd, backup_entry, backup_id, snapshot.data, proof,
                               cross_volume=locations is not None)
                state = 'VERIFIED'
                return WriteOutcome('VERIFIED', backup, final.marker, final)
            except BaseException as exc:
                cleanup_unknown = cleanup_unknown or _safe_error_flag(exc, 'cleanup_unknown')
                close_unknown = close_unknown or _safe_error_flag(exc, 'close_unknown')
                marker_unknown = marker_unknown or _safe_error_flag(exc, 'marker_unknown')
                raise
            finally:
                staging_cleanup_unknown = False
                if temp and temp_identity:
                    try:
                        info = os.stat(temp, dir_fd=temp_fd, follow_symlinks=False)
                        if (info.st_dev, info.st_ino) == temp_identity:
                            os.unlink(temp, dir_fd=temp_fd)
                        else:
                            staging_cleanup_unknown = True
                    except FileNotFoundError:
                        pass
                    except BaseException:
                        staging_cleanup_unknown = True
                cleanup_unknown = cleanup_unknown or staging_cleanup_unknown
                if staging_cleanup_unknown:
                    active = sys.exc_info()[1]
                    close_unknown = (close_unknown or _safe_error_flag(active, 'close_unknown')
                                     or _safe_write_error_code(active) == 'fd_close_unknown')
                    staging_path = (os.path.join(
                        locations.staging.path if temp_fd == runtime_temp_fd
                        else os.path.dirname(snapshot.target_path), temp)
                        if locations is not None else temp)
                    raise MacWriteError('staging_cleanup_unknown', state=state,
                                        backup_name=backup, staging_name=staging_path,
                                        cleanup_unknown=True,
                                        close_unknown=close_unknown,
                                        marker_unknown=(marker_unknown or
                                                        _safe_error_flag(active, 'marker_unknown'))) from None
    except BaseException as exc:
        # The backend reports a close failure by its sanitized error code.
        code = _safe_write_error_code(
            exc, default='interrupted' if issubclass(type(exc), KeyboardInterrupt)
            else 'filesystem_or_backend_error')
        close_unknown = (close_unknown or _safe_error_flag(exc, 'close_unknown')
                         or code == 'fd_close_unknown')
        cleanup_unknown = cleanup_unknown or _safe_error_flag(exc, 'cleanup_unknown')
        marker_unknown = marker_unknown or _safe_error_flag(exc, 'marker_unknown')
        if state == 'NOT_REPLACED' and issubclass(type(exc), MacWriteError):
            state = _safe_error_state(exc)
        if state in ('REPLACED', 'VERIFIED'):
            state = 'POSTCHECK_FAILED'
        staging_name = None
        if cleanup_unknown and temp:
            staging_name = (os.path.join(
                locations.staging.path if temp_fd == runtime_temp_fd
                else os.path.dirname(snapshot.target_path), temp)
                if locations is not None else temp)
        raise MacWriteError(code, state=state, backup_name=backup,
                            staging_name=staging_name,
                            cleanup_unknown=cleanup_unknown, close_unknown=close_unknown,
                            marker_unknown=marker_unknown) from None
