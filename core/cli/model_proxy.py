#!/usr/bin/env python3
"""Service bootstrap; resolves the project root independently of cwd."""
import sys
from pathlib import Path

if sys.version_info < (3, 10):
    sys.exit("Error: Python >= 3.10 is required by current code; platform support has not been fully validated.")


def _darwin_initial_launch():
    """Read our kernel launch arguments/environment before importing server."""
    import ctypes
    import os
    import struct

    libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    call = libc.sysctl
    call.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                     ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    call.restype = ctypes.c_int
    mib = (ctypes.c_int * 2)(1, 8)
    limit, size = ctypes.c_int(), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
    if call(mib, 2, ctypes.byref(limit), ctypes.byref(size), None, 0) or size.value != 4 or not 4096 <= limit.value <= 8 * 1024 * 1024:
        raise ValueError('kernel arguments unavailable')
    mib = (ctypes.c_int * 3)(1, 49, os.getpid())
    buf, size = ctypes.create_string_buffer(limit.value), ctypes.c_size_t(limit.value)
    if call(mib, 3, buf, ctypes.byref(size), None, 0) or not 8 <= size.value < len(buf):
        raise ValueError('kernel arguments unavailable')
    raw = buf.raw[:size.value]
    argc = struct.unpack_from('=i', raw)[0]
    if not 1 <= argc <= 128:
        raise ValueError('kernel arguments unavailable')
    end = raw.find(b'\0', 4)
    if end <= 4:
        raise ValueError('kernel arguments unavailable')
    pos = end + 1
    while pos < len(raw) and raw[pos] == 0:
        pos += 1
    args = []
    for _ in range(argc):
        end = raw.find(b'\0', pos)
        if end < pos:
            raise ValueError('kernel arguments unavailable')
        args.append(os.fsdecode(raw[pos:end]))
        pos = end + 1
    selected = {}
    while pos < len(raw):
        end = raw.find(b'\0', pos)
        if end < 0:
            raise ValueError('kernel environment unavailable')
        item, pos = raw[pos:end], end + 1
        if not item:
            break
        key, sep, value = item.partition(b'=')
        if sep and key in (b'MODEL_PROXY_CONFIG', b'MODEL_PROXY_START_NONCE', b'MODEL_PROXY_PORT',
                           b'MODEL_PROXY_INTERNAL_REEXEC', b'LC_CTYPE'):
            name = os.fsdecode(key)
            if name in selected:
                raise ValueError('duplicate environment')
            selected[name] = os.fsdecode(value)
    else:
        raise ValueError('kernel environment unavailable')
    return args, selected


