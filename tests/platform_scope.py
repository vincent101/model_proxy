"""测试文件的平台适用范围清单（唯一真相源）。

**为什么需要这张表**

`tests/` 里混着三种用例，靠文件名或错误串猜都会失真：

1. **原生专测**：`test_windows_*`（Win32 句柄/ACL/cmd.exe）、`test_mac_*` /
   `test_darwin_*`（ACL、xattr）。只能在对应宿主验收。
2. **平台语义用例**：POSIX `flock`/`fcntl`/`O_NOFOLLOW`、`st_dev+st_ino` 身份、
   `chmod` 权限位、POSIX FIFO 契约。在 Windows 上这些原语根本不存在，失败不
   代表缺陷。
3. **宿主专用夹具**：被测逻辑是跨平台的，但夹具用「普通 tempfile + 裸读配置」，
   在 Windows 上产品代码要求句柄快照 + 项目树内已验证边界（`_windows_config_boundary`），
   于是夹具先失败。这类标注 `gap=True`——它是**待补的 Windows 覆盖缺口**，
   CI 每次都会列出来，不算绿。

以前 `run_unit_suite.py` 用「文件名前缀 + 错误串特征」猜，产生两种失真：
Windows 上本该报的缺陷被 `windows-native` 标签吞掉；POSIX 夹具的用例在
Windows 上假失败，把真信号淹掉。

**约定（fail-closed）**

- 未列出的文件 = `both`：必须在 macOS 与 Windows 上都通过。
- 只有显式声明的文件/用例才会被跳过，且跳过会**带原因打印**，不静默。
- 声明了 `posix` 的文件在本平台失败 → 照常红（声明只免掉另一平台的失败）。
- `gap=True` 的条目计入「Windows 覆盖缺口」，是待办清单而不是豁免。

**维护规则**

新增测试默认走 `both`。要声明平台时先自问：这是语义上真的只属于该平台，
还是夹具偷懒？后者应修夹具，修不了才登记成 `gap=True`。
"""
from __future__ import annotations

POSIX = 'posix'
WINDOWS = 'windows'
BOTH = 'both'

# --- 整个文件只属于某个平台 -------------------------------------------------
# 键：文件名；值：(scope, reason[, gap])
FILE_SCOPE: dict[str, tuple] = {
    # POSIX 原语 / POSIX 实现，Windows 侧另有实现与用例
    'test_platform_lock.py': (POSIX, 'POSIX flock 语义；Windows 锁走 platform_windows（test_windows_*）'),
    'test_secure_files.py': (POSIX, 'core.secure_files 的 POSIX 快照/ACL；Windows 侧为 platform_unverified 且另有 test_windows_secure_*'),
    'test_backup_close_failure.py': (POSIX, 'core.secure_files 关闭失败注入；同上属 POSIX 实现'),
    'test_cli_write_preflight.py': (POSIX, '依赖 fcntl 与 POSIX FIFO 的 reload marker/fd 契约；Windows 走 marker 文件契约'),
    'test_config_snapshot.py': (POSIX, 'POSIX 稳定快照语义（st_dev/st_ino/mtime+属主）；Windows 句柄快照由 test_windows_config_boundary 覆盖'),
    'test_discovery_anchor.py': (POSIX, 'POSIX inode 身份 preflight；Windows 身份由 test_windows_discovery 覆盖'),
    'test_install_ops.py': (POSIX, 'POSIX 私有安装目标校验；Windows 安装由 test_windows_secure_hook_install 覆盖'),
    # 这些用例显式 `platform="posix"`，测的是 POSIX 解析器本身；Windows 侧对应
    # test_windows_paths / test_windows_discovery / test_windows_discovery。
    'test_runtime_unify_paths.py': (POSIX, '显式 platform=posix 的路径解析器单测，与平台无关的产品分支'),
    # 直接构造 UsageTotalsStore（`_load`）按产品设计在 Windows 上保留「严格默认」：
    # 只认已完成 preflight 的账本角色，其余一律 fail-closed（见 server.py
    # 「Other ledger callers and unproven snapshots retain the strict default」）。
    # 本文件验的正是 direct 模式的账本语义；Windows 启动路径的账本校验由
    # test_windows_gate 覆盖。
    'test_usage_totals.py': (POSIX, 'direct 构造的账本读取在 Windows 上按设计严格 fail-closed'),
    'test_windows_control_a_followup.py': (POSIX, '在 POSIX 上用假 Win32 后端模拟 Windows 控制；原生 Windows 会先被真实启动前置条件拦下'),
}

