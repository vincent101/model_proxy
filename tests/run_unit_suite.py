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


def collectable(path: pathlib.Path) -> bool:
    """能否在本机直接收集（rc=2 表示 import/收集阶段就拒绝）。"""
    done = subprocess.run([sys.executable, '-m', 'pytest', str(path), '--co', '-q'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return done.returncode != 2


def run_one(path: pathlib.Path) -> tuple[str, bool, str]:
    done = subprocess.run([sys.executable, '-m', 'pytest', str(path), '-q'],
                          capture_output=True, text=True)
    tail = (done.stdout or done.stderr).strip().splitlines()
    summary = tail[-1] if tail else '(no output)'
    # pytest 用 5 表示「没有收集到用例」，对本套件不算失败
    ok = done.returncode in (0, 5)
    return path.name, ok, summary


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

    failures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for name, ok, summary in pool.map(run_one, runnable):
            if ok:
                print(f'ok   {name:<52} {summary}')
            else:
                print(f'FAIL {name:<52} {summary}')
                failures.append(name)

    print()
    print(f'files run: {len(runnable)}  passed: {len(runnable) - len(failures)}'
          f'  failed: {len(failures)}  skipped(isolation): {len(skipped)}')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
