#!/usr/bin/env python3
"""macOS 手动迁移事务。

多文件可恢复事务不是全局原子操作；文件身份或中断状态不能证明时保留现场。
调用者须停止新请求、停稳所有实例、禁自动入口并冻结主仓/W 编辑及 Git 命令。
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import ctypes
import datetime
import errno
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import signal
import stat
import subprocess
import sys
import unicodedata
from typing import Any


class SafetyError(RuntimeError):
    """面向操作者的非敏感、失败即阻断错误。"""


_CHILD_ACTIVE = False
_INTERRUPT_PENDING = False
_UNSPECIFIED = object()


def _run_without_signals(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    """子进程期间继续持锁；不使用 timeout 自动 kill，也不主动发信号。"""
    global _CHILD_ACTIVE, _INTERRUPT_PENDING
    if _INTERRUPT_PENDING:
        raise InterruptedError("interrupted")
    _CHILD_ACTIVE = True
    try:
        child = subprocess.Popen(command, env=_safe_env(), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
        stdout, stderr = child.communicate()
        result = subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
    finally:
        _CHILD_ACTIVE = False
    if _INTERRUPT_PENDING:
        _INTERRUPT_PENDING = False
        raise InterruptedError("interrupted")
    return result


def _marker(st: os.stat_result) -> tuple[int, ...]:
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid, st.st_nlink,
            st.st_size, st.st_mtime_ns, st.st_ctime_ns, getattr(st, "st_flags", 0))


def _dir_marker(st: os.stat_result) -> tuple[int, ...]:
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid,
            getattr(st, "st_flags", 0))


def _verify_darwin_local_no_acl(fd: int) -> None:
    """本地 APFS/HFS；文件不接受 ACL，目录仅接受稳定的 deny-only ACL。"""
    if sys.platform != "darwin" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise SafetyError("无法验证 macOS 本地文件系统")

    class StatFS(ctypes.Structure):
        _fields_ = [("bsize", ctypes.c_uint32), ("iosize", ctypes.c_int32)] + [
            (name, ctypes.c_uint64) for name in ("blocks", "bfree", "bavail", "files", "ffree")
        ] + [("fsid", ctypes.c_int32 * 2), ("owner", ctypes.c_uint32),
             ("type", ctypes.c_uint32), ("flags", ctypes.c_uint32),
             ("subtype", ctypes.c_uint32), ("fstype", ctypes.c_char * 16),
             ("mnton", ctypes.c_char * 1024), ("mntfrom", ctypes.c_char * 1024),
             ("flags_ext", ctypes.c_uint32), ("reserved", ctypes.c_uint32 * 7)]

    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        fs = libc.fstatfs64
        fs.argtypes = [ctypes.c_int, ctypes.POINTER(StatFS)]
        fs.restype = ctypes.c_int
        value = StatFS()
        if (fs(fd, ctypes.byref(value)) != 0 or not value.flags & 0x1000
                or value.flags & 0x00200000 or value.fstype not in (b"apfs", b"hfs")):
            raise SafetyError("文件系统非本地 APFS/HFS 或无法验证")
        signatures = {
            "acl_get_fd_np": ([ctypes.c_int, ctypes.c_int], ctypes.c_void_p),
            "acl_dup": ([ctypes.c_void_p], ctypes.c_void_p),
            "acl_valid": ([ctypes.c_void_p], ctypes.c_int),
            "acl_get_entry": ([ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)], ctypes.c_int),
            "acl_get_tag_type": ([ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)], ctypes.c_int),
            "acl_get_permset_mask_np": ([ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64)], ctypes.c_int),
            "acl_size": ([ctypes.c_void_p], ctypes.c_ssize_t),
            "acl_copy_ext": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t], ctypes.c_ssize_t),
            "acl_free": ([ctypes.c_void_p], ctypes.c_int),
        }
        for name, (argtypes, restype) in signatures.items():
            call = getattr(libc, name)
            call.argtypes, call.restype = argtypes, restype

        def external(acl: int) -> bytes:
            size = libc.acl_size(acl)
            if not 0 < size <= 65536:
                raise SafetyError("ACL 大小无法验证")
            buffer = ctypes.create_string_buffer(size)
            if libc.acl_copy_ext(buffer, acl, size) != size:
                raise SafetyError("ACL 无法复制核验")
            return buffer.raw

        def snapshot() -> bytes | None:
            ctypes.set_errno(0)
            acl = libc.acl_get_fd_np(fd, 0x100)
            if not acl:
                if ctypes.get_errno() == errno.ENOENT:
                    return None
                raise SafetyError("对象 ACL 无法验证")
            try:
                if not stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise SafetyError("文件带扩展 ACL；无法安全迁移")
                duplicate = libc.acl_dup(acl)
                if not duplicate:
                    raise SafetyError("ACL 无法复制核验")
                try:
                    if libc.acl_valid(duplicate) != 0:
                        raise SafetyError("ACL 无效")
                finally:
                    if libc.acl_free(duplicate) != 0:
                        raise SafetyError("ACL 资源释放状态不明")
                raw = external(acl)
                count, entry_index = 0, 0
                mask = sum(1 << bit for bit in range(1, 14)) | (1 << 20)
                while True:
                    entry = ctypes.c_void_p()
                    ctypes.set_errno(0)
                    result = libc.acl_get_entry(acl, entry_index, ctypes.byref(entry))
                    if result == -1 and entry_index == -1 and count and ctypes.get_errno() == errno.EINVAL:
                        break
                    if result != 0 or not entry.value or count >= 128:
                        raise SafetyError("ACL 条目无法枚举")
                    tag, permissions = ctypes.c_int(), ctypes.c_uint64()
                    if (libc.acl_get_tag_type(entry, ctypes.byref(tag)) != 0 or tag.value != 2
                            or libc.acl_get_permset_mask_np(entry, ctypes.byref(permissions)) != 0
                            or permissions.value & ~mask):
                        raise SafetyError("目录 ACL 不属于可证明的 deny-only 类型")
                    count += 1
                    entry_index = -1
                extra = ctypes.c_void_p()
                ctypes.set_errno(0)
                if libc.acl_get_entry(acl, count, ctypes.byref(extra)) != -1 or ctypes.get_errno() != errno.EINVAL:
                    raise SafetyError("ACL 条目无法交叉验证")
                if external(acl) != raw:
                    raise SafetyError("ACL 检查期间发生变化")
                return raw
            finally:
                if libc.acl_free(acl) != 0:
                    raise SafetyError("ACL 资源释放状态不明")

        before = os.fstat(fd)
        marker = _dir_marker if stat.S_ISDIR(before.st_mode) else _marker
        first = snapshot()
        middle = os.fstat(fd)
        second = snapshot()
        after = os.fstat(fd)
        if marker(before) != marker(middle) or marker(middle) != marker(after) or first != second:
            raise SafetyError("ACL 检查期间对象发生变化")
    except SafetyError:
        raise
    except (OSError, AttributeError, TypeError, ValueError):
        raise SafetyError("文件系统或 ACL 无法验证") from None


def _open_directory(path: Path, *, private: bool = False, sticky_ok: bool = False) -> int:
    """按路径及 fd 双重核验目录；调用方负责 close。"""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = -1
    try:
        before = path.lstat()
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if not stat.S_ISDIR(before.st_mode) or _dir_marker(before) != _dir_marker(opened):
            raise SafetyError("目录身份不明")
        if opened.st_uid not in (os.geteuid(), 0) or getattr(opened, "st_flags", 0):
            raise SafetyError("目录所有权或标志不安全")
        if private:
            if opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o700:
                raise SafetyError("私有目录必须由当前用户拥有且权限为 0700")
        elif opened.st_mode & 0o022:
            if not (sticky_ok and opened.st_uid == 0 and opened.st_mode & stat.S_ISVTX):
                raise SafetyError("目录可被其他用户写入")
        _verify_darwin_local_no_acl(fd)
        if _dir_marker(path.lstat()) != _dir_marker(os.fstat(fd)):
            raise SafetyError("目录检查期间发生变化")
        return fd
    except Exception:
        if fd >= 0:
            os.close(fd)
        raise


def _read_stable_file(path: Path, *, limit: int = 64 * 1024 * 1024,
                      expected_marker: tuple[int, ...] | None = None) -> tuple[bytes, tuple[int, ...]]:
    """读取普通文件并绑定 inode、元数据和直接父目录；不跟随末级链接。"""
    pfd = _open_directory(path.parent)
    fd = -1
    try:
        before_parent = _dir_marker(os.fstat(pfd))
        before = path.lstat()
        if expected_marker is not None and _marker(before) != tuple(expected_marker):
            raise SafetyError("对象在读取前发生变化")
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid not in (os.geteuid(), 0) or before.st_mode & 0o022
                or getattr(before, "st_flags", 0)):
            raise SafetyError("文件类型、所有权或权限不安全")
        if before.st_size > limit:
            raise SafetyError("配置、账本或 Git 文件超过 64 MiB 安全上限")
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=pfd)
        if _marker(before) != _marker(os.fstat(fd)):
            raise SafetyError("文件打开时发生变化")
        _verify_darwin_local_no_acl(fd)
        chunks: list[bytes] = []
        length = 0
        while True:
            block = os.read(fd, min(1024 * 1024, limit + 1 - length))
            if not block:
                break
            chunks.append(block)
            length += len(block)
            if length > limit:
                raise SafetyError("文件超过允许的大小")
        if (_marker(before) != _marker(os.fstat(fd))
                or _marker(before) != _marker(os.stat(path.name, dir_fd=pfd, follow_symlinks=False))
                or before_parent != _dir_marker(path.parent.lstat())):
            raise SafetyError("文件读取期间发生变化")
        return b"".join(chunks), _marker(before)
    except (OSError, OverflowError):
        raise SafetyError("文件读取或身份核验失败") from None
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(pfd)


class HeldLock:
    """持有 flock 直到调用者关闭；不截断、替换或移除锁文件。"""

    def __init__(self, path: Path, *, sticky_parent: bool = False, must_create: bool = False):
        self.path = path
        self.pfd = _open_directory(path.parent, sticky_ok=sticky_parent)
        self.fd = -1
        try:
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
            try:
                if must_create:
                    self.fd = os.open(path.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self.pfd)
                    os.fsync(self.pfd)
                else:
                    self.fd = os.open(path.name, flags, dir_fd=self.pfd)
            except FileNotFoundError:
                try:
                    self.fd = os.open(path.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self.pfd)
                    os.fsync(self.pfd)
                except FileExistsError:
                    raise SafetyError("创建锁时发生竞争") from None
            except FileExistsError:
                raise SafetyError("新锁位置已有对象") from None
            opened = os.fstat(self.fd)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or opened.st_uid != os.geteuid() or opened.st_mode & 0o022
                    or getattr(opened, "st_flags", 0)):
                raise SafetyError("锁文件身份或权限不安全")
            _verify_darwin_local_no_acl(self.fd)
            self.parent_id = _dir_marker(os.fstat(self.pfd))
            self.identity = (opened.st_dev, opened.st_ino)
            self.marker = _marker(opened)
            self.verify()
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EACCES):
                    raise SafetyError("相关锁被占用") from None
                raise SafetyError("相关锁无法核验") from None
            self.verify()
        except Exception:
            self.close()
            raise

    def verify(self) -> None:
        try:
            current = os.stat(self.path.name, dir_fd=self.pfd, follow_symlinks=False)
            fdst = os.fstat(self.fd)
            if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                    or current.st_uid != os.geteuid() or current.st_mode & 0o022
                    or getattr(current, "st_flags", 0)
                    or (current.st_dev, current.st_ino) != self.identity
                    or (fdst.st_dev, fdst.st_ino) != self.identity
                    or _marker(current) != self.marker or _marker(fdst) != self.marker
                    or _dir_marker(self.path.parent.lstat()) != self.parent_id):
                raise SafetyError("锁或其父目录身份发生变化")
            _verify_darwin_local_no_acl(self.fd)
            if (_marker(os.fstat(self.fd)) != self.marker
                    or _marker(os.stat(self.path.name, dir_fd=self.pfd, follow_symlinks=False)) != self.marker):
                raise SafetyError("锁在权限检查期间发生变化")
        except OSError:
            raise SafetyError("锁身份无法复核") from None

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        if self.pfd >= 0:
            os.close(self.pfd)
            self.pfd = -1

    def __enter__(self) -> "HeldLock":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def _safe_env() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_PAGER": "cat",
    }


def _git(root: Path, *args: str, allowed: tuple[int, ...] = (0,),
         before_launch: Any = None) -> bytes:
    """调用本机 Git，禁 hooks/fsmonitor/global config；不打印 Git 输出。"""
    command = ["/usr/bin/git", "-C", os.fspath(root), "-c", "core.hooksPath=/dev/null",
               "-c", "core.fsmonitor=false", "-c", "core.attributesFile=/dev/null",
               "-c", "core.pager=cat", "-c", "gc.auto=0", "-c", "maintenance.auto=false",
               "-c", "core.autocrlf=false", "-c", "core.eol=lf",
               *args]
    try:
        if before_launch is not None:
            before_launch()
        result = _run_without_signals(command)
    except OSError:
        raise SafetyError("Git 命令无法安全完成") from None
    if result.returncode not in allowed:
        raise SafetyError("Git 状态不满足要求或命令失败")
    return result.stdout


def _tree(root: Path, oid: str) -> dict[bytes, tuple[bytes, bytes]]:
    result: dict[bytes, tuple[bytes, bytes]] = {}
    for record in _git(root, "ls-tree", "-r", "-z", "--full-tree", oid).split(b"\0"):
        if not record:
            continue
        try:
            header, path = record.split(b"\t", 1)
            mode, kind, blob = header.split(b" ", 2)
        except ValueError:
            raise SafetyError("Git 树无法解析") from None
        if mode not in (b"100644", b"100755", b"120000") or kind != b"blob":
            raise SafetyError("Git 树含子模块或未知类型")
        if not path or b"\0" in path or path in result:
            raise SafetyError("Git 树路径不安全")
        result[path] = (mode, blob)
    return result


def _index(root: Path) -> dict[bytes, tuple[bytes, bytes]]:
    result: dict[bytes, tuple[bytes, bytes]] = {}
    for record in _git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        try:
            header, path = record.split(b"\t", 1)
            mode, blob, stage = header.split(b" ", 2)
        except ValueError:
            raise SafetyError("Git 索引无法解析") from None
        if stage != b"0" or path in result:
            raise SafetyError("Git 索引有冲突")
        result[path] = (mode, blob)
    for record in _git(root, "ls-files", "-v", "-z").split(b"\0"):
        if record and not record.startswith(b"H "):
            raise SafetyError("Git 索引存在跳过工作树或假定未更改条目")
    return result


def _git_blob_digest(data: bytes, blob: bytes) -> bytes:
    algorithm = hashlib.sha1 if len(blob) == 40 else hashlib.sha256 if len(blob) == 64 else None
    if algorithm is None:
        raise SafetyError("Git 对象摘要算法不受支持")
    digest = algorithm()
    digest.update(b"blob " + str(len(data)).encode("ascii") + b"\0")
    digest.update(data)
    return digest.hexdigest().encode("ascii")


def _alias_key(name: str) -> str:
    return unicodedata.normalize("NFD", name).casefold()


def _path_presence(root: Path, relative: bytes) -> Path | None:
    """仅检查变更路径及祖先；名称别名、符号链接祖先一律拒绝。"""
    parts = os.fsdecode(relative).split("/")
    if any(part in ("", ".", "..", ".git") for part in parts):
        raise SafetyError("Git 路径不安全")
    parent = root
    for index, part in enumerate(parts):
        try:
            before = _directory_identity(parent)
            with os.scandir(parent) as entries:
                names = [entry.name for entry in entries
                         if _alias_key(entry.name) == _alias_key(part)]
            if _directory_identity(parent) != before:
                raise SafetyError("变更路径父目录身份发生变化")
        except (OSError, UnicodeError):
            raise SafetyError("路径父目录无法检查") from None
        if len(names) > 1 or names and names[0] != part:
            raise SafetyError("发现大小写或 Unicode 路径别名")
        if not names:
            return None
        current = parent / part
        try:
            st = current.lstat()
        except OSError:
            raise SafetyError("路径检查时发生变化") from None
        if index < len(parts) - 1:
            if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
                raise SafetyError("变更路径祖先不是普通目录")
            parent = current
        else:
            return current
    return None


def check_changed_paths(root: Path, old_tree: dict[bytes, tuple[bytes, bytes]],
                        new_tree: dict[bytes, tuple[bytes, bytes]], *,
                        allowed_collision: bytes | None = None,
                        authorized_config: bytes | None = None) -> list[bytes]:
    """NUL 安全 Git 树输入的保守逐路径冲突检查，不修改文件。"""
    changed = sorted(p for p in old_tree.keys() | new_tree.keys()
                     if old_tree.get(p) != new_tree.get(p))
    for path in changed:
        if new_tree.get(path, (None,))[0] == b"120000":
            raise SafetyError("目标变更涉及符号链接，无法安全迁移")
        existing = _path_presence(root, path)
        old = old_tree.get(path)
        if old is None:
            if existing is not None and path != allowed_collision:
                raise SafetyError("目标路径与未跟踪或 ignored 对象冲突")
            if existing is not None:
                _read_stable_file(existing)
            continue
        if existing is None:
            raise SafetyError("变更涉及的 tracked 文件已缺失")
        if old[0] == b"120000":
            raise SafetyError("变更涉及符号链接，无法安全迁移")
        data, marker = _read_stable_file(existing)
        if path == authorized_config:
            if new_tree.get(path, (None,))[0] not in (b"100644", b"100755"):
                raise SafetyError("授权 tracked 配置的目标类型不安全")
            continue
        expected_mode = 0o755 if old[0] == b"100755" else 0o644
        if bool(marker[2] & 0o111) != bool(expected_mode & 0o111):
            raise SafetyError("变更涉及的 tracked 文件执行权限已变化")
        if _git_blob_digest(data, old[1]) != old[1]:
            raise SafetyError("变更涉及的 tracked 文件已修改")
    return changed


LEGACY_PATHS = {
    "log": ".model_proxy.log", "totals": ".model_proxy_totals.json",
    "lock": "/tmp/model_proxy.lock", "pid": "/tmp/model_proxy.pid",
    "ensure_log": "/tmp/model_proxy_ensure.log", "start_lock": "/tmp/model_proxy_start.lock",
}
NEW_PATHS = {
    "log": ".runtime/model_proxy.log", "totals": ".runtime/model_proxy_totals.json",
    "lock": ".runtime/model_proxy.lock", "pid": ".runtime/model_proxy.pid",
    "ensure_log": ".runtime/model_proxy_ensure.log", "start_lock": ".runtime/model_proxy_start.lock",
}
MINIMUM_TARGET = "4486c49b522293c137181caad229c363cc5e44fe"
CONFLICT_PATH = b"docs/designs/2026-11-12-model-proxy\xe7\xbb\x9f\xe4\xb8\x80\xe5\x86\x85\xe6\xa0\xb8\xe4\xb8\x8e\xe5\x8e\x9f\xe7\x94\x9fWindows-CLI\xe8\xae\xbe\xe8\xae\xa1.md"
DELIVERY = (b"tools/macos_switch.sh", b"tools/macos_rollback.sh", b"tools/macos_switch_impl.py")


def _tree_blob(root: Path, tree: dict[bytes, tuple[bytes, bytes]], path: bytes) -> bytes:
    entry = tree.get(path)
    if entry is None or entry[0] not in (b"100644", b"100755"):
        raise SafetyError("目标提交缺少必要文件")
    return _git(root, "cat-file", "blob", os.fsdecode(entry[1]))


def _decode_json(raw: bytes) -> dict[str, Any]:
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs,
                            parse_constant=_invalid_constant)
        if type(result) is not dict:
            raise ValueError()
        return result
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise SafetyError("JSON 无效或含重复字段") from None


def _directory_identity(path: Path) -> tuple[int, ...]:
    fd = _open_directory(path)
    try:
        return _dir_marker(os.fstat(fd))
    finally:
        os.close(fd)


def _git_scalar(root: Path, *args: str) -> str:
    try:
        result = _git(root, *args).decode("utf-8", "strict").strip()
    except UnicodeError:
        raise SafetyError("Git 身份无法解析") from None
    if not result or "\n" in result:
        raise SafetyError("Git 身份无法解析")
    return result


def _worktrees(root: Path) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for block in _git(root, "worktree", "list", "--porcelain", "-z").split(b"\0\0"):
        if not block:
            continue
        record: dict[str, str] = {}
        for line in block.split(b"\0"):
            if not line:
                continue
            key, sep, value = line.partition(b" ")
            name = os.fsdecode(key)
            if name in record:
                raise SafetyError("Git worktree 登记无法解析")
            record[name] = os.fsdecode(value) if sep else ""
        if "worktree" not in record or record["worktree"] in result:
            raise SafetyError("Git worktree 登记无法解析")
        result[record["worktree"]] = record
    return result


def _verify_no_git_inflight(root: Path, worktree: Path) -> tuple[str, tuple[int, ...]]:
    git_dir = root / ".git"
    admin = Path(_git_scalar(worktree, "rev-parse", "--path-format=absolute", "--git-dir")).resolve(strict=True)
    expected_parent = (git_dir / "worktrees").resolve(strict=True)
    if admin.parent != expected_parent:
        raise SafetyError("W 的 Git 管理目录不在共享 Git worktrees 中")
    admin_id = _directory_identity(admin)
    for path in (
        git_dir / "index.lock", git_dir / "HEAD.lock", git_dir / "packed-refs.lock",
        git_dir / "config.lock", git_dir / "shallow.lock", git_dir / "refs/heads/master.lock",
        git_dir / "MERGE_HEAD", git_dir / "CHERRY_PICK_HEAD", git_dir / "REVERT_HEAD",
        git_dir / "BISECT_LOG", git_dir / "rebase-merge", git_dir / "rebase-apply",
        git_dir / "sequencer", admin / "index.lock", admin / "HEAD.lock",
        admin / "MERGE_HEAD", admin / "CHERRY_PICK_HEAD", admin / "REVERT_HEAD",
        admin / "rebase-merge", admin / "rebase-apply", admin / "sequencer",
    ):
        if os.path.lexists(path):
            raise SafetyError("存在进行中的 Git 操作或锁")
    return os.fspath(admin), admin_id


def _no_git_filters(root: Path, trees: tuple[dict[bytes, tuple[bytes, bytes]], ...],
                    changed: list[bytes], common_dir: Path, git_dir: Path) -> None:
    if _git(root, "config", "--name-only", "--get-regexp", r"^filter\.", allowed=(0, 1)).strip():
        raise SafetyError("仓库配置了 checkout filter，无法安全执行 Git")
    sparse = _git(root, "config", "--get", "core.sparseCheckout", allowed=(0, 1)).strip().lower()
    if sparse and sparse not in (b"false", b"no", b"off", b"0"):
        raise SafetyError("不支持稀疏检出")
    if any(_alias_key(os.fsdecode(path).split("/")[-1]) == ".gitattributes"
           for tree in trees for path in tree):
        raise SafetyError("提交包含 Git attributes；本实现拒绝推断 filter 行为")
    for directory in (common_dir, git_dir):
        if os.path.lexists(directory / "info" / "attributes"):
            raise SafetyError("Git info/attributes 未经验证")
    checked: set[Path] = set()
    for path in changed:
        parent = root
        for part in os.fsdecode(path).split("/")[:-1]:
            parent = parent / part
            if parent in checked:
                continue
            checked.add(parent)
            if os.path.lexists(parent / ".gitattributes"):
                raise SafetyError("工作树存在未验证的 Git attributes")
    if os.path.lexists(root / ".gitattributes"):
        raise SafetyError("工作树存在未验证的 Git attributes")


def inspect_repository(root: Path, worktree: Path, target: str, *,
                       minimum_oid: str = MINIMUM_TARGET,
                       protected_roles: dict[str, Any] | None = None) -> dict[str, Any]:
    """只读的首次切换 Git 预检；不创建锁、目录或索引。"""
    try:
        root = root.resolve(strict=True)
        worktree = worktree.resolve(strict=True)
        if root == worktree or root not in worktree.parents:
            raise SafetyError("W 不是主仓内的嵌套 worktree")
        root_id = _directory_identity(root)
        work_id = _directory_identity(worktree)
        git_stat = (root / ".git").lstat()
        if not stat.S_ISDIR(git_stat.st_mode):
            raise SafetyError("主仓 .git 不是共享 Git 目录")
        git_id = _directory_identity(root / ".git")
        w_git = (worktree / ".git").lstat()
        if not stat.S_ISREG(w_git.st_mode) or w_git.st_nlink != 1:
            raise SafetyError("嵌套 worktree 的 Git 文件身份不明")
        if Path(_git_scalar(root, "rev-parse", "--show-toplevel")).resolve(strict=True) != root:
            raise SafetyError("主仓根与 Git 根不一致")
        if Path(_git_scalar(worktree, "rev-parse", "--show-toplevel")).resolve(strict=True) != worktree:
            raise SafetyError("W 根与 Git 根不一致")
        common = Path(_git_scalar(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)
        w_common = Path(_git_scalar(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)
        git_dir = Path(_git_scalar(root, "rev-parse", "--path-format=absolute", "--git-dir")).resolve(strict=True)
        if common != (root / ".git").resolve(strict=True) or w_common != common or git_dir != common:
            raise SafetyError("主仓和 W 不共享预期 Git 实体")
        w_admin, w_admin_id = _verify_no_git_inflight(root, worktree)
        if _git_scalar(root, "symbolic-ref", "-q", "HEAD") != "refs/heads/master":
            raise SafetyError("主仓当前分支不是 master")
        old = _git_scalar(root, "rev-parse", "--verify", "HEAD^{commit}")
        resolved = _git_scalar(root, "rev-parse", "--verify", target + "^{commit}")
        w_head = _git_scalar(worktree, "rev-parse", "--verify", "HEAD^{commit}")
        if target != resolved or target != w_head or target == old:
            raise SafetyError("目标必须是已验收的 W HEAD，且不同于旧 HEAD")
        _git(root, "merge-base", "--is-ancestor", old, target)
        _git(root, "merge-base", "--is-ancestor", minimum_oid, target)
        registrations = _worktrees(root)
        r = registrations.get(os.fspath(root))
        w = registrations.get(os.fspath(worktree))
        if not r or not w or r.get("HEAD") != old or w.get("HEAD") != target or r.get("branch") != "refs/heads/master":
            raise SafetyError("主仓/W 的 worktree 登记与提交不一致")
        old_tree, new_tree = _tree(root, old), _tree(root, target)
        for tree in (old_tree, new_tree):
            for path in tree:
                parts = [_alias_key(part) for part in os.fsdecode(path).split("/")]
                if (parts[0] in (".model_proxy.log", ".model_proxy_totals.json", ".runtime")):
                    raise SafetyError("Git 树包含受保护的运行日志或账本")
        if _index(root) != old_tree:
            raise SafetyError("Git 索引与旧提交不一致")
        old_ignore = old_tree.get(b".gitignore")
        if old_ignore is None or old_ignore[0] not in (b"100644", b"100755"):
            raise SafetyError("旧提交缺少可验证的根忽略文件")
        old_ignore_raw, old_ignore_id = _read_stable_file(root / ".gitignore")
        if _git_blob_digest(old_ignore_raw, old_ignore[1]) != old_ignore[1]:
            raise SafetyError("旧根忽略文件与提交不一致")
        ignored_old = _git(root, "check-ignore", "--no-index", "-v", "--",
                           ".model_proxy_totals.json", allowed=(0, 1))
        ignored_old_plain = _git(root, "check-ignore", "--no-index", "--",
                                 ".model_proxy_totals.json", allowed=(0, 1))
        if (not ignored_old.startswith(b".gitignore:")
                or not ignored_old.endswith(b"\t.model_proxy_totals.json\n")
                or ignored_old_plain != b".model_proxy_totals.json\n"
                or _read_stable_file(root / ".gitignore") != (old_ignore_raw, old_ignore_id)):
            raise SafetyError("旧账本未被旧提交的根忽略规则保护")
        target_ignore = new_tree.get(b".gitignore")
        if target_ignore is None or target_ignore[0] not in (b"100644", b"100755"):
            raise SafetyError("目标提交缺少可验证的根忽略文件")
        target_ignore_raw, target_ignore_id = _read_stable_file(worktree / ".gitignore")
        if _git_blob_digest(target_ignore_raw, target_ignore[1]) != target_ignore[1]:
            raise SafetyError("W 根忽略文件与目标提交不一致")
        target_old = _git(worktree, "check-ignore", "--no-index", "-v", "--",
                          ".model_proxy_totals.json", allowed=(0, 1))
        target_old_plain = _git(worktree, "check-ignore", "--no-index", "--",
                                ".model_proxy_totals.json", allowed=(0, 1))
        if (not target_old.startswith(b".gitignore:")
                or not target_old.endswith(b"\t.model_proxy_totals.json\n")
                or target_old_plain != b".model_proxy_totals.json\n"
                or _read_stable_file(worktree / ".gitignore") != (target_ignore_raw, target_ignore_id)):
            raise SafetyError("目标提交的根忽略规则未保护原旧账本")
        changed = sorted(p for p in old_tree.keys() | new_tree.keys()
                         if old_tree.get(p) != new_tree.get(p))
        if protected_roles is None:
            raise SafetyError("未先核验配置、sidecar 和凭据路径，拒绝读取变更文件")
        tracked_config = None
        config_path = Path(protected_roles["config_original"])
        if config_path == Path(protected_roles["config"]["target"]) and root in config_path.parents:
            relative = os.fsencode(os.fspath(config_path.relative_to(root)))
            if relative in changed:
                raise SafetyError("Git 版本变更涉及业务配置，不支持自动迁移")
        if tracked_config:
            tracked_keys = {tuple(_alias_key(part) for part in os.fsdecode(p).split("/"))
                            for p in old_tree.keys() | new_tree.keys()}
            for reference in protected_roles["protected_references"]:
                try:
                    ref = Path(reference).relative_to(root)
                except ValueError:
                    continue
                if tuple(_alias_key(part) for part in ref.parts) in tracked_keys:
                    raise SafetyError("凭据路径被 Git 跟踪，不能安全准备 tracked 配置切换")
        _protect_business_roles({"root": os.fspath(root), "worktree": os.fspath(worktree),
                                 "changed": [os.fsdecode(p) for p in changed],
                                 "tracked_config": os.fsdecode(tracked_config) if tracked_config else None},
                                protected_roles)
        _no_git_filters(root, (old_tree, new_tree), changed, common, git_dir)
        if (_decode_json(_tree_blob(root, old_tree, b"config/runtime_paths.json")) != LEGACY_PATHS
                or _decode_json(_tree_blob(root, new_tree, b"config/runtime_paths.json")) != NEW_PATHS):
            raise SafetyError("旧/新提交运行路径与规格不一致")
        for relative in DELIVERY:
            entry = new_tree.get(relative)
            if entry is None:
                raise SafetyError("目标提交未包含交付脚本")
            path = worktree / os.fsdecode(relative)
            raw, marker = _read_stable_file(path)
            if (_git_blob_digest(raw, entry[1]) != entry[1]
                    or bool(marker[2] & 0o111) != (entry[0] == b"100755")):
                raise SafetyError("W 执行脚本与已验收提交不一致")
        collision = None
        collision_initial = None
        if CONFLICT_PATH not in old_tree and CONFLICT_PATH in new_tree:
            existing = _path_presence(root, CONFLICT_PATH)
            if existing is not None:
                listed = _git(root, "ls-files", "--others", "--exclude-standard", "-z",
                              "--", os.fsdecode(CONFLICT_PATH)).split(b"\0")
                if CONFLICT_PATH not in listed:
                    raise SafetyError("唯一例外文档不是未跟踪且非 ignored 的普通文件")
                collision = CONFLICT_PATH
                collision_raw, collision_id = _read_stable_file(existing)
                collision_initial = {"identity": collision_id, "sha256": _sha256(collision_raw)}
        check_changed_paths(root, old_tree, new_tree, allowed_collision=collision,
                            authorized_config=tracked_config)
        if os.path.lexists(root / ".runtime"):
            raise SafetyError("首次切换时 .runtime 已存在，不能自动接管")
        if (_git_scalar(root, "rev-parse", "--verify", "HEAD^{commit}") != old
                or _git_scalar(worktree, "rev-parse", "--verify", "HEAD^{commit}") != target
                or _directory_identity(root) != root_id or _directory_identity(worktree) != work_id
                or _directory_identity(root / ".git") != git_id):
            raise SafetyError("预检期间 Git 或目录身份发生变化")
        return {"root": os.fspath(root), "worktree": os.fspath(worktree),
                "old_oid": old, "target_oid": target, "root_id": root_id,
                "worktree_id": work_id, "git_id": git_id, "w_git_id": _marker(w_git),
                "w_admin": w_admin, "w_admin_id": w_admin_id,
                "changed": [os.fsdecode(p) for p in changed],
                "collision": os.fsdecode(collision) if collision else None,
                "collision_initial": collision_initial,
                "tracked_config": os.fsdecode(tracked_config) if tracked_config else None}
    except (OSError, ValueError, UnicodeError):
        raise SafetyError("仓库身份或 Git 预检失败") from None


def _possible_model_proxy_entry(command: bytes) -> bool:
    """仅按可见命令筛选已知入口，不推断通用 server.py 的归属。"""
    command = command.lower()
    if any(entry in command for entry in
           (b"model_proxy.py", b"model_proxy_cli", b"core/server.py", b"core/cli/main.py")):
        return True
    if re.search(rb"""(?:^|\s)-m\s*["']?(?:core\.(?:server|cli\.main)|"""
                 rb"""[a-z0-9_.-]*model[_-]?proxy[a-z0-9_.-]*)["']?(?=\s|$)""", command):
        return True
    # ps 命令文本可能包含未转义的路径空格，不能把空格后的片段当作相对路径。
    return bool(re.search(rb"""model[_-]?proxy[^\n]*\.py(?=[\s"']|$)""", command))


