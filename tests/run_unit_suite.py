#!/usr/bin/env python3
"""单元测试批量入口：**每个文件独立进程**运行（见 run_unit_suite.sh 的说明）。

直接 `pytest tests/` 有两个结构性障碍：
1. `tests/` 下的隔离回归用例（test_cli_* / test_mac_* / test_windows_* 等）
   在 import 阶段就要求「隔离项目 + 假 HOME」，直接收集会让整个会话中止；
   它们必须走 `tests/run_isolated.sh`（README-isolation.md）。
2. Windows 仿真用例会临时替换 `os.name`；同进程内跨文件串联会互相污染，
   表现为后续用例集体报 WindowsPath/POSIX 类错误。

所以这里按文件分进程跑：先探一次「能否在本机直接收集」，能收集的再并行执行。

用法：
    python3 tests/run_unit_suite.py            # 全部可直跑的单元测试文件
    python3 tests/run_unit_suite.py -j 8       # 并行度（默认 4）
    python3 tests/run_unit_suite.py tests/test_x.py ...
"""
from __future__ import annotations

import argparse
import concurrent.futures
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import platform_scope  # noqa: E402  (同目录清单，唯一真相源)

# Windows 控制台默认 cp1252，中文输出会直接 UnicodeEncodeError 打断整套测试
# （实测 GitHub Actions windows-latest）。这里强制 UTF-8，并在不支持时降级为
# 替换字符——回归工具不该因为终端编码而失败。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass


def collectable(path: pathlib.Path) -> bool:
    """能否在本机直接收集（rc=2 表示 import/收集阶段就拒绝）。"""
    done = subprocess.run([sys.executable, '-m', 'pytest', str(path), '--co', '-q'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return done.returncode != 2


# 这些用例在运行期才读隔离环境变量（收集阶段看不出来），必须走 run_isolated.sh。
# 判据用环境变量名出现在失败输出里，而不是猜文件名。
NEEDS_ISOLATION_ENV = ('MP_TEST_ROOT', 'MP_TEST_PROJECT', 'MP_TEST_VENV', 'MP_TEST_GUARD')
# Windows 原生原语（句柄/ACL/记录目录）在 macOS 上必然失败，验收位置是 Windows runner。
WINDOWS_NATIVE_HINT = (
    'cannot instantiate \'WindowsPath\'',
    'WindowsPrivateCreationError',
    'Invalid fixed directory',
    'record_unavailable',
)


FAILED_ID = re.compile(r"tests/([A-Za-z0-9_]+\.py)::([A-Za-z0-9_]+::)?([A-Za-z0-9_\[\]=,\.\-']+)")


def failed_ids(output: str) -> list[tuple[str, str]]:
    """从 pytest 输出里取出失败的 (文件名, 'Class.test' / 'test')。"""
    found, seen = [], set()
    for line in output.splitlines():
        if 'FAILED' not in line:
            continue
        for match in FAILED_ID.finditer(line):
            cls, test = match.group(2), match.group(3)
            test_id = (cls[:-2] + '.' + test) if cls else test
            key = (match.group(1), test_id)
            if key not in seen:
                seen.add(key)
                found.append(key)
    return found


def classify(name: str, output: str) -> str:
    """ok / isolation / out-of-scope / windows-native / macos-native / fail

    判据顺序：隔离环境变量 → 平台范围清单 → 文件名前缀（原生专测）→ 错误特征。

    平台范围清单（`tests/platform_scope.py`）优先：显式声明了 `posix`/`windows`
    的文件或用例，在另一宿主上的失败是「不适用」，不是缺陷。未声明的文件默认
    两边都必须通过（fail-closed），所以新用例想逃过 Windows 验收必须显式登记。
    """
    if any(var in output for var in NEEDS_ISOLATION_ENV):
        return 'isolation'
    ids = failed_ids(output)
    if ids and all(platform_scope.covers(file_name, test_id, HOST) for file_name, test_id in ids):
        return 'out-of-scope'
    # 文件级声明覆盖「整份都只属于另一平台」但输出里没解析出具体用例 id 的情况
    scope, _, _ = platform_scope.file_scope(name)
    if scope != platform_scope.BOTH and scope != HOST:
        return 'out-of-scope'
    if HOST == 'posix' and (name.startswith('test_windows_') or name.startswith('test_wine_')):
        return 'windows-native'
    if sys.platform != 'darwin' and (name.startswith('test_mac_')
                                     or name.startswith('test_darwin_')):
        return 'macos-native'
    # 特征串只在「非 Windows 宿主」上作为 Windows 专测的兜底；在 Windows 上出现
    # 这些特征就是真失败（之前会把它错标成 windows-native 从而永久掩盖）。
    if HOST != 'windows' and any(hint in output for hint in WINDOWS_NATIVE_HINT):
        return 'windows-native'
    return 'fail'


HOST = platform_scope.host_scope()


DETAIL_LINES = 60     # 失败时要保留的输出行数（够看到首个断言的完整回溯）


def _failure_detail(output: str) -> list[str]:
    """挑出「首个失败段头部 + 真正的异常行」。

    只取头部不够：pytest 会在回溯里展开整段被调函数源码（本例里有 40+ 行），
    把真正抛错的 `E ...` 行顶出窗口。所以头部与尾部各取一段，中间用省略号
    连接——头部给定位，尾部给错误码。
    """
    lines = output.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.startswith('_') and line.strip('_ ')), None)
    if start is None:
        start = max(0, len(lines) - DETAIL_LINES)
    head = lines[start:start + DETAIL_LINES]
    tail_start = max(start + DETAIL_LINES, len(lines) - 15)
    tail = lines[tail_start:] if tail_start < len(lines) else []
    if not tail:
        return head
    return [*head, f'     | ... ({tail_start - start - DETAIL_LINES} lines omitted)', *tail]


