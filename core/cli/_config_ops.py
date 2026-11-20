#!/usr/bin/env python3
"""model_proxy 配置 CRUD + effort 探测 helper。

由 model_proxy_cli.sh 调用，不直接面向用户。所有交互式提示（input()）、字段校验、
安全写入入口都收敛在本文件，避免 CLI 脚本里重复
heredoc 样板。

调用约定：
    python3 _config_ops.py <subcommand> <config_file> [args...]

每次操作结束打印一行 `__RELOAD__=yes` 或 `__RELOAD__=no`（供 model_proxy_cli.sh
解析决定是否要调用 reload_proxy），退出码 0=成功 / 1=失败或用户取消。
"""

import json
import ntpath
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from typing import NamedTuple

from core.reasoning.registry import resolve_protocol
from core.server import (
    validate_supply_ext,
    inject_system_block,
    resolve_appkey,
    _extract_body_error_text,
    match_body_error,
)
from core.cli._format_ops import (
    mask_appkey as _mask_appkey,
    strategy_route_desc as _strategy_route_desc,
    format_supplies as _format_supplies,
    format_routes as _format_routes,
    format_strategies as _format_strategies,
)

PROBE_VALUE = "__probe_invalid__"
VALID_PROTOCOLS = ("anthropic", "chat", "responses")
VALID_FAILOVER = ("on", "off")
TIER_NAMES = ("opus", "sonnet", "haiku")
_WRITE_BASELINE = None
_WRITE_STARTED = False
_WRITE_OPERATION = None
_WRITE_AUTHORIZATION = None
_WRITE_PREFLIGHT = None
_WRITE_INPUT_PATH = None
_WRITE_COMMITTED = None
_WRITE_LOCATIONS = None
_WRITE_BACKUP_BASELINE = None
_MARKER_BASELINE = None
_MARKER_CONTENT = None
_MARKER_REQUIRED = False
ANONYMOUS_MARKER_FD_PROTOCOL = 2
ANONYMOUS_MARKER_FD_PRECHECK = 1


class _WindowsLocations(NamedTuple):
    stage: str
    backup: str
    role_baseline: object
    path_baseline: tuple
    directory_baseline: tuple


# ---------------------------------------------------------------------------
# 配置文件紧凑格式：根字段逐行，根数组元素逐行，内部值紧凑
# ---------------------------------------------------------------------------

def compact_config_json(obj):
    """序列化配置对象，不修改值或输入对象；不用于请求体。"""
    if not isinstance(obj, dict):
        return json.dumps(obj, indent=2, ensure_ascii=False)
    if not obj:
        return "{}"

    def compact(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ": "))

    fields = []
    for key, value in obj.items():
        prefix = "  " + json.dumps(key, ensure_ascii=False) + ": "
        if isinstance(value, list) and value:
            elements = ",\n".join("    " + compact(element) for element in value)
            fields.append(prefix + "[\n" + elements + "\n  ]")
        else:
            fields.append(prefix + compact(value))
    return "{\n" + ",\n".join(fields) + "\n}"


