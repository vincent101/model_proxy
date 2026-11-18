"""Cooperative Python dir_fd guard, not an OS or native-code sandbox.

Only directory descriptors opened or duplicated through these wrappers are
accepted. Each use revalidates the live FD's kernel path and device/inode.
The last check and syscall are not atomic against a concurrent same-user actor.
"""
import contextlib
import os
import stat
import sys
import threading
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None


def install(root, readonly):
    root = Path(root)
    local = threading.local()
    lock = threading.RLock()
    known = {}
    names = ('open', 'stat', 'lstat', 'readlink', 'rename', 'replace', 'link',
             'unlink', 'remove', 'mkdir', 'rmdir', 'symlink', 'dup', 'dup2',
             'close', 'fchdir', 'listdir', 'scandir', 'chmod', 'chown', 'utime', 'access', 'truncate')
    raw = {name: getattr(os, name) for name in names if hasattr(os, name)}

    @contextlib.contextmanager
    def internal():
        depth = getattr(local, 'depth', 0)
        local.depth = depth + 1
        try:
            yield
        finally:
            local.depth = depth

    def bypass():
        return bool(getattr(local, 'depth', 0))

    def realpath(value):
        with internal():
            return Path(os.path.realpath(os.fsdecode(value)))

    def fd_path(fd):
        if type(fd) is not int or fd < 0:
            raise PermissionError('link test guard: unknown dir_fd')
        with lock:
            expected = known.get(fd)
        if expected is None:
            raise PermissionError('link test guard: unknown dir_fd')
        try:
            with internal():
                info = os.fstat(fd)
                if sys.platform == 'darwin' and fcntl is not None and hasattr(fcntl, 'F_GETPATH'):
                    path = fcntl.fcntl(fd, fcntl.F_GETPATH, b'\0' * 1024).split(b'\0', 1)[0]
                    if not path:
                        raise OSError('empty F_GETPATH')
                    path = realpath(path)
                elif sys.platform.startswith('linux'):
                    path = realpath(raw['readlink']('/proc/self/fd/%d' % fd))
                else:
                    raise OSError('descriptor path unsupported')
                named = raw['stat'](str(path))
                after = os.fstat(fd)
            ident = (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
            if (not stat.S_ISDIR(info.st_mode) or ident != expected[1] or path != expected[0]
                    or (named.st_dev, named.st_ino) != ident[:2]
                    or (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode)) != ident):
                raise OSError('descriptor identity changed')
            return path
        except (OSError, ValueError, TypeError):
            raise PermissionError('link test guard: dir_fd identity unknown') from None

    def candidate(value, dir_fd=None, *, follow=True):
        # Even for an absolute pathname, an explicitly supplied unknown FD is
        # rejected rather than relying on the kernel ignoring it.
        base = fd_path(dir_fd) if dir_fd is not None else None
        if not isinstance(value, (str, bytes, os.PathLike)):
            raise PermissionError('link test guard: invalid path')
        text = os.fsdecode(value)
        if not text or '\0' in text:
            raise PermissionError('link test guard: invalid path')
        if not os.path.isabs(text):
            base = base if base is not None else Path(os.getcwd())
            text = os.path.join(str(base), text)
        if follow or os.path.basename(text) in ('', '.', '..'):
            return realpath(text)
        return realpath(os.path.dirname(text)) / os.path.basename(text)

    def check(value, dir_fd=None, *, write=False, follow=True, ancestor=False):
        path = candidate(value, dir_fd, follow=follow)
        allowed = path == Path('/dev/null') or path == root or root in path.parents
        if not write:
            allowed = allowed or any(path == item or item in path.parents for item in readonly)
            if ancestor:
                allowed = allowed or path in root.parents or any(path in item.parents for item in readonly)
                if not follow:
                    allowed = allowed or realpath(path) in root.parents
        if not allowed:
            raise PermissionError('link test guard: dir_fd path outside test root')
        return path

    def record_dir(fd):
        try:
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode):
                return
            with internal():
                if sys.platform == 'darwin' and fcntl is not None and hasattr(fcntl, 'F_GETPATH'):
                    text = fcntl.fcntl(fd, fcntl.F_GETPATH, b'\0' * 1024).split(b'\0', 1)[0]
                elif sys.platform.startswith('linux'):
                    text = raw['readlink']('/proc/self/fd/%d' % fd)
                else:
                    raise OSError('descriptor path unsupported')
                path = realpath(text)
                named = raw['stat'](str(path))
            ident = (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
            allowed = (path == root or root in path.parents or path in root.parents
                       or any(path == item or item in path.parents or path in item.parents for item in readonly))
            if not text or not allowed or (named.st_dev, named.st_ino) != ident[:2]:
                raise OSError('descriptor pathname unknown')
            with lock:
                known[fd] = (path, ident)
        except (OSError, ValueError, TypeError):
            with internal():
                raw['close'](fd)
            raise PermissionError('link test guard: directory FD unavailable') from None

    def wrapped_open(path, flags, mode=0o777, *, dir_fd=None):
        if bypass():
            return raw['open'](path, flags, mode, dir_fd=dir_fd)
        write = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        nofollow = bool(flags & (os.O_NOFOLLOW | getattr(os, 'O_SYMLINK', 0)))
        check(path, dir_fd, write=write, follow=not nofollow,
              ancestor=not write and bool(flags & os.O_DIRECTORY))
        with internal():
            fd = raw['open'](path, flags, mode, dir_fd=dir_fd)
        record_dir(fd)
        return fd

    def wrapped_stat(path, *, dir_fd=None, follow_symlinks=True):
        if bypass():
            return raw['stat'](path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if isinstance(path, int):
            if dir_fd is not None:
                raise PermissionError('link test guard: invalid fd stat')
            fd_path(path)
        else:
            check(path, dir_fd, follow=follow_symlinks, ancestor=True)
        with internal():
            return raw['stat'](path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    def wrapped_lstat(path, *, dir_fd=None):
        if not bypass():
            check(path, dir_fd, follow=False, ancestor=True)
        with internal():
            return raw['lstat'](path, dir_fd=dir_fd)

    def wrapped_readlink(path, *, dir_fd=None):
        if not bypass():
            check(path, dir_fd, follow=False, ancestor=True)
        with internal():
            return raw['readlink'](path, dir_fd=dir_fd)

    def rename_like(name, src, dst, src_dir_fd=None, dst_dir_fd=None):
        if not bypass():
            check(src, src_dir_fd, write=True, follow=False)
            check(dst, dst_dir_fd, write=True, follow=False)
        with internal():
            return raw[name](src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    def wrapped_rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        return rename_like('rename', src, dst, src_dir_fd, dst_dir_fd)

    def wrapped_replace(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        return rename_like('replace', src, dst, src_dir_fd, dst_dir_fd)

    def wrapped_link(src, dst, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
        if not bypass():
            check(src, src_dir_fd, write=True, follow=follow_symlinks)
            check(dst, dst_dir_fd, write=True, follow=False)
        with internal():
            return raw['link'](src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd,
                               follow_symlinks=follow_symlinks)

    def one_path(name, path, *, dir_fd=None):
        if not bypass():
            check(path, dir_fd, write=True, follow=False)
        with internal():
            return raw[name](path, dir_fd=dir_fd)

    def wrapped_unlink(path, *, dir_fd=None): return one_path('unlink', path, dir_fd=dir_fd)
    def wrapped_remove(path, *, dir_fd=None): return one_path('remove', path, dir_fd=dir_fd)
    def wrapped_rmdir(path, *, dir_fd=None): return one_path('rmdir', path, dir_fd=dir_fd)

    def wrapped_mkdir(path, mode=0o777, *, dir_fd=None):
        if not bypass():
            check(path, dir_fd, write=True, follow=False)
        with internal():
            return raw['mkdir'](path, mode, dir_fd=dir_fd)

    def wrapped_symlink(src, dst, target_is_directory=False, *, dir_fd=None):
        if not bypass():
            check(dst, dir_fd, write=True, follow=False)
        with internal():
            return raw['symlink'](src, dst, target_is_directory=target_is_directory, dir_fd=dir_fd)

    def wrapped_chmod(path, mode, *, dir_fd=None, follow_symlinks=True):
        if not bypass():
            check(path, dir_fd, write=True, follow=follow_symlinks)
        with internal():
            return raw['chmod'](path, mode, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    def wrapped_chown(path, uid, gid, *, dir_fd=None, follow_symlinks=True):
        if not bypass():
            check(path, dir_fd, write=True, follow=follow_symlinks)
        with internal():
            return raw['chown'](path, uid, gid, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    def wrapped_utime(path, times=None, *, ns=None, dir_fd=None, follow_symlinks=True):
        if not bypass():
            check(path, dir_fd, write=True, follow=follow_symlinks)
        with internal():
            if ns is None:
                return raw['utime'](path, times, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
            return raw['utime'](path, ns=ns, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    def wrapped_access(path, mode, *, dir_fd=None, effective_ids=False, follow_symlinks=True):
        if not bypass():
            check(path, dir_fd, follow=follow_symlinks, ancestor=True)
        with internal():
            return raw['access'](path, mode, dir_fd=dir_fd, effective_ids=effective_ids,
                                 follow_symlinks=follow_symlinks)

    def wrapped_truncate(path, length):
        if not bypass():
            check(path, write=True)
        with internal():
            return raw['truncate'](path, length)

    def wrapped_close(fd):
        with lock:
            known.pop(fd, None)
        return raw['close'](fd)

    def wrapped_dup(fd):
        with lock:
            value = known.get(fd)
        if value is not None:
            fd_path(fd)
        new = raw['dup'](fd)
        if value is not None:
            with lock:
                known[new] = value
        return new

    def wrapped_dup2(fd, fd2, inheritable=True):
        with lock:
            value = known.get(fd)
        if value is not None:
            fd_path(fd)
        result = raw['dup2'](fd, fd2, inheritable=inheritable)
        with lock:
            known.pop(fd2, None)
            if value is not None:
                known[fd2] = value
        return result

    def wrapped_fchdir(fd):
        path = fd_path(fd)
        if not (path == root or root in path.parents):
            raise PermissionError('link test guard: fchdir outside test root')
        with internal():
            return raw['fchdir'](fd)

    def directory_contents(name, path='.'):
        if not bypass():
            if isinstance(path, int):
                actual = fd_path(path)
                if not (actual == root or root in actual.parents
                        or any(actual == item or item in actual.parents for item in readonly)):
                    raise PermissionError('link test guard: directory listing outside test root')
            else:
                check(path)
        with internal():
            return raw[name](path)

    def wrapped_listdir(path='.'): return directory_contents('listdir', path)
    def wrapped_scandir(path='.'): return directory_contents('scandir', path)

    wrappers = {name[8:]: value for name, value in locals().copy().items()
                if name.startswith('wrapped_')}
    for name, wrapper in wrappers.items():
        if name not in raw:
            continue
        original = raw[name]
        setattr(os, name, wrapper)
        # Standard-library callers feature-detect using function identity.
        for support in ('supports_dir_fd', 'supports_fd', 'supports_follow_symlinks'):
            values = getattr(os, support, None)
            if values is not None and original in values:
                values.add(wrapper)
    return bypass
