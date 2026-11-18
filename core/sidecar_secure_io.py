"""Protected sidecar adapter; not a deployment or a new authorization source.

Integration contract (all arguments are internal, not user-controlled policy):

* Instantiate ``SidecarSecureIO(project_root=..., target=..., guard=...)`` and
  inject it into SessionOverridesSidecar(target, secure_io=adapter). target is
  the sidecar under the *original* config path's parent, not the config's
  resolved target parent. The instance and sidecar thread locks must be held by
  the existing caller. This module does not acquire them.
* ``guard(original_target, actual_target, staging_directory, artifact)`` must return exactly True or
  raise. It must re-observe the original path chain, independently authorize
  this exact target and its parent, and check it and the staging namespace
  against every protected role, including absent/ambiguous aliases. When
  artifact is None it must prove the *entire* writer-generated temporary-name
  namespace disjoint from those roles (Mac's writer does not expose the name
  before creation). If an artifact is supplied it must also check that exact
  artifact. If the external-target staging exception is enabled, the guard is
  also called for that target-parent namespace before staging. It must
  revalidate the already-held instance-lock and caller's path/role evidence.
  A trivial True callback is not a production guard. No such complete guard is
  currently wired by this module; integration must supply and audit one.
* .runtime/tmp must already exist, be private, and be authorized for staging.
  A target outside project_root is an external exception only if guard proves
  it derives from an explicitly selected external config. To authorize that
  specific exception the integration must also pass
  ``allow_external_target_staging=True``. The writer selects the target parent
  only when the actual target and runtime staging differ in real volume.

Mac preserves original-link evidence and refuses unprovable or dangling paths.
The writes are not CAS and provide no crash durability or
protection against unobservable ABA. No sensitive bytes occur in diagnostics.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
import stat
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from core.commands import SidecarSnapshot, SidecarWriteError

log = logging.getLogger(__name__)


class SidecarIOError(RuntimeError):
    """A sanitized capture/authorization failure, never a proved absence."""


_RELEASE_CODES = frozenset(('fd_close_unknown', 'release_unknown', 'close_failed',
                            'handle_close_failed', 'resource_release_failed'))


def _resource_uncertainty(exc):
    """Read only fixed flags/codes; uninspectable exception means uncertainty."""
    try:
        code = getattr(exc, 'code', None)
        close = getattr(exc, 'close_unknown', False) is True
        release = getattr(exc, 'release_unknown', False) is True
        cleanup = getattr(exc, 'cleanup_unknown', False) is True
        known_code = type(code) is str and code in _RELEASE_CODES
        close = close or (type(code) is str and code == 'fd_close_unknown')
        return close, release or close or cleanup or known_code, cleanup
    except BaseException:
        return True, True, True


def _capture_error(exc):
    close, release, cleanup = _resource_uncertainty(exc)
    error = SidecarIOError('capture_unverified')
    error.close_unknown, error.release_unknown, error.cleanup_unknown = close, release, cleanup
    return error


def _unknown_error(exc, candidate=None, *, force_release=False):
    close, release, cleanup = _resource_uncertainty(exc)
    error = SidecarWriteError('UNKNOWN', candidate, release_unknown=release or force_release)
    error.close_unknown, error.cleanup_unknown = close, cleanup
    return error


def _absolute(value):
    value = os.fspath(value)
    if not isinstance(value, str) or not value or '\x00' in value:
        raise SidecarIOError('invalid_path')
    # abspath/normpath would change the meaning of a symlink followed by '..'.
    if '..' in Path(value).parts:
        raise SidecarIOError('path_binding_unavailable')
    return os.path.abspath(value)



def _source(value):
    """Preserve Darwin symlink/.. semantics; bind a relative input to cwd."""
    if sys.platform == 'darwin' and os.name == 'posix':
        from core.mac_config_path import _absolute_source
        return _absolute_source(value, None)
    return _absolute(value)


@dataclass(frozen=True, repr=False)
class _MacBinding:
    source: str
    target: str
    chain: tuple = field(repr=False)
    entry: tuple | None = field(repr=False)


@dataclass(frozen=True, repr=False)
class _MacCapture:
    binding: _MacBinding
    target: object = field(repr=False)
    external: bool


def _mac_walk(source, project_root):
    """Descriptor-bound walk of original spelling, including absent final files.

    The traversal adopts the Mac config walk's link, sticky-directory, fd
    ownership and dotdot checks. Outside the project it records stable local
    directory identity, but applies strict protection to every traversed link's
    parent and the final direct parent. Project directories retain strict
    protection. This is not an authorization or an atomic freeze.
    """
    from core import mac_config_path as mac, secure_files as secure
    if sys.platform != 'darwin' or os.name != 'posix':
        raise SidecarIOError('platform_unverified')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with ExitStack() as opened:
        def own(fd):
            lease = mac._OwnedFD(fd)
            opened.callback(lease.close)
            return lease

        def directory_proof(fd, path):
            if path == project_root or path.startswith(project_root.rstrip('/') + '/'):
                return ('protected', mac._protection(fd, True))
            before = mac._dir_meta(os.fstat(fd))
            volume = secure._mac_local_apfs(fd, secure._mac_native())
            if mac._dir_meta(os.fstat(fd)) != before:
                raise mac.MacConfigError('directory_changed')
            return ('observed', before, volume)

        def root():
            lease = own(os.open('/', flags))
            proof = directory_proof(lease.fd, '/')
            events.append(('root', proof))
            return [(lease, '', proof)]

        events = []
        stack = root()
        pending = mac._parts(source)
        links = 0
        final_link = False
        while pending:
            part = pending.pop(0)
            parent, _, parent_proof = stack[-1]
            fd = parent.fd
            if part == '.':
                events.append(('dot', parent_proof))
                continue
            if part == '..':
                up = own(mac._path_operation(os.open, '..', flags, dir_fd=fd,
                                             changed='parent_changed'))
                expected = stack[-2] if len(stack) > 1 else stack[0]
                name_parts = [item[1] for item in stack[1:-1]]
                parent_path = '/' + '/'.join(name_parts)
                proof = directory_proof(up.fd, parent_path)
                if proof != expected[2]:
                    raise mac.MacConfigError('parent_changed')
                events.append(('dotdot', proof))
                if len(stack) > 1:
                    stack.pop()[0].close()
                previous = stack[-1][0]
                stack[-1] = (up, stack[-1][1], proof)
                previous.close()
                continue
            try:
                info = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                if pending or final_link:
                    raise mac.MacConfigError('file_not_found') from None
                parent_protected = mac._protection(fd, True)
                events.append(('target-parent', parent_protected))
                try:
                    os.stat(part, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    target = '/' + '/'.join([item[1] for item in stack[1:]] + [part])
                    return _MacBinding(source, target, tuple(events), None)
                raise mac.MacConfigError('target_changed') from None
            mac._sticky_entry(os.fstat(fd), info)
            if stat.S_ISLNK(info.st_mode):
                if not pending:
                    final_link = True
                parent_protected = mac._protection(fd, True)
                before = mac._entry_meta(info)
                link = own(mac._path_operation(os.open, part, os.O_SYMLINK | os.O_CLOEXEC,
                                                dir_fd=fd, changed='link_changed'))
                if mac._entry_meta(os.fstat(link.fd)) != before:
                    raise mac.MacConfigError('link_changed')
                link_proof = mac._link_protection(link.fd)
                text = mac._path_operation(os.readlink, part, dir_fd=fd,
                                           changed='link_changed', invalid_means_changed=True)
                after = mac._path_operation(os.stat, part, dir_fd=fd, follow_symlinks=False,
                                            changed='link_changed')
                if (not text or mac._entry_meta(after) != before or link_proof[0] != before
                        or mac._link_protection(link.fd) != link_proof
                        or mac._protection(fd, True) != parent_protected):
                    raise mac.MacConfigError('link_changed')
                link.close()
                links += 1
                if links > mac.MAX_SYMLINKS:
                    raise mac.MacConfigError('too_many_links')
                events.append(('link', part, before, text, parent_protected, link_proof))
                if text.startswith('/'):
                    new_root = root()
                    for ancestor, _, _ in reversed(stack):
                        ancestor.close()
                    stack = new_root
                pending = mac._parts(text) + pending
                continue
            if pending:
                if not stat.S_ISDIR(info.st_mode):
                    raise mac.MacConfigError('not_directory')
                child = own(mac._path_operation(os.open, part, flags, dir_fd=fd,
                                                 changed='directory_changed'))
                child_path = '/' + '/'.join([item[1] for item in stack[1:]] + [part])
                if mac._dir_meta(os.fstat(child.fd)) != mac._dir_meta(info):
                    raise mac.MacConfigError('directory_changed')
                proof = directory_proof(child.fd, child_path)
                again = mac._path_operation(os.stat, part, dir_fd=fd, follow_symlinks=False,
                                             changed='directory_changed')
                if mac._dir_meta(again) != mac._dir_meta(info):
                    raise mac.MacConfigError('directory_changed')
                events.append(('directory', part, proof))
                stack.append((child, part, proof))
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise mac.MacConfigError('not_regular_file')
            entry = mac._entry_meta(info)
            parent_protected = mac._protection(fd, True)
            again = mac._path_operation(os.stat, part, dir_fd=fd, follow_symlinks=False,
                                         changed='target_changed')
            if mac._entry_meta(again) != entry:
                raise mac.MacConfigError('target_changed')
            events.append(('target-parent', parent_protected))
            target = '/' + '/'.join([item[1] for item in stack[1:]] + [part])
            return _MacBinding(source, target, tuple(events), entry)
        raise mac.MacConfigError('not_regular_file')

def _same(a, b):
    return (isinstance(a, SidecarSnapshot) and isinstance(b, SidecarSnapshot)
            and a.identity is not None and a.identity == b.identity
            and a.contents == b.contents)


def _mac_snapshot(value):
    raw = value.target
    return SidecarSnapshot(raw.contents,
                           ('mac', value.binding.source, value.binding.chain, raw.comparable_identity,
                            raw.identity[7] if raw.exists else None),
                           raw.identity[7] / 1e9 if raw.exists else 0.0)


def _mac_candidate(value, payload, binding):
    from core.secure_files import StagedCandidate
    if (binding is None or not isinstance(value, StagedCandidate)
            or value.path != binding.target or value.digest != hashlib.sha256(payload).digest()):
        return None
    return SidecarSnapshot(payload,
                           ('mac', binding.source, binding.chain, value.comparable_identity, value.mtime_ns),
                           value.mtime_ns / 1e9)


def _win_parent(value):
    evidence = value._target_lease.evidence
    parts = evidence.components[:-1] if value.exists else evidence.components
    return (evidence.drive, tuple((p.name, p.observation) for p in parts))


def _win_snapshot(value):
    obs = value.identity_observation
    parent = _win_parent(value)
    # Retain the complete exact native marker in identity; mtime is only a
    # compatibility hint and cannot represent every Windows 100 ns tick.
    identity = ('windows', value.path, parent, obs.file_identity, obs.file_protection, obs.marker)
    # The platform marker is nanoseconds since 1601; only monotonic comparison
    # with Path.stat().st_mtime is needed by commands.py, so convert to Unix.
    mtime = ((obs.marker[2] - 11_644_473_600_000_000_000) / 1_000_000_000) if obs.marker else 0.0
    return SidecarSnapshot(value.contents, identity, mtime)


def _win_candidate(path, parent, parent_observation, obs, payload):
    from core.windows_secure_files import IdentityObservation
    if (not isinstance(obs, IdentityObservation) or not obs.exists
            or obs.parent != parent_observation or obs.file_identity is None
            or obs.file_protection is None or not isinstance(obs.marker, tuple)
            or len(obs.marker) != 5 or any(type(x) is not int or x < 0 for x in obs.marker)
            or obs.marker[2] <= 0 or obs.marker[3] <= 0 or obs.marker[4] != len(payload)
            or obs.marker[:2] != (obs.file_identity.volume_serial, obs.file_identity.file_index)):
        return None
    mtime = (obs.marker[2] - 11_644_473_600_000_000_000) / 1_000_000_000
    return SidecarSnapshot(payload, ('windows', path, parent, obs.file_identity,
                                     obs.file_protection, obs.marker), mtime)


class SidecarSecureIO:
    """Use the actual Darwin/Win32 writers; unsupported platforms fail closed."""

    def __init__(self, *, project_root, target, guard, max_bytes=16 * 1024 * 1024,
                 allow_external_target_staging=False):
        if (not callable(guard) or type(max_bytes) is not int or max_bytes < 0
                or type(allow_external_target_staging) is not bool):
            raise SidecarIOError('integration_missing')
        self.root = _absolute(project_root)
        self.target = _source(target)
        self.allow_external_target_staging = allow_external_target_staging
        self.staging = os.path.join(self.root, '.runtime', 'tmp')
        self.guard = guard
        self.max_bytes = max_bytes
        try:
            self.external = os.path.commonpath((self.root, self.target)) != self.root
        except ValueError:
            self.external = True
        self.boundary = os.path.dirname(self.target) if self.external else self.root
        if os.name == 'nt':
            # A case/8.3 spelling is not proof that an internal target is external.
            from core.discovery import _windows_trusted_boundary
            try:
                self.boundary = _windows_trusted_boundary(self.root, self.target)
            except Exception:
                raise SidecarIOError('boundary_unverified') from None
            self.external = self.boundary != self.root

    def _mac_boundary(self, actual):
        """Use the actual target, rejecting aliases that weaken the boundary."""
        parent = os.path.dirname(actual)
        root_info = os.lstat(self.root)
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
            raise SidecarIOError('boundary_unverified')
        if parent == self.root or parent.startswith(self.root.rstrip('/') + '/'):
            return self.root, False
        cursor = parent
        while True:
            entry = os.stat(cursor)
            if (entry.st_dev, entry.st_ino) == (root_info.st_dev, root_info.st_ino):
                raise SidecarIOError('boundary_unverified')
            previous = os.path.dirname(cursor)
            if previous == cursor:
                break
            cursor = previous
        return parent, True

    def _path(self, path):
        if _source(path) != self.target:
            raise SidecarIOError('target_mismatch')
        return self.target

    def _guard(self, artifact=None, *, stage=None, actual=None, external=None):
        actual = actual or self.target
        external = self.external if external is None else external
        try:
            selected = stage or self.staging
            if self.guard(self.target, actual, selected, artifact) is not True:
                raise SidecarIOError('authorization_unverified')
            parent = os.path.dirname(actual)
            check_parent = (os.name == 'nt' or getattr(self, 'allow_external_target_staging', False))
            if external and check_parent and selected != parent:
                if self.guard(self.target, actual, parent, None) is not True:
                    raise SidecarIOError('authorization_unverified')
        except Exception as exc:
            close, release, cleanup = _resource_uncertainty(exc)
            error = SidecarIOError('authorization_unverified')
            error.close_unknown, error.release_unknown, error.cleanup_unknown = close, release, cleanup
            raise error from None

    def _platform(self):
        if sys.platform == 'darwin':
            return 'mac'
        if os.name == 'nt':
            return 'windows'
        raise SidecarIOError('platform_unverified')

    def _mac_capture(self):
        from core import secure_files as secure
        binding = _mac_walk(self.target, self.root)
        boundary, external = self._mac_boundary(binding.target)
        self._guard(actual=binding.target, external=external)
        target = secure.capture_target(binding.target, authorized_root=os.path.dirname(binding.target),
                                       protection_reader=secure.mac_private_protection,
                                       max_bytes=self.max_bytes, trusted_boundary=boundary)
        if ((binding.entry is None) != (not target.exists)
                or (target.exists and binding.entry[:9] != target.identity)):
            raise SidecarIOError('target_changed')
        if _mac_walk(self.target, self.root) != binding:
            raise SidecarIOError('path_changed')
        self._guard(actual=binding.target, external=external)
        return _MacCapture(binding, target, external)

    def _mac_revalidate_binding(self, expected, external):
        try:
            binding = _mac_walk(self.target, self.root)
            if (binding.source != expected.source or binding.target != expected.target
                    or binding.chain != expected.chain
                    or self._mac_boundary(binding.target)[1] != external):
                raise SidecarIOError('path_changed')
            self._guard(actual=binding.target, external=external)
        except BaseException as exc:
            close, release, cleanup = _resource_uncertainty(exc)
            if release and not close:
                # The Mac writer exposes close/cleanup flags, not a separate
                # generic release flag. Conservatively retain that uncertainty.
                error = SidecarIOError('path_unverified')
                error.close_unknown = True
                error.release_unknown = True
                error.cleanup_unknown = cleanup
                raise error from None
            raise

    def _win_capture(self):
        from core import windows_secure_files as secure
        return secure.capture_target(self.target, authorized_root=os.path.dirname(self.target),
                                     max_bytes=self.max_bytes, trusted_boundary=self.boundary)

    def capture(self, path):
        self._path(path)
        try:
            if self._platform() == 'mac':
                value = self._mac_capture()
                # The guard can itself take time; bind the target again.
                again = self._mac_capture()
                if value != again:
                    raise SidecarIOError('target_changed')
                return _mac_snapshot(again)
            self._guard()
            value = self._win_capture()
            try:
                self._guard()
                value.revalidate()
                result = _win_snapshot(value)
            finally:
                value.close()
            return result
        except Exception as exc:
            raise _capture_error(exc) from None
        except BaseException as exc:
            # Do not suppress an ordinary read-only interruption, but never lose
            # a reported uncertain release while unwinding one.
            if _resource_uncertainty(exc)[1]:
                raise _capture_error(exc) from None
            raise

    def commit(self, path, baseline, payload):
        try:
            self._path(path)
            if (not isinstance(baseline, SidecarSnapshot) or baseline.identity is None
                    or not isinstance(payload, bytes) or len(payload) > self.max_bytes):
                raise SidecarIOError('invalid_request')
            platform = self._platform()
        except Exception:
            raise SidecarWriteError('NOT_REPLACED') from None
        try:
            if platform == 'mac':
                return self._commit_mac(baseline, payload)
            return self._commit_windows(baseline, payload)
        except SidecarWriteError:
            raise
        except BaseException as exc:
            # An interruption in adapter-side outcome classification can occur
            # after the syscall, even if the platform writer already returned.
            raise _unknown_error(exc) from None

    def _commit_mac(self, baseline, payload):
        from core import secure_files as secure
        entered = False
        current = None
        try:
            current = self._mac_capture()
            if not _same(_mac_snapshot(current), baseline):
                raise SidecarIOError('baseline_changed')
            staging = secure.capture_private_directory(self.staging, purpose='staging')
            locations = secure.RuntimeWriteLocations(
                staging, None, self.allow_external_target_staging and current.external)
            def boundary_guard():
                self._mac_revalidate_binding(current.binding, current.external)
            entered = True
            outcome = secure.secure_replace_runtime(
                current.target, payload, max_bytes=self.max_bytes, locations=locations,
                make_backup=False, boundary_guard=boundary_guard)
        except secure.SecureWriteError as exc:
            resource_unknown = exc.close_unknown or exc.cleanup_unknown
            state = 'NOT_REPLACED' if exc.state == 'NOT_REPLACED' and not resource_unknown else 'UNKNOWN'
            candidate = _mac_candidate(exc.candidate, payload, current.binding if current else None) if exc.state != 'NOT_REPLACED' else None
            error = SidecarWriteError(state, candidate, release_unknown=resource_unknown)
            error.close_unknown = exc.close_unknown
            error.cleanup_unknown = exc.cleanup_unknown
            if exc.cleanup_unknown:
                error.cleanup_unknown = True
                error.staging = exc.staging
                log.warning('sidecar: staging cleanup unverified; artifact may remain')
            raise error from None
        except BaseException as exc:
            _, resource_unknown, _ = _resource_uncertainty(exc)
            if entered:
                log.warning('sidecar: transaction interrupted; staging cleanup unverified')
            if entered or resource_unknown:
                raise _unknown_error(exc) from None
            raise SidecarWriteError('NOT_REPLACED') from None
        # Writer has already committed. Any later guard/read/close failure is UNKNOWN.
        candidate = None
        try:
            candidate = _mac_candidate(outcome.candidate, payload, current.binding)
            if outcome.state != 'VERIFIED' or candidate is None:
                raise SidecarIOError('postcheck_unverified')
            final = self.capture(self.target)
            if not _same(final, candidate):
                raise SidecarIOError('postcheck_changed')
            return final
        except BaseException as exc:
            raise _unknown_error(exc, candidate) from None

    def _win_stage(self, snapshot):
        """Determine volume from native held handles, never from drive spelling."""
        from core import windows_paths
        lease = windows_paths.inspect(self.staging, backend=snapshot._monitor,
                                      kind='directory', private=True,
                                      trusted_boundary=self.root)
        try:
            lease.revalidate()
            snapshot.revalidate()
            src = lease._handles[-1].volume
            dst = snapshot._target_lease._handles[-2 if snapshot.exists else -1].volume
            if not src or not dst:
                raise SidecarIOError('volume_unverified')
            if src == dst:
                return self.staging
            if not self.external or not self.allow_external_target_staging:
                raise SidecarIOError('cross_volume_unverified')
            return os.path.dirname(self.target)
        finally:
            lease.close()

    def _commit_windows(self, baseline, payload):
        from core import windows_secure_files as secure
        from core.windows_secure_native import ProductWriteBackend

        class RecordingBackend(ProductWriteBackend):
            # Preserve real stage evidence; this is not a replacement OS backend.
            finish_attempted = False

            def postcheck(inner):
                result = super().postcheck()
                inner.verified_result = result
                return result

            def finish(inner):
                try:
                    return super().finish()
                finally:
                    inner.finish_attempted = True

        snap = backend = None
        parent = parent_obs = None
        entered = False
        try:
            self._guard()
            snap = self._win_capture()
            snap.revalidate()
            observed = _win_snapshot(snap)
            if not _same(observed, baseline) or observed.mtime != baseline.mtime:
                raise SidecarIOError('baseline_changed')
            parent = _win_parent(snap)
            parent_obs = snap.identity_observation.parent
            stage = self._win_stage(snap)
            self._guard(stage=stage)
            # Runtime staging is private. The cross-volume external exception
            # explicitly binds the separately verified direct target parent.
            options = (dict(stage_directory=stage, stage_boundary=self.root)
                       if stage == self.staging else
                       dict(stage_directory=stage, stage_boundary=self.boundary,
                            stage_at_target_parent=True))
            backend = RecordingBackend(snap, **options)
            def preflight():
                self._guard(stage=stage)
                return True
            def artifact_preflight(role, path):
                if role != 'stage':
                    return False
                self._guard(path, stage=stage)
                return True
            entered = True
            outcome = secure._finite_write(snap, payload, backend, preflight,
                                            make_backup=False, artifact_preflight=artifact_preflight)
        except secure.SecureWriteError as exc:
            resource_unknown = (exc.release_unknown or exc.creation_unknown or exc.staging is not None)
            state = 'NOT_REPLACED' if exc.state == 'NOT_REPLACED' and not resource_unknown else 'UNKNOWN'
            candidate = (_win_candidate(self.target, parent, parent_obs, exc.candidate, payload)
                         if exc.state != 'NOT_REPLACED' else None)
            error = SidecarWriteError(state, candidate, release_unknown=bool(resource_unknown))
            if exc.staging is not None or exc.creation_unknown:
                error.cleanup_unknown = True
                error.staging = exc.staging
                log.warning('sidecar: Windows staging cleanup unverified; artifact may remain')
            raise error from None
        except BaseException as exc:
            _, resource_unknown, _ = _resource_uncertainty(exc)
            if entered or resource_unknown:
                raise _unknown_error(exc) from None
            raise SidecarWriteError('NOT_REPLACED') from None
        finally:
            try:
                if backend is not None and not backend.finish_attempted:
                    backend.finish()
                elif backend is None and snap is not None:
                    snap.close()
            except BaseException as exc:
                raise _unknown_error(exc, force_release=True) from None
        # _finite_write already completed finish; compare a fresh protected read
        # to the exact pre-commit stage observation, not just equal JSON bytes.
        candidate = None
        try:
            stage_read = backend._stage_read
            obs = secure.IdentityObservation(True, parent_obs, stage_read.identity,
                                             stage_read.protection, stage_read.marker) if stage_read else None
            candidate = _win_candidate(self.target, parent, parent_obs, obs, payload)
            checked = backend.verified_result
            # A rename may change the native marker. Without identical staged
            # and postchecked evidence, fail UNKNOWN rather than invent C.
            if (outcome.state != 'VERIFIED' or candidate is None
                    or checked.identity != stage_read.identity or checked.data != payload
                    or checked.protection != stage_read.protection or checked.marker != stage_read.marker):
                raise SidecarIOError('postcheck_unverified')
            final = self.capture(self.target)
            if not _same(final, candidate):
                raise SidecarIOError('postcheck_changed')
            return final
        except BaseException as exc:
            raise _unknown_error(exc, candidate) from None
