"""Protected ledger adapters for existing macOS and Windows file primitives.

The caller must provide the real held-lock, runtime-role and authorization
guard; this module is not an authority and never constructs a permissive guard.

No fsync/crash-durability or same-user/ABA guarantee is implied.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import sys
from pathlib import Path


log = logging.getLogger(__name__)


def _resource_unknown(exc):
    try:
        return (bool(getattr(exc, 'close_unknown', False))
                or bool(getattr(exc, 'cleanup_unknown', False))
                or bool(getattr(exc, 'release_unknown', False))
                or getattr(exc, 'code', '') in
                ('fd_close_unknown', 'release_unknown', 'lock_release_unknown',
                 'handle_close_failed', 'resource_release_failed', 'close_failed',
                 'parent_close_unknown', 'staging_close_unknown',
                 'staging_cleanup_unknown', 'staging_cleanup_failed'))
    except Exception:
        return True


class LedgerIOError(RuntimeError):
    """Sanitized ledger adapter error; never includes contents or paths."""


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate')
        value[key] = item
    return value


class MacLedgerSecureIO:
    """Use the protected Mac capture/runtime-replace chain, not a fake backend."""
    def __init__(self, *, root, target, guard, max_bytes=sys.maxsize):
        from core.server import _LedgerRuntimeGuard
        if (sys.platform != 'darwin' or os.name != 'posix'
                or type(guard) is not _LedgerRuntimeGuard):
            raise LedgerIOError('platform_or_guard_unavailable')
        self.root = Path(root)
        self.target = Path(target)
        self.staging = self.root / '.runtime' / 'tmp'
        if (self.root != guard.root or self.target != guard.target
                or not self.root.is_absolute() or not self.target.is_absolute()
                or '..' in self.target.parts or type(max_bytes) is not int or max_bytes <= 0):
            raise LedgerIOError('target_not_supported')
        self.guard = guard
        self.boundary = guard.boundary
        self.external = guard.external
        self.max_bytes = max_bytes
        self._resources_unknown = False

    def _quarantine_resources(self):
        if not self._resources_unknown:
            self._resources_unknown = True
            log.warning('usage_totals.persistence resource release or staging cleanup unverified; writes paused')

    def _check_path(self, path):
        if Path(path) != self.target:
            raise LedgerIOError('target_changed')

    def _capture(self):
        from core import secure_files as secure
        if self._resources_unknown:
            raise LedgerIOError('resource_release_unverified')
        try:
            self.guard()
            result = secure.capture_target(
                self.target, authorized_root=self.target.parent,
                protection_reader=secure.mac_private_protection,
                max_bytes=self.max_bytes, trusted_boundary=self.boundary)
            self.guard()
            again = secure.capture_target(
                self.target, authorized_root=self.target.parent,
                protection_reader=secure.mac_private_protection,
                max_bytes=self.max_bytes, trusted_boundary=self.boundary)
            if result != again:
                raise LedgerIOError('target_changed')
            return again
        except Exception as exc:
            if _resource_unknown(exc):
                self._quarantine_resources()
            raise

    @staticmethod
    def _decoded(raw, marker):
        from core.config_snapshot import ConfigSnapshot
        from core.server import validate_migration_ledger_snapshot
        try:
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
            if type(value) is not dict:
                raise ValueError('not_object')
            validate_migration_ledger_snapshot(ConfigSnapshot(value, marker))
            return value
        except Exception:
            raise LedgerIOError('ledger_invalid') from None

    def _observation(self, snap):
        from core.server import LedgerObservation
        if not snap.exists:
            return LedgerObservation(None, ('darwin', snap.comparable_identity, None, None))
        from core.config_snapshot import FileMarker
        info = snap.identity
        marker = FileMarker(info[0], info[1], info[7], info[8], info[6])
        data = self._decoded(snap.contents, marker)
        return LedgerObservation(data, ('darwin', snap.comparable_identity, info[7], snap.contents))

    def _candidate(self, candidate, payload, data):
        from core.secure_files import StagedCandidate
        from core.server import LedgerObservation
        if (not isinstance(candidate, StagedCandidate)
                or candidate.digest != hashlib.sha256(payload).digest()):
            return None
        return LedgerObservation(copy.deepcopy(data),
                                 ('darwin', candidate.comparable_identity, candidate.mtime_ns, payload))

    def bind_startup(self, path, snapshot):
        from core.config_snapshot import ConfigSnapshot, FileMarker
        self._check_path(path)
        snap = self._capture()
        observed = self._observation(snap)
        if snapshot is None:
            if snap.exists:
                raise LedgerIOError('startup_changed')
        elif isinstance(snapshot, ConfigSnapshot) and snap.exists:
            info = snap.identity
            marker = FileMarker(info[0], info[1], info[7], info[8], info[6])
            if marker != snapshot.marker or observed.data != snapshot.config:
                raise LedgerIOError('startup_changed')
        else:
            raise LedgerIOError('startup_unavailable')
        return observed

    def observe(self, path):
        self._check_path(path)
        return self._observation(self._capture())

    @staticmethod
    def matches(observed, prior):
        from core.server import LedgerObservation
        return (isinstance(observed, LedgerObservation) and isinstance(prior, LedgerObservation)
                and observed.evidence is not None and observed.evidence == prior.evidence
                and observed.data == prior.data)

    @staticmethod
    def _same_volume_evidence(staging, target):
        source_device = staging.protection[0]
        target_device = target.parents[-1][1][0]
        if type(source_device) is not int or type(target_device) is not int or source_device <= 0 or target_device <= 0:
            raise LedgerIOError('volume_unverified')
        return source_device == target_device

    def commit(self, path, baseline, data):
        from core import secure_files as secure
        from core.server import LedgerCommitResult
        entered = False
        payload = None
        try:
            self._check_path(path)
            payload = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False).encode('utf-8')
            if len(payload) > self.max_bytes:
                raise LedgerIOError('too_large')
            self.guard()
            current = self._capture()
            if not self.matches(self._observation(current), baseline):
                raise LedgerIOError('baseline_changed')
            # This method performs actual role/lock checks and exclusive mkdir,
            # and verifies the protected directory before returning its snapshot.
            staging = self.guard.prepare_staging()
            same_volume = self._same_volume_evidence(staging, current)
            if not same_volume and not self.external:
                raise LedgerIOError('cross_volume_not_authorized')
            use_target_parent = self.external and not same_volume
            locations = secure.RuntimeWriteLocations(staging, None, use_target_parent)
            def boundary_guard():
                self.guard()
                if use_target_parent:
                    self.guard.check_mac_target_staging()
            entered = True
            outcome = secure.secure_replace_runtime(
                current, payload, max_bytes=self.max_bytes, locations=locations,
                make_backup=False, boundary_guard=boundary_guard)
        except secure.SecureWriteError as exc:
            resource_unknown = _resource_unknown(exc)
            if resource_unknown:
                self._quarantine_resources()
            unknown = exc.state != 'NOT_REPLACED' or resource_unknown
            candidate = self._candidate(exc.candidate, payload, data) if payload is not None and unknown else None
            return LedgerCommitResult('UNKNOWN' if unknown else 'NOT_APPLIED', candidate)
        except Exception as exc:
            # An unclassified interruption inside the writer also leaves its
            # resource-release/cleanup result unproven. No blind retry.
            resource_unknown = _resource_unknown(exc) or entered
            if resource_unknown:
                self._quarantine_resources()
            return LedgerCommitResult('UNKNOWN' if resource_unknown else 'NOT_APPLIED')
        candidate = self._candidate(outcome.candidate, payload, data)
        try:
            if outcome.state != 'VERIFIED' or candidate is None:
                raise LedgerIOError('postcheck_unverified')
            final = self.observe(path)
            if not self.matches(final, candidate):
                raise LedgerIOError('postcheck_changed')
            return LedgerCommitResult('CONFIRMED', candidate, final)
        except Exception:
            return LedgerCommitResult('UNKNOWN', candidate)


class WindowsLedgerSecureIO:
    """Native Windows target leases and finite protected atomic commits.

    This path has only fake-backend coverage until separately validated on
    native Windows. No staged artifact or unknown HANDLE is silently cleaned.
    """
    def __init__(self, *, root, target, guard, max_bytes=sys.maxsize):
        from core.server import _LedgerRuntimeGuard
        if sys.platform != 'win32' or os.name != 'nt' or type(guard) is not _LedgerRuntimeGuard:
            raise LedgerIOError('platform_or_guard_unavailable')
        self.root, self.target = os.fspath(root), os.fspath(target)
        if self.root != os.fspath(guard.root) or self.target != os.fspath(guard.target):
            raise LedgerIOError('target_not_authorized')
        self.staging, self.boundary = os.fspath(guard.staging), os.fspath(guard.boundary)
        self.external, self.guard = guard.external, guard
        if type(max_bytes) is not int or max_bytes <= 0:
            raise LedgerIOError('invalid_limit')
        self.max_bytes = max_bytes
        self._resources_unknown = False

    def _quarantine_resources(self):
        if not self._resources_unknown:
            self._resources_unknown = True
            log.warning('usage_totals.persistence Windows release or staging cleanup unverified; writes paused')

    def _check_path(self, path):
        if os.fspath(path) != self.target:
            raise LedgerIOError('target_changed')
        if self._resources_unknown:
            raise LedgerIOError('resource_release_unverified')

    def _capture(self):
        from core import windows_secure_files as secure
        import ntpath
        self.guard()
        return secure.capture_target(self.target, authorized_root=ntpath.dirname(self.target),
                                     max_bytes=self.max_bytes, trusted_boundary=self.boundary)

    @staticmethod
    def _parent(snapshot):
        ev = snapshot._target_lease.evidence
        parts = ev.components[:-1] if snapshot.exists else ev.components
        return (ev.drive, tuple((part.name, part.observation) for part in parts))

    def _observation(self, snapshot):
        from core.server import LedgerObservation
        from core.config_snapshot import FileMarker
        obs = snapshot.identity_observation
        parent = self._parent(snapshot)
        if not snapshot.exists:
            return LedgerObservation(None, ('windows', self.target, parent, None, None, None))
        marker = FileMarker(*obs.marker)
        data = MacLedgerSecureIO._decoded(snapshot.contents, marker)
        return LedgerObservation(data, ('windows', self.target, parent, obs.file_identity,
                                        obs.file_protection, snapshot.contents))

    def _candidate(self, parent, parent_obs, observation, payload, data):
        from core.windows_secure_files import IdentityObservation
        from core.server import LedgerObservation
        if (not isinstance(observation, IdentityObservation) or not observation.exists
                or observation.parent != parent_obs or observation.file_identity is None
                or observation.file_protection is None or not isinstance(observation.marker, tuple)
                or len(observation.marker) != 5 or observation.marker[4] != len(payload)):
            return None
        return LedgerObservation(copy.deepcopy(data), ('windows', self.target, parent,
                                 observation.file_identity, observation.file_protection, payload))

    def _observe(self):
        from core.config_snapshot import FileMarker
        snap = None
        try:
            snap = self._capture()
            self.guard()
            snap.revalidate()
            observed = self._observation(snap)
            marker = FileMarker(*snap.identity_observation.marker) if snap.exists else None
            return observed, marker
        except Exception as exc:
            if _resource_unknown(exc):
                self._quarantine_resources()
            raise
        finally:
            if snap is not None:
                try:
                    snap.close()
                except Exception:
                    self._quarantine_resources()
                    raise LedgerIOError('resource_release_unverified') from None

    def bind_startup(self, path, snapshot):
        from core.config_snapshot import ConfigSnapshot
        self._check_path(path)
        observed, marker = self._observe()
        if snapshot is None:
            if observed.data is not None:
                raise LedgerIOError('startup_changed')
        elif not isinstance(snapshot, ConfigSnapshot) or snapshot.marker != marker or snapshot.config != observed.data:
            raise LedgerIOError('startup_changed')
        return observed

    def observe(self, path):
        self._check_path(path)
        return self._observe()[0]

    matches = staticmethod(MacLedgerSecureIO.matches)

    def _stage_directory(self, snapshot):
        from core import windows_paths
        import ntpath
        self.guard.prepare_staging()
        lease = None
        try:
            lease = windows_paths.inspect(self.staging, backend=snapshot._monitor,
                                          kind='directory', private=True,
                                          trusted_boundary=self.root)
            lease.revalidate()
            snapshot.revalidate()
            src = lease._handles[-1].volume
            dst = snapshot._target_lease._handles[-2 if snapshot.exists else -1].volume
            if not src or not dst:
                raise LedgerIOError('volume_unverified')
            if src == dst:
                return self.staging
            if not self.external:
                raise LedgerIOError('cross_volume_not_authorized')
            return ntpath.dirname(self.target)
        finally:
            if lease is not None:
                try:
                    lease.close()
                except Exception:
                    self._quarantine_resources()
                    raise LedgerIOError('resource_release_unverified') from None

    def commit(self, path, baseline, data):
        from core import windows_secure_files as secure
        from core.windows_secure_native import ProductWriteBackend
        from core.server import LedgerCommitResult

        class RecordingBackend(ProductWriteBackend):
            finish_attempted = False
            def postcheck(inner):
                inner.verified_result = super().postcheck()
                return inner.verified_result
            def finish(inner):
                try:
                    return super().finish()
                finally:
                    inner.finish_attempted = True

        snap = backend = None
        parent = parent_obs = stage = payload = candidate = None
        entered = writer_returned = False
        result = None
        try:
            self._check_path(path)
            payload = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False).encode('utf-8')
            if len(payload) > self.max_bytes:
                raise LedgerIOError('too_large')
            snap = self._capture()
            snap.revalidate()
            if not self.matches(self._observation(snap), baseline):
                raise LedgerIOError('baseline_changed')
            parent = self._parent(snap)
            parent_obs = snap.identity_observation.parent
            stage = self._stage_directory(snap)
            self.guard()
            options = (dict(stage_directory=stage, stage_boundary=self.root)
                       if stage == self.staging else
                       dict(stage_directory=stage, stage_boundary=self.boundary, stage_at_target_parent=True))
            backend = RecordingBackend(snap, **options)
            def preflight():
                return self.guard() is True
            def artifact_preflight(role, artifact):
                return role == 'stage' and self.guard.check_windows_artifact(stage, artifact) is True
            entered = True
            outcome = secure._finite_write(snap, payload, backend, preflight,
                                            make_backup=False, artifact_preflight=artifact_preflight)
            writer_returned = True
            read = backend._stage_read
            obs = secure.IdentityObservation(True, parent_obs, read.identity, read.protection, read.marker) if read else None
            candidate = self._candidate(parent, parent_obs, obs, payload, data)
            checked = backend.verified_result
            if (outcome.state != 'VERIFIED' or candidate is None
                    or checked.identity != read.identity or checked.data != payload
                    or checked.protection != read.protection):
                raise LedgerIOError('postcheck_unverified')
            final = self.observe(path)
            if not self.matches(final, candidate):
                raise LedgerIOError('postcheck_changed')
            result = LedgerCommitResult('CONFIRMED', candidate, final)
        except secure.SecureWriteError as exc:
            resource_unknown = exc.release_unknown or exc.creation_unknown
            if exc.staging is not None and not resource_unknown:
                try:
                    # The finite writer reports the reserved name even when a
                    # native move consumed it. Verify absence, never delete it.
                    resource_unknown = not self.guard.check_windows_artifact(
                        stage, exc.staging, require_absent=True)
                except Exception:
                    resource_unknown = True
            if resource_unknown:
                self._quarantine_resources()
            unknown = exc.state != 'NOT_REPLACED' or resource_unknown
            candidate = self._candidate(parent, parent_obs, exc.candidate, payload, data) if unknown and payload is not None else None
            result = LedgerCommitResult('UNKNOWN' if unknown else 'NOT_APPLIED', candidate)
        except Exception as exc:
            resource_unknown = _resource_unknown(exc) or (entered and not writer_returned)
            if resource_unknown:
                self._quarantine_resources()
            unknown = entered or resource_unknown or self._resources_unknown
            result = LedgerCommitResult('UNKNOWN' if unknown else 'NOT_APPLIED', candidate)
        finally:
            try:
                if backend is not None and not backend.finish_attempted:
                    backend.finish()
                elif backend is None and snap is not None:
                    snap.close()
            except Exception:
                self._quarantine_resources()
                result = LedgerCommitResult('UNKNOWN')
        return result
