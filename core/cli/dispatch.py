"""Shared command dispatcher. System-dependent control is injectable."""
from __future__ import annotations

import json
import contextlib
import ntpath
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import unicodedata
from collections import deque
from pathlib import Path
from types import SimpleNamespace

from core.cli.control import UnavailableControl
from core.cli.bre import BREError, compile_bre
from core.cli.help_text import write_help, write_platform_text
from core.cli import stats
from core.runtime_paths import read_runtime_paths, parse_runtime_paths, PATH_KEYS, RuntimePathsError
from core.discovery import inspect_runtime_identities, PreflightError

ROOT = Path(__file__).resolve().parents[2]
def config_path(root=ROOT):
    raw = os.environ.get('MODEL_PROXY_CONFIG') or str(root / 'config' / 'model_proxy_config.json')
    # Path() drops trailing slash and '.' components before the Mac path
    # verifier sees them. Preserve the launch spelling on that platform.
    return raw if sys.platform == 'darwin' else Path(raw)


def runtime_paths(root=ROOT):
    # 调用方可能已把 root 归一化成 str（Windows 分支用 ntpath 拼路径后就是这样），
    # 而这里要用 `/` 运算符拼子路径。统一在本函数内换成 Path，两种入参都接受。
    root = Path(os.fspath(root))
    paths = read_runtime_paths(root / 'config' / 'runtime_paths.json', project_root=root,
                               local_appdata=os.environ.get('LOCALAPPDATA'))
    # Read-only preflight is not a service ownership proof. It is nevertheless
    # required before interpreting the configured paths.
    inspect_runtime_identities(root, paths)
    return paths


def validate_editable_config(cfg, operation=None):
    """Check only collections consumed by this operation, not a full schema."""
    if not isinstance(cfg, dict):
        raise ValueError('configuration is not an object')
    required = {
        'supply-add': ('supplies',), 'supply-edit': ('supplies',),
        'supply-check': ('supplies',), 'supply-del': ('supplies', 'routes'),
        'route-add': ('routes', 'supplies'), 'route-edit': ('routes', 'supplies'),
        'route-del': ('routes', 'strategies'),
        'strategy-add': ('strategies', 'routes'), 'strategy-edit': ('strategies', 'routes'),
        'strategy-del': ('strategies',), 'switch': ('strategies', 'routes'),
        'install': ('supplies', 'routes', 'strategies'),
    }.get(operation, ())
    for key in required:
        if key in cfg and (not isinstance(cfg[key], list)
                           or any(not isinstance(item, dict) for item in cfg[key])):
            raise ValueError('invalid configuration collection')


def write_preflight(root, config, extra_targets=(), distinct_pairs=(), *, operation=None):
    """Check runtime identities and configuration before a possible write.

    The identity snapshot is read-only, not a lock or service ownership proof.
    Test both replaced roles so each candidate is compared with all six
    runtime targets as well as the fixed state directory and records.
    """
    try:
        paths = runtime_paths(root)
        evidence = None
        if sys.platform == 'darwin':
            from core.mac_config_path import capture_config, verify_config
            evidence = capture_config(config)
            # The actual target is used for identity comparisons, but the
            # original spelling remains the source of the sidecar and helper.
            source = Path(evidence.source_path)
            config = Path(evidence.target_path)
            sidecar = source.parent / 'session_overrides.json'
            absolute = lambda path: Path(path) if Path(path).is_absolute() else Path.cwd() / path
        else:
            absolute = lambda path: Path(os.path.abspath(path))
        config = absolute(config)
        paths_file = absolute(Path(root) / 'config' / 'runtime_paths.json')
        targets = []
        for target in extra_targets:
            raw = os.fspath(target)
            if not isinstance(raw, str) or not raw or '\0' in raw:
                raise ValueError('invalid write target')
            targets.append(absolute(raw))
        if evidence is not None:
            targets.append(sidecar)
        for candidate in (config, paths_file, *targets):
            for role in ('log', 'totals'):
                probe = dict(paths)
                probe[role] = candidate
                inspect_runtime_identities(root, probe)
        pairs = [(config, paths_file), *(
            (target, protected) for target in targets for protected in (config, paths_file))]
        pairs.extend(distinct_pairs)
        for first, second in pairs:
            probe = dict(paths)
            probe['log'], probe['totals'] = first, second
            inspect_runtime_identities(root, probe)
        if evidence is not None:
            cfg = json.loads(evidence.data.decode('utf-8'))
            verify_config(evidence)
        else:
            with config.open(encoding='utf-8') as stream:
                cfg = json.load(stream)
        validate_editable_config(cfg, operation)
        return True
    except (RuntimePathsError, PreflightError) as exc:
        print(f'Error: write preflight failed ({exc.code}).', file=sys.stderr)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RuntimeError, RecursionError):
        print('Error: write preflight unavailable or configuration invalid.', file=sys.stderr)
    return False


