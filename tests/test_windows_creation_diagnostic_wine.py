"""Read-only fixture inspection plus one fresh private-creation test on Wine.

No services, user clients, credentials, existing process or system ACL changes.
Results are diagnostic observations, not native Windows validation.
"""
from __future__ import annotations
import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import platform_windows as win, windows_acl as acl, windows_paths as paths
from tests.test_windows_acl_wine_diagnostic import observe_directory


def requested_descriptor(parent, *, directory=True):
    p = win.open_directory(parent)
    try:
        obs = acl._NativeBackend().read_observation(p.handle, 'directory', paths._DIR_CHECK_ACCESS)
        user = obs.user
    finally:
        p.close()
    authority = int.from_bytes(user[2:8], 'big')
    components = [str(int.from_bytes(user[i:i+4], 'little')) for i in range(8, len(user), 4)]
    sid = 'S-' + str(user[0]) + '-' + str(authority)
    if components:
        sid += '-' + '-'.join(components)
    # Exact same descriptor requested by _NativeAPI.create_private_directory.
    inheritance = 'OICI' if directory else ''
    text = 'O:' + sid + 'D:P(A;' + inheritance + ';FA;;;' + sid + ')'
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int32
    adv.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint16),
                                                ctypes.POINTER(ctypes.c_uint32)]
    adv.GetSecurityDescriptorControl.restype = ctypes.c_int32
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    desc = ctypes.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(text, 1, ctypes.byref(desc), None):
        return {'conversion': 'failed', 'winerror': ctypes.get_last_error()}
    try:
        control, revision = ctypes.c_uint16(), ctypes.c_uint32()
        if not adv.GetSecurityDescriptorControl(desc, ctypes.byref(control), ctypes.byref(revision)):
            return {'conversion': 'control_unavailable'}
        return {'conversion': 'ok', 'dacl_protected': bool(control.value & 0x1000),
                'control': hex(control.value), 'explicit_user_only': True}
    finally:
        kernel.LocalFree(desc)


class FixtureSecurityAttributes(ctypes.Structure):
    _fields_ = [('length', ctypes.c_uint32), ('descriptor', ctypes.c_void_p),
                ('inherit', ctypes.c_int32)]


def make_fixture(path, *, directory, protected=False):
    """Call Win32 directly on an empty diagnostic fixture."""
    parent = win.open_directory(os.path.dirname(path))
    try:
        user = acl._NativeBackend().read_observation(
            parent.handle, 'directory', paths._DIR_CHECK_ACCESS).user
    finally:
        parent.close()
    authority = int.from_bytes(user[2:8], 'big')
    components = [str(int.from_bytes(user[i:i+4], 'little')) for i in range(8, len(user), 4)]
    sid = 'S-' + str(user[0]) + '-' + str(authority)
    if components:
        sid += '-' + '-'.join(components)
    flags = 'OICI' if directory else ''
    text = 'O:' + sid + 'D:' + ('P' if protected else '') + '(A;' + flags + ';FA;;;' + sid + ')'
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int32
    adv.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint16),
                                                ctypes.POINTER(ctypes.c_uint32)]
    adv.GetSecurityDescriptorControl.restype = ctypes.c_int32
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int32
    desc = ctypes.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(text, 1, ctypes.byref(desc), None):
        return {'result': 'descriptor_unavailable', 'winerror': ctypes.get_last_error()}
    try:
        control, revision = ctypes.c_uint16(), ctypes.c_uint32()
        if not adv.GetSecurityDescriptorControl(desc, ctypes.byref(control), ctypes.byref(revision)):
            return {'result': 'control_unavailable'}
        attrs = FixtureSecurityAttributes(ctypes.sizeof(FixtureSecurityAttributes), desc, False)
        if directory:
            kernel.CreateDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(FixtureSecurityAttributes)]
            kernel.CreateDirectoryW.restype = ctypes.c_int32
            if not kernel.CreateDirectoryW(path, ctypes.byref(attrs)):
                return {'result': 'create_unavailable', 'winerror': ctypes.get_last_error()}
        else:
            kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.POINTER(FixtureSecurityAttributes), ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
            kernel.CreateFileW.restype = ctypes.c_void_p
            handle = kernel.CreateFileW(path, 0x80000000 | 0x40000000, 3,
                                        ctypes.byref(attrs), 1, 0x00200000, None)
            if handle is None or handle == win.INVALID_HANDLE_VALUE:
                return {'result': 'create_unavailable', 'winerror': ctypes.get_last_error()}
            if not kernel.CloseHandle(handle):
                return {'result': 'close_unknown'}
        return {'result': 'created', 'requested_protected': bool(control.value & 0x1000),
                'requested_control': hex(control.value)}
    finally:
        kernel.LocalFree(desc)