def check_processes_and_port(port: int) -> None:
    """只读检查可见进程和监听；不能发现任意改名或自定义入口。"""
    if sys.platform != "darwin":
        raise SafetyError("仅支持 macOS 进程和端口检查")
    try:
        listener = _run_without_signals(
            ["/usr/sbin/lsof", "-nP", "-iTCP:" + str(port), "-sTCP:LISTEN", "-t"])
        if listener.returncode not in (0, 1) or listener.stderr:
            raise SafetyError("TCP 监听状态无法验证")
        if listener.stdout.strip():
            raise SafetyError("指定端口仍有监听")
        processes = _run_without_signals(["/bin/ps", "-ww", "-axo", "pid=,uid=,command="])
        if processes.returncode != 0 or processes.stderr:
            raise SafetyError("进程列表无法验证")
        for line in processes.stdout.splitlines():
            fields = line.strip().split(None, 2)
            if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
                raise SafetyError("进程列表存在无法解析的条目")
            pid = int(fields[0])
            if pid == os.getpid():
                continue
            if _possible_model_proxy_entry(fields[2]):
                raise SafetyError("发现可能的 model-proxy 入口进程")
    except SafetyError:
        raise
    except (OSError, ValueError):
        raise SafetyError("进程或端口状态无法验证") from None


def _config_path(root: Path, cwd: str, raw: str | None) -> Path:
    """按产品 Path(MODEL_PROXY_CONFIG) 语义还原原始路径，不展开 ~。"""
    if raw is None:
        return root / "config" / "model_proxy_config.json"
    if not raw or "\0" in raw or raw.startswith("~"):
        raise SafetyError("配置路径声明无效")
    path = Path(raw)
    if ".." in path.parts:
        raise SafetyError("配置路径含 ..，无法证明原始路径与 sidecar 语义")
    return Path(os.path.abspath(path if path.is_absolute() else Path(cwd) / path))


def _nofollow_file(path: Path, *, missing_ok: bool = False,
                   private: bool = False) -> tuple[tuple[int, ...] | None, list[list[Any]], tuple[int, ...]]:
    """仅 lstat 各层，不跟随链接、不打开或读取末级对象。"""
    if not path.is_absolute() or len(path.parts) < 2:
        raise SafetyError("对象路径无效")
    current = Path("/")
    chain: list[list[Any]] = []
    parts = path.parts[1:]
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if index != len(parts) - 1 or not missing_ok:
                raise SafetyError("对象或父目录不存在") from None
            parent_id = _directory_identity(path.parent)
            _verify_chain_nofollow(chain)
            if os.path.lexists(path):
                raise SafetyError("对象缺失状态发生变化")
            return None, chain, parent_id
        except OSError:
            raise SafetyError("对象路径无法检查") from None
        if stat.S_ISLNK(info.st_mode):
            raise SafetyError("不支持符号链接，拒绝读取或覆盖")
        if index != len(parts) - 1:
            if not stat.S_ISDIR(info.st_mode):
                raise SafetyError("对象父路径不是普通目录")
            chain.append(["directory", os.fspath(current), info.st_dev, info.st_ino,
                          info.st_mode, info.st_uid, info.st_gid])
            continue
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or info.st_mode & 0o022 or getattr(info, "st_flags", 0)):
            raise SafetyError("对象不是当前用户的安全单链接普通文件")
        if private and stat.S_IMODE(info.st_mode) != 0o600:
            raise SafetyError("业务配置、sidecar 和账本必须为 0600")
        if not private and not info.st_mode & 0o400:
            raise SafetyError("凭据文件权限不明")
        parent_id = _directory_identity(path.parent)
        _verify_chain_nofollow(chain)
        if _marker(path.lstat()) != _marker(info):
            raise SafetyError("对象身份在检查期间发生变化")
        return _marker(info), chain, parent_id
    raise SafetyError("对象路径无效")


def _role_collisions(roles: list[tuple[str, Path, tuple[int, ...] | None]]) -> None:
    """仅合并同路径、同身份的只读凭据引用；其他角色重叠一律拒绝。"""
    seen: list[tuple[str, Path, tuple[str, ...], tuple[int, ...] | None]] = []
    for name, path, identity in roles:
        key = tuple(_alias_key(part) for part in path.parts)
        duplicate = False
        for other, other_path, other_key, other_id in seen:
            if name == other == "appkey_file" and path == other_path:
                if identity is None or other_id is None or identity != other_id:
                    raise SafetyError("重复 appkey_file 观察到的身份不一致")
                duplicate = True
                continue
            if (key[:len(other_key)] == other_key or other_key[:len(key)] == key
                    or identity is not None and other_id is not None
                    and identity[:2] == other_id[:2]):
                raise SafetyError("业务对象或 appkey_file 角色重叠")
        if not duplicate:
            seen.append((name, path, key, identity))


def _default_roles(root: Path, *, config_missing: bool = False,
                   ledger_missing: bool = False) -> tuple[list[tuple[str, Path, tuple[int, ...] | None]], dict[str, Any]]:
    """限定默认配置、同目录 sidecar 和旧账本；不读取任何内容。"""
    root = Path(root)
    config = root / "config" / "model_proxy_config.json"
    sidecar = root / "config" / "session_overrides.json"
    ledger = root / ".model_proxy_totals.json"
    result = []
    evidence = {}
    for name, path, missing in (("config", config, config_missing),
                                ("sidecar", sidecar, True),
                                ("ledger", ledger, ledger_missing)):
        identity, chain, parent_id = _nofollow_file(path, missing_ok=missing, private=True)
        result.append((name, path, identity))
        evidence[name] = {"id": identity, "chain": chain, "parent_id": parent_id}
    _role_collisions(result)
    return result, evidence


def _config_credentials(config: dict[str, Any], cwd: str, home: str) -> list[Path]:
    supplies = config.get("supplies")
    if type(supplies) is not list:
        raise SafetyError("配置供应列表无法验证")
    references = []
    for supply in supplies:
        if type(supply) is not dict:
            raise SafetyError("配置供应列表无法验证")
        value = supply.get("appkey_file")
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise SafetyError("appkey_file 引用无效")
        references.append(_appkey_path(value, cwd, home))
    return references