# --- 文件的某几个用例只属于某个平台 -----------------------------------------
# 键：(文件名, 'Class.test' 或 'test')；值：(scope, reason[, gap])
TEST_SCOPE: dict[tuple, tuple] = {
    # 配置读取夹具把配置文件放在 tempdir；Windows 侧只认项目树内的已验证边界
    ('test_config_ops.py', 'TestConfigAtomicAndCsvContracts'): (
        POSIX, '原子写与权限位属 POSIX 语义（Windows 写入走 windows_secure_files）'),
    ('test_sidecar_review_followup.py', 'TestLegacyWriterResources.test_fdopen_close_failure_before_closing_is_unknown_and_not_retried'): (
        POSIX, '断言 POSIX 路径字符串与 fdopen 语义'),
    ('test_route_sidecar_unknown.py', 'TestSidecarUnknown.test_changed_protection_evidence_does_not_resolve_unknown'): (
        POSIX, '靠 chmod 改动权限位制造证据变化；Windows 上 chmod 无此语义'),
    ('test_provenance_compat.py', 'TestUnexpectedAttributeStillRejected.test_quarantine_is_still_rejected_by_protection_reader'): (
        POSIX, '依赖 os.O_NOFOLLOW 与 macOS 扩展属性'),
    ('test_runtime_paths_cross_platform.py', 'RuntimePathFileTests.test_valid_file_is_parsed_with_injected_fixture_defaults'): (
        POSIX, '用例注入 POSIX 绝对路径作为项目根'),
}


# --- 已知红灯（隔离清单） ---------------------------------------------------
# 这些文件在**两个平台**上都已经红了（历史遗留，或与本轮无关的另一项改动撞车）。
# 与 FILE_SCOPE/TEST_SCOPE 的区别：那两者是「该平台不适用」，这里是「本该通过
# 但确实坏了」。清单会每次都打印，且一旦某文件开始通过就会报 STALE 并让 CI 红，
# 逼着把它从清单里删掉——不允许长期沉淀成新的静默掩盖。
KNOWN_BROKEN: dict[str, tuple[str, str]] = {
    'test_windows_gate.py': (
        '2026-11-23',
        '启动顺序用例在 macOS 上 25 项、Windows 上 14 项失败（此前被 test_windows_ '
        '前缀误判为「仅 Windows 可测」而长期掩盖）；部分断言仍用旧 lock 身份 '
        '{device,inode}，与在改的 macos_volume_uuid 方案撞车，需单独修复',
    ),
    'test_windows_secure_install_wiring.py': (
        '2026-11-23',
        '两平台各 9 项失败（夹具早于 2026-11-22 安装边界复核：伪造 os 缺 fspath、'
        '未提供项目内 runtime_paths.json，导致 write_target 未被调用）；此前被 '
        'windows-native 标签掩盖。真实 Windows 写入链另有原生验收：'
        'test_windows_config_write_integration（6 项原生通过）与 '
        'test_windows_secure_hook_install',
    ),
}


def host_scope() -> str:
    import sys
    return WINDOWS if sys.platform == 'win32' else POSIX


def _split(entry):
    return entry[0], entry[1], (entry[2] if len(entry) > 2 else False)


def file_scope(name: str):
    """(scope, reason, gap)；未声明返回 (BOTH, '', False)。"""
    return _split(FILE_SCOPE[name]) if name in FILE_SCOPE else (BOTH, '', False)


def test_scope(name: str, test_id: str):
    """精确匹配 `Class.test`；否则按类名/方法名后缀匹配；都没有返回 None。"""
    if (name, test_id) in TEST_SCOPE:
        return _split(TEST_SCOPE[(name, test_id)])
    for (file_name, pattern), entry in TEST_SCOPE.items():
        if file_name != name or not pattern:
            continue
        if test_id == pattern or test_id.startswith(pattern + '.') or test_id.endswith('.' + pattern):
            return _split(entry)
    return None


def covers(name: str, test_id: str, host: str) -> bool:
    """本用例在 host 上是否属于「不适用」——文件级或用例级任一判定即适用。"""
    scope, _, _ = file_scope(name)
    if scope != BOTH and scope != host:
        return True
    entry = test_scope(name, test_id)
    if entry is not None and entry[0] != BOTH and entry[0] != host:
        return True
    return False


def gaps() -> list[tuple[str, str]]:
    """Windows 覆盖缺口：被测逻辑跨平台、但夹具绑定 POSIX。"""
    out = []
    for name, entry in FILE_SCOPE.items():
        scope, reason, gap = _split(entry)
        if gap and scope == POSIX:
            out.append((name, reason))
    for (name, pattern), entry in TEST_SCOPE.items():
        if pattern is None:
            continue
        scope, reason, gap = _split(entry)
        if gap and scope == POSIX:
            out.append((f'{name}::{pattern}', reason))
    return sorted(out)