def named_control(path):
    """Independent name-based Win32 security-descriptor readback."""
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    ptr, dword = ctypes.c_void_p, ctypes.c_uint32
    adv.GetNamedSecurityInfoW.argtypes = [ctypes.c_wchar_p, dword, dword,
        ctypes.POINTER(ptr), ctypes.POINTER(ptr), ctypes.POINTER(ptr),
        ctypes.POINTER(ptr), ctypes.POINTER(ptr)]
    adv.GetNamedSecurityInfoW.restype = dword
    adv.GetSecurityDescriptorControl.argtypes = [ptr, ctypes.POINTER(ctypes.c_uint16),
                                                ctypes.POINTER(dword)]
    adv.GetSecurityDescriptorControl.restype = ctypes.c_int32
    kernel.LocalFree.argtypes = [ptr]
    kernel.LocalFree.restype = ptr
    owner, group, dacl, descriptor = ptr(), ptr(), ptr(), ptr()
    code = adv.GetNamedSecurityInfoW(path, 1, 0x7, ctypes.byref(owner),
                                      ctypes.byref(group), ctypes.byref(dacl), None,
                                      ctypes.byref(descriptor))
    if code or not descriptor.value:
        return {'result': 'unavailable', 'winerror': int(code)}
    try:
        control, revision = ctypes.c_uint16(), dword()
        if not adv.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            return {'result': 'control_unavailable'}
        return {'result': 'ok', 'control': hex(control.value),
                'dacl_protected': bool(control.value & 0x1000)}
    finally:
        kernel.LocalFree(descriptor)


def observe_file(path):
    owned = win.open_read_file(path)
    backend = acl._NativeBackend()
    try:
        required = acl.FILE_GENERIC_READ | acl.FILE_GENERIC_WRITE
        observation = backend.read_observation(owned.handle, 'file', required)
        names = {acl._SYSTEM: 'SYSTEM', acl._ADMINISTRATORS: 'Administrators',
                 acl._EVERYONE: 'Everyone', acl._BUILTIN_USERS: 'Builtin Users',
                 acl._AUTHENTICATED_USERS: 'Authenticated Users'}
        def role(sid):
            return names.get(sid, 'effective user' if sid == observation.user else 'other')
        result = {'owner_is_user': observation.owner == observation.user,
                  'dacl_protected': bool(observation.control & 0x1000),
                  'access_granted': observation.access_granted,
                  'privilege_count': observation.privilege_count,
                  'aces': [{'principal': role(a.sid), 'mask': hex(a.mask),
                            'flags': hex(a.flags)} for a in observation.aces],
                  'file_size': owned._backend.file_marker(owned.handle)[4]}
        try:
            acl.read_stable_evidence(owned.handle, kind='file', policy='private',
                                     required_access=required, backend=backend)
            result['private'] = 'accepted'
        except acl.ACLVerificationError as exc:
            result['private'] = exc.code
        return result
    finally:
        owned.close()