def _read_checked_default(path: Path, expected: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """角色排重后，才读取默认普通文件并复核 lstat 身份。"""
    identity = expected["id"]
    if identity is None:
        now, chain, parent_id = _nofollow_file(path, missing_ok=True, private=True)
        if now is not None or chain != expected["chain"] or parent_id != expected["parent_id"]:
            raise SafetyError("对象缺失状态变化")
        return None, {"exists": False, "parent_id": parent_id,
                      "parent_target": os.fspath(path.parent), "parent_chain": chain[:-1]}
    raw, marker = _read_stable_file(path, expected_marker=identity)
    now, chain, parent_id = _nofollow_file(path, private=True)
    if marker != identity or now != identity or chain != expected["chain"] or parent_id != expected["parent_id"]:
        raise SafetyError("默认业务文件读取期间发生变化")
    return _decode_json(raw), {"exists": True, "parent_id": parent_id, "path_id": marker,
                               "target": os.fspath(path), "target_id": marker,
                               "target_parent_id": parent_id, "link_chain": chain}


def _trace_links(path: Path) -> tuple[str, list[list[Any]]]:
    """只用 lstat/readlink 追踪各层链接与目录身份；不打开目标内容。"""
    if not path.is_absolute():
        raise SafetyError("路径不是绝对路径")
    pending = list(path.parts[1:])
    current = Path("/")
    chain: list[list[Any]] = []
    links = 0
    steps = 0
    while pending:
        steps += 1
        if steps > 4096:
            raise SafetyError("链接链过长")
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        candidate = current / part
        before = candidate.lstat()
        if stat.S_ISLNK(before.st_mode):
            links += 1
            if links > 40:
                raise SafetyError("链接链过长")
            value = os.readlink(candidate)
            if _marker(candidate.lstat()) != _marker(before) or os.readlink(candidate) != value:
                raise SafetyError("链接检查期间发生变化")
            chain.append(["link", os.fspath(candidate), before.st_dev, before.st_ino,
                          before.st_mode, before.st_uid, before.st_gid, value])
            target = Path(value)
            if target.is_absolute():
                current = Path("/")
                pending = list(target.parts[1:]) + pending
            else:
                pending = list(target.parts) + pending
        else:
            if pending:
                if not stat.S_ISDIR(before.st_mode):
                    raise SafetyError("链接链的祖先不是目录")
                chain.append(["directory", os.fspath(candidate), before.st_dev, before.st_ino,
                              before.st_mode, before.st_uid, before.st_gid])
            current = candidate
    return os.fspath(current), chain


def _read_linked_json(path: Path, *, absent_ok: bool) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """读取配置或 sidecar；保留原始路径，并验证末级链接及目标身份。"""
    parent_id = _directory_identity(path.parent)
    try:
        link_before = path.lstat()
    except FileNotFoundError:
        if not absent_ok:
            raise SafetyError("业务配置不存在") from None
        if _directory_identity(path.parent) != parent_id or os.path.lexists(path):
            raise SafetyError("sidecar 缺失状态发生变化")
        parent_target, parent_chain = _trace_links(path.parent)
        if (_directory_identity(path.parent) != parent_id or os.path.lexists(path)
                or _trace_links(path.parent) != (parent_target, parent_chain)):
            raise SafetyError("sidecar 父目录身份发生变化")
        return None, {"exists": False, "parent_id": parent_id,
                      "parent_target": parent_target, "parent_chain": parent_chain}
    except OSError:
        raise SafetyError("配置或 sidecar 路径无法核验") from None
    if not (stat.S_ISREG(link_before.st_mode) or stat.S_ISLNK(link_before.st_mode)):
        raise SafetyError("配置或 sidecar 不是普通文件/链接")
    try:
        target_text, chain = _trace_links(path)
        target = Path(target_text)
        target_parent_id = _directory_identity(target.parent)
        raw, target_id = _read_stable_file(target)
        data = _decode_json(raw)
        if (_marker(path.lstat()) != _marker(link_before) or _trace_links(path) != (target_text, chain)
                or _directory_identity(path.parent) != parent_id
                or _directory_identity(target.parent) != target_parent_id):
            raise SafetyError("配置或 sidecar 读取期间发生变化")
        return data, {"exists": True, "parent_id": parent_id, "path_id": _marker(link_before),
                      "target": os.fspath(target), "target_id": target_id,
                      "target_parent_id": target_parent_id, "link_chain": chain}
    except (OSError, RuntimeError):
        raise SafetyError("配置或 sidecar 链接身份不明") from None


def _appkey_path(ref: str, cwd: str, home: str) -> Path:
    if not ref or "\0" in ref:
        raise SafetyError("appkey_file 引用无效")
    if ".." in Path(ref).parts:
        raise SafetyError("appkey_file 含 ..，无法安全确认路径")
    if ref == "~":
        expanded = home
    elif ref.startswith("~/"):
        expanded = os.path.join(home, ref[2:])
    elif ref.startswith("~"):
        # ~other 的解析依赖账户数据库，不从当前 HOME 推断。
        raise SafetyError("暂不支持 ~用户名 格式的 appkey_file")
    else:
        expanded = ref
    return Path(os.path.abspath(expanded if os.path.isabs(expanded) else os.path.join(cwd, expanded)))


def _check_sidecar(data: dict[str, Any] | None) -> None:
    if data is None:
        return
    for client, sessions in data.items():
        if not isinstance(client, str) or not client or type(sessions) is not dict:
            raise SafetyError("sidecar 与新旧版本兼容性无法证明")
        for session, entry in sessions.items():
            if not isinstance(session, str) or not session:
                raise SafetyError("sidecar 与新旧版本兼容性无法证明")
            route = entry if isinstance(entry, str) else entry.get("route_id") if type(entry) is dict else None
            if not isinstance(route, str) or not route:
                raise SafetyError("sidecar 与新旧版本兼容性无法证明")


def _listen(config: dict[str, Any], env_port: str | None) -> tuple[str, int]:
    if env_port is not None:
        try:
            supplied_port = _port(env_port)
        except argparse.ArgumentTypeError:
            raise SafetyError("MODEL_PROXY_PORT 声明无效") from None
    else:
        supplied_port = None
    listen = config.get("listen")
    if listen is None:
        host, port = "127.0.0.1", supplied_port if supplied_port is not None else 18889
    elif type(listen) is dict:
        host, value = listen.get("host"), listen.get("port")
        if not isinstance(host, str) or not host.strip() or isinstance(value, bool):
            raise SafetyError("监听配置无效")
        try:
            port = _port(str(value))
        except argparse.ArgumentTypeError:
            raise SafetyError("监听配置无效") from None
        host = host.strip()
    else:
        raise SafetyError("监听配置无效")
    try:
        if host.lower() != "localhost":
            ipaddress.ip_address(host)
    except ValueError:
        raise SafetyError("监听地址不是可证明的数字地址或 localhost") from None
    allow = config.get("allow_hosts")
    if allow is not None and (type(allow) is not list
                              or any(not isinstance(x, str) or not x.strip() for x in allow)):
        raise SafetyError("监听白名单无效")
    if host.lower() not in ("127.0.0.1", "localhost", "::1") and not allow:
        raise SafetyError("非本机监听缺少白名单")
    return host, port


def inspect_context(root: Path, *, old_cwd: str, new_cwd: str, old_home: str, new_home: str,
                    config_raw: str | None, old_env_port: str | None,
                    new_env_port: str | None, declared_port: int) -> dict[str, Any]:
    """先确认默认角色和凭据不重叠，再读取 sidecar；不打开凭据文件。"""
    contexts = [old_cwd, new_cwd, old_home, new_home]
    ids = []
    for item in contexts:
        if not os.path.isabs(item) or ".." in Path(item).parts:
            raise SafetyError("启动上下文必须是明确且不含 .. 的绝对路径")
        ids.append(_directory_identity(Path(item)))
    old_config = _config_path(root, old_cwd, config_raw)
    new_config = _config_path(root, new_cwd, config_raw)
    if old_config != new_config:
        raise SafetyError("新旧启动上下文的原始配置路径不同")
    if old_config != root / "config" / "model_proxy_config.json":
        raise SafetyError("仅支持项目默认业务配置路径")
    roles, role_info = _default_roles(root)
    config, config_evidence = _read_checked_default(old_config, role_info["config"])
    if config is None:
        raise SafetyError("业务配置不存在")
    old_refs = _config_credentials(config, old_cwd, old_home)
    new_refs = _config_credentials(config, new_cwd, new_home)
    if old_refs != new_refs:
        raise SafetyError("appkey_file 在新旧上下文中展开结果不同")
    references = []
    protected_references = []
    for ref in old_refs:
        identity, chain, parent_id = _nofollow_file(ref)
        if identity is None:
            raise SafetyError("appkey_file 目标无法证明")
        roles.append(("appkey_file", ref, identity))
        references.append({"reference": os.fspath(ref), "entry_id": identity,
                           "target_id": identity, "target": os.fspath(ref),
                           "target_parent_id": parent_id, "link_chain": chain})
        protected_references.extend((os.fspath(ref), os.fspath(ref)))
    _role_collisions(roles)
    # 角色校验完成后再次 lstat；只有证据不变才读取 sidecar。
    current_roles, current_info = _default_roles(root)
    if current_info != role_info:
        raise SafetyError("默认业务对象在角色校验期间发生变化")
    for ref, recorded in zip(old_refs, references):
        identity, chain, parent_id = _nofollow_file(ref)
        if (identity != recorded["target_id"] or chain != recorded["link_chain"]
                or parent_id != recorded["target_parent_id"]):
            raise SafetyError("appkey_file 身份在角色校验期间发生变化")
    sidecar_path = root / "config" / "session_overrides.json"
    sidecar, sidecar_evidence = _read_checked_default(sidecar_path, role_info["sidecar"])
    _check_sidecar(sidecar)
    old_listen = _listen(config, old_env_port)
    new_listen = _listen(config, new_env_port)
    if old_listen[1] != declared_port:
        raise SafetyError("声明端口与实际配置/启动环境解析结果不一致")
    if [_directory_identity(Path(item)) for item in contexts] != ids:
        raise SafetyError("启动上下文目录身份发生变化")
    return {"config_original": os.fspath(old_config), "sidecar_original": os.fspath(sidecar_path),
            "config": config_evidence, "sidecar": sidecar_evidence,
            "context_ids": ids, "appkey_identities": references,
            "protected_references": protected_references,
            "old_listen": old_listen, "new_listen": new_listen}


def _protect_business_roles(repo: dict[str, Any], context: dict[str, Any]) -> None:
    """Git 变更不得命中原始/解析后配置、sidecar 或 appkey_file。"""
    root = Path(repo["root"])
    paths = [(context["config_original"], "config"), (context["sidecar_original"], "sidecar"),
             (context["config"]["target"], "config")]
    if context["sidecar"].get("exists"):
        paths.append((context["sidecar"]["target"], "sidecar"))
    paths.extend((path, "credential") for path in context["protected_references"])

    def components(path: Path) -> tuple[str, ...]:
        return tuple(_alias_key(part) for part in path.parts)

    protected = [(components(Path(path)), role) for path, role in paths]
    if "worktree" in repo:
        for relative in DELIVERY:
            delivery = components(Path(repo["worktree"]) / os.fsdecode(relative))
            for reference in context["protected_references"]:
                key = components(Path(reference))
                if delivery[:len(key)] == key or key[:len(delivery)] == delivery:
                    raise SafetyError("凭据路径与迁移脚本冲突")
    for changed in repo["changed"]:
        actual = components(root / changed)
        for role, kind in protected:
            if (kind == "config" and repo.get("tracked_config") == changed
                    and actual == role):
                continue
            if actual[:len(role)] == role or role[:len(actual)] == actual:
                raise SafetyError("Git 变更与配置、sidecar 或凭据路径冲突")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise SafetyError("文件写入未完成")
        view = view[written:]


def _new_private_file(directory: Path, name: str, raw: bytes, *,
                      private_directory: bool = True) -> tuple[int, ...]:
    """在已验证目录独占写入 0600 文件、同步、核验；绝不覆盖既有文件。"""
    if "/" in name or name in ("", ".", ".."):
        raise SafetyError("私有文件名无效")
    parent = _open_directory(directory, private=private_directory)
    fd = -1
    try:
        parent_id = _dir_marker(os.fstat(parent))
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=parent)
        os.fchmod(fd, 0o600)
        _write_all(fd, raw)
        os.fsync(fd)
        st = os.fstat(fd)
        _verify_darwin_local_no_acl(fd)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (_marker(st) != _marker(current) or not stat.S_ISREG(st.st_mode)
                or st.st_nlink != 1 or st.st_uid != os.geteuid()
                or stat.S_IMODE(st.st_mode) != 0o600 or st.st_size != len(raw)
                or _dir_marker(directory.lstat()) != parent_id):
            raise SafetyError("私有文件写入结果不明")
        os.fsync(parent)
        readback, after = _read_stable_file(directory / name)
        if after != _marker(st) or readback != raw:
            raise SafetyError("私有文件后验不一致")
        return after
    except OSError:
        raise SafetyError("私有文件无法独占创建或同步") from None
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent)


def _create_private_directory(parent_path: Path, name: str) -> Path:
    if "/" in name or name in ("", ".", ".."):
        raise SafetyError("私有目录名无效")
    parent = _open_directory(parent_path)
    try:
        parent_id = _dir_marker(os.fstat(parent))
        os.mkdir(name, 0o700, dir_fd=parent)
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        try:
            if (os.fstat(fd).st_uid != os.geteuid()
                    or stat.S_IMODE(os.fstat(fd).st_mode) != 0o700):
                raise SafetyError("新目录所有权或权限不明")
            created = _dir_marker(os.fstat(fd))
        finally:
            os.close(fd)
        if _dir_marker(parent_path.lstat()) != parent_id:
            raise SafetyError("创建目录时父目录发生变化")
        result = parent_path / name
        fd = _open_directory(result, private=True)
        try:
            if _dir_marker(os.fstat(fd)) != created:
                raise SafetyError("新目录身份不明")
        finally:
            os.close(fd)
        os.fsync(parent)
        return result
    except OSError:
        raise SafetyError("私有目录无法独占创建") from None
    finally:
        os.close(parent)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _file_xattrs(fd: int) -> dict[str, str]:
    """读取受保护文件的扩展属性；结果只可存入 0600 私有元数据。"""
    if sys.platform != "darwin":
        raise SafetyError("扩展属性无法验证")
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        listing, get = libc.flistxattr, libc.fgetxattr
        listing.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        listing.restype = ctypes.c_ssize_t
        get.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                        ctypes.c_uint32, ctypes.c_int]
        get.restype = ctypes.c_ssize_t
        size = listing(fd, None, 0, 0)
        if size < 0 or size > 1024 * 1024:
            raise SafetyError("扩展属性列表无法验证")
        if not size:
            return {}
        buf = ctypes.create_string_buffer(size)
        if listing(fd, buf, size, 0) != size or not buf.raw.endswith(b"\0"):
            raise SafetyError("扩展属性列表发生变化")
        names = buf.raw[:-1].split(b"\0")
        if len(names) > 256 or len(set(names)) != len(names) or any(not n for n in names):
            raise SafetyError("扩展属性列表无效")
        result = {}
        for name in names:
            length = get(fd, name, None, 0, 0, 0)
            if length < 0 or length > 1024 * 1024:
                raise SafetyError("扩展属性无法验证")
            value = ctypes.create_string_buffer(max(length, 1))
            if get(fd, name, value, length, 0, 0) != length:
                raise SafetyError("扩展属性读取时发生变化")
            result[base64.b64encode(name).decode("ascii")] = base64.b64encode(value.raw[:length]).decode("ascii")
        return result
    except SafetyError:
        raise
    except (OSError, AttributeError, TypeError, ValueError):
        raise SafetyError("扩展属性无法验证") from None


def _stable_xattrs(path: Path, expected: tuple[int, ...]) -> dict[str, str]:
    pfd = _open_directory(path.parent)
    fd = -1
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=pfd)
        if _marker(os.fstat(fd)) != expected:
            raise SafetyError("扩展属性目标发生变化")
        _verify_darwin_local_no_acl(fd)
        first, second = _file_xattrs(fd), _file_xattrs(fd)
        if (first != second or _marker(os.fstat(fd)) != expected
                or _marker(os.stat(path.name, dir_fd=pfd, follow_symlinks=False)) != expected):
            raise SafetyError("扩展属性发生变化")
        return first
    except OSError:
        raise SafetyError("扩展属性目标无法验证") from None
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(pfd)


def _set_xattrs(fd: int, expected: dict[str, str]) -> None:
    """只对本事务新建的暂存 inode 操作，失败不覆盖目标。"""
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        remove, put = libc.fremovexattr, libc.fsetxattr
        remove.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        remove.restype = ctypes.c_int
        put.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                        ctypes.c_uint32, ctypes.c_int]
        put.restype = ctypes.c_int
        for encoded in _file_xattrs(fd):
            if encoded not in expected:
                name = base64.b64decode(encoded, validate=True)
                if remove(fd, name, 0) != 0:
                    raise SafetyError("无法恢复原扩展属性")
        for encoded, value in expected.items():
            name = base64.b64decode(encoded, validate=True)
            raw = base64.b64decode(value, validate=True)
            buf = ctypes.create_string_buffer(raw, max(len(raw), 1))
            if put(fd, name, buf, len(raw), 0, 0) != 0:
                raise SafetyError("无法恢复原扩展属性")
        if _file_xattrs(fd) != expected:
            raise SafetyError("扩展属性后验不一致")
    except SafetyError:
        raise
    except (OSError, AttributeError, TypeError, ValueError):
        raise SafetyError("扩展属性恢复失败") from None


def _rename_exclusive(source_dir: Path, source: str, dest_dir: Path, dest: str) -> None:
    """Darwin renameatx_np(RENAME_EXCL)：已存在目标时绝不覆盖。"""
    if any("/" in name or name in ("", ".", "..") for name in (source, dest)):
        raise SafetyError("重命名路径无效")
    srcfd = _open_directory(source_dir)
    dstfd = _open_directory(dest_dir)
    moved = False
    try:
        if os.fstat(srcfd).st_dev != os.fstat(dstfd).st_dev:
            raise SafetyError("保全位置跨文件系统，无法原子迁出")
        try:
            libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
            func = libc.renameatx_np
            func.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            func.restype = ctypes.c_int
            if func(srcfd, os.fsencode(source), dstfd, os.fsencode(dest), 0x00000004) != 0:
                raise SafetyError("无覆盖重命名失败或目标已存在")
            moved = True
            os.fsync(srcfd)
            if _dir_marker(os.fstat(dstfd)) != _dir_marker(os.fstat(srcfd)):
                os.fsync(dstfd)
        except (OSError, AttributeError, TypeError):
            raise SafetyError("重命名结果 UNKNOWN" if moved else "无覆盖重命名原语不可用") from None
    finally:
        os.close(srcfd)
        os.close(dstfd)


class Manifest:
    """私有事务清单：每次状态变更均为同步后的原子替换。"""

    def __init__(self, directory: Path, data: dict[str, Any], identity: tuple[int, ...]):
        self.directory = directory
        self.data = data
        self.identity = identity
        self.dir_id = _directory_identity(directory)

    @classmethod
    def create(cls, base: Path, payload: dict[str, Any], *, on_allocated: Any = None) -> "Manifest":
        txid = secrets.token_hex(16)
        if on_allocated is not None:
            on_allocated(txid, base / txid)
        directory = _create_private_directory(base, txid)
        data = {"format": 1, "transaction": txid, "stage": "PRECHECK", "events": [], **payload}
        raw = cls._encode(data)
        identity = _new_private_file(directory, "manifest.json", raw)
        return cls(directory, data, identity)

    @classmethod
    def load(cls, base: Path, txid: str) -> "Manifest":
        if re.fullmatch(r"[0-9a-f]{32}", txid) is None:
            raise SafetyError("事务 ID 格式不受支持")
        directory = base / txid
        fd = _open_directory(directory, private=True)
        os.close(fd)
        raw, identity = _read_stable_file(directory / "manifest.json")
        data = _decode_json(raw)
        if (data.get("format") != 1 or data.get("transaction") != txid
                or type(data.get("events")) is not list or not isinstance(data.get("stage"), str)):
            raise SafetyError("事务记录无效")
        if stat.S_IMODE(identity[2]) != 0o600:
            raise SafetyError("事务记录权限不安全")
        return cls(directory, data, identity)

    @staticmethod
    def _encode(data: dict[str, Any]) -> bytes:
        return (json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()

    def update(self, data: dict[str, Any]) -> None:
        if _directory_identity(self.directory) != self.dir_id:
            raise SafetyError("事务目录身份发生变化")
        current, marker = _read_stable_file(self.directory / "manifest.json")
        if marker != self.identity or current != self._encode(self.data):
            raise SafetyError("事务记录发生变化，状态 UNKNOWN")
        name = ".manifest-" + secrets.token_hex(16)
        candidate = _new_private_file(self.directory, name, self._encode(data))
        current2, marker2 = _read_stable_file(self.directory / "manifest.json")
        if marker2 != self.identity or current2 != current:
            raise SafetyError("事务记录提交前发生变化，状态 UNKNOWN")
        parent = _open_directory(self.directory, private=True)
        try:
            if _dir_marker(os.fstat(parent)) != self.dir_id:
                raise SafetyError("事务目录发生变化，状态 UNKNOWN")
            if _marker(os.stat(name, dir_fd=parent, follow_symlinks=False)) != candidate:
                raise SafetyError("事务暂存文件发生变化，状态 UNKNOWN")
            os.replace(name, "manifest.json", src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        except OSError:
            raise SafetyError("事务记录提交结果 UNKNOWN") from None
        finally:
            os.close(parent)
        observed, identity = _read_stable_file(self.directory / "manifest.json")
        if (identity[0:2] != candidate[0:2] or observed != self._encode(data)):
            raise SafetyError("事务记录后验 UNKNOWN")
        self.data = data
        self.identity = identity

    def intent(self, action: str, evidence: dict[str, Any]) -> None:
        data = dict(self.data)
        data["events"] = [*self.data["events"], {"action": action, "state": "INTENT", **evidence}]
        self.update(data)

    def done(self, action: str, evidence: dict[str, Any], *, stage: str | None = None) -> None:
        if (not self.data["events"] or self.data["events"][-1].get("action") != action
                or self.data["events"][-1].get("state") != "INTENT"):
            raise SafetyError("事务步骤与 intent 不匹配")
        data = dict(self.data)
        data["events"] = [*self.data["events"][:-1],
                          {**self.data["events"][-1], **evidence, "state": "DONE"}]
        if stage:
            data["stage"] = stage
        self.update(data)


def _same_object(marker: tuple[int, ...] | list[int], other: tuple[int, ...] | list[int]) -> bool:
    return tuple(marker[:2]) == tuple(other[:2])


def _hash_stable_file(path: Path) -> tuple[str, tuple[int, ...]]:
    """流式摘要；日志可大于内存快照的 64 MiB 上限。"""
    parent = _open_directory(path.parent)
    fd = -1
    try:
        parent_id = _dir_marker(os.fstat(parent))
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid not in (os.geteuid(), 0) or before.st_mode & 0o022
                or getattr(before, "st_flags", 0)):
            raise SafetyError("日志或备份身份、权限不安全")
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        if _marker(os.fstat(fd)) != _marker(before):
            raise SafetyError("日志或备份打开时发生变化")
        _verify_darwin_local_no_acl(fd)
        digest = hashlib.sha256()
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        if (_marker(os.fstat(fd)) != _marker(before)
                or _marker(os.stat(path.name, dir_fd=parent, follow_symlinks=False)) != _marker(before)
                or _dir_marker(path.parent.lstat()) != parent_id):
            raise SafetyError("日志或备份读取期间发生变化")
        return digest.hexdigest(), _marker(before)
    except OSError:
        raise SafetyError("日志或备份无法验证") from None
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent)