def _normalize_darwin_launch():
    """Re-exec only when necessary, preserving the launcher/venv and -B.

    Do not normpath config: symlink/.. must retain traversal semantics. An
    optional transition marker is checked only for rejection, not authority.
    """
    import hashlib
    import json
    import os
    import re
    import shutil
    from core.discovery import process_executable

    args, initial_env = _darwin_initial_launch()
    if args != getattr(sys, 'orig_argv', None):
        raise ValueError('launch arguments changed')
    if len(args) == 2:
        flags, entry = [], args[1]
    elif len(args) == 3 and args[1] == '-B':
        flags, entry = ['-B'], args[2]
    else:
        raise ValueError('unsupported Python invocation')
    if len(sys.argv) != 1 or sys.argv[0] != entry or not entry or entry.startswith('-'):
        raise ValueError('unsupported service arguments')
    for key in ('MODEL_PROXY_CONFIG', 'MODEL_PROXY_START_NONCE', 'MODEL_PROXY_PORT',
                'MODEL_PROXY_INTERNAL_REEXEC'):
        if initial_env.get(key) != os.environ.get(key):
            raise ValueError('launch environment changed')
    image = process_executable(os.getpid())
    cwd = os.getcwd()
    original_image = args[0]
    if not os.path.isabs(original_image) and '/' not in original_image:
        original_image = shutil.which(original_image)
    if not image or not original_image or not os.path.isabs(image):
        raise ValueError('Python process image unavailable')
    if not os.path.samefile(original_image, image):
        raise ValueError('Python process image unavailable')
    image_path = Path(image)
    if not (re.fullmatch(r'python(?:3(?:\.\d+)?)?', image_path.name)
            or image_path.name == 'Python'
            and image_path.parts[-4:] == ('Python.app', 'Contents', 'MacOS', 'Python')
            and 'Python.framework' in image_path.parts):
        raise ValueError('unsupported Python image')
    resolved_entry = Path(entry).resolve(strict=True)
    if resolved_entry != Path(__file__).resolve(strict=True):
        raise ValueError('service entry changed')
    config = os.environ.get('MODEL_PROXY_CONFIG')
    relative_config = bool(config) and not os.path.isabs(config)
    needs_exec = not os.path.isabs(args[0]) or not os.path.isabs(entry) or relative_config
    marker = os.environ.get('MODEL_PROXY_INTERNAL_REEXEC')
    # This marker can only reject a changed transition, never authorize a
    # process: controller authority still reads the OS, records and connection.
    # An injected/malformed marker is an error, not a reason to retry exec.
    if not needs_exec and marker is None:
        return
    launcher = sys.executable
    if not launcher or not os.path.isabs(launcher) or not os.path.isfile(launcher):
        raise ValueError('Python launcher unavailable')
    before_entry, before_launcher, before_image = os.stat(resolved_entry), os.stat(launcher), os.stat(image)
    desired_env = dict(os.environ)
    desired_env.pop('MODEL_PROXY_INTERNAL_REEXEC', None)
    if relative_config:
        desired_env['MODEL_PROXY_CONFIG'] = os.path.join(cwd, config)
    digest = hashlib.sha256()
    for key, value in sorted(desired_env.items()):
        for item in (os.fsencode(key), os.fsencode(value)):
            digest.update(len(item).to_bytes(8, 'big'))
            digest.update(item)
    expected = {'pid': os.getpid(), 'cwd': cwd, 'entry': str(resolved_entry),
                'entry_id': [before_entry.st_dev, before_entry.st_ino],
                'image_id': [before_image.st_dev, before_image.st_ino],
                'launcher': launcher, 'launcher_id': [before_launcher.st_dev, before_launcher.st_ino],
                'prefix': sys.prefix, 'base_prefix': sys.base_prefix, 'flags': list(sys.flags),
                'environment_digest': digest.hexdigest()}
    if marker is not None:
        if needs_exec or len(marker) > 16384 or json.loads(marker) != expected:
            raise ValueError('re-exec identity changed')
        del os.environ['MODEL_PROXY_INTERNAL_REEXEC']
        return
    # exec would silently close a standard descriptor with FD_CLOEXEC.
    # A descriptor that was already closed remains closed across exec.
    import errno
    import fcntl
    for fd in (0, 1, 2):
        try:
            descriptor_flags = fcntl.fcntl(fd, fcntl.F_GETFD)
        except OSError as exc:
            if exc.errno == errno.EBADF:
                continue
            raise
        if descriptor_flags & fcntl.FD_CLOEXEC:
            raise ValueError('standard descriptor would be closed')
    env = desired_env.copy()
    # CPython may coerce LC_CTYPE during startup. Reuse the kernel-observed
    # original value so the second interpreter makes the same UTF-8 decision.
    if 'LC_CTYPE' in initial_env:
        env['LC_CTYPE'] = initial_env['LC_CTYPE']
    else:
        env.pop('LC_CTYPE', None)
    env['MODEL_PROXY_INTERNAL_REEXEC'] = json.dumps(expected, separators=(',', ':'))
    current_entry, current_launcher = os.stat(entry), os.stat(launcher)
    if ((before_entry.st_dev, before_entry.st_ino) != (current_entry.st_dev, current_entry.st_ino)
            or (before_launcher.st_dev, before_launcher.st_ino) != (current_launcher.st_dev, current_launcher.st_ino)):
        raise ValueError('launch path changed')
    os.execve(launcher, [launcher, *flags, str(resolved_entry)], env)
    raise RuntimeError('exec unexpectedly returned')


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _normalize_windows_launch(args, environment):
    """Apply internal explicit launch arguments before importing the server."""
    import ntpath
    if not args:
        return
    if len(args) != 4 or set(args[::2]) != {'--config', '--port'}:
        raise ValueError('invalid internal launch arguments')
    options = dict(zip(args[::2], args[1::2]))
    config, port = options['--config'], options['--port']
    drive, tail = ntpath.splitdrive(config)
    if (len(drive) != 2 or drive[1] != ':' or not drive[0].isascii()
            or not drive[0].isalpha() or not tail.startswith('\\')
            or '\0' in config or ntpath.normpath(config) != config
            or not port.isascii() or not port.isdigit()
            or len(port) > 5 or not 1 <= int(port) <= 65535
            or str(int(port)) != port):
        raise ValueError('invalid internal launch arguments')
    environment['MODEL_PROXY_CONFIG'] = config
    environment['MODEL_PROXY_PORT'] = port