def _config_write_evidence(root, snapshot, operation):
    """Read-only evidence for one exact Mac business-config snapshot.

    A successful result is not a write grant. The immutable runtime and
    candidate observations are retained and compared again at commit time.
    """
    from core.mac_config_path import verify_config
    from core.cli.authority import _capture

    if sys.platform != 'darwin':
        raise PreflightError('config_unverified', 'Mac write evidence unavailable')
    verify_config(snapshot)
    root = Path(root)
    if not root.is_absolute() or root != root.resolve(strict=True):
        raise PreflightError('config_unverified', 'Installation root unavailable')
    runtime_file = root / 'config' / 'runtime_paths.json'
    runtime = _capture(runtime_file, private=False)
    if not runtime.exists:
        raise PreflightError('config_unverified', 'Runtime configuration unavailable')
    parsed = parse_runtime_paths(json.loads(runtime.contents.decode('utf-8')),
                                 project_root=root,
                                 local_appdata=os.environ.get('LOCALAPPDATA'))
    paths = {key: str(parsed[key]) for key in PATH_KEYS}
    validate_editable_config(json.loads(snapshot.data.decode('utf-8')), operation)

    config = Path(snapshot.target_path)
    sidecar = Path(snapshot.source_path).parent / 'session_overrides.json'
    observations = [inspect_runtime_identities(root, paths)]
    for candidate in (config, runtime_file, sidecar):
        for role in ('log', 'totals'):
            probe = dict(paths)
            probe[role] = candidate
            observations.append(inspect_runtime_identities(root, probe))
    for first, second in ((config, runtime_file), (config, sidecar), (runtime_file, sidecar)):
        probe = dict(paths)
        probe['log'], probe['totals'] = first, second
        observations.append(inspect_runtime_identities(root, probe))
    # The writer chooses random sibling names after this callback. Refuse a
    # protected role which could be one of those names even when absent;
    # otherwise staging could create a designated runtime file before the
    # next callback has a chance to notice the collision.
    normalized = lambda value: unicodedata.normalize('NFD', value).casefold()
    names = (r'\.model-proxy-config-[0-9a-f]{24}',
             re.escape(normalized(config.name)) + r'\.bak\.[0-9a-f]{16}')
    for observation in observations:
        for ident in observation.as_dict().values():
            candidate = Path(ident.canonical)
            try:
                same_parent = os.path.samefile(candidate.parent, config.parent)
            except FileNotFoundError:
                same_parent = False
            if (same_parent and candidate != config
                    and any(re.fullmatch(pattern, normalized(candidate.name)) for pattern in names)):
                raise PreflightError('config_conflict', 'Staging name overlaps protected path')
    if runtime != _capture(runtime_file, private=False):
        raise PreflightError('config_changed', 'Runtime configuration changed')
    verify_config(snapshot)
    return runtime, tuple(observations)


def _config_write_recheck(root, snapshot, operation):
    """Bind later checks to the same snapshot and runtime observations."""
    baseline = _config_write_evidence(root, snapshot, operation)

    def recheck(current):
        if current != snapshot:
            return False
        try:
            return _config_write_evidence(root, current, operation) == baseline
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, RuntimeError, RecursionError):
            return False

    return recheck


def _prepare_mac_config_runtime(root, snapshot, operation, baseline):
    """Prepare only the fixed private directories after read-only preflight."""
    from core.discovery import _verify_directory, verify_record_directory
    root = Path(root)
    state = root / '.runtime'
    if not baseline[1][0].as_dict()['state_dir'].exists:
        _verify_directory(root, private=False)
        # Exclusive creation: do not silently adopt an object that appeared
        # after the read-only observation.
        state.mkdir(mode=0o700)
    verify_record_directory(state)
    after_state = _config_write_evidence(root, snapshot, operation)
    # Only the exclusively-created state directory may appear between the
    # read-only preflight and its next observation. Do not silently accept a
    # concurrently created/replaced runtime or configuration target.
    if baseline[0] != after_state[0] or len(baseline[1]) != len(after_state[1]):
        raise PreflightError('config_changed', 'Runtime paths changed during directory preparation')
    state_identity = after_state[1][0].as_dict()['state_dir']
    for before, after in zip(baseline[1], after_state[1]):
        if (before.project_root != after.project_root
                or before.as_dict().keys() != after.as_dict().keys()
                or after.as_dict()['state_dir'] != state_identity):
            raise PreflightError('config_changed', 'Runtime paths changed during directory preparation')
        for role, old in before.as_dict().items():
            new = after.as_dict()[role]
            if old == new:
                continue
            extra = new.trace[len(old.trace):]
            state_created = (not old.exists and new.trace[:len(old.trace)] == old.trace
                             and len(extra) == 1 and extra[0][0] == str(state)
                             and extra[0] == state_identity.trace[-1]
                             and old.requested == new.requested and old.canonical == new.canonical
                             and ((role == 'state_dir' and new.exists)
                                  or (role != 'state_dir' and not new.exists
                                      and old.unresolved[1:] == new.unresolved)))
            if not state_created:
                raise PreflightError('config_changed', 'Runtime paths changed during directory preparation')
    temporary = state / 'tmp'
    try:
        temporary.lstat()
    except FileNotFoundError:
        temporary.mkdir(mode=0o700)
    _verify_directory(temporary, private=True)
    verify_record_directory(state)
    after_tmp = _config_write_evidence(root, snapshot, operation)
    if after_tmp != after_state:
        raise PreflightError('config_changed', 'Runtime paths changed during directory preparation')
    return after_tmp


def _config_write_authorization(snapshot, *, external_confirmed=False):
    from core.mac_config_path import open_target_parent
    from core.mac_config_writer import WriteAuthorization, validate_write_authorization

    with open_target_parent(snapshot) as (fd, _):
        parent = os.fstat(fd)
        authorization = WriteAuthorization(snapshot.target_path, (parent.st_dev, parent.st_ino),
                                           tuple(snapshot.final_identity[:2]), True,
                                           external_confirmed=external_confirmed)
    validate_write_authorization(snapshot, authorization)
    return authorization