def _backup_log(tx: Manifest, source: Path) -> dict[str, Any]:
    try:
        source.lstat()
    except FileNotFoundError:
        parent_id = _directory_identity(source.parent)
        tx.intent("BACKUP_OLD_LOG", {"source": os.fspath(source), "source_missing": True,
                                     "parent_id": parent_id})
        if os.path.lexists(source) or _directory_identity(source.parent) != parent_id:
            raise SafetyError("旧日志缺失状态发生变化")
        tx.done("BACKUP_OLD_LOG", {"result": "MISSING"})
        return {"missing": True, "parent_id": parent_id}
    except OSError:
        raise SafetyError("旧日志无法检查") from None
    digest, source_id = _hash_stable_file(source)
    parent_id = _directory_identity(source.parent)
    name = "old-log.backup"
    tx.intent("BACKUP_OLD_LOG", {"source": os.fspath(source), "source_id": source_id,
                                  "parent_id": parent_id, "sha256": digest,
                                  "size": source_id[6], "backup": name})
    src_parent = _open_directory(source.parent)
    dst_parent = -1
    src = dst = -1
    try:
        dst_parent = _open_directory(tx.directory, private=True)
        if _dir_marker(os.fstat(src_parent)) != parent_id:
            raise SafetyError("旧日志父目录发生变化")
        src = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=src_parent)
        if _marker(os.fstat(src)) != source_id:
            raise SafetyError("旧日志备份前发生变化")
        dst = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                      0o600, dir_fd=dst_parent)
        os.fchmod(dst, 0o600)
        copied = hashlib.sha256()
        size = 0
        while True:
            block = os.read(src, 1024 * 1024)
            if not block:
                break
            _write_all(dst, block)
            copied.update(block)
            size += len(block)
        os.fsync(dst)
        backup_id = _marker(os.fstat(dst))
        if (copied.hexdigest() != digest or size != source_id[6]
                or _marker(os.fstat(src)) != source_id
                or _marker(os.stat(source.name, dir_fd=src_parent, follow_symlinks=False)) != source_id
                or _marker(os.stat(name, dir_fd=dst_parent, follow_symlinks=False)) != backup_id
                or not stat.S_ISREG(backup_id[2]) or backup_id[3] != os.geteuid()
                or backup_id[5] != 1 or stat.S_IMODE(backup_id[2]) != 0o600):
            raise SafetyError("旧日志备份结果不明")
        _verify_darwin_local_no_acl(dst)
        os.fsync(dst_parent)
    except OSError:
        raise SafetyError("旧日志备份未完成，保留现场") from None
    finally:
        if src >= 0:
            os.close(src)
        if dst >= 0:
            os.close(dst)
        os.close(src_parent)
        if dst_parent >= 0:
            os.close(dst_parent)
    saved_hash, saved_id = _hash_stable_file(tx.directory / name)
    current_hash, current_id = _hash_stable_file(source)
    if saved_hash != digest or saved_id != backup_id or current_hash != digest or current_id != source_id:
        raise SafetyError("旧日志备份或原文件后验不一致")
    tx.done("BACKUP_OLD_LOG", {"backup_id": backup_id})
    return {"source_id": source_id, "parent_id": parent_id, "sha256": digest,
            "size": source_id[6], "backup": name, "backup_id": backup_id}


def _verify_log_source(source: Path, evidence: dict[str, Any]) -> None:
    if evidence.get("missing") or evidence.get("source_missing"):
        if os.path.lexists(source) or _directory_identity(source.parent) != tuple(evidence["parent_id"]):
            raise SafetyError("旧日志缺失状态发生变化")
        return
    digest, marker = _hash_stable_file(source)
    if (marker != tuple(evidence["source_id"]) or digest != evidence["sha256"]
            or _directory_identity(source.parent) != tuple(evidence["parent_id"])):
        raise SafetyError("已保全旧日志发生变化")


def _copy_to_backup(tx: Manifest, action: str, source: Path, backup_name: str, *,
                    ledger: bool = False, missing_ok: bool = False) -> dict[str, Any]:
    try:
        source.lstat()
    except FileNotFoundError:
        if not missing_ok:
            raise SafetyError("必须保全的源文件不存在") from None
        parent_id = _directory_identity(source.parent)
        tx.intent(action, {"source": os.fspath(source), "source_missing": True,
                           "parent_id": parent_id})
        if os.path.lexists(source) or _directory_identity(source.parent) != parent_id:
            raise SafetyError("源文件缺失状态发生变化")
        tx.done(action, {"result": "MISSING"})
        return {"missing": True, "parent_id": parent_id}
    except OSError:
        raise SafetyError("源文件无法检查") from None
    raw, source_id = _read_stable_file(source)
    if ledger:
        validate_ledger_bytes(raw)
    digest = _sha256(raw)
    parent_id = _directory_identity(source.parent)
    tx.intent(action, {"source": os.fspath(source), "source_id": source_id,
                       "parent_id": parent_id, "sha256": digest,
                       "size": len(raw), "backup": backup_name})
    backup_id = _new_private_file(tx.directory, backup_name, raw)
    after, after_id = _read_stable_file(source)
    if after_id != source_id or after != raw or _directory_identity(source.parent) != parent_id:
        raise SafetyError("保全期间源文件发生变化，保留事务现场")
    saved, saved_id = _read_stable_file(tx.directory / backup_name)
    if saved != raw or saved_id != backup_id:
        raise SafetyError("备份后验不一致")
    tx.done(action, {"backup_id": backup_id})
    return {"source_id": source_id, "parent_id": parent_id, "sha256": digest,
            "size": len(raw), "backup": backup_name, "backup_id": backup_id}


def _observe_snapshot_role(path: Path, *, missing_ok: bool,
                           expected_marker: Any = _UNSPECIFIED) -> tuple[bytes | None, dict[str, Any]]:
    """仅支持不含符号链接、当前用户 0600 单链接的普通业务文件。"""
    identity, chain, parent_id = _nofollow_file(path, missing_ok=missing_ok, private=True)
    if expected_marker is not _UNSPECIFIED and identity != expected_marker:
        raise SafetyError("业务对象在读取前发生变化")
    parent_target = os.fspath(path.parent)
    parent_chain = chain[:-1]
    if identity is None:
        return None, {"path": os.fspath(path), "exists": False, "parent_id": parent_id,
                      "parent_target": parent_target, "parent_chain": parent_chain}
    raw, current = _read_stable_file(path, expected_marker=identity)
    if current != identity:
        raise SafetyError("业务对象快照期间发生变化")
    attrs = _stable_xattrs(path, identity)
    after, after_chain, after_parent = _nofollow_file(path, private=True)
    if after != identity or after_chain != chain or after_parent != parent_id:
        raise SafetyError("业务对象快照期间身份发生变化")
    return raw, {"path": os.fspath(path), "exists": True, "parent_id": parent_id,
                 "parent_target": parent_target, "parent_chain": parent_chain,
                 "entry_id": identity, "target": os.fspath(path), "target_id": identity,
                 "target_parent_id": parent_id, "chain": chain, "link_attributes": [],
                 "xattrs": attrs, "acl": "none"}


def _check_snapshot_roles(metadata: list[dict[str, Any]], context: dict[str, Any]) -> None:
    """不可将同一对象同时授权为配置、sidecar、账本或外部凭据。"""
    def key(path: str) -> tuple[str, ...]:
        return tuple(_alias_key(part) for part in Path(path).parts)
    roles: list[set[tuple[str, ...]]] = []
    for item in metadata:
        paths = {key(item["path"])}
        if item["exists"]:
            paths.add(key(item["target"]))
        roles.append(paths)
    protected = [key(path) for path in context["protected_references"]]
    for i, paths in enumerate(roles):
        for path in paths:
            for other in roles[i + 1:]:
                if any(path[:len(p)] == p or p[:len(path)] == path for p in other):
                    raise SafetyError("业务快照角色发生重叠")
            if any(path[:len(p)] == p or p[:len(path)] == path for p in protected):
                raise SafetyError("业务快照角色与 appkey_file 凭据重叠")


def _recheck_switch_roles(root: Path, context: dict[str, Any]) -> None:
    """快照前只用 lstat 复核所有角色，先排重，再允许读取 sidecar。"""
    roles, info = _default_roles(root)
    for binding in context["appkey_identities"]:
        ref = Path(binding["reference"])
        identity, chain, parent_id = _nofollow_file(ref)
        if (identity != tuple(binding["target_id"]) or chain != binding["link_chain"]
                or parent_id != tuple(binding["target_parent_id"])):
            raise SafetyError("appkey_file 身份发生变化")
        roles.append(("appkey_file", ref, identity))
    _role_collisions(roles)
    for name in ("config", "sidecar"):
        expected = context[name]
        current = info[name]["id"]
        if bool(current is not None) != expected["exists"]:
            raise SafetyError("默认业务对象存在性发生变化")
        if current is not None and current != tuple(expected["target_id"]):
            raise SafetyError("默认业务对象身份发生变化")


def _snapshot_role(tx: Manifest, name: str, path: Path, *, missing_ok: bool,
                   expected_marker: Any = _UNSPECIFIED) -> None:
    raw, metadata = _observe_snapshot_role(path, missing_ok=missing_ok, expected_marker=expected_marker)
    if name == "ledger":
        if raw is None:
            raise SafetyError("旧账本不存在")
        validate_ledger_bytes(raw)
    action = "SNAPSHOT_" + name.upper()
    meta_raw = Manifest._encode(metadata)
    tx.intent(action, {"path": os.fspath(path), "exists": raw is not None,
                       "snapshot": name + ".snapshot" if raw is not None else None,
                       "metadata": name + ".metadata", "sha256": _sha256(raw) if raw is not None else None,
                       "metadata_sha256": _sha256(meta_raw)})
    backup_id = _new_private_file(tx.directory, name + ".snapshot", raw) if raw is not None else None
    meta_id = _new_private_file(tx.directory, name + ".metadata", meta_raw)
    current, current_meta = _observe_snapshot_role(path, missing_ok=missing_ok,
                                                   expected_marker=expected_marker)
    if current != raw or Manifest._encode(current_meta) != meta_raw:
        raise SafetyError("业务对象快照期间发生变化；保留事务现场")
    if backup_id is not None:
        verified, identity = _read_stable_file(tx.directory / (name + ".snapshot"))
        if verified != raw or identity != backup_id:
            raise SafetyError("业务快照后验不一致")
    verified_meta, identity = _read_stable_file(tx.directory / (name + ".metadata"))
    if verified_meta != meta_raw or identity != meta_id:
        raise SafetyError("业务快照元数据后验不一致")
    tx.done(action, {"snapshot_id": backup_id, "metadata_id": meta_id})