def diagnostic(existing_state):
    backend = acl._NativeBackend()
    raw = paths.NativeWin32()
    result = {'environment': 'Wine/Whisky; not native Windows'}
    if existing_state:
        result['blackbox_state_readonly'] = observe_directory(existing_state, 'blackbox state', raw, backend)
        result['blackbox_named_control'] = named_control(existing_state)
        try:
            value = win.ensure_private_directory(existing_state)
            result['blackbox_state_recheck'] = {'result': 'verified', 'created': value.created}
        except win.WindowsPrivateCreationError as exc:
            result['blackbox_state_recheck'] = {'result': exc.code, 'created': exc.created,
                                                 'release_unknown': exc.release_unknown}
    with tempfile.TemporaryDirectory(prefix='mp-b-create-diagnostic-') as parent:
        result['requested'] = requested_descriptor(parent)
        result['requested_file'] = requested_descriptor(parent, directory=False)
        state = os.path.join(parent, 'fake-state')
        try:
            value = win.ensure_private_directory(state)
            result['fresh_call'] = {'result': 'verified', 'created': value.created}
        except win.WindowsPrivateCreationError as exc:
            result['fresh_call'] = {'result': exc.code, 'created': exc.created,
                                    'release_unknown': exc.release_unknown}
        if os.path.isdir(state):
            result['fresh_observed'] = observe_directory(state, 'fresh state', raw, backend)
            result['fresh_named_control'] = named_control(state)
            try:
                value = win.ensure_private_directory(state)
                result['fresh_recheck'] = {'result': 'verified', 'created': value.created}
            except win.WindowsPrivateCreationError as exc:
                result['fresh_recheck'] = {'result': exc.code, 'created': exc.created,
                                           'release_unknown': exc.release_unknown}
        result['security_attributes_layout'] = {
            'size': ctypes.sizeof(FixtureSecurityAttributes),
            'descriptor_offset': FixtureSecurityAttributes.descriptor.offset,
            'inherit_offset': FixtureSecurityAttributes.inherit.offset}
        direct_dir = os.path.join(parent, 'direct-protected-directory')
        result['direct_protected_dir_create'] = make_fixture(direct_dir, directory=True, protected=True)
        if os.path.isdir(direct_dir):
            result['direct_protected_dir_named'] = named_control(direct_dir)
        direct_file = os.path.join(parent, 'direct-protected-file')
        result['direct_protected_file_create'] = make_fixture(direct_file, directory=False, protected=True)
        if os.path.isfile(direct_file):
            result['direct_protected_file_named'] = named_control(direct_file)
        # A separate existing-object fixture was never requested as protected.
        # This exercises only B's existing-private-object branch, not creation.
        existing_fixture = os.path.join(parent, 'already-existing-private')
        os.mkdir(existing_fixture)
        existing_log = os.path.join(existing_fixture, 'fake-existing-log')
        with open(existing_log, 'xb'):
            pass
        result['fixture_existing_dir_observed'] = observe_directory(
            existing_fixture, 'existing fixture', raw, backend)
        try:
            existing = win.ensure_private_directory(existing_fixture)
            result['fixture_existing_dir_check'] = {'result': 'verified', 'created': existing.created}
        except win.WindowsPrivateCreationError as exc:
            result['fixture_existing_dir_check'] = {'result': exc.code, 'created': exc.created}
        try:
            observed = win.read_guarded_file(existing_log, max_bytes=64)
            result['fixture_existing_file_check'] = {'result': 'verified',
                'empty': observed.data == b'',
                'dacl_protected': bool(observed.protection[2] & 0x1000)}
        except win.WindowsReadError as exc:
            result['fixture_existing_file_check'] = {'result': exc.code}
        private_fixture = os.path.join(parent, 'existing-mode-700')
        os.mkdir(private_fixture, 0o700)
        private_log = os.path.join(private_fixture, 'fake-existing-log')
        descriptor = os.open(private_log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        result['fixture_mode700_dir_observed'] = observe_directory(
            private_fixture, 'existing mode-700 fixture', raw, backend)
        try:
            existing = win.ensure_private_directory(private_fixture)
            result['fixture_mode700_dir_check'] = {'result': 'verified', 'created': existing.created}
        except win.WindowsPrivateCreationError as exc:
            result['fixture_mode700_dir_check'] = {'result': exc.code, 'created': exc.created}
        try:
            result['fixture_mode700_file_observed'] = observe_file(private_log)
        except Exception as exc:
            result['fixture_mode700_file_observed'] = {'result': getattr(exc, 'code', type(exc).__name__)}
        try:
            observed = win.read_guarded_file(private_log, max_bytes=64)
            result['fixture_mode700_file_check'] = {'result': 'verified',
                'empty': observed.data == b'',
                'dacl_protected': bool(observed.protection[2] & 0x1000)}
        except win.WindowsReadError as exc:
            result['fixture_mode700_file_check'] = {'result': exc.code}
        fixture_unprotected = os.path.join(parent, 'existing-explicit-unprotected')
        result['fixture_unprotected_dir_create'] = make_fixture(
            fixture_unprotected, directory=True)
        if os.path.isdir(fixture_unprotected):
            result['fixture_unprotected_dir_observed'] = observe_directory(
                fixture_unprotected, 'explicit unprotected fixture', raw, backend)
            try:
                checked = win.ensure_private_directory(fixture_unprotected)
                result['fixture_unprotected_dir_check'] = {'result': 'verified', 'created': checked.created}
            except win.WindowsPrivateCreationError as exc:
                result['fixture_unprotected_dir_check'] = {'result': exc.code, 'created': exc.created}
            fixture_log = os.path.join(fixture_unprotected, 'fake-existing-log')
            result['fixture_unprotected_log_create'] = make_fixture(fixture_log, directory=False)
            if os.path.isfile(fixture_log):
                result['fixture_unprotected_log_observed'] = observe_file(fixture_log)
                try:
                    checked = win.read_guarded_file(fixture_log, max_bytes=64)
                    result['fixture_unprotected_log_check'] = {'result': 'verified',
                        'empty': checked.data == b'',
                        'dacl_protected': bool(checked.protection[2] & 0x1000)}
                except win.WindowsReadError as exc:
                    result['fixture_unprotected_log_check'] = {'result': exc.code}
        log = os.path.join(parent, 'fake-log')
        try:
            value = win.create_private_empty_file(log)
            result['fresh_log'] = {'result': 'verified', 'created': value.created}
        except win.WindowsPrivateCreationError as exc:
            result['fresh_log'] = {'result': exc.code, 'created': exc.created,
                                   'release_unknown': exc.release_unknown}
        if os.path.isfile(log):
            result['fresh_log_observed'] = observe_file(log)
            result['fresh_log_named_control'] = named_control(log)
            try:
                read = win.read_guarded_file(log, max_bytes=64)
                result['fresh_log_existing_recheck'] = {
                    'result': 'verified', 'empty': read.data == b'',
                    'dacl_protected': bool(read.protection[2] & 0x1000)}
            except win.WindowsReadError as exc:
                result['fresh_log_existing_recheck'] = {'result': exc.code}
    return result


if __name__ == '__main__':
    if os.name != 'nt' or len(sys.argv) != 3 or not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise SystemExit('Windows Python -I -S -B, fresh result path and fake state path required')
    output = diagnostic(sys.argv[2])
    with open(sys.argv[1], 'x', encoding='utf-8') as stream:
        json.dump(output, stream, indent=2)
