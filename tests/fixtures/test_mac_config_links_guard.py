"""Cooperative Python-only guard for the Mac link integration slice.

Not an OS sandbox: native libraries and a hostile same-user process can bypass
it. It limits reviewed Python's files, sockets and child creation; product peer
verification and test listener checks remain necessary and are not mocked.
"""
import os
import json
import socket
import subprocess
import sys
from pathlib import Path

_RAW_POPEN = subprocess.Popen
_OWNED_PROBES = set()


def endpoint(address, ports, *, bind=False):
    if (not isinstance(address, tuple) or len(address) != 2
            or address[0] not in ('127.0.0.1', 'localhost')
            or type(address[1]) is not int
            or address[1] == 18889
            or address[1] not in (set(ports) | ({0} if bind else set()))):
        raise PermissionError('link test guard: network destination denied')
    return address[1]


def path_allowed(path, root, readonly_roots, write=False):
    if isinstance(path, int):
        return True
    try:
        p = Path(os.fsdecode(path)).resolve(strict=False)
    except (TypeError, ValueError, OSError):
        return False
    if p == Path('/dev/null').resolve():
        return True
    if p == root or root in p.parents:
        return True
    return not write and any(p == r or r in p.parents for r in readonly_roots)


def owned_listener(pid, port):
    if type(pid) is not int or pid <= 1 or port == 18889:
        return False
    proc = _RAW_POPEN(['/usr/sbin/lsof', '-nP', '-a', '-p', str(pid),
                       '-iTCP@127.0.0.1:%d' % port, '-sTCP:LISTEN', '-t'],
                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    _OWNED_PROBES.add(proc.pid)
    try:
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()  # exactly the direct lsof child just created
            proc.communicate()
            return False
    finally:
        _OWNED_PROBES.discard(proc.pid)
    return proc.returncode == 0 and str(pid).encode() in out.splitlines()


def service_checks(parent_pid, mock_port):
    def listener(port):
        return (os.getppid() == parent_pid and port == mock_port
                and owned_listener(parent_pid, port))
    def peer(sock):
        try:
            address = sock.getpeername()
        except OSError:
            return False
        if address[1] != mock_port:
            return True  # server response to an inbound test client
        if os.getppid() != parent_pid:
            return False
        # Verify the *same connected socket* before sending the fake upstream key.
        from core.discovery import process_start_identity, process_executable
        from core.platform_process import verify_connected_peer
        start, image = process_start_identity(parent_pid), process_executable(parent_pid)
        if start is None or image is None:
            return False
        try:
            verify_connected_peer(sock, parent_pid, start, image, timeout=2.0)
        except (OSError, RuntimeError, ValueError):
            return False
        return True
    return listener, peer


def install(root, ports, role, *, listener_check=None, peer_check=None, allowed_pid=None, allowed_signal=None, owned_history=None):
    root = Path(root).resolve(strict=True)
    ports = tuple(int(x) for x in ports)
    if (not root.name.startswith('mp-regression-') or len(ports) != 2
            or ports[0] == ports[1] or any(p == 18889 or not 1024 <= p <= 65535 for p in ports)
            or role not in ('worker', 'service', 'helper')):
        raise RuntimeError('link test guard: invalid isolation envelope')
    home = Path(os.environ.get('HOME', '/invalid')).resolve(strict=False)
    if root not in home.parents:
        raise RuntimeError('link test guard: HOME outside test root')
    readonly = tuple(Path(p).resolve(strict=False) for p in
                     (sys.base_prefix, sys.prefix, '/usr/lib', '/usr/sbin', '/System/Library',
                      '/Library/Developer/CommandLineTools/Library'))
    guard_dir = root / 'guard'
    bootstrap_files = tuple(guard_dir / name for name in
                            ('sitecustomize.py', 'test_mac_config_links_guard.py',
                             'test_mac_config_links_fdguard.py'))
    def bootstrap_evidence():
        result = []
        for path in bootstrap_files:
            info = path.lstat()
            if not path.is_file() or path.is_symlink() or info.st_nlink != 1:
                raise PermissionError('link test guard: bootstrap file unverified')
            result.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, path.read_bytes()))
        return tuple(result)
    baseline_bootstrap = bootstrap_evidence()
    owner = str(os.getpid()) if role == 'worker' else os.environ.get('MP_LINKS_OWNER_PID', '')
    def parse_history(raw):
        try:
            if not isinstance(raw, str) or len(raw) > 16384:
                raise ValueError('invalid history')
            records = json.loads(raw)
            if (not isinstance(records, list) or len(records) > 64
                    or any(not isinstance(item, dict) or set(item) != {'pid', 'start'}
                           or type(item['pid']) is not int or item['pid'] <= 1
                           or not isinstance(item['start'], dict) or not item['start']
                           for item in records)
                    or len({item['pid'] for item in records}) != len(records)):
                raise ValueError('invalid history')
            if json.dumps(records, sort_keys=True, separators=(',', ':')) != raw:
                raise ValueError('noncanonical history')
            return {item['pid']: item['start'] for item in records}
        except (ValueError, TypeError, KeyError):
            raise PermissionError('link test guard: owned-process history unverified') from None
    initial_history = os.environ.get('MP_LINKS_OWNED_HISTORY', '[]')
    previous_owned = parse_history(initial_history) if role in ('service', 'helper') else {}
    def guarded_environment(env, expected_role):
        if not isinstance(env, dict):
            raise PermissionError('link test guard: explicit child environment required')
        required = {'PYTHONPATH': str(guard_dir), 'PYTHONNOUSERSITE': '1',
                    'PYTHONDONTWRITEBYTECODE': '1', 'MP_LINKS_GUARD_ROOT': str(root),
                    'MP_LINKS_GUARD_PORTS': ','.join(map(str, ports)),
                    'MP_LINKS_GUARD_ROLE': expected_role, 'MP_LINKS_OWNER_PID': owner,
                    'MP_LINKS_OWNED_HISTORY': (owned_history() if role == 'worker' and owned_history is not None
                                               else initial_history)}
        if (any(env.get(key) != value for key, value in required.items())
                or any(key in env for key in ('PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONINSPECT'))):
            raise PermissionError('link test guard: child bootstrap environment changed')
        home = Path(env.get('HOME', '/invalid')).resolve(strict=False)
        temp = Path(env.get('TMPDIR', '/invalid')).resolve(strict=False)
        if root not in home.parents or not (temp == root or root in temp.parents):
            raise PermissionError('link test guard: child HOME/TMP outside isolation')
        if bootstrap_evidence() != baseline_bootstrap:
            raise PermissionError('link test guard: bootstrap files changed')

    from test_mac_config_links_fdguard import install as install_fd_guard
    fd_internal = install_fd_guard(root, readonly)
    def audit(event, args):
        if fd_internal():
            return
        if event == 'open':
            value = args[0]
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            write = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or bool(
                flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            ancestor_dir = (not write and isinstance(value, (str, bytes, os.PathLike))
                            and bool(flags & getattr(os, 'O_DIRECTORY', 0))
                            and Path(os.fsdecode(value)).resolve(strict=False) in root.parents)
            if not ancestor_dir and not path_allowed(value, root, readonly, write=write):
                raise PermissionError('link test guard: file access outside test root')
        elif event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.chown', 'os.truncate'):
            if not path_allowed(args[0], root, (), write=True):
                raise PermissionError('link test guard: mutation outside test root')
        elif event in ('os.rename', 'os.link', 'os.symlink'):
            values = args[1:2] if event == 'os.symlink' else args[:2]
            if any(not path_allowed(value, root, (), write=True) for value in values):
                raise PermissionError('link test guard: mutation outside test root')
        elif event == 'subprocess.Popen':
            executable, argv, cwd, _env = args
            if not isinstance(argv, (list, tuple)) or not argv:
                raise PermissionError('link test guard: child command not provable')
            executable = os.path.realpath(os.fsdecode(executable))
            if executable == '/usr/sbin/lsof':
                valid = (len(argv) == 8 and argv[1:4] == ['-nP', '-a', '-p']
                         and argv[6:] == ['-sTCP:LISTEN', '-t'] and str(argv[4]).isdecimal())
                pid = int(argv[4]) if valid else -1
                valid = valid and argv[5] in tuple('-iTCP@127.0.0.1:%d' % p for p in ports)
                if role == 'service':
                    valid = valid and pid == int(os.environ.get('MP_LINKS_OWNER_PID', '0'))
                elif role == 'worker':
                    valid = valid and allowed_pid is not None and allowed_pid(pid)
                else:
                    valid = False
                if not valid:
                    raise PermissionError('link test guard: unowned listener probe denied')
            elif role == 'worker' and executable == os.path.realpath(sys.executable) and len(argv) >= 2:
                script = Path(argv[1])
                if not script.is_absolute():
                    script = Path(cwd or os.getcwd()) / script
                script = script.resolve(strict=False)
                if (root not in script.parents or script.name not in ('model_proxy.py', 'helper_entry.py')
                        or tuple(script.parts[-3:-1]) != ('core', 'cli')):
                    raise PermissionError('link test guard: non-test child denied')
                child_role = 'service' if script.name == 'model_proxy.py' else 'helper'
                guarded_environment(_env, child_role)
            else:
                raise PermissionError('link test guard: child command denied')
        elif event == 'os.kill':
            pid, signal_number = args
            valid = (type(pid) is int and pid > 1 and
                     ((pid == os.getpid() and signal_number == 0) or pid in _OWNED_PROBES
                      or (role == 'worker' and allowed_signal is not None
                          and allowed_signal(pid, signal_number))))
            # The worker supplies only already-exited children it actually
            # launched and whose start identity it recorded. A service may
            # perform the real, read-only kill(pid, 0) probe for those PIDs;
            # no nonzero signal is authorized. Reject a recycled PID if its
            # current start identity is observable and different.
            if (not valid and role == 'service' and signal_number == 0
                    and pid in previous_owned and os.getppid() == int(owner)):
                try:
                    from core.discovery import process_start_identity
                    current = process_start_identity(pid)
                    valid = current is None or current == previous_owned[pid]
                except (ImportError, OSError, ValueError, TypeError, RuntimeError):
                    valid = False
            if not valid:
                raise PermissionError('link test guard: signal to unowned process denied')
        elif event in ('os.system', 'os.fork', 'os.killpg'):
            raise PermissionError('link test guard: uncontrolled process denied')
        elif event == 'os.exec':
            executable, argv = args[:2]
            script = Path(os.fsdecode(argv[-1])).resolve(strict=False) if argv else Path('/')
            if (role != 'service' or os.path.realpath(os.fsdecode(executable)) != os.path.realpath(sys.executable)
                    or root not in script.parents or tuple(script.parts[-3:]) != ('core', 'cli', 'model_proxy.py')
                    or len(argv) not in (2, 3) or (len(argv) == 3 and argv[1] != '-B')):
                raise PermissionError('link test guard: uncontrolled exec denied')
            guarded_environment(args[2] if len(args) > 2 and args[2] is not None else dict(os.environ), 'service')
        elif event == 'os.chdir':
            if not path_allowed(args[0], root, (), write=True):
                raise PermissionError('link test guard: cwd outside test root')
    sys.addaudithook(audit)
    connect, bind = socket.socket.connect, socket.socket.bind
    send, sendall = socket.socket.send, socket.socket.sendall
    gai = socket.getaddrinfo
    def checked_connect(sock, address):
        if sock.family != socket.AF_INET or sock.type & socket.SOCK_STREAM != socket.SOCK_STREAM:
            raise PermissionError('link test guard: IPv4 TCP required')
        port = endpoint(address, ports)
        if role == 'helper' or (role == 'service' and port != ports[1]):
            raise PermissionError('link test guard: connection not permitted for role')
        if listener_check is not None and not listener_check(port):
            raise PermissionError('link test guard: listener ownership unavailable')
        return connect(sock, ('127.0.0.1', port))
    def checked_connect_ex(sock, address):
        try:
            checked_connect(sock, address)
            return 0
        except OSError as exc:
            return exc.errno or 1
    def checked_bind(sock, address):
        if role != 'worker' and address[1] != ports[0]:
            raise PermissionError('link test guard: service bind not permitted')
        if sock.family != socket.AF_INET:
            raise PermissionError('link test guard: IPv4 required')
        port = endpoint(address, ports, bind=True)
        return bind(sock, ('127.0.0.1', port))
    def checked_send(sock, data, *args, **kwargs):
        if peer_check is not None and not peer_check(sock):
            raise PermissionError('link test guard: same socket peer not verified')
        return send(sock, data, *args, **kwargs)
    def checked_sendall(sock, data, *args, **kwargs):
        if peer_check is not None and not peer_check(sock):
            raise PermissionError('link test guard: same socket peer not verified')
        return sendall(sock, data, *args, **kwargs)
    def checked_gai(host, port, family=0, type=0, proto=0, flags=0):
        endpoint((host, int(port)), ports)
        return gai('127.0.0.1', str(port), socket.AF_INET, type, proto,
                   flags | socket.AI_NUMERICHOST | socket.AI_NUMERICSERV)
    def deny(*_args, **_kwargs):
        raise PermissionError('link test guard: unsupported network operation')
    socket.socket.connect = checked_connect
    socket.socket.connect_ex = checked_connect_ex
    socket.socket.bind = checked_bind
    socket.socket.send = checked_send
    socket.socket.sendall = checked_sendall
    socket.socket.sendto = deny
    if hasattr(socket.socket, 'sendmsg'):
        socket.socket.sendmsg = deny
    socket.getaddrinfo = checked_gai
    socket.gethostbyname = lambda host: '127.0.0.1' if host in ('127.0.0.1', 'localhost') else deny()
    socket.gethostbyname_ex = lambda host: ('localhost', [], [socket.gethostbyname(host)])
    socket.gethostbyaddr = lambda host: ('localhost', [], [socket.gethostbyname(host)])
    def checked_nameinfo(address, _flags):
        if (not isinstance(address, tuple) or len(address) != 2
                or address[0] not in ('127.0.0.1', 'localhost')
                or type(address[1]) is not int or not 0 <= address[1] <= 65535):
            return deny()
        return ('127.0.0.1', str(address[1]))
    socket.getnameinfo = checked_nameinfo
    socket.getfqdn = lambda host='': 'localhost' if host in ('127.0.0.1', 'localhost') else deny()
    if role in ('service', 'helper'):
        subprocess.Popen = deny
    if role == 'worker':
        os.environ['MP_LINKS_WORKER_GUARD_ACTIVE'] = '1'
