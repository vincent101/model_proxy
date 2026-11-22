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
import subprocess
import sys

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


def classify(name: str, output: str) -> str:
    """ok / isolation / windows-native / macos-native / fail

    判据顺序：隔离环境变量 → 文件名（平台专测）→ 错误信息特征。

    `test_windows_*` / `test_wine_*` 专测 Win32 句柄/ACL/记录目录，
    `test_mac_*` / `test_darwin_*` 专测 macOS 原生保护——各自只能在对应宿主上
    验收。跨平台 CI 必须两边都跑才能覆盖全部；缺哪边，哪边就是「未原生验收」。
    """
    if any(var in output for var in NEEDS_ISOLATION_ENV):
        return 'isolation'
    if sys.platform != 'win32' and (name.startswith('test_windows_')
                                    or name.startswith('test_wine_')):
        return 'windows-native'
    if sys.platform != 'darwin' and (name.startswith('test_mac_')
                                     or name.startswith('test_darwin_')):
        return 'macos-native'
    if any(hint in output for hint in WINDOWS_NATIVE_HINT):
        return 'windows-native'
    return 'fail'


DETAIL_LINES = 24     # 失败时要保留的输出行数（足够看到首个断言）


def _failure_detail(output: str) -> list[str]:
    """挑出「首个失败用例 + 断言」附近的若干行。

    `-q` 模式的失败段以 `____ test_name ____` 开头；直接取第一段比取整个
    输出尾部更有用——尾部常常只剩进度点和汇总行。
    """
    lines = output.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.startswith('_') and line.strip('_ ')), None)
    if start is None:
        start = max(0, len(lines) - DETAIL_LINES)
    return lines[start:start + DETAIL_LINES]


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
    parser.add_argument('files', nargs='*')
    args = parser.parse_args(argv)

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

    buckets = {'ok': [], 'isolation': [], 'windows-native': [], 'macos-native': [],
               'fail': []}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for name, kind, summary, detail in pool.map(run_one, runnable):
            buckets[kind].append(name)
            prefix = {'ok': 'ok  ', 'isolation': 'iso ',
                      'windows-native': 'win ', 'macos-native': 'mac ',
                      'fail': 'FAIL'}[kind]
            print(f'{prefix} {name:<52} {summary}')
            for line in detail:
                print(f'     | {line}')

    print()
    print(f'files run: {len(runnable)}  ok: {len(buckets["ok"])}'
          f'  needs-isolation: {len(skipped) + len(buckets["isolation"])}'
          f'  windows-native: {len(buckets["windows-native"])}'
          f'  macos-native: {len(buckets["macos-native"])}'
          f'  意外失败: {len(buckets["fail"])}')
    if buckets['fail']:
        print()
        print('意外失败（既不是隔离环境缺失，也不是 Windows 原生限制）:')
        for name in buckets['fail']:
            print('  -', name)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