def _load_snapshot_metadata(tx: Manifest, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    event = _find_event(tx, "SNAPSHOT_" + name.upper())
    if event is None or event.get("metadata") != name + ".metadata":
        raise SafetyError("业务快照记录缺失")
    meta_raw, meta_id = _read_stable_file(tx.directory / (name + ".metadata"))
    if tuple(event["metadata_id"]) != meta_id or _sha256(meta_raw) != event["metadata_sha256"]:
        raise SafetyError("业务快照元数据不一致")
    metadata = _decode_json(meta_raw)
    if bool(metadata.get("exists")) != event["exists"] or metadata.get("path") != event["path"]:
        raise SafetyError("业务快照元数据无效")
    return event, metadata


def _load_snapshot_role(tx: Manifest, name: str) -> tuple[bytes | None, dict[str, Any]]:
    event, metadata = _load_snapshot_metadata(tx, name)
    if not event["exists"]:
        return None, metadata
    if event.get("snapshot") != name + ".snapshot":
        raise SafetyError("业务快照文件名无效")
    raw, identity = _read_stable_file(tx.directory / (name + ".snapshot"))
    if tuple(event["snapshot_id"]) != identity or _sha256(raw) != event["sha256"]:
        raise SafetyError("业务快照文件不一致")
    if name == "ledger":
        validate_ledger_bytes(raw)
    return raw, metadata


def _chain_semantics(chain: list[list[Any]]) -> list[list[Any]]:
    # 保留原链接 inode 及元数据，不接管中途重建的未知链接。
    result = []
    for entry in chain:
        if entry[0] == "link":
            result.append(entry)
        elif entry[0] == "directory":
            result.append(entry)
        else:
            raise SafetyError("路径链身份无效")
    return result


def _verify_chain_nofollow(chain: list[list[Any]]) -> None:
    """按记录顺序核对祖先，不沿已变化的链接探测第三方目标。"""
    for entry in chain:
        try:
            if entry[0] not in ("directory", "link") or len(entry) != (7 if entry[0] == "directory" else 8):
                raise SafetyError("业务对象路径链记录无效")
            path = Path(entry[1])
            current = path.lstat()
            if ((current.st_dev, current.st_ino, current.st_mode, current.st_uid, current.st_gid)
                    != tuple(entry[2:7])):
                raise SafetyError("业务对象路径链身份发生变化")
            if entry[0] == "directory":
                if not stat.S_ISDIR(current.st_mode):
                    raise SafetyError("业务对象祖先不是原目录")
            elif not stat.S_ISLNK(current.st_mode) or os.readlink(path) != entry[7]:
                raise SafetyError("业务对象链接发生变化")
        except OSError:
            raise SafetyError("业务对象路径链无法验证") from None


def _authorized_role_current(metadata: dict[str, Any], *,
                             expected_marker: Any = _UNSPECIFIED,
                             expected_inode: tuple[int, int] | None = None) -> tuple[bytes | None, dict[str, Any]]:
    """仅处理原默认普通文件；新链接、硬链接和未知新增 sidecar 直接拒绝。"""
    path = Path(metadata["path"])
    identity, chain, parent_id = _nofollow_file(path, missing_ok=True, private=True)
    if expected_marker is not _UNSPECIFIED and identity != expected_marker:
        raise SafetyError("业务对象在读取前发生变化")
    if expected_inode is not None and (identity is None or identity[:2] != expected_inode):
        raise SafetyError("业务对象结果身份不一致")
    if (parent_id != tuple(metadata["parent_id"])
            or os.fspath(path.parent) != metadata["parent_target"]
            or chain[:-1] != metadata["parent_chain"]):
        raise SafetyError("业务对象父目录身份发生变化")
    if not metadata["exists"]:
        if identity is not None:
            raise SafetyError("原 sidecar 不存在，新对象来源未证明；不读取、不删除")
        return None, {"exists": False}
    if (not stat.S_ISREG(metadata["entry_id"][2])
            or metadata["target"] != os.fspath(path)
            or metadata.get("link_attributes") != []):
        raise SafetyError("原业务对象不是受支持的默认普通文件")
    if identity is None:
        return None, {"exists": False, "parent_id": metadata["parent_id"]}
    if tuple(identity[3:5]) != tuple(metadata["target_id"][3:5]):
        raise SafetyError("业务对象目标所有权变化")
    raw, current = _observe_snapshot_role(path, missing_ok=False, expected_marker=identity)
    if raw is None or tuple(current["target_id"]) != identity:
        raise SafetyError("业务对象状态发生变化")
    return raw, current


def _restore_role(tx: Manifest, name: str, raw: bytes | None, metadata: dict[str, Any], *,
                  expected_marker: Any = _UNSPECIFIED) -> None:
    """按用户授权恢复一个业务角色；每次替换前后重验对象及父目录。"""
    path = Path(metadata["path"])
    current, observation = _authorized_role_current(metadata, expected_marker=expected_marker)
    if raw is None:
        if name != "sidecar" or metadata["exists"]:
            raise SafetyError("快照不存在状态无效")
        if current is not None:
            raise SafetyError("原 sidecar 不存在，新对象来源未证明；不删除")
        _checkpoint(tx, "RESTORE_SIDECAR_ABSENT", stage=tx.data["stage"])
        return
    if not metadata["exists"]:
        raise SafetyError("业务对象快照与当前状态不一致")
    target = Path(metadata["target"])
    tmp = ".snapshot-" + tx.data["transaction"] + "-" + name
    parent_id = _directory_identity(target.parent)
    if stat.S_IMODE(metadata["target_id"][2]) != 0o600:
        raise SafetyError("旧业务文件不是 0600，拒绝恢复")
    tx.intent("STAGE_" + name.upper() + "_SNAPSHOT", {
        "candidate": tmp, "parent": os.fspath(tx.directory), "parent_id": tx.dir_id,
        "target_parent_id": parent_id,
        "snapshot_sha256": _sha256(raw), "target": os.fspath(target),
    })
    # 敏感字节始终在仓库外 0700 事务目录以 0600 暂存，即使中断亦不进入 Git。
    candidate = _new_private_file(tx.directory, tmp, raw)
    pfd = _open_directory(tx.directory, private=True)
    fd = -1
    try:
        fd = os.open(tmp, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=pfd)
        if _marker(os.fstat(fd)) != candidate:
            raise SafetyError("快照暂存身份发生变化")
        expected = metadata["target_id"]
        if (os.fstat(fd).st_uid, os.fstat(fd).st_gid) != (expected[3], expected[4]):
            os.fchown(fd, expected[3], expected[4])
        os.fchmod(fd, 0o600)
        _set_xattrs(fd, metadata["xattrs"])
        _verify_darwin_local_no_acl(fd)
        os.fsync(fd)
        candidate = _marker(os.fstat(fd))
        if (_marker(os.stat(tmp, dir_fd=pfd, follow_symlinks=False)) != candidate
                or _dir_marker(os.fstat(pfd)) != tx.dir_id
                or (candidate[3], candidate[4], stat.S_IMODE(candidate[2]))
                != (expected[3], expected[4], 0o600)):
            raise SafetyError("快照暂存元数据不一致")
        os.fsync(pfd)
    except OSError:
        raise SafetyError("快照暂存元数据无法恢复") from None
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(pfd)
    staged_raw, staged_id = _read_stable_file(tx.directory / tmp)
    if staged_raw != raw or staged_id != candidate or _stable_xattrs(tx.directory / tmp, candidate) != metadata["xattrs"]:
        raise SafetyError("快照暂存后验不一致")
    tx.done("STAGE_" + name.upper() + "_SNAPSHOT", {"candidate_id": candidate})
    again, after = _authorized_role_current(metadata, expected_marker=expected_marker)
    if again != current or Manifest._encode(after) != Manifest._encode(observation):
        raise SafetyError("业务对象在恢复前发生变化")
    tx.intent("RESTORE_" + name.upper() + "_SNAPSHOT", {
        "target": os.fspath(target), "parent_id": parent_id, "candidate": tmp,
        "candidate_parent": os.fspath(tx.directory), "candidate_parent_id": tx.dir_id,
        "candidate_id": candidate, "candidate_sha256": _sha256(raw),
        "before_id": observation.get("target_id"),
        "before_sha256": _sha256(current) if current is not None else None,
        "operation": "replace" if current is not None else "rename_exclusive",
    })
    again, after = _authorized_role_current(metadata, expected_marker=expected_marker)
    if again != current or Manifest._encode(after) != Manifest._encode(observation):
        raise SafetyError("业务对象提交前发生变化")
    if current is None:
        if os.path.lexists(target):
            raise SafetyError("缺失业务对象的目标已出现，不覆盖")
        _rename_exclusive(tx.directory, tmp, target.parent, target.name)
    else:
        srcfd = _open_directory(tx.directory, private=True)
        dstfd = -1
        try:
            dstfd = _open_directory(target.parent)
            if (_dir_marker(os.fstat(srcfd)) != tx.dir_id
                    or _dir_marker(os.fstat(dstfd)) != tuple(parent_id)
                    or os.fstat(srcfd).st_dev != os.fstat(dstfd).st_dev
                    or _marker(os.stat(target.name, dir_fd=dstfd, follow_symlinks=False))
                    != tuple(observation["target_id"])
                    or _marker(os.stat(tmp, dir_fd=srcfd, follow_symlinks=False)) != candidate):
                raise SafetyError("业务对象或暂存文件提交前发生变化")
            os.replace(tmp, target.name, src_dir_fd=srcfd, dst_dir_fd=dstfd)
            os.fsync(srcfd)
            os.fsync(dstfd)
        except OSError:
            raise SafetyError("业务快照替换结果 UNKNOWN") from None
        finally:
            if dstfd >= 0:
                os.close(dstfd)
            os.close(srcfd)
    final, final_meta = _authorized_role_current(metadata, expected_inode=tuple(candidate[:2]))
    if (final != raw or not _same_object(final_meta["target_id"], candidate)
            or stat.S_IMODE(final_meta["target_id"][2]) != stat.S_IMODE(metadata["target_id"][2])
            or final_meta["xattrs"] != metadata["xattrs"]):
        raise SafetyError("业务快照恢复后验 UNKNOWN")
    tx.done("RESTORE_" + name.upper() + "_SNAPSHOT", {"result_id": final_meta["target_id"]})


def _create_runtime(tx: Manifest, root: Path) -> Path:
    parent_id = _directory_identity(root)
    if os.path.lexists(root / ".runtime"):
        raise SafetyError(".runtime 已存在，不能自动接管")
    stage_name = "runtime.staged"
    tx.intent("STAGE_RUNTIME", {"name": stage_name, "txn_parent_id": tx.dir_id})
    staged = _create_private_directory(tx.directory, stage_name)
    stage_id = _directory_identity(staged)
    tx.done("STAGE_RUNTIME", {"stage_id": stage_id})
    # 先在仓库外的私有目录建立保护，再整体迁入；任一中断点都不会
    # 在主仓留下已有账本但尚无忽略规则的运行目录。
    tx.intent("CREATE_RUNTIME_IGNORE", {"name": ".gitignore", "runtime_id": stage_id})
    ignore_id = _new_private_file(staged, ".gitignore", b"*\n")
    tx.done("CREATE_RUNTIME_IGNORE", {"identity": ignore_id})
    tx.intent("CREATE_RUNTIME", {"parent_id": parent_id, "path": ".runtime",
                                 "stage": stage_name, "stage_id": stage_id,
                                 "txn_parent_id": tx.dir_id})
    ignore_raw, ignore_now = _read_stable_file(staged / ".gitignore", expected_marker=ignore_id)
    if (_directory_identity(staged) != stage_id or _directory_identity(root) != parent_id
            or ignore_raw != b"*\n" or ignore_now != ignore_id
            or os.path.lexists(root / ".runtime")):
        raise SafetyError("运行目录提交前身份发生变化")
    _rename_exclusive(tx.directory, stage_name, root, ".runtime")
    runtime = root / ".runtime"
    fd = _open_directory(runtime, private=True)
    try:
        identity = _dir_marker(os.fstat(fd))
    finally:
        os.close(fd)
    if _directory_identity(root) != parent_id or identity != stage_id:
        raise SafetyError("运行目录父目录发生变化")
    tx.done("CREATE_RUNTIME", {"runtime_id": identity})
    return runtime


def _verify_target_runtime_ignore(tx: Manifest, runtime: Path) -> None:
    """必须由目标提交的根 .gitignore 忽略账本，不依赖运行目录哨兵。"""
    repo = tx.data["repository"]
    root = Path(repo["root"])
    created = _find_event(tx, "CREATE_RUNTIME")
    if (runtime != root / ".runtime" or created is None
            or _directory_identity(root) != tuple(repo["root_id"])
            or _directory_identity(root / ".git") != tuple(repo["git_id"])
            or _directory_identity(runtime) != tuple(created["runtime_id"])):
        raise SafetyError("运行目录或 Git 身份不明")
    target = repo["target_oid"]
    if _git_scalar(root, "rev-parse", "--verify", "HEAD^{commit}") != target:
        raise SafetyError("只有 Git 已前进到目标提交才能写入新账本")
    tree = _tree(root, target)
    ignore = tree.get(b".gitignore")
    if ignore is None or ignore[0] not in (b"100644", b"100755") or _index(root) != tree:
        raise SafetyError("目标提交的 Git 索引或根忽略文件不受支持")
    content, identity = _read_stable_file(root / ".gitignore")
    if _git_blob_digest(content, ignore[1]) != ignore[1]:
        raise SafetyError("根忽略文件不是目标提交的内容")
    txid = tx.data["transaction"]
    if re.fullmatch(r"[0-9a-f]{32}", txid) is None:
        raise SafetyError("事务 ID 格式无效")
    # 目录自身必须被目标根规则忽略，否则目录内 .gitignore 仍可反向覆盖文件规则。
    paths = [".runtime/", ".runtime/.ledger-" + txid + ".tmp",
             ".runtime/model_proxy_totals.json"]
    if _git(root, "ls-files", "-z", "--", ".runtime"):
        raise SafetyError("运行目录含已跟踪文件")
    plain = _git(root, "check-ignore", "--no-index", "--", *paths, allowed=(0, 1))
    if plain != b"".join(os.fsencode(path) + b"\n" for path in paths):
        raise SafetyError("目标提交未实际忽略全部运行账本路径")
    result = _git(root, "check-ignore", "--no-index", "-v", "--", *paths)
    lines = result.splitlines()
    if (len(lines) != len(paths) or any(
            not line.startswith(b".gitignore:") or not line.endswith(b"\t" + os.fsencode(path))
            for line, path in zip(lines, paths))):
        raise SafetyError("目标提交的根忽略文件未独立保护全部新账本路径")
    content2, identity2 = _read_stable_file(root / ".gitignore")
    if (content2 != content or identity2 != identity
            or _directory_identity(runtime) != tuple(created["runtime_id"])
            or _directory_identity(root) != tuple(repo["root_id"])
            or _directory_identity(root / ".git") != tuple(repo["git_id"])
            or _git_scalar(root, "rev-parse", "--verify", "HEAD^{commit}") != target
            or _index(root) != tree):
        raise SafetyError("运行目录忽略检查期间身份发生变化")


def _stage_ledger(tx: Manifest, directory: Path, source: Path, *, name: str,
                  action: str, private_directory: bool) -> dict[str, Any]:
    raw, source_id = _read_stable_file(source)
    validate_ledger_bytes(raw)
    parent_id = _directory_identity(directory)
    digest = _sha256(raw)
    tx.intent(action, {"source": os.fspath(source), "source_id": source_id,
                       "parent": os.fspath(directory), "parent_id": parent_id,
                       "candidate": name, "sha256": digest, "size": len(raw)})
    if (action != "STAGE_NEW_LEDGER" or name != ".ledger-" + tx.data["transaction"] + ".tmp"):
        raise SafetyError("账本暂存路径不受支持")
    _verify_target_runtime_ignore(tx, directory)
    candidate_id = _new_private_file(directory, name, raw, private_directory=private_directory)
    after, after_id = _read_stable_file(source)
    if after_id != source_id or after != raw or _directory_identity(directory) != parent_id:
        raise SafetyError("账本暂存期间源文件或父目录发生变化")
    tx.done(action, {"candidate_id": candidate_id})
    return {"candidate": name, "candidate_id": candidate_id, "parent_id": parent_id,
            "source_id": source_id, "sha256": digest, "size": len(raw)}


def _install_new_ledger(tx: Manifest, runtime: Path, staged: dict[str, Any]) -> None:
    candidate = runtime / staged["candidate"]
    raw, marker = _read_stable_file(candidate)
    if (tuple(staged["candidate_id"]) != marker or _sha256(raw) != staged["sha256"]
            or _directory_identity(runtime) != tuple(staged["parent_id"])
            or os.path.lexists(runtime / "model_proxy_totals.json")):
        raise SafetyError("新账本安装目标或暂存对象发生变化")
    tx.intent("INSTALL_NEW_LEDGER", {**staged, "target": "model_proxy_totals.json",
                                    "operation": "rename_exclusive"})
    # 记录 intent 后重新检查，期间不依赖检查与操作是全局原子。
    raw2, marker2 = _read_stable_file(candidate)
    if raw2 != raw or marker2 != marker or os.path.lexists(runtime / "model_proxy_totals.json"):
        raise SafetyError("新账本提交前发生变化")
    _verify_target_runtime_ignore(tx, runtime)
    _rename_exclusive(runtime, staged["candidate"], runtime, "model_proxy_totals.json")
    installed, identity = _read_stable_file(runtime / "model_proxy_totals.json")
    if installed != raw or not _same_object(identity, marker):
        raise SafetyError("新账本提交后状态 UNKNOWN")
    tx.done("INSTALL_NEW_LEDGER", {"result_id": identity}, stage="LEDGER_INSTALLED")


def _move_conflict(tx: Manifest, root: Path, backup: dict[str, Any]) -> None:
    source = root / os.fsdecode(CONFLICT_PATH)
    raw, identity = _read_stable_file(source)
    if (identity != tuple(backup["source_id"]) or _sha256(raw) != backup["sha256"]
            or _directory_identity(source.parent) != tuple(backup["parent_id"])
            or os.path.lexists(tx.directory / "conflict.original")):
        raise SafetyError("撞名文档源或保留位置发生变化")
    # 防止 reset --keep 删除已空的原始父目录：保留一个本事务独占、
    # 可核验的哨兵。恢复完成后仅删除这个已证明的自建对象。
    sentinel = ".model-proxy-migration-" + tx.data["transaction"]
    tx.intent("PIN_CONFLICT_PARENT", {"parent_id": backup["parent_id"], "name": sentinel})
    sentinel_id = _new_private_file(source.parent, sentinel, b"", private_directory=False)
    if _directory_identity(source.parent) != tuple(backup["parent_id"]):
        raise SafetyError("撞名文档父目录身份发生变化")
    tx.done("PIN_CONFLICT_PARENT", {"sentinel_id": sentinel_id})
    mode = stat.S_IMODE(identity[2])
    tx.intent("MOVE_CONFLICT", {
        "source": os.fsdecode(CONFLICT_PATH), "source_id": identity,
        "parent_id": backup["parent_id"], "sha256": backup["sha256"],
        "original_mode": mode, "destination": "conflict.original",
        "operation": "rename_exclusive_then_private_mode",
    })
    current, current_id = _read_stable_file(source)
    if current != raw or current_id != identity or os.path.lexists(tx.directory / "conflict.original"):
        raise SafetyError("撞名文档提交前发生变化")
    _rename_exclusive(source.parent, source.name, tx.directory, "conflict.original")
    moved = tx.directory / "conflict.original"
    moved_raw, moved_id = _read_stable_file(moved)
    if moved_raw != raw or not _same_object(moved_id, identity):
        raise SafetyError("撞名文档迁出结果 UNKNOWN")
    fd = os.open(moved, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if not _same_object(_marker(os.fstat(fd)), moved_id):
            raise SafetyError("撞名文档身份发生变化")
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    finally:
        os.close(fd)
    final, final_id = _read_stable_file(moved)
    if final != raw or not _same_object(final_id, identity) or stat.S_IMODE(final_id[2]) != 0o600:
        raise SafetyError("撞名文档保全后验 UNKNOWN")
    tx.done("MOVE_CONFLICT", {"result_id": final_id}, stage="CONFLICT_MOVED")


def _restore_conflict(tx: Manifest, root: Path, event: dict[str, Any]) -> None:
    if event.get("action") != "MOVE_CONFLICT" or event.get("state") != "DONE":
        raise SafetyError("撞名文档保全记录未知")
    source = tx.directory / "conflict.original"
    target = root / os.fsdecode(CONFLICT_PATH)
    raw, identity = _read_stable_file(source)
    if (not _same_object(identity, event["source_id"]) or _sha256(raw) != event["sha256"]
            or _directory_identity(target.parent) != tuple(event["parent_id"])
            or os.path.lexists(target)):
        raise SafetyError("撞名文档或恢复目标发生变化")
    tx.intent("RESTORE_CONFLICT", {
        "operation": "restore_mode_then_rename_exclusive", "source_id": identity,
        "sha256": event["sha256"], "target_parent_id": event["parent_id"],
        "original_mode": event["original_mode"], "target": os.fsdecode(CONFLICT_PATH),
    })
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if not _same_object(_marker(os.fstat(fd)), identity):
            raise SafetyError("撞名文档身份发生变化")
        os.fchmod(fd, event["original_mode"])
        os.fsync(fd)
    finally:
        os.close(fd)
    prepared, prepared_id = _read_stable_file(source)
    if prepared != raw or not _same_object(prepared_id, identity) or os.path.lexists(target):
        raise SafetyError("撞名文档恢复前发生变化")
    _rename_exclusive(tx.directory, source.name, target.parent, target.name)
    restored, result_id = _read_stable_file(target)
    if (restored != raw or not _same_object(result_id, identity)
            or stat.S_IMODE(result_id[2]) != event["original_mode"]):
        raise SafetyError("撞名文档恢复结果 UNKNOWN")
    tx.done("RESTORE_CONFLICT", {"result_id": result_id})


def _release_conflict_parent(tx: Manifest, root: Path, event: dict[str, Any]) -> None:
    if event.get("action") != "PIN_CONFLICT_PARENT" or event.get("state") != "DONE":
        raise SafetyError("父目录哨兵记录未知")
    parent = (root / os.fsdecode(CONFLICT_PATH)).parent
    sentinel = parent / event["name"]
    raw, identity = _read_stable_file(sentinel)
    if (raw != b"" or identity != tuple(event["sentinel_id"])
            or _directory_identity(parent) != tuple(event["parent_id"])):
        raise SafetyError("父目录哨兵发生变化，不删除")
    tx.intent("RELEASE_CONFLICT_PARENT", {
        "name": event["name"], "sentinel_id": identity, "parent_id": event["parent_id"],
    })
    fd = _open_directory(parent)
    try:
        if (_dir_marker(os.fstat(fd)) != tuple(event["parent_id"])
                or _marker(os.stat(event["name"], dir_fd=fd, follow_symlinks=False)) != identity):
            raise SafetyError("父目录哨兵提交前发生变化，不删除")
        os.unlink(event["name"], dir_fd=fd)
        os.fsync(fd)
    except OSError:
        raise SafetyError("父目录哨兵清理结果 UNKNOWN") from None
    finally:
        os.close(fd)
    tx.done("RELEASE_CONFLICT_PARENT", {})


def _verify_changed_tree(root: Path, expected: dict[bytes, tuple[bytes, bytes]],
                         changed: list[bytes]) -> None:
    for path in changed:
        entry = expected.get(path)
        existing = _path_presence(root, path)
        if entry is None:
            if existing is not None:
                raise SafetyError("Git 后验发现未知对象")
            continue
        if existing is None or entry[0] == b"120000":
            raise SafetyError("Git 后验文件不存在或类型不受支持")
        raw, identity = _read_stable_file(existing)
        if (_git_blob_digest(raw, entry[1]) != entry[1]
                or bool(identity[2] & 0o111) != (entry[0] == b"100755")):
            raise SafetyError("Git 后验文件与目标提交不一致")


def _check_git_identity(root: Path, worktree: Path, old: str, target: str,
                        *, current: str, root_id: tuple[int, ...] | list[int],
                        worktree_id: tuple[int, ...] | list[int],
                        git_id: tuple[int, ...] | list[int],
                        w_git_id: tuple[int, ...] | list[int],
                        w_admin: str, w_admin_id: tuple[int, ...] | list[int]) -> None:
    if (_directory_identity(root) != tuple(root_id)
            or _directory_identity(worktree) != tuple(worktree_id)
            or _directory_identity(root / ".git") != tuple(git_id)
            or _marker((worktree / ".git").lstat()) != tuple(w_git_id)
            or _git_scalar(root, "symbolic-ref", "-q", "HEAD") != "refs/heads/master"
            or _git_scalar(root, "rev-parse", "--verify", "HEAD^{commit}") != current
            or _git_scalar(worktree, "rev-parse", "--verify", "HEAD^{commit}") != target):
        raise SafetyError("主仓、W、Git 实体或 HEAD 发生变化")
    records = _worktrees(root)
    r, w = records.get(os.fspath(root)), records.get(os.fspath(worktree))
    if not r or not w or r.get("HEAD") != current or w.get("HEAD") != target:
        raise SafetyError("worktree 登记发生变化")
    if Path(_git_scalar(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve() != root / ".git":
        raise SafetyError("W 共享 Git 实体发生变化")
    actual_admin, actual_admin_id = _verify_no_git_inflight(root, worktree)
    if actual_admin != w_admin or actual_admin_id != tuple(w_admin_id):
        raise SafetyError("W Git 管理目录身份发生变化")
    target_tree = _tree(root, target)
    for relative in DELIVERY:
        entry = target_tree.get(relative)
        if entry is None or entry[0] not in (b"100644", b"100755"):
            raise SafetyError("目标提交缺少交付脚本")
        raw, marker = _read_stable_file(worktree / os.fsdecode(relative))
        if (_git_blob_digest(raw, entry[1]) != entry[1]
                or bool(marker[2] & 0o111) != (entry[0] == b"100755")):
            raise SafetyError("W 执行脚本与已验收提交不一致")


def _prepare_git_transition(repo: dict[str, Any], *, reverse: bool) -> None:
    root, worktree = Path(repo["root"]), Path(repo["worktree"])
    old, target = repo["old_oid"], repo["target_oid"]
    current, destination = (target, old) if reverse else (old, target)
    _check_git_identity(root, worktree, old, target, current=current,
                        root_id=repo["root_id"], worktree_id=repo["worktree_id"],
                        git_id=repo["git_id"], w_git_id=repo["w_git_id"],
                        w_admin=repo["w_admin"], w_admin_id=repo["w_admin_id"])
    current_tree, destination_tree = _tree(root, current), _tree(root, destination)
    if _index(root) != current_tree:
        raise SafetyError("Git 索引发生变化")
    changed = [os.fsencode(path) for path in repo["changed"]]
    _no_git_filters(root, (current_tree, destination_tree), changed, root / ".git", root / ".git")
    tracked_config = repo.get("tracked_config")
    check_changed_paths(root, current_tree, destination_tree,
                        authorized_config=os.fsencode(tracked_config) if tracked_config else None)


def _refresh_tracked_config_index(tx: Manifest) -> None:
    """只刷新已恢复为目标提交字节的授权配置索引 stat，不暂存新内容。"""
    repo = tx.data["repository"]
    relative = repo.get("tracked_config")
    if not relative:
        return
    root, worktree = Path(repo["root"]), Path(repo["worktree"])
    target = repo["target_oid"]
    tree = _tree(root, target)
    entry = tree.get(os.fsencode(relative))
    if entry is None or entry[0] not in (b"100644", b"100755"):
        raise SafetyError("tracked 配置目标提交身份无效")
    tracked_keys = {tuple(_alias_key(part) for part in os.fsdecode(p).split("/")) for p in tree}
    for reference in tx.data["context_evidence"]["protected_references"]:
        try:
            ref = Path(reference).relative_to(root)
        except ValueError:
            continue
        if tuple(_alias_key(part) for part in ref.parts) in tracked_keys:
            raise SafetyError("凭据路径被 Git 跟踪，不能安全刷新配置索引")
    path = root / relative
    raw, identity = _read_stable_file(path)
    if (_git_blob_digest(raw, entry[1]) != entry[1]
            or bool(identity[2] & 0o111) != (entry[0] == b"100755")
            or _index(root) != tree):
        raise SafetyError("tracked 配置未恢复为目标提交基线")
    tx.intent("REFRESH_TRACKED_CONFIG_INDEX", {"path": relative, "head": target,
                                                "file_id": identity,
                                                "blob_oid": os.fsdecode(entry[1])})
    def guard() -> None:
        _check_git_identity(root, worktree, repo["old_oid"], target, current=target,
                            root_id=repo["root_id"], worktree_id=repo["worktree_id"],
                            git_id=repo["git_id"], w_git_id=repo["w_git_id"],
                            w_admin=repo["w_admin"], w_admin_id=repo["w_admin_id"])
        now, marker = _read_stable_file(path)
        if now != raw or marker != identity or _index(root) != tree:
            raise SafetyError("tracked 配置索引刷新前发生变化")
    # -q 必须位于 --refresh 之前：允许无关 tracked 用户文档保持修改，
    # 但不把这些修改写入索引；执行前后核验整个索引仍与目标树一致。
    _git(root, "update-index", "-q", "--refresh", "--", relative, before_launch=guard)
    after, marker = _read_stable_file(path)
    if after != raw or marker != identity or _index(root) != tree:
        raise SafetyError("tracked 配置索引刷新后验 UNKNOWN")
    tx.done("REFRESH_TRACKED_CONFIG_INDEX", {})


def _git_transition(tx: Manifest, *, reverse: bool) -> None:
    repo = tx.data["repository"]
    root, worktree = Path(repo["root"]), Path(repo["worktree"])
    old, target = repo["old_oid"], repo["target_oid"]
    current, destination = (target, old) if reverse else (old, target)
    action = "GIT_RETURN" if reverse else "GIT_ADVANCE"
    _check_git_identity(root, worktree, old, target, current=current,
                        root_id=repo["root_id"], worktree_id=repo["worktree_id"],
                        git_id=repo["git_id"], w_git_id=repo["w_git_id"],
                        w_admin=repo["w_admin"], w_admin_id=repo["w_admin_id"])
    current_tree, destination_tree = _tree(root, current), _tree(root, destination)
    if _index(root) != current_tree:
        raise SafetyError("Git 索引发生变化")
    changed = [os.fsencode(path) for path in repo["changed"]]
    _no_git_filters(root, (current_tree, destination_tree), changed, root / ".git", root / ".git")
    check_changed_paths(root, current_tree, destination_tree)
    tx.intent(action, {"from_oid": current, "to_oid": destination,
                       "operation": "reset_keep" if reverse else "merge_ff_only"})
    _check_git_identity(root, worktree, old, target, current=current,
                        root_id=repo["root_id"], worktree_id=repo["worktree_id"],
                        git_id=repo["git_id"], w_git_id=repo["w_git_id"],
                        w_admin=repo["w_admin"], w_admin_id=repo["w_admin_id"])
    if _index(root) != current_tree:
        raise SafetyError("Git 操作前索引发生变化")
    check_changed_paths(root, current_tree, destination_tree)
    def final_boundary_check() -> None:
        # `reset --keep` 不保护所有 ignored 对象；在启动 Git 的最后边界
        # 重新检查。检查与 Git 自身写入之间仍有非协作并发窗口。
        _check_git_identity(root, worktree, old, target, current=current,
                            root_id=repo["root_id"], worktree_id=repo["worktree_id"],
                            git_id=repo["git_id"], w_git_id=repo["w_git_id"],
                            w_admin=repo["w_admin"], w_admin_id=repo["w_admin_id"])
        if _index(root) != current_tree:
            raise SafetyError("Git 启动边界索引发生变化")
        _no_git_filters(root, (current_tree, destination_tree), changed, root / ".git", root / ".git")
        check_changed_paths(root, current_tree, destination_tree)
        if reverse:
            _check_runtime_parked(tx, root)
    if reverse:
        _git(root, "reset", "--keep", destination, before_launch=final_boundary_check)
    else:
        _git(root, "merge", "--ff-only", "--no-overwrite-ignore", destination,
             before_launch=final_boundary_check)
    _check_git_identity(root, worktree, old, target, current=destination,
                        root_id=repo["root_id"], worktree_id=repo["worktree_id"],
                        git_id=repo["git_id"], w_git_id=repo["w_git_id"],
                        w_admin=repo["w_admin"], w_admin_id=repo["w_admin_id"])
    if _index(root) != destination_tree:
        raise SafetyError("Git 操作后索引 UNKNOWN")
    _verify_changed_tree(root, destination_tree, changed)
    tx.done(action, {}, stage="GIT_RETURNED" if reverse else "GIT_ADVANCED")


class InvalidLedger(ValueError):
    """不携带账本数据的校验错误。"""


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidLedger("账本含重复字段")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise InvalidLedger("账本含非法数值")


def validate_ledger_bytes(raw: bytes) -> None:
    """校验假定已安全、稳定读取的账本字节；不做任何文件 I/O。

    规则对照当前新版 ``validate_migration_ledger_snapshot``。此函数
    并未证明文件身份、读取稳定性、权限、ACL、旧版兼容或统计完整性；
    后续事务必须独立完成这些验证。
    """
    try:
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_pairs,
            parse_constant=_invalid_constant,
        )
    except (UnicodeError, ValueError, RecursionError, TypeError):
        raise InvalidLedger("账本格式无效") from None

    def invalid() -> None:
        raise InvalidLedger("账本结构无效")

    if type(data) is not dict:
        invalid()
    version = data.get("version", 2)
    if type(version) is not int or version not in (2, 3):
        invalid()

    def date_key(value: Any, *, month: bool = False) -> None:
        pattern = r"[0-9]{4}-[0-9]{2}" if month else r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
        if type(value) is not str or re.fullmatch(pattern, value) is None:
            invalid()
        try:
            datetime.date.fromisoformat(value + "-01" if month else value)
        except ValueError:
            invalid()

    if "since" in data:
        date_key(data["since"])
    if "keep_days" in data and (
        type(data["keep_days"]) is not int or data["keep_days"] <= 0
    ):
        invalid()

    def numbers(mapping: dict[str, Any], required: tuple[str, ...], optional: tuple[str, ...]) -> None:
        for field in required:
            if field not in mapping or type(mapping[field]) is not int or mapping[field] < 0:
                invalid()
        for field in optional:
            if field in mapping and (type(mapping[field]) is not int or mapping[field] < 0):
                invalid()

    def bucket(value: Any) -> None:
        if type(value) is not dict:
            invalid()
        numbers(value, ("requests", "ok", "fail", "sum_ms"), ("client_disconnect", "max_ms"))
        if (
            value["ok"] + value["fail"] > value["requests"]
            or value.get("client_disconnect", 0) > value["requests"]
            or value.get("max_ms", 0) > value["sum_ms"]
        ):
            invalid()
        combos = value.get("combos")
        if type(combos) is not dict:
            invalid()
        for key, combo in combos.items():
            if type(key) is not str or not key or type(combo) is not dict:
                invalid()
            numbers(
                combo,
                ("requests", "ok", "fail", "usage_in", "usage_out"),
                ("client_disconnect", "attempts", "attempt_fail", "usage_reasoning"),
            )
            if (
                combo["ok"] + combo["fail"] > combo["requests"]
                or combo.get("client_disconnect", 0) > combo["requests"]
            ):
                invalid()

    if "total" not in data:
        invalid()
    for name in ("days", "months_archive"):
        if name in data and type(data[name]) is not dict:
            invalid()
    bucket(data["total"])
    for key, value in data.get("days", {}).items():
        date_key(key)
        bucket(value)
    for key, value in data.get("months_archive", {}).items():
        date_key(key, month=True)
        bucket(value)


def _migration_base(*, create: bool = True) -> Path:
    """使用当前账户的固定私有位置；没有环境变量或 CLI 覆盖口。"""
    if sys.platform != "darwin" or os.geteuid() == 0:
        raise SafetyError("只支持当前非 root macOS 用户")
    try:
        home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
        parent = home / "Library" / "Application Support"
        _directory_identity(home)
        _directory_identity(home / "Library")
        _directory_identity(parent)
        for ancestor in (parent, *parent.parents):
            if os.path.lexists(ancestor / ".git"):
                raise SafetyError("事务目录不得位于 Git 仓库内")
        base = parent / "model-proxy-migration"
        if not os.path.lexists(base):
            if not create:
                raise SafetyError("事务目录不存在")
            base = _create_private_directory(parent, base.name)
        _verify_migration_base(base)
        return base
    except (KeyError, OSError):
        raise SafetyError("当前用户的事务目录不可用") from None


def _verify_migration_base(base: Path) -> None:
    base = base.resolve(strict=True)
    fd = _open_directory(base, private=True)
    os.close(fd)
    for parent in (base, *base.parents):
        if os.path.lexists(parent / ".git"):
            raise SafetyError("事务目录不得位于 Git 仓库内")


def _legacy_lock_path() -> Path:
    """真实入口只使用产品固定旧锁；不读取任何测试覆盖变量。"""
    try:
        parent = Path("/tmp").resolve(strict=True)
        fd = _open_directory(parent, sticky_ok=True)
        os.close(fd)
        return parent / "model_proxy.lock"
    except OSError:
        raise SafetyError("旧锁父目录无法验证") from None


def _find_event(tx: Manifest, action: str) -> dict[str, Any] | None:
    matches = [event for event in tx.data["events"]
               if event.get("action") == action and event.get("state") == "DONE"]
    return matches[-1] if matches else None


def _verify_backup_source(source: Path, evidence: dict[str, Any]) -> None:
    if evidence.get("missing"):
        if os.path.lexists(source) or _directory_identity(source.parent) != tuple(evidence["parent_id"]):
            raise SafetyError("原缺失文件状态发生变化")
        return
    raw, marker = _read_stable_file(source)
    if (marker != tuple(evidence["source_id"]) or _sha256(raw) != evidence["sha256"]
            or _directory_identity(source.parent) != tuple(evidence["parent_id"])):
        raise SafetyError("已保全源文件发生变化")


def _check_locks(locks: list[HeldLock]) -> None:
    for lock in locks:
        lock.verify()


def _check_both_listeners(quiescence: Any, context: dict[str, Any]) -> None:
    ports = (context["old_listen"][1], context["new_listen"][1])
    for port in dict.fromkeys(ports):
        quiescence(port)


def _observe_legacy_lock(path: Path) -> tuple[tuple[int, ...], tuple[int, int] | None]:
    """只观察旧锁本身及父目录，不读取内容，也不凭 PID 判断存活。"""
    pfd = _open_directory(path.parent, sticky_ok=True)
    fd = -1
    try:
        parent_id = _dir_marker(os.fstat(pfd))
        try:
            before = os.stat(path.name, dir_fd=pfd, follow_symlinks=False)
        except FileNotFoundError:
            if _dir_marker(path.parent.lstat()) != parent_id or os.path.lexists(path):
                raise SafetyError("旧锁缺失状态发生变化")
            return parent_id, None
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.geteuid() or before.st_mode & 0o022
                or getattr(before, "st_flags", 0)):
            raise SafetyError("旧锁身份或权限不安全")
        fd = os.open(path.name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=pfd)
        if _marker(os.fstat(fd)) != _marker(before):
            raise SafetyError("旧锁打开时发生变化")
        _verify_darwin_local_no_acl(fd)
        if (_marker(os.stat(path.name, dir_fd=pfd, follow_symlinks=False)) != _marker(before)
                or _dir_marker(path.parent.lstat()) != parent_id):
            raise SafetyError("旧锁观察期间发生变化")
        return parent_id, (before.st_dev, before.st_ino)
    except OSError:
        raise SafetyError("旧锁观察失败") from None
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(pfd)


def _checkpoint(tx: Manifest, name: str, *, stage: str) -> None:
    tx.intent(name, {})
    tx.done(name, {}, stage=stage)


def _acquire_created_runtime_lock(tx: Manifest, stack: contextlib.ExitStack,
                                  runtime: Path, name: str, action: str) -> HeldLock:
    tx.intent(action, {"name": name, "parent_id": _directory_identity(runtime)})
    lock = stack.enter_context(HeldLock(runtime / name, must_create=True))
    lock.verify()
    tx.done(action, {"identity": lock.identity})
    return lock


def switch_operation(
    *, root: Path, worktree: Path, target: str, port: int,
    old_cwd: str, new_cwd: str, old_home: str, new_home: str,
    config_raw: str | None, old_env_port: str | None, new_env_port: str | None,
    migration_base: Path, legacy_lock: Path,
    minimum_oid: str = MINIMUM_TARGET, quiescence: Any = check_processes_and_port,
    on_transaction: Any = None, on_allocated: Any = None,
    expected_context: dict[str, Any] | None = None,
) -> Manifest:
    """事务执行器。测试注入只通过模块级调用；CLI 不接受这些路径覆盖。"""
    _verify_migration_base(migration_base)
    with contextlib.ExitStack() as stack:
        shared = stack.enter_context(HeldLock(migration_base / "migration.lock"))
        root = root.resolve(strict=True)
        context = inspect_context(root, old_cwd=old_cwd, new_cwd=new_cwd, old_home=old_home,
                                  new_home=new_home, config_raw=config_raw, old_env_port=old_env_port,
                                  new_env_port=new_env_port, declared_port=port)
        repo = inspect_repository(root, worktree, target, minimum_oid=minimum_oid,
                                  protected_roles=context)
        root = Path(repo["root"])
        if tuple(repo["root_id"])[0] != _directory_identity(migration_base)[0]:
            raise SafetyError("事务目录与主仓不在同一文件系统，无法无覆盖提交运行目录")
        _protect_business_roles(repo, context)
        if expected_context is not None and Manifest._encode(context) != Manifest._encode(expected_context):
            raise SafetyError("上下文与用户确认的预览不一致")
        _check_both_listeners(quiescence, context)
        shared.verify()
        # 仅做稳定只读校验；账本复制仍必须等两套产品锁持有后。
        ledger_raw, ledger_id = _read_stable_file(root / ".model_proxy_totals.json")
        validate_ledger_bytes(ledger_raw)
        ledger_precheck = {"source_id": ledger_id, "sha256": _sha256(ledger_raw),
                           "size": len(ledger_raw), "parent_id": _directory_identity(root)}
        log_path = root / ".model_proxy.log"
        if os.path.lexists(log_path):
            log_hash, log_id = _hash_stable_file(log_path)
            log_precheck = {"source_id": log_id, "sha256": log_hash,
                            "parent_id": _directory_identity(root)}
        else:
            log_precheck = {"missing": True, "parent_id": _directory_identity(root)}
            _verify_log_source(log_path, log_precheck)
        tx = Manifest.create(migration_base, {
            "repository": repo,
            "contexts": {"old_cwd": old_cwd, "new_cwd": new_cwd, "old_home": old_home,
                         "new_home": new_home, "config_raw": config_raw,
                         "old_env_port": old_env_port, "new_env_port": new_env_port},
            "context_evidence": context, "ledger_precheck": ledger_precheck,
            "log_precheck": log_precheck,
        }, on_allocated=on_allocated)
        if on_transaction is not None:
            on_transaction(tx)
        old_parent_id, old_before = _observe_legacy_lock(legacy_lock)
        tx.intent("LOCK_OLD", {"path": os.fspath(legacy_lock), "parent_id": old_parent_id,
                               "before_id": old_before})
        old_lock = stack.enter_context(HeldLock(legacy_lock, sticky_parent=True,
                                                must_create=old_before is None))
        if (old_lock.parent_id != old_parent_id
                or old_before is not None and old_lock.identity != old_before):
            raise SafetyError("旧锁获取前身份变化；保留事务人工核验")
        tx.done("LOCK_OLD", {"identity": old_lock.identity})
        locks = [shared, old_lock]
        _check_both_listeners(quiescence, context)
        _check_locks(locks)
        _verify_log_source(log_path, log_precheck)
        log = _backup_log(tx, log_path)
        _check_locks(locks)
        _verify_backup_source(root / ".model_proxy_totals.json", ledger_precheck)
        _verify_log_source(log_path, log_precheck)
        runtime = _create_runtime(tx, root)
        locks.append(_acquire_created_runtime_lock(tx, stack, runtime, "anchor.lock", "LOCK_ANCHOR"))
        locks.append(_acquire_created_runtime_lock(tx, stack, runtime, "model_proxy.lock", "LOCK_NEW"))
        _check_both_listeners(quiescence, context)
        _check_locks(locks)
        ledger = _copy_to_backup(tx, "BACKUP_OLD_LEDGER", root / ".model_proxy_totals.json",
                                 "old-ledger.backup", ledger=True)
        if (tuple(ledger["source_id"]) != tuple(ledger_precheck["source_id"])
                or ledger["sha256"] != ledger_precheck["sha256"]):
            raise SafetyError("旧账本与创建运行目录前的只读校验不一致")
        _recheck_switch_roles(root, context)
        _snapshot_role(tx, "config", Path(context["config_original"]), missing_ok=False,
                       expected_marker=tuple(context["config"]["target_id"]))
        _recheck_switch_roles(root, context)
        _snapshot_role(tx, "sidecar", Path(context["sidecar_original"]), missing_ok=True,
                       expected_marker=(tuple(context["sidecar"]["target_id"])
                                        if context["sidecar"]["exists"] else None))
        _snapshot_role(tx, "ledger", root / ".model_proxy_totals.json", missing_ok=False,
                       expected_marker=tuple(ledger_precheck["source_id"]))
        config_raw_snapshot, config_meta = _load_snapshot_role(tx, "config")
        sidecar_raw_snapshot, sidecar_meta = _load_snapshot_role(tx, "sidecar")
        ledger_raw_snapshot, ledger_meta = _load_snapshot_role(tx, "ledger")
        _check_snapshot_roles([config_meta, sidecar_meta, ledger_meta], context)
        if (tuple(config_meta["target_id"]) != tuple(context["config"]["target_id"])
                or tuple(config_meta["entry_id"]) != tuple(context["config"]["path_id"])
                or sidecar_meta["exists"] != context["sidecar"]["exists"]
                or sidecar_meta["exists"] and
                (tuple(sidecar_meta["target_id"]) != tuple(context["sidecar"]["target_id"])
                 or tuple(sidecar_meta["entry_id"]) != tuple(context["sidecar"]["path_id"]))
                or ledger_raw_snapshot is None or _sha256(ledger_raw_snapshot) != ledger_precheck["sha256"]):
            raise SafetyError("业务快照与切换预检不一致")
        collision_backup = None
        if repo["collision"] is not None:
            collision_backup = _copy_to_backup(tx, "BACKUP_CONFLICT", root / repo["collision"],
                                              "conflict.backup")
        _checkpoint(tx, "BACKUP_FINISHED", stage="BACKED_UP")
        _check_locks(locks)
        if collision_backup is not None:
            _move_conflict(tx, root, collision_backup)
        else:
            _checkpoint(tx, "NO_CONFLICT", stage="CONFLICT_MOVED")
        _verify_backup_source(root / ".model_proxy_totals.json", ledger)
        _verify_log_source(root / ".model_proxy.log", log)
        _check_locks(locks)
        if repo.get("tracked_config"):
            if config_raw_snapshot is None:
                raise SafetyError("tracked 业务配置快照缺失")
            old_tree = _tree(root, repo["old_oid"])
            baseline = _tree_blob(root, old_tree, os.fsencode(repo["tracked_config"]))
            _restore_role(tx, "config_prepare_switch", baseline, config_meta)
        _git_transition(tx, reverse=False)
        if repo.get("tracked_config"):
            _restore_role(tx, "config_after_switch", config_raw_snapshot, config_meta)
        _check_locks(locks)
        _check_both_listeners(quiescence, context)
        _verify_target_runtime_ignore(tx, runtime)
        verified_snapshot, _ = _load_snapshot_role(tx, "ledger")
        snapshot_event = _find_event(tx, "SNAPSHOT_LEDGER")
        if verified_snapshot != ledger_raw_snapshot or snapshot_event is None:
            raise SafetyError("旧账本私有快照不一致")
        staged = _stage_ledger(tx, runtime, tx.directory / "ledger.snapshot",
                               name=".ledger-" + tx.data["transaction"] + ".tmp",
                               action="STAGE_NEW_LEDGER", private_directory=True)
        if (tuple(staged["source_id"]) != tuple(snapshot_event["snapshot_id"])
                or staged["sha256"] != ledger["sha256"]):
            raise SafetyError("旧账本私有快照与原始备份不一致")
        _install_new_ledger(tx, runtime, staged)
        _check_locks(locks)
        _verify_backup_source(root / ".model_proxy_totals.json", ledger)
        _verify_log_source(root / ".model_proxy.log", log)
        installed, _ = _read_stable_file(runtime / "model_proxy_totals.json")
        if _sha256(installed) != ledger["sha256"]:
            raise SafetyError("新账本与原始备份不一致")
        context_after = inspect_context(root, old_cwd=old_cwd, new_cwd=new_cwd, old_home=old_home,
                                        new_home=new_home, config_raw=config_raw,
                                        old_env_port=old_env_port, new_env_port=new_env_port,
                                        declared_port=port)
        _protect_business_roles(repo, context_after)
        if repo.get("tracked_config"):
            restored_config, restored_meta = _authorized_role_current(config_meta)
            if (restored_config != config_raw_snapshot
                    or restored_meta["xattrs"] != config_meta["xattrs"]
                    or stat.S_IMODE(restored_meta["target_id"][2]) != stat.S_IMODE(config_meta["target_id"][2])):
                raise SafetyError("升级后 tracked 业务配置未恢复旧快照")
            context_after = dict(context_after)
            context_after["config"] = context["config"]
        if Manifest._encode(context_after) != Manifest._encode(context):
            raise SafetyError("配置或启动上下文在迁移期间发生变化")
        _check_both_listeners(quiescence, context_after)
        _check_locks(locks)
        _verify_target_runtime_ignore(tx, runtime)
        _checkpoint(tx, "VERIFY_SWITCH", stage="VERIFIED")
        return tx


def _check_context_for_return(tx: Manifest, root: Path, *, port: int,
                              old_cwd: str, new_cwd: str, old_home: str, new_home: str) -> dict[str, Any]:
    return _snapshot_context_for_return(tx, root, port=port, old_cwd=old_cwd, new_cwd=new_cwd,
                                        old_home=old_home, new_home=new_home)


def _snapshot_context_for_return(tx: Manifest, root: Path, *, port: int,
                                 old_cwd: str, new_cwd: str, old_home: str,
                                 new_home: str) -> dict[str, Any]:
    """回滚前对默认业务文件及旧/新配置中的凭据角色排重，不读凭据。"""
    saved, recorded = tx.data["contexts"], tx.data["context_evidence"]
    if (old_cwd != saved["new_cwd"] or new_cwd != saved["old_cwd"]
            or old_home != saved["new_home"] or new_home != saved["old_home"]):
        raise SafetyError("回滚上下文与事务记录不一致")
    ids = recorded["context_ids"]
    if ([_directory_identity(Path(p)) for p in (old_cwd, new_cwd, old_home, new_home)]
            != [tuple(ids[1]), tuple(ids[0]), tuple(ids[3]), tuple(ids[2])]):
        raise SafetyError("回滚启动上下文目录身份变化")
    if int(port) != int(recorded["new_listen"][1]):
        raise SafetyError("当前服务端口与记录不一致，需要重新确认")
    config_raw, config_meta = _load_snapshot_role(tx, "config")
    _, sidecar_meta = _load_snapshot_metadata(tx, "sidecar")
    _, ledger_meta = _load_snapshot_metadata(tx, "ledger")
    defaults = {"config": root / "config" / "model_proxy_config.json",
                "sidecar": root / "config" / "session_overrides.json",
                "ledger": root / ".model_proxy_totals.json"}
    for name, meta in (("config", config_meta), ("sidecar", sidecar_meta), ("ledger", ledger_meta)):
        if meta.get("path") != os.fspath(defaults[name]):
            raise SafetyError("仅支持项目默认业务文件路径")
        if meta.get("exists"):
            ident = meta.get("target_id", [])
            if (len(ident) < 6 or not stat.S_ISREG(ident[2]) or ident[3] != os.geteuid()
                    or ident[5] != 1 or stat.S_IMODE(ident[2]) != 0o600
                    or meta.get("target") != os.fspath(defaults[name])
                    or meta.get("link_attributes") != []
                    or any(entry[0] != "directory" for entry in meta.get("chain", []))):
                raise SafetyError("旧业务快照不是受支持的 0600 普通文件")
    if not config_meta.get("exists") or not ledger_meta.get("exists") or config_raw is None:
        raise SafetyError("旧配置或账本快照缺失")
    _check_snapshot_roles([config_meta, sidecar_meta, ledger_meta], recorded)
    # lstat 所有当前角色，原 sidecar 不存在而新对象出现时不读取、不删除。
    roles, role_info = _default_roles(root, ledger_missing=True)
    for name, meta in (("config", config_meta), ("sidecar", sidecar_meta), ("ledger", ledger_meta)):
        current = role_info[name]
        if (tuple(meta["parent_id"]) != current["parent_id"]
                or meta["parent_target"] != os.fspath(defaults[name].parent)
                or meta["parent_chain"] != current["chain"][:-1]):
            raise SafetyError("业务对象父路径与切换前记录不一致；不读取、不覆盖")
    if not sidecar_meta["exists"] and role_info["sidecar"]["id"] is not None:
        raise SafetyError("原 sidecar 不存在，新对象来源未证明；不读取、不删除")
    current_config, _ = _read_checked_default(defaults["config"], role_info["config"])
    if current_config is None:
        raise SafetyError("当前配置不存在，无法确认凭据角色")
    old_config = _decode_json(config_raw)
    old_refs = _config_credentials(old_config, saved["old_cwd"], saved["old_home"])
    if old_refs != _config_credentials(old_config, saved["new_cwd"], saved["new_home"]):
        raise SafetyError("旧配置 appkey_file 在启动上下文中不等价")
    bindings = recorded["appkey_identities"]
    if sorted(map(os.fspath, old_refs)) != sorted(item["reference"] for item in bindings):
        raise SafetyError("旧配置 appkey_file 身份记录不一致")
    current_refs = _config_credentials(current_config, old_cwd, old_home)
    def observe_refs(paths: list[Path]) -> list[tuple[str, Path, tuple[int, ...] | None]]:
        observed = []
        for path in paths:
            identity, chain, parent_id = _nofollow_file(path)
            if identity is None:
                raise SafetyError("appkey_file 身份不明")
            observed.append(("appkey_file", path, identity))
        _role_collisions(roles + observed)
        return observed
    old_observed = observe_refs(old_refs)
    current_observed = observe_refs(current_refs)
    for item in bindings:
        identity, chain, parent_id = _nofollow_file(Path(item["reference"]))
        if (identity is None or tuple(identity[:6]) != tuple(item["target_id"][:6])
                or chain != item["link_chain"] or parent_id != tuple(item["target_parent_id"])
                or item["target"] != item["reference"]):
            raise SafetyError("旧配置 appkey_file 路径或目标身份变化")
    combined = list(old_observed)
    old_keys = {tuple(_alias_key(part) for part in path.parts): (path, identity)
                for _, path, identity in old_observed}
    for entry in current_observed:
        key = tuple(_alias_key(part) for part in entry[1].parts)
        if key in old_keys:
            prior_path, prior_identity = old_keys[key]
            if entry[1] != prior_path or entry[2] != prior_identity:
                raise SafetyError("新旧 appkey_file 路径别名或身份不一致")
        else:
            combined.append(entry)
            old_keys[key] = (entry[1], entry[2])
    _role_collisions(roles + combined)
    # 验证当前 B 中的新凭据路径也不会被随后的 Git 版本切换覆盖。
    protected = dict(recorded)
    protected["protected_references"] = [os.fspath(path) for _, path, _ in combined]
    _protect_business_roles(tx.data["repository"], protected)
    now_roles, now_info = _default_roles(root, ledger_missing=True)
    if now_info != role_info:
        raise SafetyError("回滚预检期间默认业务对象发生变化")
    for _, path, identity in combined:
        observed, _, _ = _nofollow_file(path)
        if observed != identity:
            raise SafetyError("回滚预检期间凭据身份发生变化")
    _load_snapshot_role(tx, "sidecar")
    _load_snapshot_role(tx, "ledger")
    # 只有全部角色确认安全后才读取当前 sidecar。
    for name, meta in (("config", config_meta), ("sidecar", sidecar_meta), ("ledger", ledger_meta)):
        _authorized_role_current(meta, expected_marker=role_info[name]["id"])
    return {"old_listen": recorded["new_listen"], "new_listen": recorded["old_listen"],
            "role_markers": {name: role_info[name]["id"] for name in ("config", "sidecar", "ledger")}}


def _acquire_recorded_locks(tx: Manifest, stack: contextlib.ExitStack,
                            shared: HeldLock, legacy_lock: Path, root: Path) -> list[HeldLock]:
    old_event = _find_event(tx, "LOCK_OLD")
    runtime_event = _find_event(tx, "CREATE_RUNTIME")
    if old_event is None or runtime_event is None or old_event.get("path") != os.fspath(legacy_lock):
        raise SafetyError("旧锁或运行目录记录不完整，UNKNOWN")
    old_lock = stack.enter_context(HeldLock(legacy_lock, sticky_parent=True))
    if tuple(old_event["identity"]) != old_lock.identity:
        raise SafetyError("旧锁身份发生变化")
    runtime = root / ".runtime"
    if _directory_identity(runtime) != tuple(runtime_event["runtime_id"]):
        raise SafetyError("运行目录身份发生变化")
    locks = [shared, old_lock]
    for name, action in (("anchor.lock", "LOCK_ANCHOR"), ("model_proxy.lock", "LOCK_NEW")):
        event = _find_event(tx, action)
        if event is None or not os.path.lexists(runtime / name):
            raise SafetyError("新锁记录或对象不完整，UNKNOWN")
        lock = stack.enter_context(HeldLock(runtime / name))
        if tuple(event["identity"]) != lock.identity:
            raise SafetyError("新锁身份发生变化")
        locks.append(lock)
    _check_locks(locks)
    return locks


def _verify_backup_file(tx: Manifest, event: dict[str, Any]) -> bytes:
    name = event["backup"]
    if not isinstance(name, str) or "/" in name or name in ("", ".", ".."):
        raise SafetyError("事务备份路径无效")
    raw, marker = _read_stable_file(tx.directory / name)
    if (tuple(event["backup_id"]) != marker or _sha256(raw) != event["sha256"]
            or len(raw) != event["size"]):
        raise SafetyError("事务备份发生变化")
    return raw


def _verify_legacy_log(tx: Manifest, root: Path) -> None:
    event = _find_event(tx, "BACKUP_OLD_LOG")
    if event is None:
        raise SafetyError("UNKNOWN：旧日志保全记录不完整")
    if event.get("source_missing"):
        _verify_log_source(root / ".model_proxy.log", event)
    else:
        if event.get("backup") != "old-log.backup":
            raise SafetyError("旧日志备份路径无效")
        digest, marker = _hash_stable_file(tx.directory / "old-log.backup")
        if (marker != tuple(event["backup_id"]) or digest != event["sha256"]
                or marker[6] != event["size"]):
            raise SafetyError("旧日志备份发生变化")
        _verify_log_source(root / ".model_proxy.log", event)


def _verify_restored_git(tx: Manifest, root: Path) -> None:
    repo = tx.data["repository"]
    old_tree = _tree(root, repo["old_oid"])
    if _index(root) != old_tree:
        raise SafetyError("UNKNOWN：旧 Git 索引不一致")
    changed = [os.fsencode(path) for path in repo["changed"]
               if (not repo["collision"] or path != repo["collision"])
               and path != repo.get("tracked_config")]
    _verify_changed_tree(root, old_tree, changed)
    if repo["collision"]:
        data, identity = _read_stable_file(root / repo["collision"])
        moved = _find_event(tx, "MOVE_CONFLICT")
        if moved:
            if (not _same_object(identity, moved["source_id"]) or _sha256(data) != moved["sha256"]
                    or stat.S_IMODE(identity[2]) != moved["original_mode"]):
                raise SafetyError("UNKNOWN：已恢复撞名文档不匹配")
        else:
            original = repo["collision_initial"]
            if tuple(original["identity"]) != identity or _sha256(data) != original["sha256"]:
                raise SafetyError("UNKNOWN：原始撞名文档发生变化")


def _check_runtime_parked(tx: Manifest, root: Path) -> None:
    runtime = root / ".runtime"
    created = _find_event(tx, "CREATE_RUNTIME")
    if created is None:
        raise SafetyError("运行目录身份不明")
    runtime_id = tuple(created["runtime_id"])
    locks = {"anchor.lock", "model_proxy.lock", "control.lock", "model_proxy_start.lock"}
    parent = _open_directory(runtime, private=True)
    try:
        if _dir_marker(os.fstat(parent)) != runtime_id:
            raise SafetyError("运行目录身份不明")
        with os.scandir(parent) as entries:
            for entry in entries:
                if entry.name == ".gitignore":
                    continue
                if entry.name in locks:
                    fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
                    try:
                        st = os.fstat(fd)
                        if (not stat.S_ISREG(st.st_mode) or st.st_uid not in (os.geteuid(), 0)
                                or st.st_nlink != 1 or st.st_mode & 0o022 or st.st_size != 0
                                or getattr(st, "st_flags", 0)
                                or _marker(st) != _marker(os.stat(entry.name, dir_fd=parent,
                                                                  follow_symlinks=False))):
                            raise SafetyError("运行锁含未知数据或身份不明")
                        _verify_darwin_local_no_acl(fd)
                        if (_marker(st) != _marker(os.fstat(fd))
                                or _marker(st) != _marker(os.stat(entry.name, dir_fd=parent,
                                                                  follow_symlinks=False))):
                            raise SafetyError("运行锁检查期间身份发生变化")
                    finally:
                        os.close(fd)
                elif entry.name in ("tmp", "backups"):
                    raise SafetyError("运行暂存或备份目录尚未迁出；保留现场人工处理")
                else:
                    raise SafetyError("运行目录存在未知文件；保留现场人工处理")
        if (_dir_marker(runtime.lstat()) != runtime_id
                or _directory_identity(root) != tuple(tx.data["repository"]["root_id"])):
            raise SafetyError("运行目录父路径发生变化")
    except OSError:
        raise SafetyError("运行目录暂存状态无法验证") from None
    finally:
        os.close(parent)


def _park_runtime_directory(tx: Manifest, root: Path, name: str,
                            expected: tuple[int, ...]) -> None:
    """不遍历目录内容，持有父目录和源目录 fd 作同卷无覆盖迁移。"""
    if name not in ("tmp", "backups"):
        raise SafetyError("运行目录角色无效")
    runtime = root / ".runtime"
    destination = "runtime-" + name + ".before-return"
    created = _find_event(tx, "CREATE_RUNTIME")
    if created is None:
        raise SafetyError("运行目录记录缺失")
    runtime_id = tuple(created["runtime_id"])
    source_parent = _open_directory(runtime, private=True)
    target_parent = -1
    source = -1
    moved = False
    try:
        def missing_at(fd: int, leaf: str) -> bool:
            try:
                os.stat(leaf, dir_fd=fd, follow_symlinks=False)
                return False
            except FileNotFoundError:
                return True

        target_parent = _open_directory(tx.directory, private=True)
        source = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=source_parent)
        source_stat = os.fstat(source)
        source_id = _dir_marker(source_stat)
        if (not stat.S_ISDIR(source_stat.st_mode) or source_stat.st_uid != os.geteuid()
                or stat.S_IMODE(source_stat.st_mode) != 0o700 or source_id != expected
                or getattr(source_stat, "st_flags", 0)
                or _dir_marker(os.stat(name, dir_fd=source_parent, follow_symlinks=False)) != source_id
                or _dir_marker(os.fstat(source_parent)) != runtime_id
                or _dir_marker(os.fstat(target_parent)) != tx.dir_id
                or source_stat.st_dev != os.fstat(source_parent).st_dev
                or source_stat.st_dev != os.fstat(target_parent).st_dev):
            raise SafetyError("运行目录来源、权限或卷身份不明")
        _verify_darwin_local_no_acl(source)
        if not missing_at(target_parent, destination):
            raise SafetyError("运行目录保全目标已存在")
        tx.intent("PARK_RUNTIME_DIRECTORY", {
            "source": name, "source_id": source_id, "source_parent_id": runtime_id,
            "target": destination, "target_parent_id": tx.dir_id,
        })
        _verify_target_runtime_ignore(tx, runtime)
        if (_dir_marker(runtime.lstat()) != runtime_id
                or _directory_identity(root) != tuple(tx.data["repository"]["root_id"])
                or _dir_marker(tx.directory.lstat()) != tx.dir_id
                or _dir_marker(os.fstat(source_parent)) != runtime_id
                or _dir_marker(os.fstat(target_parent)) != tx.dir_id
                or _dir_marker(os.fstat(source)) != source_id
                or _dir_marker(os.stat(name, dir_fd=source_parent, follow_symlinks=False)) != source_id
                or not missing_at(target_parent, destination)):
            raise SafetyError("运行目录迁出前身份发生变化")
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        rename = libc.renameatx_np
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(source_parent, os.fsencode(name), target_parent, os.fsencode(destination), 0x00000004) != 0:
            raise SafetyError("运行目录无覆盖迁出失败")
        moved = True
        os.fsync(source_parent)
        os.fsync(target_parent)
        target_id = _dir_marker(os.stat(destination, dir_fd=target_parent, follow_symlinks=False))
        if (target_id != source_id or _dir_marker(os.fstat(source)) != source_id
                or not missing_at(source_parent, name)
                or _dir_marker(runtime.lstat()) != runtime_id
                or _dir_marker(tx.directory.lstat()) != tx.dir_id
                or _directory_identity(root) != tuple(tx.data["repository"]["root_id"])):
            raise SafetyError("运行目录迁出后验 UNKNOWN")
        _verify_darwin_local_no_acl(source)
        tx.done("PARK_RUNTIME_DIRECTORY", {"target_id": target_id})
    except (OSError, AttributeError, TypeError):
        raise SafetyError("运行目录迁出结果 UNKNOWN" if moved else "运行目录无法安全迁出") from None
    finally:
        if source >= 0:
            os.close(source)
        if target_parent >= 0:
            os.close(target_parent)
        os.close(source_parent)


def _park_runtime_for_return(tx: Manifest, root: Path) -> list[dict[str, Any]]:
    """旧 HEAD 不一定忽略 .runtime；将已知新版数据移到仓外私有目录。"""
    runtime = root / ".runtime"
    _verify_target_runtime_ignore(tx, runtime)
    files = {
        "model_proxy_totals.json": "new-ledger.before-return",
        "model_proxy.log": "new-log.before-return",
        "model_proxy_ensure.log": "new-ensure-log.before-return",
        "model_proxy.pid": "new-pid.before-return",
        "install.json": "new-install.before-return",
        "active.json": "new-active.before-return",
        "last_good_paths.json": "new-paths.before-return",
    }
    locks = {".gitignore", "anchor.lock", "model_proxy.lock", "control.lock",
             "model_proxy_start.lock"}
    # 只验证目录本身；其内部内容一律不读取、不删除。
    directories = {}
    parent = _open_directory(runtime, private=True)
    target_parent = -1
    try:
        target_parent = _open_directory(tx.directory, private=True)
        created = _find_event(tx, "CREATE_RUNTIME")
        if (created is None or _dir_marker(os.fstat(parent)) != tuple(created["runtime_id"])
                or _dir_marker(os.fstat(target_parent)) != tx.dir_id):
            raise SafetyError("运行目录或事务目录身份不明")
        with os.scandir(parent) as entries:
            for entry in entries:
                if entry.name in ("tmp", "backups"):
                    fd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 dir_fd=parent)
                    try:
                        st = os.fstat(fd)
                        if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid()
                                or stat.S_IMODE(st.st_mode) != 0o700
                                or getattr(st, "st_flags", 0)
                                or _dir_marker(st) != _dir_marker(os.stat(entry.name, dir_fd=parent,
                                                                         follow_symlinks=False))
                                or st.st_dev != os.fstat(parent).st_dev
                                or st.st_dev != os.fstat(target_parent).st_dev):
                            raise SafetyError("运行目录来源、权限或卷身份不明")
                        _verify_darwin_local_no_acl(fd)
                        directories[entry.name] = _dir_marker(st)
                    finally:
                        os.close(fd)
                    try:
                        os.stat("runtime-" + entry.name + ".before-return", dir_fd=target_parent,
                                follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise SafetyError("运行目录保全目标已存在")
                elif entry.name not in files and entry.name not in locks:
                    raise SafetyError("运行目录存在未知文件；保留现场人工处理")
        if (_dir_marker(runtime.lstat()) != tuple(created["runtime_id"])
                or _dir_marker(tx.directory.lstat()) != tx.dir_id
                or _directory_identity(root) != tuple(tx.data["repository"]["root_id"])):
            raise SafetyError("运行目录或事务目录父路径发生变化")
    except OSError:
        raise SafetyError("运行目录暂存状态无法验证") from None
    finally:
        if target_parent >= 0:
            os.close(target_parent)
        os.close(parent)
    for name in ("backups", "tmp"):
        if name in directories:
            _park_runtime_directory(tx, root, name, directories[name])
    runtime_id = _directory_identity(runtime)
    if runtime_id != tuple(created["runtime_id"]):
        raise SafetyError("运行目录迁出后父路径发生变化")
    parked = []
    for filename, name in files.items():
        path = runtime / filename
        if not os.path.lexists(path):
            continue
        raw, identity = _read_stable_file(path)
        tx.intent("PARK_RUNTIME_FILE", {"candidate": name, "source": filename, "source_id": identity,
                                        "runtime_id": runtime_id, "sha256": _sha256(raw)})
        again, marker = _read_stable_file(path)
        _verify_target_runtime_ignore(tx, runtime)
        if (again != raw or marker != identity or _directory_identity(runtime) != runtime_id
                or os.path.lexists(tx.directory / name)):
            raise SafetyError("新版运行数据迁出前发生变化")
        _rename_exclusive(runtime, filename, tx.directory, name)
        saved, saved_id = _read_stable_file(tx.directory / name)
        if saved != raw or not _same_object(identity, saved_id) or os.path.lexists(path):
            raise SafetyError("新版运行数据迁出后验 UNKNOWN")
        tx.done("PARK_RUNTIME_FILE", {"saved_id": saved_id})
        parked.append({"name": name, "source": filename, "identity": saved_id,
                       "sha256": _sha256(raw), "runtime_id": runtime_id})
    _check_runtime_parked(tx, root)
    return parked


def _restore_runtime_if_old_ignored(tx: Manifest, root: Path,
                                    parked: list[dict[str, Any]]) -> None:
    """仅当旧提交自身忽略整个运行目录，才保留新版数据的原路径。"""
    if not parked:
        return
    old_snapshot, _ = _load_snapshot_role(tx, "ledger")
    if old_snapshot is None:
        raise SafetyError("旧账本快照缺失")
    repo = tx.data["repository"]
    tree = _tree(root, repo["old_oid"])
    entry = tree.get(b".gitignore")
    if entry is None or entry[0] not in (b"100644", b"100755"):
        return
    content, marker = _read_stable_file(root / ".gitignore")
    if (_git_blob_digest(content, entry[1]) != entry[1] or _index(root) != tree
            or _git_scalar(root, "rev-parse", "--verify", "HEAD^{commit}") != repo["old_oid"]):
        raise SafetyError("旧提交或忽略文件身份不明")
    result = _git(root, "check-ignore", "--no-index", "-v", "--", ".runtime/", allowed=(0, 1))
    plain = _git(root, "check-ignore", "--no-index", "--", ".runtime/", allowed=(0, 1))
    if (not result.startswith(b".gitignore:") or not result.endswith(b"\t.runtime/\n")
            or plain != b".runtime/\n"):
        # 新数据已保全于仓外；用户授权回滚丢弃新版阶段数据。
        return
    for saved in parked:
        if saved["source"] == "model_proxy_totals.json" and _sha256(old_snapshot) == saved["sha256"]:
            # 即使旧规则忽略，旧 HEAD 也不放回切换前账本的逐字副本。
            continue
        data, identity = _read_stable_file(tx.directory / saved["name"])
        if (identity != tuple(saved["identity"]) or _sha256(data) != saved["sha256"]
                or _directory_identity(root / ".runtime") != tuple(saved["runtime_id"])
                or os.path.lexists(root / ".runtime" / saved["source"])):
            raise SafetyError("新版运行数据保全身份不明")
        tx.intent("RETAIN_RUNTIME_FILE", {"candidate": saved["name"], "candidate_id": identity,
                                          "target": saved["source"]})
        content2, marker2 = _read_stable_file(root / ".gitignore")
        if content2 != content or marker2 != marker or _index(root) != tree:
            raise SafetyError("旧忽略规则在新版数据保留前发生变化")
        _rename_exclusive(tx.directory, saved["name"], root / ".runtime", saved["source"])
        after, after_id = _read_stable_file(root / ".runtime" / saved["source"])
        if after != data or not _same_object(after_id, identity):
            raise SafetyError("新版运行数据保留后验 UNKNOWN")
        tx.done("RETAIN_RUNTIME_FILE", {"result_id": after_id})


def rollback_operation(
    *, root: Path, worktree: Path, transaction: str, port: int,
    old_cwd: str, new_cwd: str, old_home: str, new_home: str,
    migration_base: Path, legacy_lock: Path,
    quiescence: Any = check_processes_and_port, confirm: Any = None,
) -> Manifest:
    """正常回滚；测试隔离路径只能由调用此函数的测试代码直接注入。"""
    _verify_migration_base(migration_base)
    with contextlib.ExitStack() as stack:
        shared = stack.enter_context(HeldLock(migration_base / "migration.lock"))
        tx = Manifest.load(migration_base, transaction)
        repo = tx.data["repository"]
        root = root.resolve(strict=True)
        worktree = worktree.resolve(strict=True)
        if os.fspath(root) != repo["root"] or os.fspath(worktree) != repo["worktree"]:
            raise SafetyError("主仓或 W 与事务记录不一致")
        if tx.data["stage"] in ("ROLLED_BACK", "ROLLED_BACK_RUNTIME_RETAINED"):
            raise SafetyError("事务已结束；保留的 .runtime 不可直接重跑升级")
        if tx.data["stage"] != "VERIFIED" or (
                tx.data["events"] and tx.data["events"][-1].get("state") == "INTENT"):
            raise SafetyError("UNKNOWN：事务未完成；保留快照和现场，不自动重跑，请人工核验")
        _check_context_for_return(tx, root, port=port, old_cwd=old_cwd, new_cwd=new_cwd,
                                  old_home=old_home, new_home=new_home)
        if confirm is not None:
            confirm(tx)
        context = _check_context_for_return(tx, root, port=port, old_cwd=old_cwd, new_cwd=new_cwd,
                                            old_home=old_home, new_home=new_home)
        _check_both_listeners(quiescence, context)
        locks = _acquire_recorded_locks(tx, stack, shared, legacy_lock, root)
        _check_both_listeners(quiescence, context)
        _prepare_git_transition(repo, reverse=True)
        context = _check_context_for_return(tx, root, port=port, old_cwd=old_cwd, new_cwd=new_cwd,
                                            old_home=old_home, new_home=new_home)
        snapshots = {name: _load_snapshot_role(tx, name) for name in ("config", "sidecar", "ledger")}
        _check_snapshot_roles([snapshots[name][1] for name in ("config", "sidecar", "ledger")],
                              tx.data["context_evidence"])
        for name in ("config", "sidecar", "ledger"):
            _authorized_role_current(snapshots[name][1], expected_marker=context["role_markers"][name])
        _verify_legacy_log(tx, root)
        _check_locks(locks)
        if repo.get("tracked_config"):
            target_tree = _tree(root, repo["target_oid"])
            baseline = _tree_blob(root, target_tree, os.fsencode(repo["tracked_config"]))
            _restore_role(tx, "config_prepare_rollback", baseline, snapshots["config"][1])
            _refresh_tracked_config_index(tx)
        parked_runtime = _park_runtime_for_return(tx, root)
        _check_locks(locks)
        _git_transition(tx, reverse=True)
        _restore_runtime_if_old_ignored(tx, root, parked_runtime)
        for name in ("config", "sidecar", "ledger"):
            _check_locks(locks)
            _restore_role(tx, name, *snapshots[name], expected_marker=context["role_markers"][name])
        move = _find_event(tx, "MOVE_CONFLICT")
        if move:
            _restore_conflict(tx, root, move)
            pin = _find_event(tx, "PIN_CONFLICT_PARENT")
            if pin is None:
                raise SafetyError("撞名文档父目录哨兵记录缺失，UNKNOWN")
            _release_conflict_parent(tx, root, pin)
        for name in ("config", "sidecar", "ledger"):
            old_raw, metadata = _load_snapshot_role(tx, name)
            restored = _find_event(tx, "RESTORE_" + name.upper() + "_SNAPSHOT")
            expected = tuple(restored["result_id"]) if restored else None
            current, observed = _authorized_role_current(metadata, expected_marker=expected)
            if current != old_raw or (old_raw is not None and
                    (stat.S_IMODE(observed["target_id"][2]) != stat.S_IMODE(metadata["target_id"][2])
                     or observed["xattrs"] != metadata["xattrs"])):
                raise SafetyError("业务快照最终后验 UNKNOWN")
        context = _check_context_for_return(tx, root, port=port, old_cwd=old_cwd, new_cwd=new_cwd,
                                            old_home=old_home, new_home=new_home)
        _check_git_identity(root, worktree, repo["old_oid"], repo["target_oid"],
                            current=repo["old_oid"], root_id=repo["root_id"],
                            worktree_id=repo["worktree_id"], git_id=repo["git_id"],
                            w_git_id=repo["w_git_id"], w_admin=repo["w_admin"],
                            w_admin_id=repo["w_admin_id"])
        _verify_restored_git(tx, root)
        _verify_legacy_log(tx, root)
        _check_both_listeners(quiescence, context)
        _check_locks(locks)
        _checkpoint(tx, "VERIFY_ROLLBACK", stage="ROLLED_BACK")
        return tx


def recover_operation(
    *, root: Path, worktree: Path, transaction: str, port: int,
    old_cwd: str, new_cwd: str, old_home: str, new_home: str,
    migration_base: Path, legacy_lock: Path,
    quiescence: Any = check_processes_and_port, confirm: Any = None,
) -> Manifest:
    """异常事务不猜测、不自动重放；保留快照并交由人工核验。"""
    _verify_migration_base(migration_base)
    with HeldLock(migration_base / "migration.lock"):
        tx = Manifest.load(migration_base, transaction)
        repo = tx.data["repository"]
        if (os.fspath(root.resolve(strict=True)) != repo["root"]
                or os.fspath(worktree.resolve(strict=True)) != repo["worktree"]):
            raise SafetyError("UNKNOWN：事务主仓或 W 不一致；保留现场人工核验")
        if tx.data["stage"] in ("ROLLED_BACK", "ROLLED_BACK_RUNTIME_RETAINED"):
            raise SafetyError("事务已结束；不要重复覆盖或直接重跑升级")
        events = tx.data.get("events", [])
        pending = events[-1].get("action", "未知") if events else "无"
        advance = any(event.get("action") == "GIT_ADVANCE" for event in events)
        installed = _find_event(tx, "INSTALL_NEW_LEDGER")
        hint = ("Git 可能已前进但新账本尚未确认安装；旧账本和私有快照须人工核验；"
                if advance and installed is None else "")
        raise SafetyError("UNKNOWN：异常事务不自动重放；保留快照和现场，"
                          + hint + "在停服禁自动入口并冻结 Git/编辑后人工核验；最后步骤：" + str(pending))


def _port(value: str) -> int:
    if len(value) > 5 or not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError("端口必须是 1 到 65535 的十进制整数")
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("端口必须是 1 到 65535 的十进制整数")
    return number


def _absolute_context(value: str) -> str:
    if not os.path.isabs(value):
        raise argparse.ArgumentTypeError("启动上下文须使用明确的绝对路径")
    return value


def _target(value: str) -> str:
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None:
        raise argparse.ArgumentTypeError("目标提交须为完整的小写 Git OID")
    return value


def _transaction(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None:
        raise argparse.ArgumentTypeError("事务 ID 格式无效")
    return value


def _parser(mode: str) -> argparse.ArgumentParser:
    action = "切换" if mode == "switch" else "回滚/中断恢复"
    parser = argparse.ArgumentParser(
        prog=f"macos_{'switch' if mode == 'switch' else 'rollback'}.sh",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            f"macOS 手动{action}。脚本不启停服务、不发送信号、不访问模型或外网，"
            "也不读取 appkey_file 内容。"
        ),
        epilog=(
            "实际执行前：先停止新请求并等待在途请求完成，手动停稳所有实例、"
            "关闭全部自动启动入口；冻结主仓和嵌套 W 的编辑与 Git 命令，直至完成。"
            "进程扫描只覆盖可识别入口，通用 server.py 无法判定归属；"
            "不能发现任意改名或自定义入口，须由操作者自行确保停稳。\n"
            "即便如此，也只能承诺最后可验证的磁盘账本，内存中未落盘的统计可能丢失；"
            "实际执行须由真人明确接受该风险，无法判断时报告未知。\n\n"
            "旧版启动可能先裁剪日志再获取锁；持锁不能防止误启动影响日志。"
            "迁移/恢复期间不得启动服务。新端口须核对实际监听地址，不能只凭环境变量推定。\n\n"
            "回滚时上下文参数顺序与切换相反；应与事务记录逐项核对。"
            "配置路径与启动时环境须由操作者在终端明确提供；不接受非交互式代答。"
            "配置、账本及受影响 Git 文件超过 64 MiB 时拒绝；旧日志以流式保全。\n\n"
            "仅支持默认配置、同目录 sidecar、根目录旧账本，均须为当前用户 "
            "0600 单链接普通文件；拒绝符号链接及与 appkey_file 的角色重叠。"
            "多个 appkey_file 指向同一规范路径且身份一致时允许共用；其他别名仍拒绝。"
            "若原 sidecar 不存在而后来出现对象，拒绝读取和删除，须人工处理。\n\n"
            "Git 的最终冲突扫描与实际写入之间仍有非协作并发窗口；"
            "reset --keep 不是 ignored 文件的无覆盖保证，必须持续冻结编辑和 Git 操作。\n\n"
            "异常中断后不自动重放；保留快照和现场，停服后由人工核验。"
            "这是多文件事务，不是全局原子切换。"
        ),
    )
    parser.add_argument("--root", required=True, help="明确指定的主仓根路径，可用相对路径")
    if mode == "switch":
        parser.add_argument("--target", required=True, type=_target, help="已验收并提交的 W HEAD 完整 OID")
    else:
        parser.add_argument("--transaction", required=True, type=_transaction, help="切换输出的事务 ID")
        parser.add_argument("--recover", action="store_true",
                            help="检查异常事务并报告 UNKNOWN；不自动重放，须人工处理")
    parser.add_argument("--port", required=mode == "switch", type=_port,
                        help="当前实例监听端口；回滚默认取事务记录并须人工核对")
    parser.add_argument("--old-cwd", required=mode == "switch", type=_absolute_context,
                        help="当前版本实际启动 cwd；回滚默认取事务记录")
    parser.add_argument("--new-cwd", required=mode == "switch", type=_absolute_context,
                        help="目标版本计划启动 cwd；回滚默认取事务记录")
    parser.add_argument("--old-home", required=mode == "switch", type=_absolute_context,
                        help="当前版本实际启动 HOME；回滚默认取事务记录")
    parser.add_argument("--new-home", required=mode == "switch", type=_absolute_context,
                        help="目标版本计划启动 HOME；回滚默认取事务记录")
    parser.add_argument(
        "--manual-stopped", required=True, action="store_true",
        help="本人声明相关服务及所有实例已手动停稳；并非脚本检测证明",
    )
    parser.add_argument(
        "--auto-start-disabled", required=True, action="store_true",
        help="本人声明所有自动启动入口已关闭；并非脚本检测证明",
    )
    return parser


def _ask(tty: Any, question: str) -> str:
    tty.write(question)
    tty.flush()
    value = tty.readline(4097)
    if not value or len(value) > 4096 or "\0" in value:
        raise SafetyError("缺少有效的终端人工声明")
    return value.rstrip("\r\n")


def _manual_risk_confirmation(tty: Any) -> None:
    tty.write(
        "\n请确认已停止接收新请求、等待在途完成、停稳所有相关实例、关闭全部自动启动，"
        "并冻结主仓及 W 的编辑和 Git 命令直至结束。\n"
        "进程扫描无法发现任意改名或自定义入口，须由操作者确认停稳。\n"
        "停止进程不证明内存增量已持久化；只能承诺最后可验证磁盘账本，可能缺账。"
        "回滚将恢复切换前快照，切换后的配置、路由和统计允许丢失，不合并新版账本。"
        "旧版启动可能在取锁前裁剪日志，恢复完成前不得启动。\n"
    )
    if _ask(tty, "本人确认并接受可能缺账，请输入「接受可能缺账」：") != "接受可能缺账":
        raise SafetyError("未明确接受可能缺账，未开始事务")


def _signal_interrupt(_number: int, _frame: Any) -> None:
    global _INTERRUPT_PENDING
    if _CHILD_ACTIVE:
        _INTERRUPT_PENDING = True
        return
    raise InterruptedError("interrupted")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] not in ("switch", "rollback"):
        print("请使用 macos_switch.sh 或 macos_rollback.sh。", file=sys.stderr)
        return 2
    mode, rest = args[0], args[1:]
    parsed = _parser(mode).parse_args(rest)

    print("预览：", "切换" if mode == "switch" else ("中断恢复" if parsed.recover else "回滚"))
    print("主仓：", parsed.root)
    if mode == "switch":
        print("目标 OID：", parsed.target)
    else:
        print("事务 ID：", parsed.transaction)
    if mode == "switch":
        print("声明的监听端口：", parsed.port)
        print("启动上下文（当前 → 目标）：")
        print("  cwd：", parsed.old_cwd, "→", parsed.new_cwd)
        print("  HOME：", parsed.old_home, "→", parsed.new_home)
    else:
        print("监听端口与启动上下文：从事务记录读取，并由操作者核对。")
    tx_seen: list[Manifest] = []
    allocation_seen: list[tuple[str, Path]] = []
    tx_dir: Path | None = None
    previous: dict[int, Any] = {}
    try:
        if sys.platform != "darwin":
            raise SafetyError("仅支持 macOS")
        # 必须有操作者可见的控制终端；不从 stdin 或环境变量代答。
        try:
            tty = open("/dev/tty", "r+", encoding="utf-8", buffering=1)
        except OSError:
            raise SafetyError("无交互终端，不能代替操作者确认") from None
        with tty:
            if not os.isatty(tty.fileno()):
                raise SafetyError("无法确认交互终端")
            root = Path(parsed.root).resolve(strict=True)
            worktree = Path(__file__).resolve(strict=True).parent.parent
            for number in (signal.SIGINT, signal.SIGTERM):
                previous[number] = signal.signal(number, _signal_interrupt)
            if mode == "rollback":
                base = _migration_base(create=False)
                tx_dir = base / parsed.transaction
                # 即便事务 ID 不存在，也先竞争全体事务共享锁，避免与正在
                # 切换的另一事务并行观察半成品。
                with HeldLock(base / "migration.lock"):
                    preview_tx = Manifest.load(base, parsed.transaction)
                saved = preview_tx.data["contexts"]
                saved_evidence = preview_tx.data["context_evidence"]
                expected = {
                    "port": saved_evidence["new_listen"][1],
                    "old_cwd": saved["new_cwd"], "new_cwd": saved["old_cwd"],
                    "old_home": saved["new_home"], "new_home": saved["old_home"],
                }
                for key, value in expected.items():
                    supplied = getattr(parsed, key)
                    if supplied is not None and supplied != value:
                        raise SafetyError("提供的回滚端口或启动上下文与事务记录冲突")
                    setattr(parsed, key, value)
            common = {
                "root": root, "worktree": worktree, "port": parsed.port,
                "old_cwd": parsed.old_cwd, "new_cwd": parsed.new_cwd,
                "old_home": parsed.old_home, "new_home": parsed.new_home,
            }
            if mode == "switch":
                raw = _ask(tty, "旧/新启动共同的 MODEL_PROXY_CONFIG 原值；两者均未设置请输入 UNSET：")
                config_raw = None if raw == "UNSET" else raw
                old_value = _ask(tty, "旧启动时 MODEL_PROXY_PORT 原值；未设置请输入 UNSET：")
                new_value = _ask(tty, "计划新启动时 MODEL_PROXY_PORT 原值；未设置请输入 UNSET：")
                old_env_port = None if old_value == "UNSET" else old_value
                new_env_port = None if new_value == "UNSET" else new_value
                preview = inspect_context(
                    root, old_cwd=parsed.old_cwd, new_cwd=parsed.new_cwd,
                    old_home=parsed.old_home, new_home=parsed.new_home,
                    config_raw=config_raw, old_env_port=old_env_port,
                    new_env_port=new_env_port, declared_port=parsed.port,
                )
                tty.write(f"配置原始路径：{preview['config_original']}\n")
                tty.write(f"原 sidecar 路径：{preview['sidecar_original']}\n")
                tty.write(f"旧监听地址和端口：{preview['old_listen']}\n")
                tty.write(f"计划新监听地址和端口：{preview['new_listen']}\n")
                if _ask(tty, "请确认与实际旧启动及计划新启动环境一致，输入「一致」：") != "一致":
                    raise SafetyError("启动环境未确认")
                _manual_risk_confirmation(tty)
                base = _migration_base()
                legacy = _legacy_lock_path()
                def notify(created: Manifest) -> None:
                    tx_seen.append(created)
                def allocated(txid: str, directory: Path) -> None:
                    allocation_seen.append((txid, directory))
                    print("事务候选 ID：", txid, flush=True)
                    print("拟用私有保全位置：", directory, flush=True)
                result = switch_operation(
                    **common, target=parsed.target, config_raw=config_raw,
                    old_env_port=old_env_port, new_env_port=new_env_port,
                    migration_base=base, legacy_lock=legacy, on_transaction=notify,
                    on_allocated=allocated,
                    expected_context=preview,
                )
                print("版本和磁盘交付完成，服务未启动。")
            else:
                legacy = _legacy_lock_path()
                def confirm_return(record: Manifest) -> None:
                    tx_seen.append(record)
                    saved = record.data["contexts"]
                    tty.write("\n事务记录：\n")
                    tty.write(f"  原旧 cwd/HOME：{saved['old_cwd']} / {saved['old_home']}\n")
                    tty.write(f"  原新 cwd/HOME：{saved['new_cwd']} / {saved['new_home']}\n")
                    tty.write(f"  配置原始路径：{record.data['context_evidence']['config_original']}\n")
                    tty.write(f"  sidecar 原始路径：{record.data['context_evidence']['sidecar_original']}\n")
                    tty.write(f"  记录中的新版监听：{record.data['context_evidence']['new_listen']}\n")
                    if _ask(tty, "请确认记录中的监听与新版实际启动一致、所有实例已停稳，"
                                 "且启动上下文反向对应；输入「一致」：") != "一致":
                        raise SafetyError("回滚上下文未确认")
                    _manual_risk_confirmation(tty)
                op = recover_operation if parsed.recover else rollback_operation
                result = op(**common, transaction=parsed.transaction, migration_base=base,
                            legacy_lock=legacy, confirm=confirm_return)
                print("旧版本和切换前快照已恢复，服务未启动。")
            print("事务 ID：", result.data["transaction"])
            print("保全位置：", result.directory)
            print("请由操作者自行检查并启动；该事务不是全局原子切换。")
            return 0
    except (SafetyError, InvalidLedger) as exc:
        print("已停止：", str(exc), file=sys.stderr)
    except (InterruptedError, KeyboardInterrupt):
        print("已中断：停止后续步骤，状态可能 UNKNOWN。", file=sys.stderr)
    except Exception:
        print("已停止：系统错误或后验不明，状态 UNKNOWN。", file=sys.stderr)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    if tx_seen:
        tx = tx_seen[-1]
        pending = tx.data.get("events", [])
        if (mode == "switch" and any(event.get("action") == "GIT_ADVANCE" for event in pending)
                and _find_event(tx, "INSTALL_NEW_LEDGER") is None):
            print("Git 可能已前进但新账本尚未确认安装；旧账本和私有快照均应保留，"
                  "须停服并人工核验。", file=sys.stderr)
        if pending and pending[-1].get("state") == "INTENT":
            print(f"未闭合步骤：{pending[-1].get('action', '未知')}；结果 UNKNOWN。",
                  file=sys.stderr)
        elif mode == "rollback" and parsed.recover:
            print("恢复未完成；未能验证的对象状态为 UNKNOWN。", file=sys.stderr)
        print(f"事务 ID：{tx.data['transaction']}；最后已知阶段：{tx.data['stage']}（可能滞后）；"
              f"保全位置：{tx.directory}", file=sys.stderr)
    elif allocation_seen:
        txid, directory = allocation_seen[-1]
        print(f"事务候选 ID：{txid}；创建结果 UNKNOWN；拟用位置：{directory}", file=sys.stderr)
    elif mode == "rollback":
        print(f"事务 ID：{parsed.transaction}；阶段 UNKNOWN；保全位置：{tx_dir or '未验证'}", file=sys.stderr)
    print("保留现场；不要启动服务。请在仍停服、禁自动启动、冻结编辑/Git 的前提下检查并恢复。",
          file=sys.stderr)
    print("警告：账本可能未被Git忽略，禁止 git add/push；保持停服并检查事务。", file=sys.stderr)
    if mode == "rollback" and parsed.recover:
        print("人工处理：先保存并核对事务 manifest、Git HEAD/index 与旧/新账本身份和摘要；"
              "不要重跑升级，不要删除 .runtime、锁或未知文件，也不要执行 reset --hard/clean。"
              "身份不能闭合时请人工审查后另定方案。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