def _confirm_config_write(snapshot, recheck):
    """Explicitly authorize one target and its backup/staging siblings on a TTY."""
    from core.cli.authority import _require_same_terminal
    from core.mac_config_path import verify_config

    _require_same_terminal(sys.stdin, sys.stdout)
    if recheck(snapshot) is not True:
        raise PreflightError('config_changed', 'Configuration changed')
    # JSON escaping prevents a pathname from injecting terminal controls.
    print('将替换实际配置目标: ' + json.dumps(snapshot.target_path, ensure_ascii=True))
    print('本次备份写入项目 .runtime/backups，暂存使用项目 .runtime/tmp；')
    print('仅当二者与实际目标跨卷时，才允许在该目标父目录创建私有暂存。')
    print('输入 YES 确认: ', end='', flush=True)
    if sys.stdin.readline(64).strip() != 'YES':
        raise PreflightError('config_unverified', 'Write authorization not confirmed')
    _require_same_terminal(sys.stdin, sys.stdout)
    verify_config(snapshot)
    if recheck(snapshot) is not True:
        raise PreflightError('config_changed', 'Configuration changed')
    authorization = _config_write_authorization(snapshot, external_confirmed=True)
    if recheck(snapshot) is not True:
        raise PreflightError('config_changed', 'Configuration changed')
    return authorization


def prepare_config_write(root, snapshot, *, operation):
    """Authorize one verified target, with retained, repeatable conflict checks."""
    from core.discovery import _verify_directory

    allowed = {'switch', 'supply-add', 'supply-edit', 'supply-del', 'supply-check',
               'route-add', 'route-edit', 'route-del',
               'strategy-add', 'strategy-edit', 'strategy-del'}
    if sys.platform != 'darwin' or operation not in allowed:
        raise PreflightError('config_unverified', 'Write operation unavailable')
    root = Path(root)
    recheck = _config_write_recheck(root, snapshot, operation)
    directory = root / 'config'
    # A symlinked config directory cannot silently turn an external tree into
    # the default authorized scope.
    if directory.resolve(strict=True) != directory:
        raise PreflightError('config_unverified', 'Configuration directory unavailable')
    info = _verify_directory(directory, private=False)
    scope_identity = (info.st_dev, info.st_ino)
    target = Path(snapshot.target_path)
    # A verified target may reach the same directory through APFS case or
    # Unicode aliases. Compare existing directory identities, not spellings.
    internal = any(os.path.samefile(directory, ancestor) for ancestor in target.parents)

    def bound_recheck(current):
        try:
            if directory.resolve(strict=True) != directory:
                return False
            current_dir = _verify_directory(directory, private=False)
            if (current_dir.st_dev, current_dir.st_ino) != scope_identity:
                return False
            return recheck(current) is True
        except (OSError, ValueError, TypeError, RuntimeError):
            return False

    if bound_recheck(snapshot) is not True:
        raise PreflightError('config_changed', 'Configuration changed')
    if internal:
        authorization = _config_write_authorization(snapshot)
    else:
        try:
            authorization = _confirm_config_write(snapshot, bound_recheck)
        except PreflightError as exc:
            if exc.code == 'initialization_required':
                print('Error: external config target requires explicit terminal authorization; no write.',
                      file=sys.stderr)
            raise
    if bound_recheck(snapshot) is not True:
        raise PreflightError('config_changed', 'Configuration changed')
    return authorization, bound_recheck


def _marker_fd_protocol_available(root):
    """Fail closed for old or mismatched helper code, before creating a pipe."""
    try:
        from core.cli import _config_ops
        from core.cli.authority import _capture
        version = getattr(_config_ops, 'ANONYMOUS_MARKER_FD_PROTOCOL', None)
        precheck = getattr(_config_ops, 'ANONYMOUS_MARKER_FD_PRECHECK', None)
        if (type(version) is not int or version != 2
                or type(precheck) is not int or precheck != 1):
            return False
        local = _capture(Path(_config_ops.__file__), private=False)
        helper = _capture(Path(root) / 'core' / 'cli' / '_config_ops.py', private=False)
        return (local.exists and helper.exists and local.contents == helper.contents
                and local == _capture(Path(_config_ops.__file__), private=False)
                and helper == _capture(Path(root) / 'core' / 'cli' / '_config_ops.py', private=False))
    except (OSError, ValueError, TypeError, RuntimeError, AttributeError, ImportError):
        return False


def _pipe_endpoint_protection(fd, *, write):
    """Accept only a held anonymous pipe endpoint, never a file or named FIFO."""
    import fcntl
    info = os.fstat(fd)
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    access = flags & os.O_ACCMODE
    # Darwin sets an internal O_WRITTEN bit after a successful pipe write.
    # Bind the meaningful access/status flags, not that mutable kernel bit.
    stable_flags = flags & (os.O_ACCMODE | os.O_NONBLOCK | os.O_APPEND
                            | getattr(os, 'O_ASYNC', 0) | getattr(os, 'O_SYNC', 0))
    if (not stat.S_ISFIFO(info.st_mode) or info.st_nlink != 0
            or info.st_uid != os.geteuid() or info.st_ino <= 0
            or getattr(info, 'st_flags', 0)
            or access != (os.O_WRONLY if write else os.O_RDONLY)):
        raise ValueError('anonymous marker pipe unavailable')
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, getattr(info, 'st_flags', 0), stable_flags)


def _drain_marker_pipe(fd, expected):
    """Read bounded frames; another live writer or malformed data is UNKNOWN."""
    if _pipe_endpoint_protection(fd, write=False) != expected:
        raise ValueError('marker pipe changed')
    os.set_blocking(fd, False)
    data = bytearray()
    while True:
        try:
            chunk = os.read(fd, 129 - len(data))
        except BlockingIOError:
            raise ValueError('marker writer still open') from None
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > 128:
            raise ValueError('marker pipe overflow')
    if _pipe_endpoint_protection(fd, write=False) != expected:
        raise ValueError('marker pipe changed')
    return bytes(data)