def run_one(path: pathlib.Path) -> tuple[str, str, str, list[str]]:
    done = subprocess.run([sys.executable, '-m', 'pytest', str(path), '-q'],
                          capture_output=True, text=True,
                          encoding='utf-8', errors='replace')
    output = (done.stdout or '') + (done.stderr or '')
    tail = output.strip().splitlines()
    summary = tail[-1] if tail else '(no output)'
    # pytest 用 5 表示「没有收集到用例」，对本套件不算失败
    if done.returncode in (0, 5):
        return path.name, 'ok', summary, []
    kind = classify(path.name, output)
    detail = _failure_detail(output) if kind == 'fail' else []
    return path.name, kind, summary, detail


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('-j', '--jobs', type=int, default=4)
    parser.add_argument('--list-scope', action='store_true',
                        help='打印平台范围清单后退出')
    parser.add_argument('files', nargs='*')
    args = parser.parse_args(argv)

    if args.list_scope:
        print(f'host: {HOST}')
        print('按文件声明：')
        for name in sorted(platform_scope.FILE_SCOPE):
            scope, reason, gap = platform_scope.file_scope(name)
            print(f'  {name:<52} {scope:<8}{" [gap]" if gap else ""}  {reason}')
        print('按用例声明：')
        for (name, pattern), entry in sorted(platform_scope.TEST_SCOPE.items()):
            scope, reason, gap = platform_scope._split(entry)
            print(f'  {name}::{pattern or "*"}')
            print(f'      {scope:<8}{" [gap]" if gap else ""}  {reason}')
        return 0

    root = pathlib.Path(__file__).resolve().parent
    if args.files:
        candidates = [pathlib.Path(p) for p in args.files]
    else:
        candidates = sorted(root.glob('test_*.py'))

    runnable, skipped = [], []
    for path in candidates:
        (runnable if collectable(path) else skipped).append(path)

    if skipped:
        print('skipped (需要 tests/run_isolated.sh 的隔离环境):')
        for path in skipped:
            print('  -', path.name)
        print()

    buckets = {'ok': [], 'isolation': [], 'out-of-scope': [], 'windows-native': [],
               'macos-native': [], 'fail': []}
    prefixes = {'ok': 'ok  ', 'isolation': 'iso ', 'out-of-scope': 'skip',
                'windows-native': 'win ', 'macos-native': 'mac ', 'fail': 'FAIL'}
    skipped_by_scope = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for name, kind, summary, detail in pool.map(run_one, runnable):
            buckets[kind].append(name)
            print(f'{prefixes[kind]} {name:<52} {summary}')
            if kind == 'out-of-scope':
                scope, reason, _ = platform_scope.file_scope(name)
                skipped_by_scope.append((name, scope, reason))
            for line in detail:
                print(f'     | {line}')

    print()
    print(f'host: {HOST}')
    print(f'files run: {len(runnable)}  ok: {len(buckets["ok"])}'
          f'  needs-isolation: {len(skipped) + len(buckets["isolation"])}'
          f'  本平台不适用: {len(buckets["out-of-scope"])}'
          f'  windows-native: {len(buckets["windows-native"])}'
          f'  macos-native: {len(buckets["macos-native"])}'
          f'  意外失败: {len(buckets["fail"])}')

    if skipped_by_scope:
        print()
        print('按平台范围清单跳过（tests/platform_scope.py 显式声明，非静默）:')
        for name, scope, reason in skipped_by_scope:
            print(f'  - {name:<48} scope={scope}  {reason}')

    gaps = platform_scope.gaps()
    if gaps:
        print()
        print(f'Windows 覆盖缺口 {len(gaps)} 项（跨平台逻辑、夹具仍绑定 POSIX；待补而非豁免）:')
        for name, reason in gaps:
            print(f'  - {name:<60} {reason}')

    if buckets['fail']:
        print()
        print('意外失败（不在平台范围清单里，也不是隔离环境缺失）:')
        for name in buckets['fail']:
            print('  -', name)
        print()
        print('若确认该文件/用例只属于另一平台，请在 tests/platform_scope.py 登记并写原因；'
              '若属共享逻辑，请修夹具而不是登记。')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
