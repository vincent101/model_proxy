# -*- coding: utf-8 -*-
"""跨平台独占文件锁（单实例守卫）。

keeper 只需要「同一时刻只有一个实例在跑」这一件事，不需要锁语义的其它细节。
两个平台各用原生机制：

- POSIX：``fcntl.flock(LOCK_EX | LOCK_NB)``
- Windows：``msvcrt.locking(LK_NBLCK)``（锁文件首字节；进程退出/句柄关闭即释放）

两边的共同点：**非阻塞、不可重入、进程崩溃自动释放**。区别只在调用方式，
因此上层不需要关心平台。

注意 Windows 语义差异（写进注释以免后人踩）：
1. ``msvcrt.locking`` 锁的是「从当前文件位置开始的 N 字节」，必须先 ``seek(0)``；
2. 锁区域可以超出文件末尾，因此空锁文件也能正常工作；
3. 释放必须显式 ``LK_UNLCK`` 同一区域，或直接关闭句柄——本模块统一用关闭句柄，
   与 POSIX 的 flock 一致（flock 随 fd 关闭释放）。
"""
from __future__ import annotations

import os
import sys

try:                      # POSIX
    import fcntl
except ImportError:
    fcntl = None

try:                      # Windows
    import msvcrt
except ImportError:
    msvcrt = None


class LockUnavailableError(RuntimeError):
    """当前平台没有任何可用的文件锁机制（fail-closed，不静默放行）。"""


def _open_private(path: str):
    flags = (os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    handle = open(path, "a+b", buffering=0,
                  opener=lambda name, _flags: os.open(name, flags, 0o600))
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(handle.fileno(), 0o600)
        else:
            os.chmod(path, 0o600)
        return handle
    except BaseException:
        handle.close()
        raise


def try_acquire(path: str):
    """尝试独占锁；成功返回文件句柄（调用方持有期间不得关闭），失败返回 None。

    ``path`` 不存在时创建。父目录必须已存在（keeper 先 makedirs）。

    返回值语义：非 None 表示「本进程已持有锁」，必须保留引用直到进程退出或
    显式 close；被别处持有时返回 None。
    """
    # 不缓冲：锁的持有者是 fd，不是 Python 缓冲区。
    handle = _open_private(path)
    try:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                return None
            return handle
        if msvcrt is not None:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                handle.close()
                return None
            return handle
        handle.close()
        raise LockUnavailableError(
            f"当前平台 {sys.platform!r} 没有可用的文件锁实现")
    except BaseException:
        # 任何非「锁被别人持有」的异常都不能泄露句柄。
        if not handle.closed:
            handle.close()
        raise


def release(handle) -> None:
    """显式释放（正常路径其实靠进程退出/close 即可；测试需要确定性释放）。"""
    if handle is None:
        return
    try:
        if msvcrt is not None and fcntl is None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        try:
            handle.close()
        except OSError:
            pass