def _run_mac_write_helper(command, args, config, control, root):
    """Run only with an inherited anonymous pipe, never a named marker file."""
    from core.mac_config_path import capture_config

    if not _marker_fd_protocol_available(root):
        print('Error: anonymous marker FD protocol unavailable; helper not started; no reload.',
              file=sys.stderr)
        return 1
    read_fd = write_fd = None
    pipe_attempted = pipe_created = helper_started = False
    close_confirmed = True
    runtime_attempted = False
    runtime_ready = False
    problem = None
    rc, frames = 1, b''
    try:
        snapshot = capture_config(config)
        baseline = _config_write_evidence(root, snapshot, command)
        runtime_attempted = True
        baseline = _prepare_mac_config_runtime(root, snapshot, command, baseline)
        runtime_ready = True
        pipe_attempted = True
        read_fd, write_fd = os.pipe()
        pipe_created = True
        if read_fd < 3 or write_fd < 3:
            raise ValueError('standard descriptors unavailable')
        os.set_blocking(read_fd, False)
        read_proof = _pipe_endpoint_protection(read_fd, write=False)
        write_proof = _pipe_endpoint_protection(write_fd, write=True)
        if _config_write_evidence(root, snapshot, command) != baseline:
            raise ValueError('configuration changed during marker setup')
        if not _marker_fd_protocol_available(root):
            raise ValueError('helper protocol changed')
        env = os.environ.copy()
        env.pop('CONFIG_OPS_RELOAD_MARKER', None)
        env['CONFIG_OPS_RELOAD_MARKER_FD'] = str(write_fd)
        helper = Path(root) / 'core' / 'cli' / 'helper_entry.py'
        helper_started = True
        result = subprocess.run([sys.executable, str(helper), command, str(config), *args],
                                env=env, pass_fds=(write_fd,))
        rc = result.returncode if result.returncode >= 0 else 128 - result.returncode
        if _pipe_endpoint_protection(write_fd, write=True) != write_proof:
            raise ValueError('marker pipe changed')
        closing_write_fd = write_fd
        write_fd = None
        try:
            os.close(closing_write_fd)
        except BaseException:
            # close() may have released the number despite reporting failure.
            # It can already name an unrelated FD; never close it again.
            close_confirmed = False
            raise
        frames = _drain_marker_pipe(read_fd, read_proof)
        fresh = capture_config(config)
        fresh_baseline = _config_write_evidence(root, fresh, command)
        if (fresh_baseline[0] != baseline[0] or fresh_baseline[1][0] != baseline[1][0]
                or fresh.target_path != snapshot.target_path
                or fresh.chain[:-1] != snapshot.chain[:-1]
                or fresh.target_parent_protection != snapshot.target_parent_protection
                or fresh.protection[1][2:] != snapshot.protection[1][2:]):
            raise ValueError('configuration or runtime changed')
    except KeyboardInterrupt:
        problem, rc = 'interrupted', 130
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RuntimeError, RecursionError):
        problem = 'preflight' if not helper_started else 'helper'
    finally:
        for fd in (write_fd, read_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except KeyboardInterrupt:
                    close_confirmed = False
                    problem, rc = 'interrupted', 130
                except OSError:
                    close_confirmed = False
    if problem or not close_confirmed:
        if runtime_attempted and not runtime_ready:
            print('Error: runtime directory preparation unconfirmed; possible private remnants retained.',
                  file=sys.stderr)
        state = ('created' if pipe_created else 'creation unknown' if pipe_attempted else 'not created')
        closure = ('unknown' if pipe_attempted and not pipe_created else
                   'confirmed' if close_confirmed else 'not confirmed')
        stage = ('helper may have run; configuration may have been saved'
                 if helper_started else 'helper not started')
        print(f'Error: anonymous marker pipe {state}; close {closure}; {stage}; no reload.',
              file=sys.stderr)
        return rc or 1
    if rc:
        if frames not in (b'no\n', b'writing\nno\n'):
            print('Error: helper failed; configuration may have been saved.', file=sys.stderr)
        return rc
    if frames in (b'writing\nyes\n', b'writing\nyes\nyes\n'):
        print('Configuration changed; attempting reload.', flush=True)
        rc = control.reload()
        if rc:
            print('Configuration saved, but reload was not confirmed.', file=sys.stderr)
        return rc
    if frames != b'no\n':
        print('Error: marker pipe result invalid; configuration may have been saved.', file=sys.stderr)
        return 1
    return 0


def _cleanup_windows_marker(directory, expected, root=ROOT):
    """Delete only known marker artifacts under the same protected directory."""
    from core import windows_acl, windows_paths, platform_windows
    from core.discovery import _windows_path_backend
    lease = windows_paths.inspect(directory, backend=_windows_path_backend(), kind="directory",
                                  private=True, trusted_boundary=os.fspath(root))
    try:
        if not lease.evidence.exists:
            return
        proof = windows_acl.read_stable_evidence(lease._handles[-1].raw, kind="directory",
                                                 policy="private", required_access=0x200A1)
        if (proof.observation.identity != expected.identity
                or platform_windows.acl_protection(proof) != expected.protection):
            raise RuntimeError("marker directory changed")
        lease.revalidate()
        with os.scandir(directory) as entries:
            files = list(entries)
        for entry in files:
            # Unknown artifacts are not ours to delete, even if their names
            # resemble an internal staging filename.
            if entry.name != "reload":
                raise RuntimeError("unexpected marker artifact")
            if not entry.is_file(follow_symlinks=False):
                raise RuntimeError("unexpected marker artifact")
        for entry in files:
            os.unlink(entry.path)
        lease.revalidate()
    finally:
        lease.close()
    os.rmdir(directory)


def _verified_windows_marker_directory(result, *, require_new):
    """Validate creation evidence without hiding an unknown creation result."""
    from core.platform_windows import WindowsPrivateCreationError
    created = getattr(result, "created", None)
    if getattr(result, "release_unknown", False) is True:
        state = created if type(created) is bool else None
        raise WindowsPrivateCreationError("release_unknown", created=state, release_unknown=True)
    if type(created) is not bool:
        raise WindowsPrivateCreationError("marker_creation_unknown", created=None)
    if require_new and not created:
        raise WindowsPrivateCreationError("marker_directory_not_new", created=False)
    identity, protection = getattr(result, "identity", None), getattr(result, "protection", None)
    if (not isinstance(identity, tuple) or len(identity) != 4
            or any(type(value) is not int or value < 0 for value in identity)
            or identity[1] <= 0 or not isinstance(protection, tuple) or not protection):
        raise WindowsPrivateCreationError("marker_directory_unverified", created=created)
    return result


def _windows_marker_directory_preflight(root, config, directory, held_control):
    """Prove a missing marker directory is distinct from every protected role."""
    from core import discovery, platform_lock, windows_paths
    from core.cli._config_ops import _windows_role_evidence

    root, config = os.fspath(root), ntpath.abspath(os.fspath(config))
    _windows_role_evidence(root, config, None, None, held_control=held_control)
    paths = runtime_paths(root)
    summary, roles = discovery._windows_open_preflight(root, paths)
    held = list(roles.values())
    try:
        if (not isinstance(held_control, platform_lock.HeldLock) or held_control.closed
                or held_control.role != 'control.lock' or held_control.snapshot != summary):
            raise RuntimeError('marker control lock unverified')
        with platform_lock._fork_guard, held_control._state_lock:
            platform_lock._windows_verify_guard(held_control._windows_guard, roles['control.lock'])
        monitor = roles['__root__']._backend
        lease = windows_paths.inspect(directory, backend=monitor, kind='directory', private=True,
                                      trusted_boundary=root)
        held.append(lease)
        if (lease.evidence.exists or lease.evidence.missing != (ntpath.basename(directory),)
                or not lease.evidence.direct_parent_observed):
            raise RuntimeError('marker directory not provably absent')
        temp_evidence = roles['tmp_dir'].evidence
        if (not temp_evidence.exists or
                lease.evidence.components[-1].observation != temp_evidence.components[-1].observation):
            raise RuntimeError('marker directory parent changed')
        selected = (config, ntpath.join(root, 'config', 'runtime_paths.json'),
                    ntpath.join(ntpath.dirname(config), 'session_overrides.json'))
        extra = []
        for path in selected:
            other = windows_paths.inspect(
                path, backend=monitor, kind='file', private=True,
                trusted_boundary=discovery._windows_trusted_boundary(root, path))
            held.append(other)
            if not other.evidence.direct_parent_observed:
                raise RuntimeError('marker protected parent unverified')
            extra.append(other)
        protected = [value for name, value in roles.items()
                     if name not in ('__root__', 'state_dir', 'tmp_dir')] + extra
        for item in held:
            item.revalidate()
        for other in protected:
            if (discovery._windows_evidence_contains(lease.evidence, other.evidence)
                    or discovery._windows_evidence_contains(other.evidence, lease.evidence)):
                raise RuntimeError('marker directory role conflict')
        with platform_lock._fork_guard, held_control._state_lock:
            platform_lock._windows_verify_guard(held_control._windows_guard, roles['control.lock'])
        for item in held:
            item.revalidate()
        return lease.evidence
    finally:
        discovery._windows_close_leases(held)


def _windows_marker_parent_check(snapshot, marker, directory_proof):
    """Bind this operation to the exclusively created, protected parent."""
    from core import platform_windows, windows_acl
    if snapshot.path != marker:
        raise RuntimeError('marker target changed')
    snapshot.revalidate()
    lease = snapshot._target_lease
    parent_index = -2 if snapshot.exists else -1
    parent = lease._handles[parent_index]
    observation = lease.evidence.components[parent_index].observation
    expected = directory_proof.identity
    if ((observation.volume, int.from_bytes(observation.file_id, 'big')) != expected[:2]
            or (not snapshot.exists and lease.evidence.missing != ('reload',))):
        raise RuntimeError('marker parent identity changed')
    protection = windows_acl.read_stable_evidence(
        parent.raw, kind='directory', policy='private',
        required_access=0x200A1)
    if platform_windows.acl_protection(protection) != directory_proof.protection:
        raise RuntimeError('marker parent protection changed')
    lease.revalidate()
    snapshot.revalidate()


@contextlib.contextmanager
def _windows_marker_scope(root=ROOT, config=None):
    """Initialize B's marker contract in a freshly private temporary directory."""
    from core import platform_windows as native, windows_secure_files as secure
    from core.discovery import prepare_windows_runtime_directories, windows_initial_missing_parents
    from core.platform_lock import acquire_lock
    from core.cli._config_ops import (_windows_role_evidence, _windows_runtime_locations,
                                      _windows_write_options)
    from core.server import _windows_check_config_targets
    paths = read_runtime_paths(Path(root) / 'config' / 'runtime_paths.json', project_root=root,
                               local_appdata=os.environ.get('LOCALAPPDATA'))
    if config is None:
        raise RuntimeError("configuration preflight unavailable")
    _windows_check_config_targets(
        paths, config, project_root=root,
        initial_missing_parents=windows_initial_missing_parents(
            paths, os.environ.get("LOCALAPPDATA")))
    prepare_windows_runtime_directories(root, paths, os.environ.get("LOCALAPPDATA"))
    temporary = ntpath.join(os.fspath(root), ".runtime", "tmp")
    holder = None
    try:
        # This short lock covers marker preparation only; release it before
        # interactive helper/reload, which takes control.lock independently.
        lock_snapshot = inspect_runtime_identities(root, paths)
        with acquire_lock(lock_snapshot, "control.lock", create=True) as control_lock:
            _windows_role_evidence(root, config, None, None, held_control=control_lock)
            proof = native.ensure_private_directory(temporary, trusted_boundary=os.fspath(root))
            _verified_windows_marker_directory(proof, require_new=False)
            _windows_role_evidence(root, config, None, None, held_control=control_lock)
            directory = ntpath.join(temporary, "model-proxy-cli-" + secrets.token_hex(16))
            _windows_marker_directory_preflight(root, config, directory, control_lock)
            created = native.ensure_private_directory(directory, trusted_boundary=os.fspath(root))
            _verified_windows_marker_directory(created, require_new=True)
            marker = ntpath.join(directory, "reload")
            holder = SimpleNamespace(path=marker, cleanup_ok=False, directory_proof=created)
            with secure.capture_target(marker, authorized_root=directory, max_bytes=64,
                                       trusted_boundary=os.fspath(root)) as snapshot:
                if snapshot.exists:
                    raise RuntimeError("marker already exists")
                def marker_check():
                    _windows_marker_parent_check(snapshot, marker, created)
                marker_check()
                locations = _windows_runtime_locations(root, config, None, snapshot,
                                                       held_control=control_lock)
                options = _windows_write_options(
                    root, config, None, snapshot, snapshot, locations, make_backup=False,
                    held_control=control_lock, marker_check=marker_check, config_may_change=False)
                outcome = secure.write_target(snapshot, b"started", **options)
                if outcome.state != "VERIFIED":
                    raise RuntimeError("marker write unknown")
            with secure.capture_target(marker, authorized_root=directory, max_bytes=64,
                                       trusted_boundary=os.fspath(root)) as snapshot:
                if not snapshot.exists or snapshot.contents != b"started":
                    raise RuntimeError("marker verification failed")
                _windows_marker_parent_check(snapshot, marker, created)
                _windows_role_evidence(root, config, None, snapshot, held_control=control_lock)
        yield holder
    finally:
        if holder is not None and holder.cleanup_ok:
            try:
                _cleanup_windows_marker(directory, created, root)
            except Exception:
                print("Warning: private reload marker cleanup unconfirmed.", file=sys.stderr)
        elif holder is not None:
            print("Warning: private reload marker retained; outcome or cleanup unconfirmed.", file=sys.stderr)


@contextlib.contextmanager
def _ordinary_marker_scope(root=ROOT, config=None):
    from core.discovery import _verify_directory, verify_record_directory
    state = Path(root) / '.runtime'
    verify_record_directory(state)
    temporary = state / 'tmp'
    try:
        temporary.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _verify_directory(temporary, private=True)
    with tempfile.TemporaryDirectory(prefix='model-proxy-cli-', dir=temporary) as temp:
        marker = Path(temp) / 'reload'
        marker.write_text('started', encoding='utf-8')
        yield SimpleNamespace(path=str(marker), cleanup_ok=True)


def _read_windows_marker(marker, root=ROOT, *, directory_proof):
    from core import windows_secure_files as secure
    with secure.capture_target(marker, authorized_root=ntpath.dirname(marker), max_bytes=64,
                               trusted_boundary=os.fspath(root)) as snapshot:
        if not snapshot.exists:
            raise ValueError("marker unavailable")
        _windows_marker_parent_check(snapshot, marker, directory_proof)
        value = snapshot.contents.decode("ascii")
        snapshot.revalidate()
        return value


def run_helper(command, args, config, control, *, root=ROOT):
    if not os.path.isfile(config):
        print(f'Error: config not found: {config}', file=sys.stderr)
        return 1
    readonly = command in ('supply-list', 'route-list', 'strategy-list')
    if readonly:
        # A list cannot commit or request reload. Do not write a marker that
        # needs mutable runtime configuration merely to display a list.
        # Instead require the helper's existing explicit no-reload sentinel.
        env = os.environ.copy()
        env.pop('CONFIG_OPS_RELOAD_MARKER', None)
        env.pop('CONFIG_OPS_RELOAD_MARKER_FD', None)
        # The child writes to a pipe decoded as UTF-8 below. Do not inherit a
        # conflicting PYTHONIOENCODING on any platform.
        env['PYTHONIOENCODING'] = 'utf-8'
        helper = Path(root) / 'core' / 'cli' / 'helper_entry.py'
        try:
            result = subprocess.run([sys.executable, str(helper), command, str(config), *args],
                                    env=env, stdout=subprocess.PIPE)
        except OSError:
            print('Error: read-only helper result was not confirmed.', file=sys.stderr)
            return 1
        rc = result.returncode
        if rc < 0:
            rc = 128 - rc
        try:
            output = result.stdout.decode('utf-8')
        except (UnicodeError, AttributeError):
            print('Error: read-only helper result was not confirmed.', file=sys.stderr)
            return rc or 1
        # The protocol uses complete LF or CRLF lines, not splitlines()'s
        # wider set of text separators. A truncated final line is not proof.
        terminated = output.endswith('\n')
        line_end = len(output) - (2 if output.endswith('\r\n') else 1) if terminated else len(output)
        line_start = output.rfind('\n', 0, line_end) + 1
        last = output[line_start:line_end]
        valid = terminated and last == '__RELOAD__=no'
        # Earlier matching lines are user data and must be preserved exactly.
        if last.startswith('__RELOAD__='):
            output = output[:line_start]
        write_platform_text(output, sys.platform, sys.stdout)
        if rc:
            return rc
        if not valid:
            print('Error: read-only helper result was not confirmed.', file=sys.stderr)
            return 1
        return 0
    if sys.platform == 'darwin':
        return _run_mac_write_helper(command, args, config, control, root)
    marker_scope = _windows_marker_scope if os.name == 'nt' else _ordinary_marker_scope
    try:
        with marker_scope(root, config) as holder:
            marker = holder.path
            env = os.environ.copy()
            env.pop('CONFIG_OPS_RELOAD_MARKER_FD', None)
            env['CONFIG_OPS_RELOAD_MARKER'] = str(marker)
            try:
                helper = Path(root) / 'core' / 'cli' / 'helper_entry.py'
                result = subprocess.run([sys.executable, str(helper), command, str(config), *args], env=env)
            except OSError:
                print('Error: cannot run config helper', file=sys.stderr)
                return 1
            rc = result.returncode
            if rc < 0:
                rc = 128 - rc
            try:
                if os.name == 'nt':
                    text = _read_windows_marker(marker, root, directory_proof=holder.directory_proof)
                else:
                    with open(marker, encoding='utf-8') as stream:
                        text = stream.read()
            except Exception:
                text = ''
            # A failed/interrupted child cannot trigger reload; a lost
            # marker or a possible commit remains an unknown outcome.
            if rc:
                if text not in ('started', 'no'):
                    print('Error: helper failed; configuration may have been saved.', file=sys.stderr)
                return rc
            if text == 'yes':
                holder.cleanup_ok = True
                print('Configuration changed; attempting reload.', flush=True)
                try:
                    rc = control.reload()
                except Exception:
                    print('Configuration saved, but reload was not confirmed.', file=sys.stderr)
                    return 1
                if rc:
                    print('Configuration saved, but reload was not confirmed.', file=sys.stderr)
                return rc
            if text != 'no':
                print('Error: reload marker missing or invalid; configuration may have been saved.',
                      file=sys.stderr)
                return 1
            holder.cleanup_ok = True
            return 0
    except Exception as exc:
        from core.windows_secure_files import _write_exception_info
        _, release_unknown, _ = _write_exception_info(exc)
        if release_unknown:
            print('Error: marker or control-lock handle release is unknown.', file=sys.stderr)
        print('Error: reload marker unavailable; helper outcome and possible temporary remnants unknown.',
              file=sys.stderr)
        return 1


def _interactive_stdin(stream):
    if not stream.isatty():
        return False
    if sys.platform != 'win32':
        return True
    # Windows isatty() also accepts character devices such as NUL. Only a
    # console input handle can actually support the interactive menu.
    try:
        import ctypes
        import msvcrt
        handle = ctypes.c_void_p(msvcrt.get_osfhandle(stream.fileno()))
        mode = ctypes.c_ulong()
        return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    except (ImportError, AttributeError, OSError, ValueError):
        return False


def menu(kind, config, control, root=ROOT):
    def menu_print(text=''):
        if sys.platform == 'win32':
            write_platform_text(text + '\n', sys.platform, sys.stdout)
        else:
            print(text)

    def menu_input(prompt):
        if sys.platform == 'win32':
            write_platform_text(prompt, sys.platform, sys.stdout)
            sys.stdout.flush()
            return input()
        return input(prompt)

    rc = run_helper(kind + '-list', [], config, control, root=root)
    if rc:
        return rc
    if not _interactive_stdin(sys.stdin):
        reason = 'stdin 非 TTY 或非控制台' if sys.platform == 'win32' else 'stdin 非 TTY'
        write_platform_text(f'\n当前非交互环境（{reason}），不支持交互操作，退出。\n',
                            sys.platform, sys.stdout)
        return 0
    if kind == 'supply':
        menu_print('\n可选操作: a=新增 supply / e=编辑 supply / d=删除 supply / t=连通性测试+effort探测 / q=退出')
    elif kind == 'route':
        menu_print('\n可选操作: a=新增 route 家族 / e=编辑 route / d=删除 route / q=退出')
    else:
        menu_print('\n可选操作: a=新增 strategy 绑定 / e=编辑 strategy / d=删除 strategy / q=退出')
    try:
        while True:
            op = menu_input('\n操作: [a]dd / [e]dit / [d]el' + (' / [t]est' if kind == 'supply' else '') + ' / [q]uit: ')
            if op in ('q', ''):
                return 0
            actions = {'a': 'add', 'e': 'edit', 'd': 'del'}
            if kind == 'supply':
                actions['t'] = 'check'
            if op not in actions:
                menu_print('未知操作')
            else:
                args = []
                if op in ('e', 'd', 't'):
                    action = {'e':'编辑', 'd':'删除', 't':'测试'}[op]
                    value = menu_input(f'要{action}的 {kind} {"token" if kind == "strategy" else "id"}: ')
                    if not value:
                        menu_print(f'用法: {kind} {actions[op]} <{"token" if kind == "strategy" else "id"}>')
                        continue
                    args.append(value)
                # "check" may persist accepted effort capabilities as well.
                # Recheck on every action, not only on entry to a long menu.
                if not write_preflight(root, config, operation=kind + '-' + actions[op]):
                    return 1
                rc = run_helper(kind + '-' + actions[op], args, config, control, root=root)
                if rc:
                    return rc
            menu_print()
            rc = run_helper(kind + '-list', [], config, control, root=root)
            if rc:
                return rc
    except (KeyboardInterrupt, EOFError):
        menu_print('\n已取消。')
        return 130


def logs(path, args):
    n = 30
    field, value = '', ''
    for arg in args:
        if any(c in arg for c in '\n\r\0'):
            print('Error: unsupported log filter (newline or NUL).', file=sys.stderr)
            return 1
        m = re.fullmatch(r'([a-z]+)=(.+)', arg)
        if m:
            field, value = m.groups()
        elif re.fullmatch(r'[0-9]+', arg):
            n = arg
    if field and field not in ('req', 'level', 'event'):
        print(f'Unknown filter field: {field} (supported: req/level/event)')
        return 1
    try:
        n = int(n)
    except ValueError:
        print('Error: invalid log count.', file=sys.stderr)
        return 1
    pattern = {'': ' ACCESS ', 'req': f'req_id={value}', 'level': f' {value} ', 'event': value}[field]
    # The old shell passed the pattern without -e/--, so a leading dash was
    # parsed as a grep option instead of a reliable expression. Fail closed.
    if pattern.startswith('-'):
        print('Error: unsupported log filter starting with dash.', file=sys.stderr)
        return 1
    # grep selects the first nonempty locale variable, defaulting to C.
    # C/POSIX regexes operate on bytes; UTF-8 locales operate on characters.
    locale_name = (os.environ.get('LC_ALL') or os.environ.get('LC_CTYPE')
                   or os.environ.get('LANG') or 'C')
    byte_mode = locale_name.upper() in ('C', 'POSIX')
    if not byte_mode and 'utf8' not in locale_name.lower().replace('-', '').replace('_', ''):
        print('Error: unsupported log filter locale (use C or UTF-8).', file=sys.stderr)
        return 1
    try:
        matcher = compile_bre(pattern, byte_mode=byte_mode)
    except BREError as exc:
        print(f'Error: invalid or unsupported BRE log filter: {exc}.', file=sys.stderr)
        return 1
    try:
        with Path(path).open(encoding='utf-8', newline='\n') as stream:
            def matching_lines():
                for line in stream:
                    if '\0' in line:
                        raise ValueError('binary log')
                    # grep matches without the line terminator, and adds one
                    # for a final line that was not newline-terminated.
                    content = line[:-1] if line.endswith('\n') else line
                    subject = content.encode('utf-8') if byte_mode else content
                    if matcher.search(subject):
                        yield content + '\n'
            lines = deque(matching_lines(), maxlen=n)
        print(''.join(lines), end='')
        return 0
    except (OSError, UnicodeError, ValueError, OverflowError):
        print('Error: log unavailable, non-UTF-8/binary, or invalid count.', file=sys.stderr)
        return 1


def main(argv=None, *, control=None, root=ROOT):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else ''
    args = argv[1:]
    if command in ('', '-h', '--help'):
        write_help(sys.platform, sys.stdout)
        return 0
    known = {'on', 'off', 'restart', 'status', 'reload', 'logs', 'stats', 'supply', 'route', 'strategy', 'switch', 'install'}
    if command not in known:
        print(f'Unknown command: {command}. Use --help.')
        return 1
    if control is None:
        if sys.platform == 'darwin':
            try:
                from core.cli.authority import MacInstallationAuthority
                from core.cli.control import MacControl
                control = MacControl(root, authority=MacInstallationAuthority(),
                                     config_path=config_path(root))
            except (ImportError, OSError, RuntimeError, ValueError):
                control = UnavailableControl()
        elif sys.platform == 'win32' and command in (
                'on', 'off', 'restart', 'status', 'reload',
                'supply', 'route', 'strategy', 'switch'):
            try:
                from core.cli.windows_control import WindowsControl
                raw_config = (os.environ.get('MODEL_PROXY_CONFIG') or
                              ntpath.join(os.fspath(root), 'config', 'model_proxy_config.json'))
                # Match the service's caller-cwd semantics for relative
                # overrides; never silently substitute the default config.
                control = WindowsControl(root, config_path=ntpath.abspath(raw_config))
            except (ImportError, OSError, RuntimeError, ValueError):
                control = UnavailableControl()
        else:
            control = UnavailableControl()
    if command in ('on', 'off', 'restart', 'status', 'reload'):
        return getattr(control, command)()
    if command == 'install':
        cfg = config_path(root)
        if not write_preflight(root, cfg, operation='install'):
            return 1
        from core.listen_config import resolve_listen, ListenConfigError
        from core.cli import _install_ops
        try:
            port = resolve_listen(cfg, os.environ.get('MODEL_PROXY_PORT'))['port']
            return _install_ops.cmd_install(str(cfg), str(port), [], preflight_root=root)
        except (KeyboardInterrupt, EOFError):
            print('\n已取消。')
            return 130
        except (ListenConfigError, OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError):
            print('Error: listen 配置不可用。', file=sys.stderr)
            return 1
    if command in ('supply', 'route', 'strategy'):
        return menu(command, config_path(root), control, root)
    if command == 'switch':
        if len(args) < 2 or not args[0] or not args[1]:
            print('用法: switch <client_token> <route_id>')
            return 1
        cfg = config_path(root)
        if not write_preflight(root, cfg, operation='switch'):
            return 1
        return run_helper('switch', args[:2], cfg, control, root=root)
    try:
        paths = runtime_paths(root)
        if command == 'logs':
            return logs(paths['log'], args)
        totals = Path(paths['totals'])
        if not totals.is_file():
            print(f'no stats yet (账本文件不存在: {totals})')
            return 0
        stats.main([str(totals), *args])
        return 0
    except (RuntimePathsError, PreflightError) as exc:
        print(f'Error: runtime paths unavailable ({exc.code}).', file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print('Error: cannot read runtime data.', file=sys.stderr)
        return 1
