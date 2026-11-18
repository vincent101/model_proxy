"""Runtime authority for the existing sidecar adapter, not a new authority.

The server supplies its actual held locks, selected config source and runtime
paths. A selected external config authorizes its directly derived sidecar, not
an arbitrary file reached through an external symlink. Unknown file/handle
release is latched; the adapter/store cannot resolve it by comparing B/C.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path, PureWindowsPath


class SidecarGuardError(RuntimeError):
    def __init__(self, code, *, release_unknown=False, close_unknown=False, cleanup_unknown=False):
        super().__init__(code)
        self.code = code
        self.release_unknown = release_unknown
        self.close_unknown = close_unknown
        self.cleanup_unknown = cleanup_unknown


def _uncertain(exc):
    try:
        code = getattr(exc, 'code', None)
        close = getattr(exc, 'close_unknown', False) is True
        cleanup = getattr(exc, 'cleanup_unknown', False) is True
        release = (getattr(exc, 'release_unknown', False) is True or close or cleanup
                   or code in ('release_unknown', 'lock_release_unknown', 'fd_close_unknown',
                               'close_failed', 'handle_close_failed', 'resource_release_failed',
                               'parent_close_unknown', 'staging_close_unknown',
                               'staging_cleanup_unknown', 'staging_cleanup_failed'))
        return close, release, cleanup
    except BaseException:
        return True, True, True


class SidecarRuntimeGuard:
    """Check locks, config-derived target authorization, roles and staging."""
    def __init__(self, *, root, config_path, anchor, legacy, runtime_paths, explicit_external_config=False):
        if not ((sys.platform == 'darwin' and os.name == 'posix') or
                (sys.platform == 'win32' and os.name == 'nt')):
            raise SidecarGuardError('platform_unverified')
        if type(explicit_external_config) is not bool:
            raise SidecarGuardError('authorization_unverified')
        from core.runtime_paths import PATH_KEYS
        path_type = PureWindowsPath if os.name == 'nt' else Path
        self.root = path_type(root)
        self.config = path_type(config_path)
        self.original = self.config.parent / 'session_overrides.json'
        self.state = self.root / '.runtime'
        self.staging = self.state / 'tmp'
        self.paths = {key: path_type(runtime_paths[key]) for key in PATH_KEYS}
        self.anchor, self.legacy = anchor, legacy
        self.explicit_external_config = explicit_external_config
        self._binding = None
        self._resource = None
        if not self.root.is_absolute() or not self.config.is_absolute():
            raise SidecarGuardError('path_unverified')
        try:
            held = legacy.snapshot
            if (held.project_root != str(self.root)
                    or held.as_dict()['state_dir'].canonical != str(self.state)):
                raise SidecarGuardError('locks_wrong_installation')
        except SidecarGuardError:
            raise
        except Exception:
            raise SidecarGuardError('locks_unverified') from None
        if os.name == 'nt':
            from core.discovery import _windows_trusted_boundary
            boundary = path_type(_windows_trusted_boundary(self.root, self.original))
            self.original_external = boundary != self.root
        else:
            self.original_external = os.path.commonpath((str(self.root), str(self.original))) != str(self.root)
        self.allow_external_target_staging = self.original_external and explicit_external_config

    def _latch(self, exc):
        close, release, cleanup = _uncertain(exc)
        if release or close or cleanup:
            self._resource = SidecarGuardError('release_unknown', release_unknown=release,
                                               close_unknown=close, cleanup_unknown=cleanup)
        return self._resource

    def _locks(self):
        from core import server
        try:
            server.verify_held_instance_locks(self.anchor, self.legacy)
        except Exception:
            # Shared helper may collapse a failed descriptor close into a
            # generic protection error, so do not assume release succeeded.
            exc = SidecarGuardError('release_unknown', release_unknown=True)
            self._resource = exc
            raise exc from None

    def _base(self):
        from core import server
        if self._resource is not None:
            raise self._resource
        self._locks()
        try:
            configured = server.resolve_runtime_paths(server._RUNTIME_PATHS_FILE)
        except Exception:
            self._resource = SidecarGuardError('release_unknown', release_unknown=True)
            raise self._resource from None
        if configured != self.paths:
            raise SidecarGuardError('runtime_drift')
        try:
            result = (server.inspect_runtime_identities(self.root, self.paths, windows_light=True)
                      if os.name == 'nt' else server.inspect_runtime_identities(self.root, self.paths))
            held = self.legacy.snapshot.as_dict()
            current = result.as_dict()
            if any(current[key] != held[key] for key in ('state_dir', 'anchor.lock', 'lock')):
                raise SidecarGuardError('lock_path_changed')
            server._check_config_targets(self.paths, self.config)
        except Exception as exc:
            if self._latch(exc):
                raise self._resource from None
            raise SidecarGuardError('roles_unverified') from None
        self._locks()
        return result

    def _authorize_target(self, original, actual):
        from core.sidecar_secure_io import _source
        if _source(original) != _source(self.original):
            raise SidecarGuardError('source_changed')
        if os.name == 'nt':
            if str(actual) != str(self.original):
                raise SidecarGuardError('target_unverified')
            if self.original_external and not self.explicit_external_config:
                raise SidecarGuardError('external_target_not_authorized')
            return
        if self.original_external and not self.explicit_external_config:
            raise SidecarGuardError('external_target_not_authorized')
        from core.sidecar_secure_io import _mac_walk
        try:
            binding = _mac_walk(str(self.original), str(self.root))
        except Exception as exc:
            if self._latch(exc):
                raise self._resource from None
            raise SidecarGuardError('path_unverified') from None
        if binding.target != os.fspath(actual):
            raise SidecarGuardError('target_changed')
        # Config path links are supported, but a link in the sidecar's final
        # component would authorize a different file. No separate grant for
        # that target is available to the service, so do not infer one.
        from core.discovery import _path_identity
        selected = _path_identity(Path(self.original), fixed=False, kind='file')
        if selected.final_was_link or selected.canonical != binding.target:
            raise SidecarGuardError('target_not_authorized')
        chain = (binding.source, binding.target, binding.chain)
        if self._binding is None:
            self._binding = chain
        elif self._binding != chain:
            raise SidecarGuardError('path_changed')
        inside = os.path.commonpath((str(self.root), binding.target)) == str(self.root)
        if not inside:
            # Do not classify a different spelling of the project ancestry
            # as independently authorized external space.
            root_info = os.stat(self.root)
            cursor = os.path.dirname(binding.target)
            while True:
                entry = os.stat(cursor)
                if (entry.st_dev, entry.st_ino) == (root_info.st_dev, root_info.st_ino):
                    raise SidecarGuardError('external_boundary_unverified')
                parent = os.path.dirname(cursor)
                if parent == cursor:
                    break
                cursor = parent
            # An explicitly selected external config authorizes only the
            # same-named sidecar in that selected source parent's verified
            # physical directory. Ancestor links / symlink-.. retain their
            # verified chain; a sidecar final-component link was rejected
            # above and cannot redirect this authorization to an arbitrary file.
            if (not self.original_external or not self.explicit_external_config
                    or os.path.basename(binding.target) != self.original.name):
                raise SidecarGuardError('external_target_not_authorized')

    def _mac_roles(self, snapshot, actual):
        from core import server
        from core.discovery import _path_identity
        roles = list(snapshot.as_dict().values())
        for path in (self.config, server._RUNTIME_PATHS_FILE, self.original, Path(actual),
                     self.staging, self.state / 'backups'):
            role_kind = 'directory' if path in (self.staging, self.state / 'backups') else 'file'
            value = path if path.is_absolute() else Path.cwd() / path
            roles.append(_path_identity(value, fixed=False, kind=role_kind))
        return roles

    def _mac_namespace(self, stage, actual, snapshot, artifact):
        from core.discovery import _path_identity, _identity_contains, _identity_same
        stage = Path(stage)
        if stage == self.staging:
            # Runtime preflight + config/input preflight rule out roles anywhere
            # in the fixed tmp directory, including absent-name aliases.
            if artifact is None:
                return
        elif (stage != Path(actual).parent or not self.allow_external_target_staging
              or os.path.commonpath((str(self.root), os.fspath(actual))) == str(self.root)):
            raise SidecarGuardError('staging_not_authorized')
        parent = _path_identity(stage, fixed=False, kind='directory')
        if not parent.exists:
            raise SidecarGuardError('staging_unverified')
        roles = self._mac_roles(snapshot, actual)
        if artifact is not None:
            candidate = Path(artifact)
            if candidate.parent != stage or re.fullmatch(r'\.model-proxy-runtime-[0-9a-f]{24}', candidate.name) is None:
                raise SidecarGuardError('artifact_unverified')
            identity = _path_identity(candidate, fixed=False, kind='file')
            for role in roles:
                if role.canonical == str(stage) or (stage == self.staging and role.canonical == str(self.state)):
                    continue
                if (_identity_same(identity, role) or _identity_contains(identity, role)
                        or _identity_contains(role, identity)
                        or (identity.exists and role.exists and
                            (identity.device, identity.inode) == (role.device, role.inode))):
                    raise SidecarGuardError('artifact_collision')
            return
        for role in roles:
            child = None
            for i, entry in enumerate(role.trace):
                if (entry[1], entry[2]) == (parent.device, parent.inode):
                    if i + 1 < len(role.trace):
                        child = Path(role.trace[i + 1][0]).name
                    elif role.unresolved:
                        child = role.unresolved[0]
                    else:
                        raise SidecarGuardError('namespace_collision')
                    break
            if child is None and _identity_contains(parent, role):
                raise SidecarGuardError('namespace_unverified')
            if child is not None and (not child.isascii() or re.fullmatch(
                    r'\.model-proxy-runtime-[0-9a-f]{24}', child, flags=re.IGNORECASE)):
                raise SidecarGuardError('namespace_collision')

    def _windows_namespace(self, stage, actual, artifact):
        import ntpath
        from core import server, windows_paths
        from core.discovery import _windows_open_preflight, _windows_close_leases, _windows_trusted_boundary
        stage = os.fspath(stage)
        runtime = stage == str(self.staging)
        if not runtime and (stage != ntpath.dirname(os.fspath(actual)) or not self.allow_external_target_staging):
            raise SidecarGuardError('staging_not_authorized')
        if artifact is not None and (ntpath.dirname(os.fspath(artifact)) != stage or
                re.fullmatch(r'\.model-proxy-stage-[0-9a-f]{24}', ntpath.basename(os.fspath(artifact))) is None):
            raise SidecarGuardError('artifact_unverified')
        scope = None
        extras = []
        try:
            _, scope = _windows_open_preflight(str(self.root), self.paths, light=True)
            backend = scope['__root__']._backend
            for path in (self.config, server._RUNTIME_PATHS_FILE, self.original):
                lease = windows_paths.inspect(str(path), backend=backend, kind='file', private=True,
                    trusted_boundary=_windows_trusted_boundary(self.root, path))
                extras.append(lease)
            if not runtime:
                directory = windows_paths.inspect(stage, backend=backend, kind='directory', private=False,
                    trusted_boundary=stage)
                extras.append(directory)
                if not directory.evidence.exists:
                    raise SidecarGuardError('staging_unverified')
                parent = directory.evidence.components[-1].observation
                for lease in (*scope.values(), *extras[:-1]):
                    ev = lease.evidence
                    found_parent = False
                    for index, part in enumerate(ev.components):
                        if (part.observation.volume, part.observation.file_id) == (parent.volume, parent.file_id):
                            found_parent = True
                            tail = tuple(x.name for x in ev.components[index + 1:]) + ev.missing
                            if not tail:
                                raise SidecarGuardError('namespace_collision')
                            child = tail[0]
                            if (not child.isascii() or '~' in child or re.fullmatch(
                                    r'\.model-proxy-stage-[0-9a-f]{24}', child, flags=re.IGNORECASE)):
                                raise SidecarGuardError('namespace_collision')
                            break
                    # A missing suffix may still alias this directory through
                    # Windows case/Unicode/8.3 equivalence. No physical parent
                    # match is not proof that the namespace is disjoint.
                    if not found_parent and windows_paths.may_contain(directory, lease):
                        raise SidecarGuardError('namespace_unverified')
            if artifact is not None:
                candidate = windows_paths.inspect(os.fspath(artifact), backend=backend, kind='file',
                    private=True, single_link=True,
                    trusted_boundary=_windows_trusted_boundary(self.root, artifact))
                extras.append(candidate)
                for name, lease in scope.items():
                    if name in ('__root__', 'state_dir') or (runtime and name == 'tmp_dir'):
                        continue
                    if windows_paths.may_contain(candidate, lease) or windows_paths.may_contain(lease, candidate):
                        raise SidecarGuardError('artifact_collision')
                for lease in extras[:-1]:
                    if not runtime and lease is directory:
                        continue
                    if windows_paths.may_contain(candidate, lease) or windows_paths.may_contain(lease, candidate):
                        raise SidecarGuardError('artifact_collision')
            for lease in (*scope.values(), *extras):
                lease.revalidate()
        except Exception as exc:
            if self._latch(exc):
                raise self._resource from None
            raise
        finally:
            try:
                _windows_close_leases([*extras, *(scope.values() if scope is not None else ())])
            except Exception:
                self._resource = SidecarGuardError('release_unknown', release_unknown=True)
                raise self._resource from None

    def __call__(self, original, actual, stage, artifact):
        try:
            snapshot = self._base()
            self._authorize_target(original, actual)
            if os.name == 'nt':
                self._windows_namespace(stage, actual, artifact)
            else:
                self._mac_namespace(stage, actual, snapshot, artifact)
            self._base()
            return True
        except Exception as exc:
            if self._latch(exc):
                raise self._resource from None
            if isinstance(exc, SidecarGuardError):
                raise
            raise SidecarGuardError('guard_unverified') from None

    def prepare_staging(self):
        """Create only the fixed private runtime directory while locks are held."""
        from core import server
        self._base()
        if os.name == 'nt':
            from core.platform_windows import ensure_private_directory, WindowsPrivateCreationError
            try:
                ensure_private_directory(str(self.staging), trusted_boundary=str(self.root))
            except WindowsPrivateCreationError as exc:
                if exc.created is not False or exc.release_unknown:
                    self._resource = SidecarGuardError('staging_unknown', release_unknown=bool(exc.release_unknown),
                                                       cleanup_unknown=True)
                    raise self._resource from None
                raise SidecarGuardError('staging_unverified') from None
            self._base()
            return
        from core.secure_files import capture_private_directory
        expected = self.legacy.snapshot.as_dict()['state_dir']
        try:
            server.verify_record_directory(self.state)
        except Exception:
            self._resource = SidecarGuardError('release_unknown', release_unknown=True)
            raise self._resource from None
        fd = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != (expected.device, expected.inode):
                raise SidecarGuardError('state_changed')
            try:
                os.stat('tmp', dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    os.mkdir('tmp', 0o700, dir_fd=fd)
                except FileExistsError:
                    raise SidecarGuardError('staging_changed') from None
                except OSError:
                    self._resource = SidecarGuardError('staging_unknown', cleanup_unknown=True)
                    raise self._resource from None
        finally:
            try:
                os.close(fd)
            except OSError:
                self._resource = SidecarGuardError('release_unknown', close_unknown=True, release_unknown=True)
                raise self._resource from None
        self._base()
        try:
            capture_private_directory(self.staging, purpose='staging')
        except Exception as exc:
            if self._latch(exc):
                raise self._resource from None
            raise SidecarGuardError('staging_unverified') from None


class UnavailableSidecarIO:
    """Fail-closed placeholder; never reads, writes or impersonates a guard."""
    def __init__(self, exc=None):
        close, release, cleanup = _uncertain(exc) if exc is not None else (False, False, False)
        self.error = SidecarGuardError('sidecar_adapter_unavailable', release_unknown=release,
                                       close_unknown=close, cleanup_unknown=cleanup)

    def capture(self, path):
        raise self.error

    def commit(self, path, baseline, payload):
        from core.commands import SidecarWriteError
        resource = self.error.release_unknown or self.error.close_unknown or self.error.cleanup_unknown
        raise SidecarWriteError('UNKNOWN' if resource else 'NOT_REPLACED',
                                release_unknown=self.error.release_unknown,
                                close_unknown=self.error.close_unknown,
                                cleanup_unknown=self.error.cleanup_unknown)