# ---------------------------------------------------------------------------
# 基础：读/写
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    # All mutating operations edit exactly the bytes captured before prompts.
    if _WRITE_BASELINE is not None and path == _WRITE_INPUT_PATH:
        if os.name == "nt":
            _WRITE_BASELINE.revalidate()
            return json.loads(_WRITE_BASELINE.contents.decode("utf-8"))
        from core.mac_config_path import verify_config
        verify_config(_WRITE_BASELINE)
        return json.loads(_WRITE_BASELINE.data.decode("utf-8"))
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _mac_runtime_locations(root, snapshot, preflight, *, allow_target_staging=False):
    """Authorize private fixed namespaces and, if approved, a cross-volume exception.

    The dispatch preflight checks config, its original-path sidecar, the paths
    file and every runtime/fixed role against both directory roles. Hence no
    protected role may be in either entire random-name namespace. Repeat it
    before and after preparation and at each writer boundary. Target-side
    staging is only a volume exception; the platform writer still requires the
    volumes to differ and the caller to have confirmed this exact target.
    """
    from pathlib import Path
    from core.discovery import verify_record_directory
    from core.secure_files import RuntimeWriteLocations, capture_private_directory, _close_private_fd

    root = Path(root)
    state = root / '.runtime'
    if preflight(snapshot) is not True:
        raise WritePreflightFailed('运行目录创建前角色核验失败')
    verify_record_directory(state)
    stage = capture_private_directory(state / 'tmp', purpose='staging',
                                      business_xattr_compat=True)
    fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        entry = os.stat(state, follow_symlinks=False)
        if (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid) != (
                entry.st_dev, entry.st_ino, entry.st_mode, entry.st_uid, entry.st_gid):
            raise WritePreflightFailed('运行目录身份已改变')
        try:
            os.stat('backups', dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            # Exclusive creation. A concurrently appearing entry is not adopted.
            os.mkdir('backups', 0o700, dir_fd=fd)
        verify_record_directory(state)
    finally:
        _close_private_fd(fd)
    backup = capture_private_directory(state / 'backups', purpose='backup',
                                       business_xattr_compat=True)
    if stage.protection[:2] == backup.protection[:2]:
        raise WritePreflightFailed('备份和暂存目录不独立')
    if preflight(snapshot) is not True:
        raise WritePreflightFailed('运行目录创建后角色核验失败')
    return RuntimeWriteLocations(stage, backup, bool(allow_target_staging))


def _windows_role_evidence(root, config, operation, marker, artifacts=(), targets=(), retained=(),
                           held_control=None):
    """Observe all roles together; no callback here grants target authority."""
    from core import discovery, windows_paths
    from core.cli import dispatch

    root, config = os.fspath(root), ntpath.abspath(os.fspath(config))
    sidecar = ntpath.join(ntpath.dirname(config), 'session_overrides.json')
    paths_file = ntpath.join(root, 'config', 'runtime_paths.json')
    if not dispatch.write_preflight(root, config, [sidecar, *targets], operation=operation):
        raise WritePreflightFailed('配置或 sidecar 角色预检失败')
    paths = dispatch.runtime_paths(root)
    summary, roles = discovery._windows_open_preflight(root, paths)
    held = list(roles.values())
    monitor = roles['__root__']._backend
    extras = []
    try:
        def verify_control():
            if held_control is None:
                return
            from core import platform_lock
            if not isinstance(held_control, platform_lock.HeldLock):
                raise WritePreflightFailed('控制锁身份不可验证')
            with platform_lock._fork_guard, held_control._state_lock:
                if (held_control.closed or held_control.role != 'control.lock'
                        or held_control.snapshot != summary
                        or not hasattr(held_control, '_windows_guard')):
                    raise WritePreflightFailed('控制锁身份不可验证')
                platform_lock._windows_verify_guard(held_control._windows_guard,
                                                    roles['control.lock'])
        verify_control()
        if marker is not None:
            marker.revalidate()
        for saved in retained:
            if not saved.exists:
                raise WritePreflightFailed('已验证备份不再存在')
            saved.revalidate()
        selected = [config, paths_file, sidecar]
        if marker is not None:
            selected.append(marker.path)
        selected.extend(targets)
        selected.extend(saved.path for saved in retained)
        selected.extend(artifacts)
        for path in selected:
            boundary = discovery._windows_trusted_boundary(root, path)
            lease = windows_paths.inspect(path, backend=monitor, kind='file',
                                          private=True, trusted_boundary=boundary)
            held.append(lease)
            if not lease.evidence.direct_parent_observed:
                raise WritePreflightFailed('文件直接父目录未验证')
            extras.append(lease)
        for lease in held:
            lease.revalidate()
        verify_control()
        protected = [lease for name, lease in roles.items()
                     if name not in ('__root__', 'state_dir', 'tmp_dir', 'backups_dir')]
        for index, left in enumerate(extras):
            for right in protected + extras[index + 1:]:
                if (discovery._windows_evidence_contains(left.evidence, right.evidence)
                        or discovery._windows_evidence_contains(right.evidence, left.evidence)):
                    raise WritePreflightFailed('写入产物或受保护角色发生碰撞')
        for lease in held:
            lease.revalidate()
        verify_control()
        if marker is not None:
            marker.revalidate()
        for saved in retained:
            saved.revalidate()
        if monitor.release_unknown:
            raise discovery.PreflightError('release_unknown', 'Path evidence release unknown')
        return (summary, tuple(lease.evidence for lease in extras[:len(selected) - len(artifacts)
                                                                  - len(retained)]),
                (roles['tmp_dir'].evidence, roles['backups_dir'].evidence))
    finally:
        discovery._windows_close_leases(held)


def _windows_runtime_locations(root, config, operation, marker, targets=(), *, held_control=None):
    """Prepare only fixed private directories, after a complete role scan."""
    from core import discovery, platform_windows
    from core.cli import dispatch

    root = os.fspath(root)
    _windows_role_evidence(root, config, operation, marker, targets=targets, held_control=held_control)
    paths = dispatch.runtime_paths(root)
    discovery.prepare_windows_runtime_directories(root, paths, os.environ.get('LOCALAPPDATA'))
    before = _windows_role_evidence(root, config, operation, marker, targets=targets,
                                    held_control=held_control)
    stage = ntpath.join(root, '.runtime', 'tmp')
    backup = ntpath.join(root, '.runtime', 'backups')
    after = before
    for directory in (stage, backup):
        proof = platform_windows.ensure_private_directory(directory, trusted_boundary=root)
        dispatch._verified_windows_marker_directory(proof, require_new=False)
        after = _windows_role_evidence(root, config, operation, marker, targets=targets,
                                       held_control=held_control)
        if after[:2] != before[:2]:
            raise WritePreflightFailed('运行目录创建期间角色发生变化')
    if not all(item.exists for item in after[2]):
        raise WritePreflightFailed('私有暂存或备份目录不存在')
    return _WindowsLocations(stage, backup, after[0], after[1], after[2])


def _windows_write_options(root, config, operation, snapshot, marker, locations, *,
                           make_backup, targets=(), retained=(), held_control=None,
                           marker_check=None, config_may_change=True,
                           allow_target_staging=False):
    """Bind each actual reserved candidate to the whole held role set."""
    if not isinstance(locations, _WindowsLocations):
        raise WritePreflightFailed('运行目录身份证据缺失')
    stage, backup = locations.stage, locations.backup
    target_parent_stage = ntpath.dirname(snapshot.path)
    effective_stage = stage
    seen = {}

    def probe_cross_volume():
        """Return True/False only from bound evidence; None means unverified."""
        from core import windows_paths
        try:
            stage_lease = windows_paths.inspect(stage, backend=snapshot._monitor,
                                                kind='directory', private=True,
                                                trusted_boundary=os.fspath(root))
        except BaseException:
            return None
        try:
            stage_lease.revalidate()
            if stage_lease.evidence != locations.directory_baseline[0]:
                return None
            source = stage_lease._handles[-1].volume
            parent = snapshot._target_lease._handles[-2 if snapshot.exists else -1].volume
            if not source or not parent:
                return None
            return source != parent
        except BaseException:
            return None
        finally:
            try:
                stage_lease.close()
            except BaseException:
                pass

    def verify_same_volume():
        """Return cross-volume state; the external exception uses target parent."""
        nonlocal effective_stage
        from core import windows_paths
        lease = windows_paths.inspect(stage, backend=snapshot._monitor, kind='directory',
                                      private=True, trusted_boundary=os.fspath(root))
        try:
            lease.revalidate()
            snapshot.revalidate()
            if lease.evidence != locations.directory_baseline[0]:
                raise WritePreflightFailed('暂存目录身份已改变')
            source_volume = lease._handles[-1].volume
            parent_handle = snapshot._target_lease._handles[-2 if snapshot.exists else -1]
            target_volume = parent_handle.volume
            if not source_volume or not target_volume:
                raise WritePreflightFailed('卷身份无法验证；目标未写入')
            cross = source_volume != target_volume
            if cross and not allow_target_staging:
                raise WritePreflightFailed('跨卷暂存未获授权；目标未写入')
            if cross:
                effective_stage = target_parent_stage
            lease.revalidate()
            snapshot.revalidate()
            return cross
        finally:
            lease.close()

    def preflight():
        snapshot.revalidate()
        if marker is not None:
            if marker_check is None:
                _verify_marker_binding()
            else:
                marker_check()
        observed = _windows_role_evidence(root, config, operation, marker, tuple(seen.values()),
                                          targets, retained, held_control)
        baseline = locations.path_baseline
        current = observed[1]
        marker_write = config_may_change and marker is not None and snapshot.path == marker.path
        if (observed[0] != locations.role_baseline
                or observed[2] != locations.directory_baseline
                or len(current) != len(baseline)
                or any(left != right for index, (left, right) in enumerate(zip(current, baseline))
                       if not (marker_write and index == 0))):
            raise WritePreflightFailed('受保护角色身份已改变')
        verify_same_volume()
        snapshot.revalidate()
        return True

    def artifact_preflight(role, path):
        if role not in ('stage', 'backup') or (role == 'backup' and not make_backup):
            return False
        expected = effective_stage if role == 'stage' else backup
        if (not isinstance(path, str) or ntpath.dirname(path) != expected
                or ntpath.normpath(path) != path
                or (role in seen and seen[role] != path)):
            return False
        seen[role] = path
        return preflight()

    # Options construction performs no authorization and never raises for an
    # already-changed stage: that stays a writer failure. Only a positively
    # observed cross-volume case with explicit caller approval moves staging
    # to the target parent, so the backend binds the intended directory.
    if allow_target_staging is True and probe_cross_volume() is True:
        effective_stage = target_parent_stage
    return dict(preflight=preflight, stage_directory=effective_stage,
                stage_at_target_parent=effective_stage == target_parent_stage,
                stage_boundary=(getattr(snapshot, '_trusted_boundary', None)
                                if effective_stage == target_parent_stage else os.fspath(root)),
                backup_directory=backup if make_backup else None,
                backup_boundary=os.fspath(root) if make_backup else None,
                make_backup=make_backup, artifact_preflight=artifact_preflight)


def _windows_cross_volume(root, snapshot, locations):
    """Return True only when both real volumes are known and differ."""
    if not isinstance(locations, _WindowsLocations):
        raise WritePreflightFailed('运行目录身份证据缺失')
    from core import windows_paths
    # Hold the stage and target-parent leases together; comparing a volume
    # after closing its lease would not bind it to the observed object.
    stage_lease = windows_paths.inspect(locations.stage, backend=snapshot._monitor,
                                        kind='directory', private=True,
                                        trusted_boundary=os.fspath(root))
    parent_path = ntpath.dirname(snapshot.path)
    parent_lease = None
    try:
        stage_lease.revalidate()
        if stage_lease.evidence != locations.directory_baseline[0]:
            raise WritePreflightFailed('暂存目录身份已改变')
        from core.discovery import _windows_trusted_boundary
        # The target's own trusted boundary already names its direct parent;
        # asking for the parent's parent yields the drive root for D:\foo,
        # which the Windows path parser correctly refuses as a boundary.
        parent_lease = windows_paths.inspect(
            parent_path, backend=snapshot._monitor, kind='directory', private=False,
            trusted_boundary=_windows_trusted_boundary(root, snapshot.path))
        snapshot.revalidate()
        stage_lease.revalidate()
        parent_lease.revalidate()
        source = stage_lease._handles[-1].volume
        target_parent = parent_lease._handles[-1].volume
        snapshot_parent = snapshot._target_lease._handles[-2 if snapshot.exists else -1].volume
        if not source or not target_parent or target_parent != snapshot_parent:
            raise WritePreflightFailed('卷身份无法验证；目标未写入')
        return source != target_parent
    finally:
        if parent_lease is not None:
            parent_lease.close()
        stage_lease.close()


def _prepare_write(path: str, operation: str) -> None:
    """Capture before editing; dispatch must supply an independent write grant.

    Integration contract: prepare_config_write(ROOT, snapshot, operation=...)
    returns (WriteAuthorization, recheck). recheck(snapshot) must return True
    only after rechecking the exact resolved target against runtime paths,
    sidecars and fixed state, including retained preflight evidence. Merely
    resolving the selected config symlink is not a grant.
    """
    global _WRITE_BASELINE, _WRITE_AUTHORIZATION, _WRITE_PREFLIGHT, _WRITE_INPUT_PATH, _WRITE_LOCATIONS
    if os.name == "nt":
        from core import windows_secure_files as secure
        from core.cli import dispatch
        from core.discovery import _windows_trusted_boundary
        absolute = os.path.abspath(path)
        snapshot = None
        try:
            if _MARKER_REQUIRED:
                _verify_marker_binding()
            snapshot = secure.capture_target(absolute, authorized_root=os.path.dirname(absolute),
                                             max_bytes=16 * 1024 * 1024,
                                             trusted_boundary=_windows_trusted_boundary(dispatch.ROOT, absolute))
            if not snapshot.exists:
                raise WritePreflightFailed("配置文件不存在")
            def preflight():
                if _MARKER_REQUIRED:
                    _verify_marker_binding()
                return dispatch.write_preflight(dispatch.ROOT, absolute, operation=operation)
            if preflight() is not True:
                raise WritePreflightFailed("实际目标冲突预检未通过")
            snapshot.revalidate()
            locations = _windows_runtime_locations(dispatch.ROOT, absolute, operation,
                                                   _MARKER_BASELINE if _MARKER_REQUIRED else None)
            if preflight() is not True:
                raise WritePreflightFailed("运行目录准备后配置预检未通过")
            _WRITE_BASELINE, _WRITE_AUTHORIZATION = snapshot, True
            _WRITE_PREFLIGHT, _WRITE_INPUT_PATH = preflight, path
            _WRITE_LOCATIONS = locations
            snapshot = None
        finally:
            if snapshot is not None:
                snapshot.close()
        return
    if _MARKER_REQUIRED:
        _verify_marker_binding()
    from core.mac_config_path import capture_config, verify_config
    from core.cli import dispatch
    prepare = getattr(dispatch, "prepare_config_write", None)
    if not callable(prepare):
        raise WritePreflightFailed("实际目标授权及写前冲突预检接口未接通")
    snapshot = capture_config(path)
    if _MARKER_REQUIRED:
        _verify_marker_binding()
    # The dispatch normally prepares these before launching the helper.
    # Repeat its read-only-first protocol for an authorized direct helper.
    initial = dispatch._config_write_evidence(dispatch.ROOT, snapshot, operation)
    dispatch._prepare_mac_config_runtime(dispatch.ROOT, snapshot, operation, initial)
    authorization, preflight = prepare(dispatch.ROOT, snapshot, operation=operation)
    from core.mac_config_writer import WriteAuthorization, validate_write_authorization
    if not isinstance(authorization, WriteAuthorization) or not callable(preflight):
        raise WritePreflightFailed("实际目标授权或预检证据不完整")
    validate_write_authorization(snapshot, authorization)
    verify_config(snapshot)
    if preflight(snapshot) is not True:
        raise WritePreflightFailed("实际目标冲突预检未通过")
    locations = _mac_runtime_locations(
        dispatch.ROOT, snapshot, preflight,
        allow_target_staging=bool(getattr(authorization, 'external_confirmed', False)))
    validate_write_authorization(snapshot, authorization, locations=locations)
    _WRITE_BASELINE, _WRITE_AUTHORIZATION = snapshot, authorization
    _WRITE_PREFLIGHT, _WRITE_INPUT_PATH = preflight, path
    _WRITE_LOCATIONS = locations


def atomic_write(path: str, cfg: dict) -> None:
    global _WRITE_STARTED, _WRITE_BASELINE, _WRITE_COMMITTED, _WRITE_BACKUP_BASELINE
    if os.name == "nt":
        from core import windows_secure_files as secure
        from core.cli.dispatch import validate_editable_config
        if (_WRITE_BASELINE is None or path != _WRITE_INPUT_PATH
                or not callable(_WRITE_PREFLIGHT) or _WRITE_LOCATIONS is None):
            raise WritePreflightFailed("缺少编辑前快照和明确写入授权")
        validate_editable_config(cfg, _WRITE_OPERATION)
        payload = (compact_config_json(cfg) + "\n").encode("utf-8")
        print("配置将被更新（字段值隐藏）。请暂停其他进程编辑；最后检查与替换不是原子比较交换。")
        if not confirm("确认保存本次配置更改?"):
            done(False)
            raise WritePreflightFailed("用户取消")
        from core.cli import dispatch
        # Only an explicitly confirmed external target may use the narrow
        # target-parent staging exception, and only if the real volumes differ.
        cross_volume = _windows_cross_volume(dispatch.ROOT, _WRITE_BASELINE, _WRITE_LOCATIONS)
        if cross_volume:
            print("配置目标与项目暂存目录跨卷；本次仅在该目标父目录创建私有暂存，"
                  "备份仍写入项目 .runtime/backups。")
            if not confirm("确认在该目标父目录创建本次暂存?"):
                done(False)
                raise WritePreflightFailed("跨卷暂存未获授权；目标未写入")
        options = _windows_write_options(dispatch.ROOT, os.path.abspath(path),
                                         _WRITE_OPERATION, _WRITE_BASELINE,
                                         _MARKER_BASELINE if _MARKER_REQUIRED else None,
                                         _WRITE_LOCATIONS, make_backup=True,
                                         allow_target_staging=cross_volume)
        # Reject a known cross-volume case before even creating the backup.
        # The writer repeats this real check immediately before operations.
        options['preflight']()
        _WRITE_STARTED = True
        old_bytes = _WRITE_BASELINE.contents
        outcome = secure.write_target(_WRITE_BASELINE, payload, **options)
        if outcome.state != "VERIFIED":
            raise WritePreflightFailed("配置保存结果未验证")
        _WRITE_COMMITTED = outcome
        _WRITE_BASELINE = None
        backup_snapshot = None
        try:
            if (not outcome.backup or outcome.backup_status != 'VERIFIED'
                    or ntpath.dirname(outcome.backup) != _WRITE_LOCATIONS[1]):
                raise ValueError('backup unavailable')
            backup_snapshot = secure.capture_target(
                outcome.backup, authorized_root=_WRITE_LOCATIONS[1],
                max_bytes=16 * 1024 * 1024, trusted_boundary=os.fspath(dispatch.ROOT))
            if not backup_snapshot.exists or backup_snapshot.contents != old_bytes:
                raise ValueError('backup changed')
            backup_snapshot.revalidate()
        except BaseException as exc:
            _, release_unknown, _ = secure._write_exception_info(exc)
            if backup_snapshot is not None:
                try:
                    backup_snapshot.close()
                except BaseException:
                    release_unknown = True
            raise secure.SecureWriteError('backup_recheck_unverified', state='UNKNOWN',
                                          backup=outcome.backup, backup_status='UNKNOWN',
                                          release_unknown=release_unknown,
                                          target_was_verified=True) from None
        _WRITE_BACKUP_BASELINE = backup_snapshot
        print("安全备份:", outcome.backup)
        # The Windows parent acts on this only after a successful helper exit.
        # Never publish yes until the target and backup have been verified.
        try:
            _set_marker("yes")
        finally:
            _WRITE_BACKUP_BASELINE = None
            try:
                backup_snapshot.close()
            except BaseException:
                raise secure.SecureWriteError('release_unknown', state='UNKNOWN',
                                              backup=outcome.backup, backup_status='UNKNOWN',
                                              release_unknown=True,
                                              target_was_verified=True) from None
        return
    if _WRITE_BASELINE is None or path != _WRITE_INPUT_PATH or _WRITE_LOCATIONS is None:
        raise WritePreflightFailed("缺少编辑前快照和明确写入授权")
    if _MARKER_REQUIRED:
        _verify_marker_binding()
    from core.cli.dispatch import validate_editable_config
    from core.mac_config_writer import replace_config, MacWriteError
    validate_editable_config(cfg, _WRITE_OPERATION)
    payload = (compact_config_json(cfg) + "\n").encode("utf-8")
    marker_started = False
    def before_replace():
        nonlocal marker_started
        global _WRITE_STARTED
        _set_marker("writing")
        marker_started = True
        _WRITE_STARTED = True
    def boundary_guard():
        if _MARKER_REQUIRED:
            _verify_marker_binding()
        from core.secure_files import capture_private_directory
        if (_WRITE_LOCATIONS is None or _WRITE_PREFLIGHT(_WRITE_BASELINE) is not True
                or capture_private_directory(_WRITE_LOCATIONS.staging.path, purpose='staging',
                                             business_xattr_compat=True)
                != _WRITE_LOCATIONS.staging
                or capture_private_directory(_WRITE_LOCATIONS.backup.path, purpose='backup',
                                             business_xattr_compat=True)
                != _WRITE_LOCATIONS.backup):
            raise WritePreflightFailed('写入角色或私有目录已改变')
    try:
        outcome = replace_config(_WRITE_BASELINE, payload, _WRITE_AUTHORIZATION,
                                 preflight=_WRITE_PREFLIGHT, before_replace=before_replace,
                                 boundary_guard=boundary_guard, locations=_WRITE_LOCATIONS)
    except MacWriteError as exc:
        if exc.code == 'extended_attributes_copy_failed':
            err("扩展属性复制失败；配置未提交，请检查文件系统能力和权限。")
        if exc.state == 'NOT_REPLACED' and marker_started:
            try:
                _set_marker("no")
                _WRITE_STARTED = False
            except OSError as marker_error:
                err("目标未替换，但 reload 标记状态未知。")
                raise MacWriteError(exc.code, state=exc.state,
                                    backup_name=exc.backup_name,
                                    staging_name=exc.staging_name,
                                    cleanup_unknown=exc.cleanup_unknown,
                                    close_unknown=(exc.close_unknown or
                                                   getattr(marker_error, 'close_unknown', False)),
                                    marker_unknown=True) from None
        raise
    if outcome.backup_name:
        print("安全备份:", json.dumps(outcome.backup_name, ensure_ascii=True))
    from core.mac_config_path import verify_config
    try:
        if outcome.state != 'VERIFIED' or outcome.marker != outcome.snapshot.marker:
            raise RuntimeError('writer outcome incomplete')
        _WRITE_COMMITTED = outcome.snapshot
        verify_config(_WRITE_COMMITTED)
        _set_marker("yes")
        verify_config(_WRITE_COMMITTED)
    except OSError as exc:
        err("配置已保存，但 reload 标记写入或复核失败；未触发 reload。")
        raise MacWriteError('marker_unverified_after_commit', state='POSTCHECK_FAILED',
                            backup_name=outcome.backup_name,
                            close_unknown=getattr(exc, 'close_unknown', False),
                            marker_unknown=getattr(exc, 'marker_unknown', True)) from None
    except (RuntimeError, AttributeError, TypeError):
        raise MacWriteError('postcommit_config_changed', state='POSTCHECK_FAILED',
                            backup_name=outcome.backup_name) from None
    _WRITE_BASELINE = None


class WritePreflightFailed(RuntimeError):
    pass


class MarkerSafetyError(OSError):
    """A marker whose path, identity or conflict checks cannot be proved."""
    def __init__(self, message, *, marker_unknown=False, close_unknown=False):
        super().__init__(message)
        self.marker_unknown = marker_unknown
        self.close_unknown = close_unknown


def _close_marker_fd(fd):
    # A failed close may already have released the descriptor; never retry.
    try:
        os.close(fd)
    except BaseException:
        raise MarkerSafetyError('reload marker descriptor close unknown',
                                close_unknown=True) from None


def _marker_fd_from_environment():
    """Parse only the v2 descriptor; legacy path variables are never accepted."""
    raw_fd = os.environ.get("CONFIG_OPS_RELOAD_MARKER_FD")
    if (os.name != 'posix' or sys.platform != 'darwin'
            or 'CONFIG_OPS_RELOAD_MARKER' in os.environ
            or not raw_fd or not raw_fd.isascii() or not raw_fd.isdecimal()
            or len(raw_fd) > 10):
        raise MarkerSafetyError('trusted reload marker pipe unavailable')
    fd = int(raw_fd)
    if fd < 3 or fd > 2147483647 or str(fd) != raw_fd:
        raise MarkerSafetyError('trusted reload marker pipe unavailable')
    return fd


def _precheck_inherited_marker():
    """Bind the inherited write pipe before any mutating helper work."""
    global _MARKER_BASELINE
    if os.name == "nt":
        from pathlib import Path
        from core import windows_secure_files as secure
        raw = os.environ.get("CONFIG_OPS_RELOAD_MARKER")
        if (not raw or not os.path.isabs(raw) or os.path.normpath(raw) != raw
                or "CONFIG_OPS_RELOAD_MARKER_FD" in os.environ or "\0" in raw):
            raise MarkerSafetyError("Windows reload marker unavailable")
        marker = os.path.abspath(raw)
        parent = os.path.dirname(marker)
        temporary = os.path.join(str(Path(__file__).resolve().parents[2]), ".runtime", "tmp")
        snap = None
        try:
            if (os.path.basename(marker) != "reload" or
                    not re.fullmatch(r"model-proxy-cli-[0-9a-f]{32}", os.path.basename(parent)) or
                    os.path.normcase(os.path.dirname(parent)) != os.path.normcase(temporary)):
                raise MarkerSafetyError("Windows reload marker location unverified")
            snap = secure.capture_target(marker, authorized_root=parent, max_bytes=64,
                                         trusted_boundary=str(Path(__file__).resolve().parents[2]))
            if not snap.exists or snap.contents != b"started":
                raise MarkerSafetyError("Windows reload marker changed")
            snap.revalidate()
            _MARKER_BASELINE = snap
            snap = None
            return
        except MarkerSafetyError:
            raise
        except BaseException as exc:
            code = getattr(exc, "code", "unverified")
            raise MarkerSafetyError("Windows reload marker unverified (" + str(code) + ")") from None
        finally:
            if snap is not None:
                try:
                    snap.close()
                except BaseException:
                    raise MarkerSafetyError("Windows reload marker close unknown", close_unknown=True) from None
    from core.cli.dispatch import _pipe_endpoint_protection
    try:
        fd = _marker_fd_from_environment()
        first = _pipe_endpoint_protection(fd, write=True)
        # pass_fds makes the child FD inheritable. Prevent accidental reuse by
        # any subsequently exec'd process before doing anything else.
        os.set_inheritable(fd, False)
        if os.get_inheritable(fd) or _pipe_endpoint_protection(fd, write=True) != first:
            raise MarkerSafetyError('reload marker pipe changed')
        # On Darwin a zero-byte write verifies that a reader still exists; it
        # emits no frame and never changes a regular file (which is rejected).
        if os.write(fd, b'') != 0 or _pipe_endpoint_protection(fd, write=True) != first:
            raise MarkerSafetyError('reload marker pipe unavailable')
        _MARKER_BASELINE = (fd, first)
    except MarkerSafetyError:
        raise
    except (OSError, ValueError, TypeError, OverflowError):
        raise MarkerSafetyError('trusted reload marker pipe unavailable') from None


def _verify_marker_binding():
    """Recheck the prebound descriptor, CLOEXEC and live reader, read-only."""
    if os.name == "nt":
        try:
            snap = _MARKER_BASELINE
            if snap is None or _MARKER_CONTENT is not None:
                raise MarkerSafetyError("Windows reload marker unavailable")
            snap.revalidate()
            if snap.contents != b"started":
                raise MarkerSafetyError("Windows reload marker changed")
            return snap
        except MarkerSafetyError:
            raise
        except BaseException:
            raise MarkerSafetyError("Windows reload marker unverified") from None
    from core.cli.dispatch import _pipe_endpoint_protection
    try:
        fd = _marker_fd_from_environment()
        if (_MARKER_BASELINE is None or fd != _MARKER_BASELINE[0]
                or _pipe_endpoint_protection(fd, write=True) != _MARKER_BASELINE[1]
                or os.get_inheritable(fd)):
            raise MarkerSafetyError('reload marker pipe changed')
        if (os.write(fd, b'') != 0
                or _pipe_endpoint_protection(fd, write=True) != _MARKER_BASELINE[1]):
            raise MarkerSafetyError('reload marker pipe unavailable')
        return fd, _MARKER_BASELINE[1]
    except MarkerSafetyError:
        raise
    except (OSError, ValueError, TypeError, OverflowError):
        raise MarkerSafetyError('trusted reload marker pipe unavailable') from None


def _set_marker(text: str) -> None:
    """Send a bounded state frame over a verified inherited anonymous pipe.

    A pathname is never opened. The parent retains the read end and accepts
    reload only on a successful helper exit and a valid frame sequence.
    """
    global _MARKER_BASELINE, _MARKER_CONTENT
    if (not _MARKER_REQUIRED and 'CONFIG_OPS_RELOAD_MARKER' not in os.environ
            and 'CONFIG_OPS_RELOAD_MARKER_FD' not in os.environ):
        return
    if os.name == "nt":
        from core import windows_secure_files as secure
        if text not in ("yes", "no"):
            raise MarkerSafetyError("Windows reload marker sequence invalid")
        if _MARKER_CONTENT == text:
            return
        if text == "yes" and _WRITE_COMMITTED is None:
            raise MarkerSafetyError("Windows target commit not verified")
        snap = _verify_marker_binding()
        try:
            from core.cli import dispatch
            if (_WRITE_LOCATIONS is None or _WRITE_INPUT_PATH is None
                    or _WRITE_OPERATION is None):
                raise MarkerSafetyError("Windows marker role evidence unavailable")
            options = _windows_write_options(
                dispatch.ROOT, os.path.abspath(_WRITE_INPUT_PATH), _WRITE_OPERATION,
                snap, snap, _WRITE_LOCATIONS, make_backup=False,
                retained=(() if _WRITE_BACKUP_BASELINE is None else (_WRITE_BACKUP_BASELINE,)))
            outcome = secure.write_target(snap, text.encode("ascii"), **options)
            if outcome.state != "VERIFIED":
                raise MarkerSafetyError("Windows reload marker result unverified")
        except BaseException as exc:
            raise MarkerSafetyError("Windows reload marker write unconfirmed",
                                    marker_unknown=True,
                                    close_unknown=getattr(exc, "release_unknown", False)) from None
        _MARKER_CONTENT = text
        return
    if text not in ('writing', 'yes', 'no'):
        raise MarkerSafetyError('trusted reload marker pipe unavailable')
    from core.cli.dispatch import _pipe_endpoint_protection
    from core.mac_config_path import verify_config
    duplicate = None
    mutation_started = False
    try:
        marker_fd, proof = _verify_marker_binding()
        duplicate = os.dup(marker_fd)
        if os.get_inheritable(duplicate) or _pipe_endpoint_protection(duplicate, write=True) != proof:
            raise MarkerSafetyError('reload marker pipe changed')
        evidence = (marker_fd, proof)
        if _MARKER_BASELINE is not None and _MARKER_BASELINE != evidence:
            raise MarkerSafetyError('reload marker pipe changed')
        valid = {None: ('writing', 'no'), 'writing': ('yes', 'no'),
                 'yes': ('yes',), 'no': ()}
        if text not in valid.get(_MARKER_CONTENT, ()):
            raise MarkerSafetyError('reload marker sequence invalid')
        if text == 'writing':
            if _WRITE_BASELINE is None:
                raise MarkerSafetyError('reload marker config snapshot unavailable')
            verify_config(_WRITE_BASELINE)
        if text == 'yes':
            if _WRITE_COMMITTED is None:
                raise MarkerSafetyError('reload marker committed snapshot unavailable')
            verify_config(_WRITE_COMMITTED)
        if (_verify_marker_binding() != (marker_fd, proof)
                or _pipe_endpoint_protection(duplicate, write=True) != proof):
            raise MarkerSafetyError('reload marker pipe changed')
        frame = (text + '\n').encode('ascii')
        mutation_started = True
        if os.write(duplicate, frame) != len(frame):
            raise MarkerSafetyError('reload marker frame incomplete')
        if _pipe_endpoint_protection(duplicate, write=True) != proof:
            raise MarkerSafetyError('reload marker pipe changed')
        if text == 'yes':
            verify_config(_WRITE_COMMITTED)
        _MARKER_BASELINE, _MARKER_CONTENT = evidence, text
    except MarkerSafetyError as exc:
        exc.marker_unknown = exc.marker_unknown or mutation_started
        raise
    except BaseException:
        raise MarkerSafetyError('reload marker pipe verification failed',
                                marker_unknown=mutation_started) from None
    finally:
        if duplicate is not None:
            try:
                _close_marker_fd(duplicate)
            except MarkerSafetyError as exc:
                exc.marker_unknown = exc.marker_unknown or mutation_started
                raise


def err(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)


def done(reload_needed: bool) -> None:
    """写 reload 标记。

    优先写到 CONFIG_OPS_RELOAD_MARKER 指定的文件（model_proxy_cli.sh 用临时文件传递，
    避免用 stdout 传递导致 bash 端 command substitution 缓冲、破坏 input() 交互实时性）；
    未设置该环境变量时（如单独调试本脚本）回退打印到 stdout。
    """
    marker = os.environ.get("CONFIG_OPS_RELOAD_MARKER")
    marker_fd = os.environ.get("CONFIG_OPS_RELOAD_MARKER_FD")
    text = "yes" if reload_needed else "no"
    if _MARKER_REQUIRED or marker or marker_fd or 'CONFIG_OPS_RELOAD_MARKER' in os.environ:
        try:
            _set_marker(text)
        except OSError:
            if reload_needed or _WRITE_STARTED:
                err("配置可能已保存，但 reload 标记写入失败；未确认 reload。")
            raise
    else:
        print(f"__RELOAD__={text}")


def confirm(prompt: str) -> bool:
    ans = input(f"{prompt} [y/N]: ").strip().lower()
    return ans in ("y", "yes")


def prompt_source_capability(existing: "dict | None" = None) -> "dict | None":
    """交互式录入 source 侧 tiers_source_capability。
    对 opus/sonnet/haiku 逐 tier 询问；返回 {tier: {"effort_enum":[...]}} 或 None（全跳过）。
    - 输入逗号分隔列表 -> 对应 effort_enum
    - 输入 '-'        -> 空列表 []（该 tier 完全无思考能力）
    - 留空           -> add 场景：不写该 tier 键（运行时走 _DEFAULT_ENUM）；
                         edit 场景：保留 existing 里该 tier 原值（原本没有就仍不写）
    绝不把默认值物化写入。不做白名单清洗（人工把关，跟现有 run_probe_and_maybe_accept 原则一致）。
    """
    existing = existing or {}
    result = {}
    for tier in TIER_NAMES:
        cur = existing.get(tier)
        cur_desc = cur.get("effort_enum") if cur else "(未声明，走默认5档)"
        raw = input(f"  [{tier}] 支持的思考档位（逗号分隔；- 表示空列表[]；留空=保留/跳过）当前={cur_desc}: ").strip()
        if raw == "-":
            result[tier] = {"effort_enum": []}
        elif raw:
            result[tier] = {"effort_enum": [w.strip() for w in raw.split(",") if w.strip()]}
        elif cur is not None:
            result[tier] = cur
        # else: 不写该tier键
    return result or None


# ---------------------------------------------------------------------------
# effort 探测（迁自原 cmd_probe_effort，逻辑不变，只是从"独立命令只打印"
# 改成供 add/edit 内部调用+精确解析才回写）
# ---------------------------------------------------------------------------

def _fix_mojibake(raw: bytes) -> str:
    """尝试修复网关返回的UTF-8被当Latin-1重编码的双重编码乱码。
    仅在检测到mojibake特征时生效，否则原样按utf-8解码返回。"""
    plain = raw.decode("utf-8", errors="replace")
    try:
        fixed = raw.decode("utf-8", errors="ignore").encode("latin-1", errors="ignore").decode("utf-8", errors="ignore")
    except Exception:
        return plain
    # 简单的mojibake特征检测：修复后中文字符明显增多则采用修复版
    def _cjk_count(s):
        return sum(1 for ch in s if '一' <= ch <= '鿿')
    if _cjk_count(fixed) > _cjk_count(plain):
        return fixed
    return plain


# 多措辞正则并联匹配，命中即止（对 _fix_mojibake 后的文本跑，无mojibake时退化为plain文本）。
_ENUM_PATTERNS = [
    r"[Ss]upported values (?:are)?\s*[:：]?\s*(.+)",
    r"expected one of\s+(.+?)(?:\s+at line|$)",
    r"(?:可选值为|可选值|valid values?)\s*[：:]\s*(.+)",
]


def _extract_enum_candidates(text: str) -> "list[str] | None":
    """从命中的措辞捕获组文本里抽取候选档名。

    兼容反引号包裹、单/双引号包裹，以及顿号/逗号/，/空格分隔的裸词，
    合并去重保留原始顺序。不做 name_to_canonical 白名单清洗——按方案，
    保留候选原始形态（可能含噪音），人工审查是唯一把关环节。
    """
    tail = None
    for pat in _ENUM_PATTERNS:
        m = re.search(pat, text)
        if m:
            tail = m.group(1)
            break
    if tail is None:
        return None

    backticked = re.findall(r"`([^`]+)`", tail)
    quoted = re.findall(r"['\"]([^'\"]+)['\"]", tail)
    # 裸词兜底：去掉已被反引号/引号包裹的片段后，剩余按顿号/逗号/空格分隔
    bare_source = re.sub(r"`[^`]+`", " ", tail)
    bare_source = re.sub(r"['\"][^'\"]+['\"]", " ", bare_source)
    bare = [w for w in re.split(r"[、,，\s]+", re.split(r"[.。\n]", bare_source)[0]) if w.strip()]

    seen = set()
    cands = []
    for w in backticked + quoted + bare:
        w = w.strip().strip("、,，`'\"")
        if w and w not in seen:
            seen.add(w)
            cands.append(w)

    return cands or None


def _is_response_complete(text_fixed: str) -> bool:
    """判断探测响应是否完整（未被截断）。主判据JSON完整性，其余作为参考。"""
    try:
        json.loads(text_fixed)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def _verify_probe_snapshot():
    """Recheck config, authorization and peer immediately before probing."""
    if _MARKER_REQUIRED:
        _verify_marker_binding()
        if _WRITE_BASELINE is None:
            raise WritePreflightFailed("探测前配置快照不可用")
    if _WRITE_BASELINE is not None:
        if os.name == "nt":
            # Windows preflight is a zero-argument callback bound to the exact
            # selected target; it is not a Mac WriteAuthorization/snapshot.
            _WRITE_BASELINE.revalidate()
            if _MARKER_REQUIRED:
                if (_WRITE_AUTHORIZATION is not True or not callable(_WRITE_PREFLIGHT)
                        or _WRITE_PREFLIGHT() is not True):
                    raise WritePreflightFailed("探测前授权或冲突预检未通过")
                _WRITE_BASELINE.revalidate()
        else:
            from core.mac_config_path import verify_config
            verify_config(_WRITE_BASELINE)
            if _MARKER_REQUIRED:
                from core.mac_config_writer import validate_write_authorization
                validate_write_authorization(_WRITE_BASELINE, _WRITE_AUTHORIZATION)
                if not callable(_WRITE_PREFLIGHT) or _WRITE_PREFLIGHT(_WRITE_BASELINE) is not True:
                    raise WritePreflightFailed("探测前授权或冲突预检未通过")
    if _MARKER_REQUIRED:
        _verify_marker_binding()


def probe_effort(supply: dict) -> "tuple[int | None, str, list[str] | None, bool, Exception | None]":
    """向 supply 上游发一个已知非法的 effort 值，尝试解析真实支持的枚举。

    只发一次探测请求。返回
    (http_status_or_None, text_fixed, regex_candidates, is_complete, exc)。
    正常完成/HTTP 错误响应路径下 `exc` 为 None；网络层异常（DNS 失败/连接超时/连接拒绝等）
    时 `status` 为 None、`text_fixed` 为 `str(exc)`、`exc` 为捕获到的原始异常对象本身
    （供 classify_supply_reachability 按异常类型细分原因，而不是只能用文本猜）。
    """
    _verify_probe_snapshot()
    url = (supply.get("url") or "").rstrip("/")
    # appkey 直取 / appkey_file 每请求读取（与主链路 server.py resolve_appkey 同源）；
    # 解析失败走既有五元组契约（status=None、exc=ValueError），不发网络请求
    try:
        appkey = resolve_appkey(supply)
    except ValueError as e:
        return None, str(e), None, False, e
    model = supply.get("target_model", "")

    try:
        proto = resolve_protocol(supply)
    except ValueError as e:
        return None, str(e), None, False, e

    target = url  # url 现在语义是完整终态端点，零拼接

    if proto == "anthropic":
        body = {"model": model, "max_tokens": 16,
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": PROBE_VALUE},
                "messages": [{"role": "user", "content": "probe"}]}
    elif proto == "chat":
        body = {"model": model, "max_tokens": 16,
                "reasoning_effort": PROBE_VALUE,
                "messages": [{"role": "user", "content": "probe"}]}
    else:  # responses
        body = {"model": model, "max_output_tokens": 16,
                "reasoning": {"effort": PROBE_VALUE},
                "input": "probe"}

    # system_inject：仅 anthropic 协议注入（与主链路同款守卫）；
    # inject_system_block 原地修改 body dict，必须在 json.dumps 之前调用
    if proto == "anthropic" and supply.get("system_inject"):
        inject_system_block(body, supply["system_inject"])

    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {appkey}", "x-api-key": appkey}
    # extra_headers：supply 声明的出站 header，base headers 之后应用 = 最高优先；
    # 跳过 content-length（body 长度决定）——与主链路 count_tokens 路径同语义
    for k, v in (supply.get("extra_headers") or {}).items():
        if k.lower() == "content-length":
            continue
        headers[k] = v
    req = urllib.request.Request(target, data=data, headers=headers, method="POST")

    status = None
    raw_bytes = b""
    _verify_probe_snapshot()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            raw_bytes = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        raw_bytes = e.read()
    except Exception as e:
        return None, str(e), None, False, e

    text_fixed = _fix_mojibake(raw_bytes)
    regex_candidates = _extract_enum_candidates(text_fixed)
    is_complete = _is_response_complete(text_fixed)

    return status, text_fixed, regex_candidates, is_complete, None


class ReachabilityCategory:
    """supply 连通性分类常量（保持与项目现有 CanonicalEffort 等风格一致的简单命名空间）。"""
    DNS_ERROR = "dns_error"
    TIMEOUT = "timeout"
    CONN_REFUSED = "conn_refused"
    NETWORK_OTHER = "network_other"
    CONFIG_ERROR = "config_error"
    AUTH_ERROR = "auth_error"
    MODEL_ERROR = "model_error"
    REACHABLE = "reachable"
    BODY_ERROR = "body_error"   # 2xx/400-探测被拒 + 正文命中 body_error_rules（设计 2026-10-15）
    UNKNOWN = "unknown"


_MODEL_ERROR_KEYWORDS = ("model", "not found", "不存在", "无效模型", "invalid model")


def classify_supply_reachability(status: "int | None", text_fixed: str,
                                  exc: "Exception | None") -> "tuple[str, str]":
    """纯函数：不发请求，只依据 probe_effort 的返回结果判定连通性失败原因。

    返回 (category, description)，category 取值见 ReachabilityCategory，
    description 是给用户看的中文人类可读描述。
    """
    if status is None:
        # probe 路径产生 ValueError 的来源恰好且仅有两处——resolve_protocol（非法协议）
        # 与 resolve_appkey（appkey_file 解析失败），均为本地配置问题、请求未发出；
        # urlopen 网络异常家族（URLError/gaierror/timeout/OSError）与 ValueError 类型
        # 不相交，isinstance 判别可靠
        if isinstance(exc, ValueError):
            return ReachabilityCategory.CONFIG_ERROR, f"本地配置错误（探测请求未发出）: {exc}"
        # urllib.request.urlopen 真实抛出的网络层异常几乎总是 urllib.error.URLError，
        # 底层原因（DNS/超时/拒绝连接）包在 exc.reason 里，不是裸的 socket.gaierror/
        # ConnectionRefusedError 本身——所以 gaierror/ConnectionRefusedError 的 isinstance
        # 判断必须同时覆盖「exc 本身就是该类型」和「exc 是 URLError 且 .reason 是该类型」
        # 两种形态，否则 DNS_ERROR 分支在生产环境永远走不到（reviewer 发现的真实 bug）。
        reason = getattr(exc, "reason", None) if isinstance(exc, urllib.error.URLError) else None
        if isinstance(exc, socket.gaierror) or isinstance(reason, socket.gaierror):
            return ReachabilityCategory.DNS_ERROR, "DNS解析失败，请检查url域名是否正确"
        if isinstance(exc, (socket.timeout, TimeoutError)) or isinstance(reason, (socket.timeout, TimeoutError)):
            return ReachabilityCategory.TIMEOUT, "连接超时，请检查网络/代理设置或目标服务是否可达"
        if isinstance(exc, ConnectionRefusedError) or isinstance(reason, ConnectionRefusedError):
            return ReachabilityCategory.CONN_REFUSED, "连接被拒绝，请检查目标端口/服务是否已启动"
        if isinstance(exc, urllib.error.URLError):
            reason_str = str(reason if reason is not None else exc)
            if "timed out" in reason_str.lower():
                return ReachabilityCategory.TIMEOUT, "连接超时，请检查网络/代理设置或目标服务是否可达"
            if "connection refused" in reason_str.lower():
                return ReachabilityCategory.CONN_REFUSED, "连接被拒绝，请检查目标端口/服务是否已启动"
        return ReachabilityCategory.NETWORK_OTHER, f"网络层异常，无法归入已知类别: {exc}"

    if status in (401, 403):
        return ReachabilityCategory.AUTH_ERROR, f"HTTP {status}，appkey可能失效或无权限访问该模型"

    if status == 404:
        return ReachabilityCategory.MODEL_ERROR, "HTTP 404，target_model 或路径可能配置错误"

    if status == 400:
        low = text_fixed.lower()
        if any(kw.lower() in low for kw in _MODEL_ERROR_KEYWORDS):
            return ReachabilityCategory.MODEL_ERROR, "HTTP 400 且响应含模型相关关键词，target_model 可能配置错误"
        return ReachabilityCategory.REACHABLE, "连通鉴权正常（400 是探测用的非法 effort 参数被拒，符合预期）"

    if status is not None and 200 <= status <= 299:
        return ReachabilityCategory.REACHABLE, "连通鉴权正常"

    return ReachabilityCategory.UNKNOWN, f"HTTP {status}，未归入已知类别，请人工核查响应内容"


def _body_error_check(text_fixed: str, rules) -> "tuple[str | None, str]":
    """纯函数（不发请求）：对探测响应文本跑 body_error 识别，返回 (errorcode | None, extracted_text)。

    复用生产链路同一对函数（_extract_body_error_text + match_body_error），保证测试侧与
    failover 侧识别面完全一致。text_fixed 源自 raw bytes，encode 回 UTF-8 走 json.loads
    无损。rules 为空时 match_body_error 返回 None（fail-open）。
    """
    text, saw = _extract_body_error_text(text_fixed.encode("utf-8"))
    code = match_body_error(text, rules, saw_tool_or_thinking=saw, text_len=len(text))
    return code, text


def _apply_body_error_rules(status: "int | None", category: str, desc: str,
                            text_fixed: str, body_rules) -> "tuple[str, str, str | None]":
    """REACHABLE 分类后叠加 body_error_rules 正文识别，命中降级 BODY_ERROR。

    返回 (category, desc, body_error_text)：未命中 / 无 rules / 非 REACHABLE 分类时
    原样返回 (category, desc, None)——零行为变化（fail-open）。run_connectivity_test
    与 connectivity_test_then_probe 共用本函数，保证打印结论与探测 gating 同一真相源
    （classify_supply_reachability 保持纯 status/exc 归因，不掺 rules，见设计）。
    """
    if not body_rules or category != ReachabilityCategory.REACHABLE:
        return category, desc, None
    code, text = _body_error_check(text_fixed, body_rules)
    if code is None:
        return category, desc, None
    return (ReachabilityCategory.BODY_ERROR,
            f"HTTP {status} 但正文命中 body_error 规则（{code}），supply 实际不可用或受限",
            text)


def _print_connectivity_status(status: "int | None", text_fixed: str, category: str, desc: str,
                               body_error_text: "str | None" = None) -> None:
    """打印结构化输出的第①项：当前接口联通状态（成功/失败及原因 + HTTP status，
    失败时附上游原始响应/异常文本）。供 run_connectivity_test 和
    run_probe_and_maybe_accept（独立跑分支）共用，保证展示格式统一。"""
    if category == ReachabilityCategory.REACHABLE:
        print(f"- 当前接口联通状态：成功（{desc}）")
    else:
        print(f"- 当前接口联通状态：失败，{desc}")
    print(f"    HTTP status={status}")
    if category == ReachabilityCategory.BODY_ERROR and body_error_text is not None:
        # 仅异常展示：摘要取 _extract_body_error_text 提取后的 text（跳 JSON 噪声），
        # 截 100 字符、换行折叠为空格；正常 REACHABLE 不附正文（探测节已打印全文）
        print(f"    正文摘要：{body_error_text.replace(chr(10), ' ')[:100]}")
    if category != ReachabilityCategory.REACHABLE:
        print(f"    {text_fixed}")


def run_connectivity_test(supply: dict, body_rules=None) -> "tuple[int | None, str, list[str] | None, bool, Exception | None]":
    """独立的联通性测试：调用 probe_effort 并按分类打印结构化的"联通状态"一项。

    body_rules（顶层 body_error_rules）：REACHABLE 分类后叠加正文识别，命中降级
    BODY_ERROR 并附正文摘要行；无 rules 零行为变化（fail-open）。

    返回 probe_effort 的原始五元组，供调用方在 REACHABLE 场景下直接复用
    （通过 run_probe_and_maybe_accept 的 prefetched 参数），避免重复发请求。
    """
    _verify_probe_snapshot()
    print(f"正在测试 supply={supply.get('id')} protocol={supply.get('protocol')} "
          f"model={supply.get('target_model')} 连通性 ...")
    result = probe_effort(supply)
    status, text_fixed, regex_candidates, is_complete, exc = result
    category, desc = classify_supply_reachability(status, text_fixed, exc)
    category, desc, body_error_text = _apply_body_error_rules(
        status, category, desc, text_fixed, body_rules)
    _print_connectivity_status(status, text_fixed, category, desc,
                               body_error_text=body_error_text)
    return result


def run_probe_and_maybe_accept(supply: dict, interactive_prompt: bool = True,
                                 prefetched: "tuple | None" = None) -> dict | None:
    """执行探测，展示结果，询问用户是否接受；返回要写入的
    reasoning_capability dict 或 None（跳过/失败/不写入）。

    interactive_prompt=False 时（非交互批量场景预留），探测成功也不写入，只打印，
    避免在没有人工确认的路径上悄悄写库——当前 CLI 所有调用点都是交互式，此参数保留扩展位。

    prefetched 非 None 时（须是 probe_effort 格式的五元组），跳过 probe_effort 调用，
    直接复用传入的结果（并跳过"正在探测..."打印，因为调用方已在 run_connectivity_test
    里打印过测试过程），避免对同一 supply 重复发起真实上游请求。
    """
    _verify_probe_snapshot()
    if prefetched is not None:
        # 被 connectivity_test_then_probe 串联调用：run_connectivity_test 已经打印过
        # 结构化的「① 联通状态」一项，这里不再重复打印。
        status, text_fixed, regex_candidates, is_complete, exc = prefetched
    else:
        print(f"正在探测 supply={supply.get('id')} protocol={supply.get('protocol')} "
              f"model={supply.get('target_model')} ...")
        status, text_fixed, regex_candidates, is_complete, exc = probe_effort(supply)

    category, desc = classify_supply_reachability(status, text_fixed, exc)
    if prefetched is None:
        # 独立跑（无前置 run_connectivity_test）：补上「① 联通状态」一项，
        # 保证独立调用和串联调用看到同样结构化的输出。
        _print_connectivity_status(status, text_fixed, category, desc)

    a_success = bool(regex_candidates) and is_complete

    candidates = None
    source_desc = None

    if a_success:
        print(f"- 从接口返回探测到的思考程度档位：成功，候选档位={regex_candidates}（请核对，可能含噪音）")
        print(f"    {text_fixed}")
        candidates = regex_candidates
        source_desc = "网关探测"
    else:
        if category != ReachabilityCategory.REACHABLE:
            reason = f"连通性异常（{category}：{desc}），未能进行探测"
        elif regex_candidates and not is_complete:
            reason = f"响应疑似被截断，候选可能不全（不采纳，参考: {regex_candidates}）"
        else:
            reason = "探测正则不保证准确，供应商报错格式差异大，未提取到可信档位"
        print(f"- 从接口返回探测到的思考程度档位：失败，{reason}")
        print(f"    {text_fixed}")
        candidates = None
        source_desc = "探测无结论，请查官方文档后人工判断"

    if not interactive_prompt:
        print(f"（非交互模式，不自动写入，仅供参考，来源={source_desc}）")
        return None

    # candidates 为 None（探测无结论）时，仍进入人工输入环节——不能因为"探测不出"
    # 就直接放弃，人工可能依据外部信息（官方文档/已知架构结论）判断出这个 supply
    # 真实支持的档位（包括显式空集，表示确认不支持任何档位）。留空表示"跳过，不写入"，
    # 与 candidates 非 None 时"留空=沿用候选"的语义不同，此处沿用候选无意义（候选为空）。
    if candidates is not None:
        edited = input(
            f"- 请核对/编辑要写入的档位列表（来源=网关探测，逗号分隔；"
            f"输入 - 表示空列表/确认不支持任何档位；留空=沿用上面候选 {candidates}）: "
        ).strip()
    else:
        print("- 探测结果可信度有限，建议查看模型官方文档，确认支持的 reasoning effort 分档")
        edited = input(
            "    编辑要写入的档位列表（无自动候选，逗号分隔；"
            "输入 - 表示确认不支持任何档位；留空=跳过，不写入）: "
        ).strip()
    if edited == "-":
        final_enum = []
    elif edited:
        final_enum = [w.strip() for w in edited.split(",") if w.strip()]
    elif candidates is not None:
        final_enum = candidates
    else:
        print("已跳过，不写入 reasoning_capability。")
        return None

    if confirm(f"- 接受并写入 reasoning_capability.effort_enum={final_enum}?"):
        return {"effort_enum": final_enum}
    print("已跳过，不写入 reasoning_capability。")
    return None


def connectivity_test_then_probe(supply: dict, body_rules=None) -> "tuple[str, str, dict | None]":
    """连通性测试 + REACHABLE 时复用响应做 effort 探测确认。

    body_rules（顶层 body_error_rules）透传给 run_connectivity_test；BODY_ERROR
    != REACHABLE → 自动跳过 effort 探测与 rcap 写入（测试进程不写任何运行时状态）。

    只发一次上游请求（run_connectivity_test 内的 probe_effort 是唯一请求点）。
    返回 (category, desc, rcap)：
      - category/desc 为 classify + body_error 降级后的最终结论，供调用方决定后续分支；
      - rcap：REACHABLE 且用户接受写入时为 {"effort_enum":[...]}，否则 None。
    非 REACHABLE 时不进入探测/写入环节，rcap 恒为 None。
    """
    result = run_connectivity_test(supply, body_rules=body_rules)
    status, text_fixed, regex_candidates, is_complete, exc = result
    category, desc = classify_supply_reachability(status, text_fixed, exc)
    category, desc, _ = _apply_body_error_rules(
        status, category, desc, text_fixed, body_rules)
    if category == ReachabilityCategory.REACHABLE:
        rcap = run_probe_and_maybe_accept(supply, prefetched=result)
        return category, desc, rcap
    return category, desc, None


# ---------------------------------------------------------------------------
# supply
# ---------------------------------------------------------------------------

# _mask_appkey 已迁入 _format_ops.py（通过头部 import 别名引入，调用方不变）


def supply_list(path: str) -> None:
    cfg = load_config(path)
    for line in _format_supplies(cfg.get("supplies", []), preset="MENU"):
        print(line)
    done(False)


def supply_add(path: str) -> None:
    cfg = load_config(path)
    supplies = cfg.setdefault("supplies", [])

    sid = input("Supply ID: ").strip()
    if not sid:
        err("Supply ID 不能为空")
        done(False); sys.exit(1)
    if any(s.get("id") == sid for s in supplies):
        err(f"supply id 已存在: {sid}")
        done(False); sys.exit(1)

    surl = input("完整终态端点 URL（如 https://aigc.sankuai.com/v1/anthropic/v1/messages，不是 base）: ").strip()
    sproto = input("协议 [anthropic/chat/responses]（可选，留空则从 url 尾缀自动推断）: ").strip()
    if sproto and sproto not in VALID_PROTOCOLS:
        err(f"协议非法: {sproto!r}（须为 {'/'.join(VALID_PROTOCOLS)}）")
        done(False); sys.exit(1)
    sappkey = input("Appkey: ").strip()
    smodel = input("目标模型 target_model: ").strip()
    scooldown = input("冷却时长 cooldown_seconds (回车用全局默认): ").strip()

    entry: dict = {"id": sid, "url": surl}
    if sproto:
        entry["protocol"] = sproto
    entry["appkey"] = sappkey
    entry["target_model"] = smodel
    if scooldown:
        if not scooldown.isdigit() or int(scooldown) <= 0:
            err(f"cooldown_seconds 须为正整数: {scooldown!r}")
            done(False); sys.exit(1)
        entry["cooldown_seconds"] = int(scooldown)

    # 保存前用唯一权威解析校验：protocol 留空时须能从 url 尾缀推断出来，否则拒绝保存。
    try:
        resolve_protocol(entry)
    except ValueError as e:
        err(str(e))
        done(False); sys.exit(1)

    # 保存前 supply 扩展字段校验（extra_headers/system_inject/appkey_file；与
    # ConfigStore._validate_config 同一真相源 core.server.validate_supply_ext），
    # 违者拒绝写盘。
    try:
        validate_supply_ext(entry)
    except ValueError as e:
        err(str(e))
        done(False); sys.exit(1)

    # 连通性测试 + 同步探测（add 时无条件跑，用户在场即时决定接受/跳过）。
    # REACHABLE 时复用同一次响应做档位解析，不再发第二次请求。
    category, desc, rcap = connectivity_test_then_probe(entry, cfg.get("body_error_rules"))
    if category != ReachabilityCategory.REACHABLE:
        if not confirm(f"连通性测试未通过（{desc}），是否仍要保存该 supply？"):
            print("已取消新增。")
            done(False); sys.exit(1)
    if rcap:
        entry["reasoning_capability"] = rcap

    supplies.append(entry)
    atomic_write(path, cfg)
    print(f"Added supply: {sid}")
    done(True)


def _find_supply(cfg: dict, sid: str) -> dict | None:
    for s in cfg.get("supplies", []):
        if s.get("id") == sid:
            return s
    return None


def supply_edit(path: str, sid: str) -> None:
    cfg = load_config(path)
    target = _find_supply(cfg, sid)
    if target is None:
        err(f"supply id 不存在: {sid}")
        done(False); sys.exit(1)

    def ask(field: str, label: str, current) -> str:
        shown = current if current not in (None, "") else "(空)"
        raw = input(f"{label} [{shown}]: ").strip()
        return raw if raw else (current or "")

    new_url = ask("url", "完整终态端点 URL（如 https://aigc.sankuai.com/v1/anthropic/v1/messages，不是 base）",
                  target.get("url", ""))
    new_proto = ask("protocol", "协议 [anthropic/chat/responses]（可选，留空则从 url 尾缀自动推断）",
                     target.get("protocol", ""))
    if new_proto and new_proto not in VALID_PROTOCOLS:
        err(f"协议非法: {new_proto!r}（须为 {'/'.join(VALID_PROTOCOLS)}）")
        done(False); sys.exit(1)

    cur_appkey = target.get("appkey", "")
    raw_appkey = input(f"Appkey [{_mask_appkey(cur_appkey)}]（回车保留，输入新值替换): ").strip()
    new_appkey = raw_appkey if raw_appkey else cur_appkey

    new_model = ask("target_model", "目标模型 target_model", target.get("target_model", ""))

    cur_cd = str(target.get("cooldown_seconds", "")) if "cooldown_seconds" in target else ""
    raw_cd = input(f"冷却时长 cooldown_seconds [{cur_cd or '(默认)'}]: ").strip()
    if raw_cd:
        if not raw_cd.isdigit() or int(raw_cd) <= 0:
            err(f"cooldown_seconds 须为正整数: {raw_cd!r}")
            done(False); sys.exit(1)

    target["url"] = new_url
    if new_proto:
        target["protocol"] = new_proto
    else:
        target.pop("protocol", None)  # 留空则不写入 protocol 键，运行时从 url 尾缀推断
    target["appkey"] = new_appkey
    target["target_model"] = new_model
    if raw_cd:
        target["cooldown_seconds"] = int(raw_cd)

    # 保存前用唯一权威解析校验：protocol 留空时须能从 url 尾缀推断出来，否则拒绝保存。
    try:
        resolve_protocol(target)
    except ValueError as e:
        err(str(e))
        done(False); sys.exit(1)

    # 保存前 supply 扩展字段校验（同 supply_add；编辑含手改配置文件后经 edit 收敛的场景）。
    try:
        validate_supply_ext(target)
    except ValueError as e:
        err(str(e))
        done(False); sys.exit(1)

    # reasoning_capability 重新探测（可选）：先跑连通性测试，REACHABLE 则复用响应做探测，
    # 否则提示归因后跳过 rcap 写入（edit 自身有保存流程，不阻断整体 edit）。
    if confirm("reasoning_capability 重新探测?"):
        category, desc, rcap = connectivity_test_then_probe(target, cfg.get("body_error_rules"))
        if category != ReachabilityCategory.REACHABLE:
            print(f"连通性测试未通过（{desc}），跳过 reasoning_capability 探测。")
        if rcap:
            target["reasoning_capability"] = rcap

    atomic_write(path, cfg)
    print(f"Edited supply: {sid}")
    done(True)


def supply_check(path: str, sid: str) -> None:
    """连通性测试 + REACHABLE 则继续 effort 探测确认，接受则写入 reasoning_capability。
    整合原 supply-test（只读连通性）与 supply-probe（探测写入）为单一交互入口，
    全流程只发一次上游请求。
    """
    cfg = load_config(path)
    target = _find_supply(cfg, sid)
    if target is None:
        err(f"supply id 不存在: {sid}")
        done(False); sys.exit(1)
    category, desc, rcap = connectivity_test_then_probe(target, cfg.get("body_error_rules"))
    if category != ReachabilityCategory.REACHABLE:
        print("连通性测试未通过，跳过 effort 探测。")
        done(False)
        return
    if rcap:
        target["reasoning_capability"] = rcap
        atomic_write(path, cfg)
        print(f"Updated reasoning_capability for supply: {sid}")
        done(True)
    else:
        print("未写入变更。")
        done(False)


def supply_del(path: str, sid: str) -> None:
    cfg = load_config(path)
    target = _find_supply(cfg, sid)
    if target is None:
        err(f"supply id 不存在: {sid}")
        done(False); sys.exit(1)

    refs = []
    for r in cfg.get("routes", []):
        for tier_name, sids in (r.get("tiers") or {}).items():
            if sid in (sids or []):
                refs.append(f"route={r.get('id')} tier={tier_name}")
    if refs:
        err(f"supply {sid} 仍被以下 route/tier 引用，拒绝删除: {refs}")
        done(False); sys.exit(1)

    if not confirm(f"确认删除 supply {sid}?"):
        print("已取消。")
        done(False); sys.exit(1)

    cfg["supplies"] = [s for s in cfg.get("supplies", []) if s.get("id") != sid]
    atomic_write(path, cfg)
    print(f"Deleted supply: {sid}")
    done(True)


# ---------------------------------------------------------------------------
# route
# ---------------------------------------------------------------------------

def route_list(path: str) -> None:
    cfg = load_config(path)
    for line in _format_routes(cfg.get("routes", [])):
        print(line)
    done(False)


def _find_route(cfg: dict, rid: str) -> dict | None:
    for r in cfg.get("routes", []):
        if r.get("id") == rid:
            return r
    return None


def _split_supply_ids(raw: str) -> list[str]:
    """逗号分隔解析，与 route_list 的展示格式（`",".join(...)`）保持一致。"""
    return [w.strip() for w in raw.split(",") if w.strip()]


def route_add(path: str) -> None:
    cfg = load_config(path)
    routes = cfg.setdefault("routes", [])
    known_supplies = {s.get("id") for s in cfg.get("supplies", [])}

    rid = input("Route ID: ").strip()
    if not rid:
        err("Route ID 不能为空")
        done(False); sys.exit(1)
    if any(r.get("id") == rid for r in routes):
        err(f"route id 已存在: {rid}")
        done(False); sys.exit(1)

    ropus = _split_supply_ids(input("Opus 档 supplies (逗号分隔, 按优先级排序): "))
    rsonnet = _split_supply_ids(input("Sonnet 档 supplies (逗号分隔, 按优先级排序): "))
    rhaiku = _split_supply_ids(input("Haiku 档 supplies (逗号分隔, 按优先级排序): "))
    rfailover = input("Failover [on/off]: ").strip()

    if rfailover not in VALID_FAILOVER:
        err(f"failover 非法: {rfailover!r}（须为 {'/'.join(VALID_FAILOVER)}）")
        done(False); sys.exit(1)
    bad = [x for x in (ropus + rsonnet + rhaiku) if x not in known_supplies]
    if bad:
        err(f"以下 supply id 不存在: {sorted(set(bad))}")
        done(False); sys.exit(1)

    routes.append({
        "id": rid,
        "tiers": {"opus": ropus, "sonnet": rsonnet, "haiku": rhaiku},
        "failover": rfailover,
    })
    atomic_write(path, cfg)
    print(f"Added route: id={rid} opus={ropus} sonnet={rsonnet} haiku={rhaiku} failover={rfailover}")
    done(True)


def route_edit(path: str, rid: str) -> None:
    cfg = load_config(path)
    target = _find_route(cfg, rid)
    if target is None:
        err(f"route id 不存在: {rid}")
        done(False); sys.exit(1)
    known_supplies = {s.get("id") for s in cfg.get("supplies", [])}

    tiers = target.get("tiers") or {}
    new_tiers = {}
    for tier_name in TIER_NAMES:
        cur = tiers.get(tier_name, [])
        raw = input(
            f"{tier_name.capitalize()} 档 supplies (逗号分隔) [{','.join(cur)}]: "
        ).strip()
        new_tiers[tier_name] = _split_supply_ids(raw) if raw else cur

    bad = [x for x in sum(new_tiers.values(), []) if x not in known_supplies]
    if bad:
        err(f"以下 supply id 不存在: {sorted(set(bad))}")
        done(False); sys.exit(1)

    cur_failover = target.get("failover", "off")
    raw_fo = input(f"Failover [on/off] [{cur_failover}]: ").strip()
    new_failover = raw_fo if raw_fo else cur_failover
    if new_failover not in VALID_FAILOVER:
        err(f"failover 非法: {new_failover!r}（须为 {'/'.join(VALID_FAILOVER)}）")
        done(False); sys.exit(1)

    target["tiers"] = new_tiers
    target["failover"] = new_failover
    atomic_write(path, cfg)
    print(f"Edited route: {rid}")
    done(True)


def route_del(path: str, rid: str) -> None:
    cfg = load_config(path)
    target = _find_route(cfg, rid)
    if target is None:
        err(f"route id 不存在: {rid}")
        done(False); sys.exit(1)

    refs = [s.get("client_token") for s in cfg.get("strategies", []) if s.get("route_id") == rid]
    if refs:
        err(f"route {rid} 仍被以下 strategy 引用，拒绝删除: {refs}")
        done(False); sys.exit(1)

    if not confirm(f"确认删除 route {rid}?"):
        print("已取消。")
        done(False); sys.exit(1)

    cfg["routes"] = [r for r in cfg.get("routes", []) if r.get("id") != rid]
    atomic_write(path, cfg)
    print(f"Deleted route: {rid}")
    done(True)


# ---------------------------------------------------------------------------
# strategy
# ---------------------------------------------------------------------------

# _strategy_route_desc 已迁入 _format_ops.py（通过头部 import 别名引入，
# 调用方 strategy_edit/switch 不变）


def strategy_list(path: str) -> None:
    cfg = load_config(path)
    for line in _format_strategies(cfg.get("strategies", []), style="menu"):
        print(line)
    done(False)


def _find_strategy(cfg: dict, token: str) -> dict | None:
    for s in cfg.get("strategies", []):
        if s.get("client_token") == token:
            return s
    return None


def _validate_strategy_route_fields(entry: dict) -> None:
    """校验 route_id 与 route_pool 互斥（见设计文档 §4 校验规则）。

    一条 strategy 若同时含 route_id 与 route_pool 两个字段，属非法/歧义配置
    （core/server.py 的 extract_route_candidates 遇到这种态会忽略 route_id、
    静默走 route_pool 分支，运行时可容错，但写盘时必须主动拒绝，避免脏配置
    落地）。校验失败即报错并以非零退出码终止，不写盘。
    """
    if entry.get("route_id") and entry.get("route_pool"):
        err(f"strategy client_token={entry.get('client_token', '?')} 同时配置了 "
            f"route_id 与 route_pool，两者互斥，请只保留一个后再写入")
        done(False); sys.exit(1)


def strategy_add(path: str) -> None:
    cfg = load_config(path)
    strategies = cfg.setdefault("strategies", [])
    known_routes = {r.get("id") for r in cfg.get("routes", [])}

    stoken = input("Client token: ").strip()
    if not stoken:
        err("client_token 不能为空")
        done(False); sys.exit(1)
    if any(s.get("client_token") == stoken for s in strategies):
        err(f"client_token 已存在 strategy 绑定: {stoken}")
        done(False); sys.exit(1)

    srid = input("Route ID (需为已存在的 route id): ").strip()
    if srid not in known_routes:
        err(f"route id 不存在: {srid}")
        done(False); sys.exit(1)

    print("录入 tiers_source_capability（source 侧能力，逐 tier 询问）:")
    stiers_cap = prompt_source_capability()

    snote = input("Note (可选备注): ").strip()

    entry: dict = {"client_token": stoken, "route_id": srid}
    if stiers_cap is not None:
        entry["tiers_source_capability"] = stiers_cap
    entry["note"] = snote

    _validate_strategy_route_fields(entry)
    strategies.append(entry)
    atomic_write(path, cfg)
    print(f"Added strategy: {stoken} -> {srid}")
    done(True)


def strategy_edit(path: str, token: str) -> None:
    cfg = load_config(path)
    target = _find_strategy(cfg, token)
    if target is None:
        err(f"未找到该 token 的 strategy 绑定: {token}")
        done(False); sys.exit(1)
    known_routes = {r.get("id") for r in cfg.get("routes", [])}

    # route_pool 写法（新）不走本 CLI 的单值 Route ID 编辑（见
    # docs/designs/2026-07-28-session-route-dispatch-design.md §5：
    # CLI 菜单本次不支持录入 route_pool/dispatch，但编辑其他字段时不能丢/不能因
    # 强制要求单值 route_id 而报错阻塞）。target 引用是 cfg 内原字典，
    # route_pool/dispatch 字段全程保留不动。
    if target.get("route_pool"):
        print(f"该 strategy 使用 route_pool 多路由写法（{_strategy_route_desc(target)}），"
              f"本 CLI 暂不支持编辑 route_pool/dispatch，如需修改请直接编辑配置文件。")
        new_rid = None
    else:
        cur_rid = target.get("route_id", "")
        raw_rid = input(f"Route ID [{cur_rid}]: ").strip()
        new_rid = raw_rid if raw_rid else cur_rid
        if new_rid not in known_routes:
            err(f"route id 不存在: {new_rid}")
            done(False); sys.exit(1)

    if confirm("重新录入 tiers_source_capability?"):
        new_tiers_cap = prompt_source_capability(target.get("tiers_source_capability"))
        if new_tiers_cap is not None:
            target["tiers_source_capability"] = new_tiers_cap
        else:
            target.pop("tiers_source_capability", None)

    cur_note = target.get("note", "") or ""
    raw_note = input(f"Note [{cur_note}]: ").strip()
    new_note = raw_note if raw_note else cur_note

    if new_rid is not None:
        target["route_id"] = new_rid
    target["note"] = new_note
    _validate_strategy_route_fields(target)
    atomic_write(path, cfg)
    print(f"Edited strategy: {token} -> {_strategy_route_desc(target)}")
    done(True)


def strategy_del(path: str, token: str) -> None:
    cfg = load_config(path)
    target = _find_strategy(cfg, token)
    if target is None:
        err(f"未找到该 token 的 strategy 绑定: {token}")
        done(False); sys.exit(1)

    if not confirm(f"确认删除 strategy {token}?"):
        print("已取消。")
        done(False); sys.exit(1)

    cfg["strategies"] = [s for s in cfg.get("strategies", []) if s.get("client_token") != token]
    atomic_write(path, cfg)
    print(f"Deleted strategy: {token}")
    done(True)


# ---------------------------------------------------------------------------
# switch（顶层快捷方式，非交互，参数式一行）
# ---------------------------------------------------------------------------

def switch(path: str, token: str, route_id: str) -> None:
    cfg = load_config(path)
    target = _find_strategy(cfg, token)
    if target is None:
        err(f"未找到该 token 的 strategy 绑定: {token}，请先用 strategy add 新增")
        done(False); sys.exit(1)
    if target.get("route_pool"):
        # route_id 与 route_pool 互斥（见设计文档 §4 校验规则），不能对
        # route_pool 写法的 strategy 写单值 route_id，避免产生非法/歧义配置。
        err(f"该 strategy 使用 route_pool 多路由写法（{_strategy_route_desc(target)}），"
            f"switch 只支持单值 route_id 的旧写法，如需调整请直接编辑配置文件。")
        done(False); sys.exit(1)
    if not any(r.get("id") == route_id for r in cfg.get("routes", [])):
        err(f"route id 不存在: {route_id}")
        done(False); sys.exit(1)

    old = target.get("route_id")
    target["route_id"] = route_id
    atomic_write(path, cfg)
    print(f"已切换: {token} -> route_id={route_id}（原 route_id={old}）")
    done(True)


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

_DISPATCH = {
    "supply-list": (supply_list, 0),
    "supply-add": (supply_add, 0),
    "supply-edit": (supply_edit, 1),
    "supply-del": (supply_del, 1),
    "supply-check": (supply_check, 1),
    "route-list": (route_list, 0),
    "route-add": (route_add, 0),
    "route-edit": (route_edit, 1),
    "route-del": (route_del, 1),
    "strategy-list": (strategy_list, 0),
    "strategy-add": (strategy_add, 0),
    "strategy-edit": (strategy_edit, 1),
    "strategy-del": (strategy_del, 1),
    "switch": (switch, 2),
}


def main() -> None:
    global _WRITE_BASELINE, _WRITE_STARTED, _WRITE_OPERATION
    global _WRITE_AUTHORIZATION, _WRITE_PREFLIGHT, _WRITE_INPUT_PATH, _WRITE_COMMITTED, _WRITE_LOCATIONS
    global _WRITE_BACKUP_BASELINE
    global _MARKER_BASELINE, _MARKER_CONTENT, _MARKER_REQUIRED
    if len(sys.argv) < 3:
        err("用法: _config_ops.py <subcommand> <config_file> [args...]")
        sys.exit(1)
    subcmd = sys.argv[1]
    config_path = sys.argv[2]
    extra = sys.argv[3:]

    entry = _DISPATCH.get(subcmd)
    if entry is None:
        err(f"未知子命令: {subcmd}")
        sys.exit(1)
    func, nargs = entry
    if len(extra) < nargs:
        err(f"{subcmd} 需要 {nargs} 个参数，收到 {len(extra)} 个")
        sys.exit(1)

    try:
        _WRITE_BASELINE = None
        _WRITE_STARTED = False
        _WRITE_OPERATION = subcmd
        _WRITE_AUTHORIZATION = _WRITE_PREFLIGHT = _WRITE_COMMITTED = None
        _WRITE_LOCATIONS = None
        _WRITE_BACKUP_BASELINE = None
        _WRITE_INPUT_PATH = config_path
        _MARKER_BASELINE = _MARKER_CONTENT = None
        _MARKER_REQUIRED = not subcmd.endswith("-list")
        if _MARKER_REQUIRED:
            _precheck_inherited_marker()
            _prepare_write(config_path, subcmd)
        elif ('CONFIG_OPS_RELOAD_MARKER' in os.environ
              or 'CONFIG_OPS_RELOAD_MARKER_FD' in os.environ):
            raise MarkerSafetyError('read-only helper does not accept marker transport')
        func(config_path, *extra[:nargs])
    except FileNotFoundError:
        err(f"config not found: {config_path}")
        sys.exit(1)
    except (json.JSONDecodeError, ValueError) as e:
        err(f"config 解析失败: {e}")
        sys.exit(1)
    except RuntimeError as e:
        from core.mac_config_writer import MacWriteError
        from core.windows_secure_files import SecureWriteError as WindowsWriteError
        from core.platform_windows import WindowsPrivateCreationError
        from core.discovery import PreflightError
        from core.secure_files import SecureSnapshotError
        if isinstance(e, WindowsWriteError):
            state_message = {'NOT_REPLACED': '目标未替换', 'REPLACED': '目标已替换，尚未完成验证',
                             'POSTCHECK_FAILED': '目标已替换但后验失败', 'UNKNOWN': '提交结果未知',
                             'VERIFIED': '目标保存已验证'}.get(e.target_state, '提交结果未知')
            if e.target_was_verified:
                state_message = '目标曾通过后验，但后续事务状态未知；整体写入未完成'
            err(f"配置写入阻断 ({e.code})；{state_message}；备份状态 {e.backup_status}；未触发 reload。")
            if e.backup:
                err("备份或残留文件: " + e.backup)
            if e.staging:
                err("暂存文件可能残留: " + e.staging)
            if e.release_unknown or e.creation_unknown:
                err("句柄释放或产物创建状态未知，请人工检查。")
        elif isinstance(e, MacWriteError):
            state_message = {
                'NOT_REPLACED': '目标未替换',
                'REPLACED': '目标已替换，尚未完成验证',
                'POSTCHECK_FAILED': '目标已改变，后验验证失败、保护状态未知',
                'UNKNOWN': '提交结果未知',
            }.get(e.state, '提交结果未知')
            err(f"配置写入阻断 ({e.code})；{state_message}；未触发 reload。")
            if e.backup_name:
                err("备份或残留文件: " + json.dumps(e.backup_name, ensure_ascii=True))
            if e.staging_name:
                err("暂存文件可能残留: " + json.dumps(e.staging_name, ensure_ascii=True))
            if e.cleanup_unknown or e.close_unknown:
                err("暂存清理或文件描述符关闭状态未知，请人工检查相关目录。")
            if e.marker_unknown:
                err("reload 标记写入状态未知；未触发 reload。")
        elif isinstance(e, WritePreflightFailed):
            err(f"配置写入阻断：{e}；未触发 reload。")
        elif isinstance(e, WindowsPrivateCreationError):
            state = ('目录可能已创建' if e.created is True else
                     '目录创建状态未知' if e.created is None else '目录未创建')
            err(f"运行目录保护未确认；{state}" +
                ("；句柄关闭状态未知" if e.release_unknown else "") + "；未触发 reload。")
        elif isinstance(e, PreflightError) and e.code == 'release_unknown':
            err("路径核验句柄关闭状态未知；未触发 reload。")
        elif isinstance(e, SecureSnapshotError) and getattr(e, 'close_unknown', False):
            err("私有目录或文件描述符关闭状态未知；未触发 reload。")
        else:
            err("配置读取或路径校验失败；未触发 reload。")
        sys.exit(1)
    except OSError as e:
        if os.name == "nt" and isinstance(e, MarkerSafetyError) and _WRITE_COMMITTED is not None:
            err("配置已保存并验证，但 reload 标记未确认；未触发 reload。")
        elif isinstance(e, MarkerSafetyError) and e.close_unknown:
            err("reload 标记 FD 关闭状态未知；配置可能已保存，未触发 reload。")
        elif isinstance(e, MarkerSafetyError) and not _WRITE_STARTED:
            if os.name == "nt":
                err(f"reload 标记不可用（{e}）；未提交，未触发 reload。")
            else:
                err("可信 marker 管道不可用；未提交，未触发 reload。")
        else:
            err("配置读写失败；如已开始提交，保存或 reload 标记状态可能未知。")
        sys.exit(1)
    except KeyboardInterrupt:
        if _WRITE_STARTED:
            print("\n已中断；配置可能已保存，未确认 reload。")
        else:
            print("\n已取消。")
            done(False)
        sys.exit(130)
    finally:
        if os.name == "nt":
            for snapshot in (_WRITE_BASELINE, _MARKER_BASELINE):
                if snapshot is not None:
                    try:
                        snapshot.close()
                    except BaseException:
                        err("Windows 快照关闭状态未知；未确认 reload。")
                        raise


if __name__ == "__main__":
    main()
