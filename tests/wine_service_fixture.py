"""Explicitly unsafe, test-process-only Windows startup substitutes.

These cannot establish Windows filesystem, ACL, process ownership, safe record
commit, or listener exclusivity. Paths are restricted to this test's fresh
fixture. Product startup orchestration, ConfigStore, handler, TCP binding and
HTTP stack are not substituted.
"""
import contextlib
import json
import msvcrt
import os
from pathlib import Path
from unittest.mock import patch


def install(stack, server, root, project, config, pathsfile, paths):
    from core.config_snapshot import ConfigSnapshot, ConfigSnapshotError, FileMarker
    from core.discovery import PathIdentity, PathPreflight
    from core.runtime_paths import PATH_KEYS

    root, project = Path(root), Path(project)
    state = project / '.model_proxy_runtime'
    anchor_path = state / 'anchor.lock'
    allowed_snapshots = {config, pathsfile, paths['totals']}

    def within(p):
        p = Path(p)
        if not p.is_absolute() or not p.is_relative_to(root):
            raise AssertionError('test substitute path escaped fixture')
        return p

    def read_fake(path, **kwargs):
        path = within(path)
        if path not in allowed_snapshots or path.is_symlink():
            raise AssertionError('unexpected snapshot path')
        try:
            with path.open('rb') as f:
                before = os.fstat(f.fileno())
                data = json.load(f)
                after = os.fstat(f.fileno())
            if before != after or not isinstance(data, dict):
                raise ValueError('changed or non-object')
            marker = FileMarker(before.st_dev, before.st_ino, before.st_mtime_ns,
                                before.st_ctime_ns, before.st_size)
            return ConfigSnapshot(data, marker)
        except (OSError, ValueError):
            raise ConfigSnapshotError('config_read_error') from None

    def check_targets(actual_paths, actual_config):
        if actual_paths != paths or Path(actual_config) != config:
            raise AssertionError('unexpected target')
        for path in (*paths.values(), config, pathsfile, state):
            within(path)

    def identities(actual_root, actual_paths):
        if Path(actual_root) != project or actual_paths != paths:
            raise AssertionError('unexpected identity request')
        targets = dict(paths, state_dir=state, **{'anchor.lock': anchor_path,
                      'install.json': state / 'install.json'})
        result = []
        for key, p in targets.items():
            p = within(p)
            try:
                info = p.stat()
            except FileNotFoundError:
                info = None
            result.append((key, PathIdentity(
                requested=str(p), canonical=str(p), exists=info is not None,
                device=info.st_dev if info else None, inode=info.st_ino if info else None,
                ancestor_device=0, ancestor_inode=0, unresolved=() if info else (p.name,),
                trace=(), final_was_link=False, mode=None, uid=None, gid=None,
                link_count=info.st_nlink if info else None)))
        return PathPreflight(str(project), tuple(result))

    def legacy(snapshot):
        values = snapshot.as_dict()
        if values['pid'].exists or values['start_lock'].exists:
            raise AssertionError('unexpected legacy process artifacts')
        return tuple((key, None) for key in PATH_KEYS)

    class Held:
        def __init__(self, file, snapshot):
            self.file = file
            self.snapshot = snapshot
        def fileno(self):
            return self.file.fileno()

    @contextlib.contextmanager
    def lock(snapshot, role, **kwargs):
        p = anchor_path if role == 'anchor.lock' else paths['lock']
        with within(p).open('a+b') as f:
            if f.tell() == 0:
                f.write(b'0'); f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            held = Held(f, identities(project, paths))
            try:
                yield held
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)

    def verify_locks(anchor, legacy):
        for h in (anchor, legacy):
            if h.file.closed:
                raise AssertionError('test lock not held')

    def lock_ids(anchor, legacy):
        return {name: {'device': os.fstat(h.fileno()).st_dev,
                       'inode': os.fstat(h.fileno()).st_ino}
                for name, h in (('anchor', anchor), ('legacy', legacy))}

    def read_records(directory):
        if Path(directory) != state:
            raise AssertionError('unexpected records directory')
        install_file = state / 'install.json'
        if not install_file.exists():
            return None
        def read(name):
            p = state / (name + '.json')
            return json.loads(p.read_text(encoding='utf-8')) if p.exists() else None
        return {name: read(name) for name in ('install', 'active', 'last_good_paths')}

    def write_record(directory, name, data, **kwargs):
        if Path(directory) != state or name not in ('install.json', 'active.json', 'last_good_paths.json'):
            raise AssertionError('unexpected record write')
        within(state / name).write_text(json.dumps(data), encoding='utf-8')

    def initialize(anchor, legacy, sock, **kwargs):
        if kwargs.get('confirmed') is not True:
            raise AssertionError('fixture initialization must be explicit')
        kwargs['precommit_check']()
        write_record(state, 'install.json', {'schema': server.INSTANCE_SCHEMA, 'record': 'install',
                     'install_id': 'a' * 32, 'project_root': str(project), 'ever_started': False,
                     'last_instance_nonce': None})

    def bypass_listener_gate():
        print('BYPASS REACHED: Windows listener exclusivity gate', flush=True)

    substitutions = {
        '_check_config_targets': check_targets,
        'inspect_runtime_identities': identities,
        '_check_legacy_observations': legacy,
        'verify_record_directory': lambda path: within(path),
        'acquire_lock': lock,
        'verify_held_instance_locks': verify_locks,
        '_lock_identities': lock_ids,
        'read_stable_config': read_fake,
        'current_file_marker': lambda path: read_fake(path).marker,
        'read_instance_set': read_records,
        'write_instance_record': write_record,
        'initialize_installation': initialize,
        '_confirm_initialization': lambda: 'test-only fake installation',
        '_require_windows_listener_validation': bypass_listener_gate,
    }
    for name, value in substitutions.items():
        stack.enter_context(patch.object(server, name, side_effect=value))
    print('BYPASSES: config target/path/ACL evidence; runtime path identities and legacy '
          'observations; record-directory ACL; held-lock evidence (fixture msvcrt locks); '
          'config/ledger guarded reads (fixture files only); installation attestation and '
          'record read/write/commit; Windows listener exclusivity gate', flush=True)
