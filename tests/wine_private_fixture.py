"""Prepare *only a caller-owned* mp-minimal-b fake project for Wine tests.

Prepare before the harness creates ``runtime``, ``.model_proxy_runtime``, or
either fake config. This uses real Win32 APIs and the copied project's real
validators; preparation never changes an existing ACL, overwrites a file,
reads configuration, or starts a process. Verification reads only those two
fake config paths.
The unprotected DACL is intentional: this is an existing-object test fixture,
not a test or authorization of the product's protected new-object creation.
The temporary-path/name checks are accident guards, not proof of ownership.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import ntpath
import os
from pathlib import PureWindowsPath
import re
import sys
import tempfile


class FixtureError(RuntimeError):
    def __init__(self, code: str, *, created=()):
        super().__init__(code)
        self.code = code
        self.created = tuple(created)


def _project_path(value):
    if os.name != 'nt':
        raise FixtureError('windows_required')
    try:
        path = os.fspath(value)
    except TypeError:
        raise FixtureError('invalid_project') from None
    if not isinstance(path, str) or not path or '\0' in path:
        raise FixtureError('invalid_project')
    # Deliberately only the fresh layout made by the B acceptance harness.
    if not ntpath.isabs(path) or ntpath.normpath(path) != path:
        raise FixtureError('invalid_project')
    project = PureWindowsPath(path)
    parent = project.parent
    temporary = PureWindowsPath(ntpath.abspath(tempfile.gettempdir()))
    if (not re.fullmatch(r'[A-Za-z]:', project.drive) or
            project.name != 'project' or parent.parent != temporary or
            not re.fullmatch(r'mp-minimal-b-[a-zA-Z0-9_]{8}', parent.name)):
        raise FixtureError('not_owned_fixture_layout')
    return str(project)


def _product_modules(project):
    expected = ntpath.normcase(ntpath.abspath(ntpath.join(project, 'core', '__init__.py')))
    loaded = sys.modules.get('core')
    if loaded is not None:
        origin = getattr(loaded, '__file__', '') or ''
        if ntpath.normcase(ntpath.abspath(origin)) != expected:
            raise FixtureError('different_core_already_loaded')
    else:
        sys.path.insert(0, project)
    try:
        from core import discovery, platform_windows, windows_acl, windows_paths
        from core.runtime_paths import PATH_KEYS
    except ImportError:
        raise FixtureError('product_import_unavailable') from None
    return discovery, platform_windows, windows_acl, windows_paths, PATH_KEYS


def _sid_text(sid, acl):
    if not acl._valid_sid(sid):
        raise FixtureError('effective_user_unverified')
    authority = int.from_bytes(sid[2:8], 'big')
    parts = [str(int.from_bytes(sid[i:i + 4], 'little')) for i in range(8, len(sid), 4)]
    return 'S-' + str(sid[0]) + '-' + str(authority) + (('-' + '-'.join(parts)) if parts else '')


def _create(path, *, directory, user, api, acl, attrs_type, created, label):
    """CREATE_NEW with explicit *unprotected* user-only SDDL; never write data."""
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = api._kernel
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int32
    adv.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint16), ctypes.POINTER(ctypes.c_uint32)]
    adv.GetSecurityDescriptorControl.restype = ctypes.c_int32
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    kernel.CreateDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(attrs_type)]
    kernel.CreateDirectoryW.restype = ctypes.c_int32
    flags = 'OICI' if directory else ''
    sddl = 'O:' + _sid_text(user, acl) + 'D:(A;' + flags + ';FA;;;' + _sid_text(user, acl) + ')'
    descriptor = ctypes.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise FixtureError('descriptor_unavailable', created=created)
    handle = None
    try:
        control, revision = ctypes.c_uint16(), ctypes.c_uint32()
        if (not adv.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision))
                or control.value & 0x1000):
            raise FixtureError('requested_descriptor_unverified', created=created)
        attrs = attrs_type(ctypes.sizeof(attrs_type), descriptor, False)
        if directory:
            if not kernel.CreateDirectoryW(path, ctypes.byref(attrs)):
                raise FixtureError('directory_create_unverified', created=created)
            created.append(label)
        else:
            handle = kernel.CreateFileW(path, 0x80000000 | 0x40000000, 3,
                                        ctypes.byref(attrs), 1, 0x00200000, None)
            if handle is None or handle == ctypes.c_void_p(-1).value:
                handle = None
                raise FixtureError('file_create_unverified', created=created)
            created.append(label)
        return handle
    finally:
        if kernel.LocalFree(descriptor):
            code = 'descriptor_release_unknown'
            if handle is not None:
                try:
                    api.close(handle)
                except BaseException:
                    code = 'descriptor_and_handle_release_unknown'  # never retry
            raise FixtureError(code, created=created)


def prepare_private_fixture(project_path):
    """Create eight empty, private, intentionally unprotected fixture objects.

    ``project_path`` must be the caller's freshly created
    ``<Windows temp>/mp-minimal-b-XXXXXXXX/project`` containing its copied
    ``core`` and an existing ``config`` directory. ``runtime``,
    ``.model_proxy_runtime``, and both fake config files must be absent.
    No cleanup or retry is attempted on error; ``FixtureError.created`` lists
    paths this invocation created. Never use for real configurations/secrets.
    """
    project = _project_path(project_path)
    discovery, win, acl, paths, path_keys = _product_modules(project)
    from core.discovery import _WindowsPathMonitor, _windows_path_backend
    try:
        api = win._NativeAPI()
    except OSError:
        raise FixtureError('native_api_unavailable') from None
    created = []
    objects = {}
    state = ntpath.join(project, '.model_proxy_runtime')
    runtime = ntpath.join(project, 'runtime')
    # Never reuse or change an existing directory, including a failed prior run.
    config = ntpath.join(project, 'config')
    config_files = [ntpath.join(config, name) for name in
                    ('runtime_paths.json', 'model_proxy_config.json')]
    for path in (state, runtime, *config_files):
        if os.path.lexists(path):
            raise FixtureError('fixture_target_already_exists', created=created)
    monitor = _WindowsPathMonitor(_windows_path_backend())
    try:
        with paths.inspect(project, backend=monitor, kind='directory', private=False) as project_lease:
            if not project_lease.evidence.exists:
                raise FixtureError('project_missing', created=created)
            # The effective token must be able to create directories under this
            # exact checked project; no group/System grant is manufactured.
            raw = project_lease.borrowed_component_handle(len(project_lease.evidence.components) - 1).raw
            parent = acl.read_stable_evidence(raw, kind='directory', policy='ancestor',
                                              required_access=paths._DIR_CHECK_ACCESS | 0x4)
            user = parent.observation.user
            for label, path in (('.model_proxy_runtime', state), ('runtime', runtime)):
                project_lease.revalidate()
                acl.verify_unchanged(raw, parent)
                _create(path, directory=True, user=user, api=api, acl=acl,
                        attrs_type=win.SECURITY_ATTRIBUTES, created=created, label=label)
                result = win.ensure_private_directory(path, required_access=0x200A3)
                if (result.created is not False or result.protection[0] != user or
                        result.protection[2] & 0x1000):
                    raise FixtureError('directory_readback_unverified', created=created)
                objects[label] = {'private_acl': True, 'effective_access': True,
                                  'protected': False}
            targets = ((state, 'control.lock'), (state, 'anchor.lock'),
                       (runtime, 'lock'), (runtime, 'log'),
                       (config, 'runtime_paths.json'), (config, 'model_proxy_config.json'))
            for directory, name in targets:
                path = ntpath.join(directory, name)
                label = ntpath.relpath(path, project)
                with win.open_directory(directory, backend=api) as parent_handle:
                    directory_proof = acl.read_stable_evidence(
                        parent_handle.handle, kind='directory',
                        policy='ancestor' if directory == config else 'private',
                        required_access=paths._DIR_CHECK_ACCESS | 0x2)
                    if directory_proof.observation.user != user:
                        raise FixtureError('effective_user_changed', created=created)
                    raw_file = _create(path, directory=False, user=user, api=api, acl=acl,
                                       attrs_type=win.SECURITY_ATTRIBUTES, created=created, label=label)
                    try:
                        owned = win.WindowsFileHandle(raw_file, api, created=True)
                    except BaseException:
                        try:
                            api.close(raw_file)
                        except BaseException:
                            raise FixtureError('file_handle_release_unknown', created=created) from None
                        raise
                    try:
                        identity = win.file_identity(owned.handle, backend=api)
                        proof = acl.read_stable_evidence(
                            owned.handle, kind='file', policy='private',
                            required_access=acl.FILE_GENERIC_READ | acl.FILE_GENERIC_WRITE)
                        if (proof.observation.owner != user or proof.observation.control & 0x1000 or
                                proof.observation.identity != (identity.volume_serial, identity.file_index,
                                                               identity.link_count, identity.attributes) or
                                api.file_marker(owned.handle)[4] != 0):
                            raise FixtureError('file_handle_unverified', created=created)
                        acl.verify_unchanged(owned.handle, proof)
                        acl.verify_unchanged(parent_handle.handle, directory_proof)
                        protection = win.acl_protection(proof)
                    finally:
                        try:
                            owned.close()
                        except BaseException:
                            raise FixtureError('file_handle_release_unknown', created=created) from None
                read = win.read_guarded_file(path, max_bytes=0)
                if read.data != b'' or read.identity != identity or read.protection != protection:
                    raise FixtureError('file_path_unverified', created=created)
                objects[label] = {'private_acl': True, 'effective_access': True,
                                  'protected': False, 'empty': True,
                                  'identity': [identity.volume_serial, identity.file_index,
                                               identity.link_count, identity.attributes]}
            project_lease.revalidate()
        if monitor.release_unknown:
            raise FixtureError('release_unknown', created=created)
        runtime_paths = {key: ntpath.join(runtime, key) for key in path_keys}
        snapshot = discovery.inspect_runtime_identities(project, runtime_paths, windows_light=True)
        values = snapshot.as_dict()
        if not all(values[key].exists for key in ('state_dir', 'control.lock', 'anchor.lock', 'lock', 'log')):
            raise FixtureError('runtime_preflight_unverified', created=created)
        if any(values[key].exists for key in ('pid', 'totals', 'ensure_log', 'start_lock', 'active.json')):
            raise FixtureError('unexpected_runtime_object', created=created)
        project_observation = snapshot.windows_dict()['__root__'].components[-1].observation
        if discovery.verify_record_directory(state) != (
                project_observation.volume, int.from_bytes(project_observation.file_id, 'big')):
            raise FixtureError('record_directory_unverified', created=created)
        return {'requested_dacl': 'explicit user-only, unprotected', 'objects': objects,
                'runtime_preflight': 'verified', 'record_directory': 'verified',
                'scope': 'existing unprotected fake objects only; not native Windows proof'}
    except FixtureError:
        raise
    except BaseException as exc:
        raise FixtureError(getattr(exc, 'code', 'fixture_unverified'), created=created) from None


def verify_private_configs(project_path):
    """Read the two fake files through the real product's private-path checks."""
    project = _project_path(project_path)
    _, win, _, _, _ = _product_modules(project)
    objects = {}
    try:
        for name in ('runtime_paths.json', 'model_proxy_config.json'):
            label = ntpath.join('config', name)
            read = win.read_guarded_file(ntpath.join(project, label), max_bytes=1024 * 1024)
            if read.protection[2] & 0x1000 or not isinstance(read.data, bytes):
                raise FixtureError('config_readback_unverified')
            identity = read.identity
            objects[label] = {'private_acl': True, 'protected': False,
                              'identity': [identity.volume_serial, identity.file_index,
                                           identity.link_count, identity.attributes],
                              'sha256': hashlib.sha256(read.data).hexdigest()}
        return {'objects': objects, 'scope': 'private fake config readback only'}
    except FixtureError:
        raise
    except BaseException as exc:
        raise FixtureError(getattr(exc, 'code', 'config_readback_unverified')) from None


def main():
    if len(sys.argv) == 2:
        action, project = prepare_private_fixture, sys.argv[1]
    elif len(sys.argv) == 3 and sys.argv[1] == '--verify-config':
        action, project = verify_private_configs, sys.argv[2]
    else:
        raise SystemExit('usage: wine_private_fixture.py [--verify-config] <fresh-fake-project>')
    try:
        print(json.dumps(action(project), ensure_ascii=True, indent=2))
    except FixtureError as exc:
        print(json.dumps({'error': exc.code, 'created': exc.created}, ensure_ascii=True), file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
