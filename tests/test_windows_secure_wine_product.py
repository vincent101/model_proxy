"""Explicit Whisky diagnostic with fresh fake data; not native Windows proof.

Run as a script with -I -S -B and a new result filename. No product service,
real client configuration, credentials or existing process is accessed.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import platform_windows as win, windows_acl, windows_paths
from core.windows_secure_files import capture_target, write_target


def trace(event):
    # An explicitly supplied fresh trace belongs only to this fixture.
    if len(sys.argv) == 3:
        with open(sys.argv[2], 'a', encoding='utf-8') as stream:
            stream.write(event + '\n')
            stream.flush()


def diagnostic():
    result = {'environment': 'Wine/Whisky; not native Windows', 'new_writer': None,
              'existing_writer': None, 'explicit_creation': None}
    with tempfile.TemporaryDirectory(prefix='mp-private-write-fixture-') as directory:
        # All paths and bytes in this block belong solely to this new fixture.
        parent = windows_paths.NativeWin32()
        raw = parent.open_existing(directory, windows_paths._READ_ACCESS,
                                   windows_paths._SHARE_READ_WRITE, windows_paths._OPEN_FLAGS)
        try:
            token = windows_acl._NativeBackend().read_observation(
                raw, 'directory', windows_acl.FILE_GENERIC_READ).user
        finally:
            parent.close(raw)
        api = win._NativeAPI()
        candidate = os.path.join(directory, 'explicit-empty-fixture')
        handle = None
        try:
            handle = api.open_private_candidate(candidate, user_sid=token)
            try:
                proof = windows_acl.read_stable_evidence(
                    handle, kind='file', required_access=windows_acl.FILE_GENERIC_READ | windows_acl.FILE_GENERIC_WRITE)
                result['explicit_creation'] = ('private_protected' if proof.observation.control & 0x1000
                                                else 'protected_flag_missing')
            except windows_acl.ACLVerificationError as exc:
                result['explicit_creation'] = 'rejected:' + exc.code
        except OSError as exc:
            result['explicit_creation'] = 'api_failed:' + str(getattr(exc, 'winerror', 'unknown'))
        finally:
            if handle is not None:
                api.close(handle)
        for role in ('new', 'existing'):
            trace(role + ':begin')
            target = os.path.join(directory, role + '-fake-client.json')
            # Fixture setup uses a non-secret constant, not a product write.
            if role == 'existing':
                handle = api.open_private_candidate(target, user_sid=token)
                try:
                    api.write_all_and_flush(handle, b'fixture-before')
                finally:
                    api.close(handle)
            snap = None
            try:
                trace(role + ':capture-start')
                snap = capture_target(target, authorized_root=directory, max_bytes=1024)
                trace(role + ':write-start')
                outcome = write_target(snap, b'{"fixture":true}\n', preflight=lambda: True)
                trace(role + ':write-returned')
                result[role + '_writer'] = outcome.state
            except Exception as exc:
                result[role + '_writer'] = 'rejected:' + getattr(exc, 'code', type(exc).__name__)
                trace(role + ':write-rejected')
            finally:
                if snap is not None:
                    snap.close()
            if role == 'existing':
                with open(target, 'rb') as fixture:
                    result['existing_original_unchanged'] = fixture.read() == b'fixture-before'
    return result


if __name__ == '__main__':
    if os.name != 'nt' or len(sys.argv) not in (2, 3) or not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise SystemExit('Windows Python with -I -S -B and one fresh result path required')
    output = diagnostic()
    with open(sys.argv[1], 'x', encoding='utf-8') as stream:
        json.dump(output, stream, indent=2)