def _normalize_windows_direct_launch(environment, *, root, entry, executable, original,
                                     spawn=None, resolve=None):
    """Run a no-argument direct launch with observable explicit options.

    No private token or persistent setting is passed. Unrecognized interpreter
    switches are rejected before startup rather than silently discarded.
    """
    import ntpath
    import subprocess
    if spawn is None:
        spawn = subprocess.Popen
    if resolve is None:
        from core.listen_config import resolve_listen as resolve
    if (not isinstance(original, (tuple, list)) or len(original) < 2
            or any(not isinstance(arg, str) for arg in original)
            or any(flag not in ('-B', '-I', '-S') for flag in original[1:-1])
            or ntpath.normcase(ntpath.abspath(original[-1])) != ntpath.normcase(entry)
            or not ntpath.isabs(executable) or not ntpath.isabs(entry)):
        raise ValueError('unsupported Windows direct launch')
    config = ntpath.abspath(environment.get('MODEL_PROXY_CONFIG')
                            or ntpath.join(root, 'config', 'model_proxy_config.json'))
    listen = resolve(config, environment.get('MODEL_PROXY_PORT'))
    port = str(listen['port'])
    # Validate the complete target arguments before invoking exec.
    _normalize_windows_launch(['--config', config, '--port', port], {})
    arguments = [executable, *original[1:-1], entry, '--config', config, '--port', port]
    # Popen owns Windows quoting and the parent waits for the child result.
    # CRT execv under Wine returned success before a failed child and did not
    # quote paths containing spaces. No alternate protocol or config is added.
    return spawn(arguments, close_fds=True).wait()


if __name__ == '__main__' and sys.platform == 'darwin':
    try:
        _normalize_darwin_launch()
    except Exception:
        sys.exit('Error: Mac service launch cannot be verified or normalized; '
                 'use an absolute Python launcher and python [-B] <service-entry> '
                 'without extra arguments (exec failure or unsupported launch).')

if __name__ == '__main__':
    if sys.platform == 'win32':
        import os
        try:
            if not sys.argv[1:]:
                raise SystemExit(_normalize_windows_direct_launch(
                    os.environ, root=str(ROOT), entry=str(Path(__file__).resolve()),
                    executable=sys.executable, original=getattr(sys, 'orig_argv', ())))
            _normalize_windows_launch(sys.argv[1:], os.environ)
        except Exception:
            sys.exit('Error: Windows service launch cannot be safely normalized.')
    from core.server import main
    main()
elif sys.platform != 'darwin':
    # Preserve the existing non-Mac import surface; on Mac, importing this
    # bootstrap must not expose an unchecked server-start shortcut.
    from core.server import main
