"""Scratch-project construction and cooperative Python safety guards.

The guards are defense in depth for reviewed Python code, not an OS sandbox.
"""
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

# Mirror the confirmed shipped defaults without reading the worktree's config.
DEFAULT_PATHS = {
    'log': '.runtime/model_proxy.log',
    'totals': '.runtime/model_proxy_totals.json',
    'lock': '.runtime/model_proxy.lock',
    'pid': '.runtime/model_proxy.pid',
    'ensure_log': '.runtime/model_proxy_ensure.log',
    'start_lock': '.runtime/model_proxy_start.lock',
}
PATH_KEYS = tuple(DEFAULT_PATHS)


# Match the exact two literals AND their surrounding initialization sequence.
# Only these literal bytes, never the control/lock/initialization code, change.
ORIGINAL_CONFIRMATION = (
    b'"Confirm no old starter or enabled autostart, no shared ledger, "\n'
    b'                        "and no cloud-synchronized installation or data.\\n"'
)
ORIGINAL_INITIALIZATION_CONTEXT = (
    b'                    stdout.write(\n'
    b'                        ' + ORIGINAL_CONFIRMATION + b'\n'
    b'                        "Installation/recovery source: "\n'
    b'                    )\n'
    b'                    stdout.flush()\n'
    b'                    source = stdin.readline(512)\n'
    b'                    stdout.write("Type YES to confirm: ")\n'
    b'                    stdout.flush()\n'
    b'                    confirmation = stdin.readline(64)\n'
)


def attestation_for_test(proxy_port, mock_port, project_name=None):
    if project_name is None:  # The existing fixture has its own test-only prompt.
        return ('Confirm this newly created fake project uses its own independent ledger '
                'and test ports %d and %d.' % (proxy_port, mock_port)).encode('ascii')
    prefix = 'mp-regression-'
    suffix = project_name.removeprefix(prefix)
    if (not project_name.startswith(prefix) or len(suffix) != 8
            or any(ch not in 'abcdefghijklmnopqrstuvwxyz0123456789_' for ch in suffix)):
        raise RuntimeError('invalid randomly generated fake project name')
    return ('Confirm only the fake project in randomly generated test directory %s has its own independent ledger '
            'and test ports %d and %d.' % (project_name, proxy_port, mock_port)).encode('ascii')


def _file_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _snapshot_regular(path):
    """Snapshot a stable single-link file without following its final symlink."""
    if not hasattr(os, 'O_NOFOLLOW'):
        raise RuntimeError('safe source open unavailable')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RuntimeError('authority file must be a single-link regular file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read()
        after = os.fstat(fd)
        current = path.lstat()
        identity = _file_identity(before)
        if identity != _file_identity(after) or identity != _file_identity(current):
            raise RuntimeError('authority file changed during snapshot')
        return identity, data
    finally:
        os.close(fd)


def resolved_default_paths(project):
    return {key: str(project / path) for key, path in DEFAULT_PATHS.items()}


def fake_config(proxy_port, mock_port):
    """Only loopback supplies and artificial tokens; baseline route is unchanged."""
    base = 'http://127.0.0.1:%d' % mock_port
    def route(name, supply):
        return {'id': name, 'tiers': {'opus': [supply], 'sonnet': [supply], 'haiku': [supply]},
                'failover': 'on'}
    def strategy(token, name):
        return {'client_token': token, 'route_pool': [{'route_id': name, 'weight': 1}]}
    return {
        'listen': {'host': '127.0.0.1', 'port': proxy_port},
        'admin_token': 'fake-admin-for-test-only',
        'supplies': [
            {'id': 'mock', 'url': base + '/v1/messages', 'protocol': 'anthropic',
             'appkey': 'fake-upstream-for-test-only', 'target_model': 'fixture-model'},
            {'id': 'mock-responses', 'url': base + '/v1/responses', 'protocol': 'responses',
             'appkey': 'fake-upstream-a2r', 'target_model': 'fixture-model-a2r'},
            {'id': 'mock-anthropic-r2a', 'url': base + '/v1/messages', 'protocol': 'anthropic',
             'appkey': 'fake-upstream-r2a', 'target_model': 'fixture-model-r2a'},
        ],
        'routes': [route('mock-route', 'mock'), route('mock-a2r', 'mock-responses'),
                   route('mock-r2a', 'mock-anthropic-r2a')],
        'strategies': [strategy('fake-client-for-test-only', 'mock-route'),
                       strategy('fake-client-a2r', 'mock-a2r'),
                       strategy('fake-client-r2a', 'mock-r2a')],
    }


def overlaps(a, b):
    a, b = Path(a).resolve(), Path(b).resolve()
    return a == b or a in b.parents or b in a.parents


def _copy_file(src, dst):
    if src.is_symlink() or not src.is_file():
        raise RuntimeError('source must be a regular non-symlink file: ' + str(src))
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not hasattr(os, 'O_NOFOLLOW'):
        raise RuntimeError('safe source open unavailable')
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError('source must be a single-link regular file: ' + str(src))
        with os.fdopen(fd, 'rb', closefd=False) as source_file, open(dst, 'xb') as target_file:
            shutil.copyfileobj(source_file, target_file)
    finally:
        os.close(fd)


def copy_core(src, dst):
    if src.is_symlink() or not src.is_dir():
        raise RuntimeError('core directory missing or symlinked')
    for root, dirs, files in os.walk(src, followlinks=False):
        root = Path(root)
        for name in dirs:
            if (root / name).is_symlink():
                raise RuntimeError('symlink in source tree: ' + str(root / name))
        dirs[:] = [name for name in dirs if name != '__pycache__']
        for name in files:
            if name.endswith('.py'):
                _copy_file(root / name, dst / root.relative_to(src) / name)


class Scratch:
    def __init__(self, source):
        self.source = Path(source).resolve()
        # Ignore inherited TMPDIR on POSIX; it may name a production path.
        parent = Path('/tmp' if os.name == 'posix' else tempfile.gettempdir()).resolve()
        if overlaps(parent, self.source):
            raise RuntimeError('temporary parent overlaps source tree')
        self.root = Path(tempfile.mkdtemp(prefix='mp-regression-', dir=str(parent)))
        st = self.root.lstat()
        self.identity = (st.st_dev, st.st_ino)
        self.project = self.root / 'project'
        self.authority_source = None
        self.authority_snapshot = None
        self.patched_authority_snapshot = None
        try:
            for name in ('project/config', 'project/.runtime', 'home', 'tmp', 'guard'):
                (self.root / name).mkdir(parents=True, exist_ok=True)
            (self.project / '.runtime').chmod(0o700)
            # An empty venv avoids loading user/system site-packages before the
            # service's sitecustomize guard, while preserving the actual CLI's
            # two-element Python + service-script process argv.
            self.venv = self.root / 'interpreter'
            venv.EnvBuilder(with_pip=False, symlinks=True).create(str(self.venv))
            self.python = self.venv / 'bin/python'
            self.site_packages = self.venv / 'lib' / ('python%d.%d' % sys.version_info[:2]) / 'site-packages'
            cfg_lines = (self.venv / 'pyvenv.cfg').read_text().lower().splitlines()
            no_system_site = [line.split('=', 1)[1].strip() for line in cfg_lines
                              if line.split('=', 1)[0].strip() == 'include-system-site-packages' and '=' in line]
            if (not self.python.is_file() or not self.site_packages.is_dir()
                    or any(self.site_packages.iterdir()) or no_system_site != ['false']):
                raise RuntimeError('fresh isolated interpreter is unavailable')
        except BaseException:
            self.cleanup()
            raise

    def stage(self, fixture, proxy_port, mock_port):
        source = self.source
        if fixture:
            fixture_root = source / 'tests/fixtures/worktree'
            for name in ('cli.py', 'service.py'):
                _copy_file(fixture_root / name, self.project / name)
            entry = self.project / 'cli.py'
        else:
            # Inspect no user configuration: only this precise source file.
            for directory in (source / 'core', source / 'core/cli'):
                if directory.is_symlink() or not directory.is_dir():
                    raise RuntimeError('source authority directory unavailable or symlinked')
            self.authority_source = source / 'core/cli/authority.py'
            self.authority_snapshot = _snapshot_regular(self.authority_source)
            copy_core(source / 'core', self.project / 'core')
            self._patch_copied_authority(proxy_port, mock_port)
            for name in ('VERSION',):
                _copy_file(source / name, self.project / name)
            entry = self.project / 'core/cli/main.py'
            if not entry.is_file():
                raise RuntimeError('new Python CLI not present; legacy shell is never run')
        paths = dict(DEFAULT_PATHS)
        resolved = resolved_default_paths(self.project)
        if len(set(resolved.values())) != 6 or any(not Path(p).is_relative_to(self.project / '.runtime') for p in resolved.values()):
            raise RuntimeError('invalid runtime paths')
        config = fake_config(proxy_port, mock_port)
        for name, value in [('runtime_paths.json', paths), ('model_proxy_config.json', config)]:
            config_path = self.project / 'config' / name
            config_path.write_text(json.dumps(value), encoding='utf-8')
            config_path.chmod(0o600)
        _copy_file(source / 'tests/isolation_bootstrap.py', self.root / 'guard/isolation_bootstrap.py')
        _copy_file(source / 'tests/isolated_runner.py', self.root / 'guard/worker.py')
        (self.root / 'guard/sitecustomize.py').write_text(
            "import os, sys, site\n"
            "try:\n"
            "    if os.path.realpath(sys.prefix) != os.environ['MP_TEST_VENV'] or site.ENABLE_USER_SITE:\n"
            "        raise RuntimeError('isolated interpreter unavailable')\n"
            "    for path in sys.path:\n"
            "        if 'site-packages' in path and not os.path.realpath(path).startswith(os.environ['MP_TEST_ROOT'] + os.sep):\n"
            "            raise RuntimeError('external site-packages unavailable')\n"
            "    import isolation_bootstrap\n"
            "    isolation_bootstrap.install_guard()\n"
            "    if os.environ.get('MP_TEST_GUARD_INSTALLED') != '1': raise RuntimeError('guard unavailable')\n"
            "except BaseException:\n"
            "    os._exit(126)\n", encoding='utf-8')
        return entry

    def assert_authority_unchanged(self):
        if (self.authority_source is None or self.authority_snapshot is None
                or _snapshot_regular(self.authority_source) != self.authority_snapshot):
            raise RuntimeError('source authority changed since staging; refusing regression')
        if (self.patched_authority_snapshot is not None
                and _snapshot_regular(self.project / 'core/cli/authority.py') != self.patched_authority_snapshot):
            raise RuntimeError('copied authority changed since replacement; refusing regression')

    def _patch_copied_authority(self, proxy_port, mock_port):
        """Replace one exact prompt literal only in this newly created copy."""
        if (type(proxy_port) is not int or type(mock_port) is not int
                or any(port < 1024 or port > 65535 or port == 18889
                       for port in (proxy_port, mock_port)) or proxy_port == mock_port):
            raise RuntimeError('invalid isolated test ports')
        self.assert_authority_unchanged()
        root_stat = self.root.lstat()
        if (self.root.is_symlink() or (root_stat.st_dev, root_stat.st_ino) != self.identity
                or self.project.parent != self.root):
            raise RuntimeError('scratch root changed identity')
        for directory in (self.project, self.project / 'core', self.project / 'core/cli'):
            if directory.is_symlink() or not directory.is_dir():
                raise RuntimeError('copied authority directory unavailable or symlinked')
        target = self.project / 'core/cli/authority.py'
        before = _snapshot_regular(target)
        original = self.authority_snapshot[1]
        if (before[1] != original or original.count(ORIGINAL_CONFIRMATION) != 1
                or original.count(ORIGINAL_INITIALIZATION_CONTEXT) != 1):
            raise RuntimeError('copied authority or exact initialization prompt differs')
        # repr emits a single Python string literal; all remaining bytes are identical.
        literal = repr(attestation_for_test(proxy_port, mock_port, self.root.name).decode('ascii') + '\n').encode('ascii')
        replacement = original.replace(ORIGINAL_CONFIRMATION, literal, 1)
        fd = os.open(target, os.O_RDWR | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
        try:
            if _file_identity(os.fstat(fd)) != before[0]:
                raise RuntimeError('copied authority changed before replacement')
            with os.fdopen(fd, 'r+b', closefd=False) as stream:
                if stream.read() != original:
                    raise RuntimeError('copied authority changed before replacement')
                stream.seek(0)
                stream.write(replacement)
                stream.truncate()
        finally:
            os.close(fd)
        patched = _snapshot_regular(target)
        if patched[1] != replacement:
            raise RuntimeError('copied authority replacement verification failed')
        self.patched_authority_snapshot = patched
        self.assert_authority_unchanged()

    def env(self, proxy_port, mock_port):
        p = self.root
        return {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(p / 'home'),
                'TMPDIR': str(p / 'tmp'), 'TMP': str(p / 'tmp'), 'TEMP': str(p / 'tmp'),
                'XDG_CONFIG_HOME': str(p / 'home/config'), 'XDG_CACHE_HOME': str(p / 'home/cache'),
                'LOCALAPPDATA': str(p / 'home/local'), 'APPDATA': str(p / 'home/appdata'),
                'MODEL_PROXY_CONFIG': str(self.project / 'config/model_proxy_config.json'),
                'MODEL_PROXY_PORT': str(proxy_port), 'PYTHONPATH': str(p / 'guard'),
                'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
                'NO_PROXY': '*', 'no_proxy': '*', 'MP_TEST_VENV': str(self.venv),
                'MP_TEST_ROOT': str(p), 'MP_TEST_REAL_HOME': str(Path.home()),
                'MP_TEST_PROJECT': str(self.project), 'MP_TEST_ROLE': 'worker',
                'MP_TEST_PORTS': '%d,%d' % (proxy_port, mock_port)}

    def cleanup(self):
        try:
            st = self.root.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or (st.st_dev, st.st_ino) != self.identity:
            raise RuntimeError('scratch root changed identity; refusing cleanup')
        # Python's fd-based rmtree does not traverse symlink entries. Fail closed
        # where this implementation is not available.
        if not shutil.rmtree.avoids_symlink_attacks:
            raise RuntimeError('safe rmtree not available; scratch retained')
        shutil.rmtree(self.root)


def install_guard():
    """Restrict normal Python network/process APIs in the controlled child tree."""
    project = Path(os.environ['MP_TEST_PROJECT']).resolve()
    scratch = Path(os.environ['MP_TEST_ROOT']).resolve()
    real_home = Path(os.environ['MP_TEST_REAL_HOME']).resolve()
    ports = {int(x) for x in os.environ['MP_TEST_PORTS'].split(',')}
    children = []
    role = os.environ.get('MP_TEST_ROLE')
    baseline = dict(os.environ)
    def startup_file(path):
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or path.is_symlink():
            raise PermissionError('test guard: startup file is not a regular file')
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, path.read_bytes())
    startup_paths = (scratch / 'guard/sitecustomize.py', scratch / 'guard/isolation_bootstrap.py',
                     Path(os.environ['MP_TEST_VENV']) / 'pyvenv.cfg')
    startup_files = tuple(startup_file(path) for path in startup_paths)

    def allowed(address):
        if (not isinstance(address, tuple) or len(address) < 2 or
                address[0] not in ('127.0.0.1', 'localhost') or address[1] not in ports):
            raise PermissionError('test guard: non-test network address denied')

    original_connect, original_bind = socket.socket.connect, socket.socket.bind
    original_popen, original_kill = subprocess.Popen, os.kill
    proxy_port = int(os.environ['MP_TEST_PORTS'].split(',')[0])
    def listener_is_child(port):
        lsof = shutil.which('lsof')
        if not lsof:
            return False
        for child in children:
            if child.poll() is not None:
                continue
            probe = original_popen([lsof, '-nP', '-a', '-p', str(child.pid),
                                    '-iTCP@127.0.0.1:%d' % port, '-sTCP:LISTEN', '-t'],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                out, _ = probe.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                # This is the exact lsof child created above, never a scanned PID.
                if probe.poll() is None:
                    try:
                        original_kill(probe.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                probe.communicate()
                return False
            if probe.returncode == 0 and str(child.pid).encode() in out.splitlines():
                return True
        return False
    def connect(sock, address):
        if sock.family != socket.AF_INET or sock.type & socket.SOCK_STREAM != socket.SOCK_STREAM:
            raise PermissionError('test guard: non-IPv4 TCP socket denied')
        allowed(address)
        if address[1] == proxy_port and not listener_is_child(proxy_port):
            raise PermissionError('test guard: proxy listener is not an owned child')
        return original_connect(sock, address)
    def connect_ex(sock, address):
        try:
            connect(sock, address)
            return 0
        except OSError as exc:
            return exc.errno or 1
    def bind(sock, address):
        if sock.family != socket.AF_INET:
            raise PermissionError('test guard: non-IPv4 socket denied')
        allowed(address)
        return original_bind(sock, address)
    original_gai = socket.getaddrinfo
    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        allowed((host, int(port)))
        # Numeric-only flags also prevent canonical-name lookup in libc.
        flags |= socket.AI_NUMERICHOST | socket.AI_NUMERICSERV
        return original_gai('127.0.0.1', str(port), socket.AF_INET, type, proto, flags)
    def local_host(name):
        if name not in ('127.0.0.1', 'localhost'):
            raise PermissionError('test guard: non-local name resolution denied')
        return name
    def gethostbyname(name):
        local_host(name)
        return '127.0.0.1'
    def gethostbyname_ex(name):
        return (local_host(name), [], ['127.0.0.1'])
    def gethostbyaddr(name):
        local_host(name)
        return ('localhost', [], ['127.0.0.1'])
    def getnameinfo(address, flags):
        if not isinstance(address, tuple) or len(address) != 2:
            raise PermissionError('test guard: non-local name resolution denied')
        local_host(address[0])
        if not isinstance(address[1], int) or not 0 <= address[1] <= 65535:
            raise PermissionError('test guard: invalid port')
        return ('127.0.0.1', str(address[1]))
    def getfqdn(name=''):
        local_host(name)
        return 'localhost'
    socket.socket.connect, socket.socket.connect_ex, socket.socket.bind = connect, connect_ex, bind
    socket.getaddrinfo = getaddrinfo
    socket.gethostbyname, socket.gethostbyname_ex = gethostbyname, gethostbyname_ex
    socket.gethostbyaddr, socket.getnameinfo, socket.getfqdn = gethostbyaddr, getnameinfo, getfqdn
    def deny_datagram(*args, **kwargs):
        raise PermissionError('test guard: datagram send denied')
    socket.socket.sendto = deny_datagram
    if hasattr(socket.socket, 'sendmsg'):
        socket.socket.sendmsg = deny_datagram

    def guarded_popen(args, *pos, **kwargs):
        if role != 'worker':
            raise PermissionError('test guard: nested descendants denied')
        if (not isinstance(args, (tuple, list)) or len(args) < 2 or kwargs.get('shell') or
                os.path.realpath(str(args[0])) != os.path.realpath(sys.executable)):
            raise PermissionError('test guard: only project Python children allowed')
        rest = list(args[1:])
        while rest and rest[0] == '-B':
            rest.pop(0)
        if not rest or str(rest[0]).startswith('-'):
            raise PermissionError('test guard: unsupported Python invocation')
        script = Path(rest[0]).resolve()
        if not script.is_relative_to(project) or not script.is_file():
            raise PermissionError('test guard: child script outside test project')
        # A detached direct service is required by the real CLI. Retain its
        # Popen handle and never permit detached helpers or grandchildren.
        service_entries = (project / 'service.py', project / 'core/cli/model_proxy.py')
        if kwargs.get('start_new_session') and script not in service_entries:
            raise PermissionError('test guard: detached non-service child denied')
        if kwargs.get('preexec_fn') or kwargs.get('process_group', -1) != -1:
            raise PermissionError('test guard: unsupported child process setup')
        if kwargs.get('pass_fds') or kwargs.get('close_fds') is False:
            raise PermissionError('test guard: inherited child descriptors denied')
        if kwargs.get('cwd') is not None and os.path.abspath(os.fsdecode(kwargs['cwd'])) != str(project):
            raise PermissionError('test guard: child working directory changed')
        env = kwargs.get('env', os.environ).copy()
        for key in ('PYTHONPATH', 'MP_TEST_PROJECT', 'MP_TEST_ROOT', 'MP_TEST_REAL_HOME', 'MP_TEST_PORTS',
                    'MP_TEST_VENV',
                    'HOME', 'TMPDIR', 'TMP', 'TEMP', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME',
                    'LOCALAPPDATA', 'APPDATA', 'MODEL_PROXY_CONFIG', 'MODEL_PROXY_PORT'):
            if env.get(key) != baseline.get(key):
                raise PermissionError('test guard: child environment changed: ' + key)
        if any(k.startswith('PYTHON') and env[k] != baseline.get(k) for k in env):
            raise PermissionError('test guard: Python startup environment changed')
        env['MP_TEST_ROLE'] = 'service'
        env.pop('MP_TEST_GUARD_INSTALLED', None)
        kwargs['env'] = env
        # No wrapper arguments: production validates exact kernel argv.
        if any((Path(os.environ['MP_TEST_VENV']) / 'lib' /
                ('python%d.%d' % sys.version_info[:2]) / 'site-packages').iterdir()):
            raise PermissionError('test guard: interpreter site-packages changed')
        if tuple(startup_file(path) for path in startup_paths) != startup_files:
            raise PermissionError('test guard: interpreter bootstrap changed')
        kwargs.setdefault('stdin', subprocess.DEVNULL)
        kwargs.setdefault('stdout', subprocess.DEVNULL)
        kwargs.setdefault('stderr', subprocess.DEVNULL)
        p = original_popen(args, *pos, **kwargs)
        children.append(p)
        return p

    def kill(pid, sig):
        for child in children:
            if child.pid == pid:
                if child.poll() is not None:
                    raise ProcessLookupError('test child already exited')
                return original_kill(pid, sig)
        raise PermissionError('test guard: signal to non-child denied')
    def denied(*args, **kwargs):
        raise PermissionError('test guard: unsupported process operation')
    subprocess.Popen = guarded_popen
    os.kill = kill
    for name in ('killpg', 'system', 'popen', 'fork', 'forkpty', 'posix_spawn', 'posix_spawnp',
                 'execl', 'execle', 'execlp', 'execlpe', 'execv', 'execve', 'execvp', 'execvpe', 'setsid'):
        if hasattr(os, name):
            setattr(os, name, denied)
    # Only the worker uses this list, and it remains alive while managing its children.
    sys._mp_test_children = children

    # Keep Python-origin file operations in scratch. This is not a kernel
    # boundary: native code or unreviewed interpreters can bypass Python hooks.
    realpath = os.path.realpath
    libraries = [Path(realpath(sys.base_prefix)), Path(realpath(sys.prefix)),
                 Path('/usr/lib'), Path('/System/Library'), Path('/Library/Frameworks')]
    def inside(path, root):
        return path == root or root in path.parents
    def check_file(value, write=False, directory_only=False):
        if isinstance(value, int):
            return
        try:
            path = Path(realpath(os.fsdecode(value)))
        except (TypeError, ValueError):
            raise PermissionError('test guard: invalid filesystem path')
        if str(path) == '/dev/null':
            return
        if inside(path, scratch):
            return
        # Native protection checks open each ancestor directory read-only.
        # Permit only those exact ancestors, and only O_DIRECTORY opens; this
        # does not permit reading their other files or writing outside scratch.
        if not write and directory_only and path in scratch.parents and not inside(path, real_home):
            return
        if not write and any(inside(path, lib) for lib in libraries) and not inside(path, real_home):
            return
        raise PermissionError('test guard: filesystem path outside scratch')
    def audit(event, args):
        if event == 'open':
            mode, flags = args[1], args[2]
            writing = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
                isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))
            directory_only = isinstance(flags, int) and bool(flags & getattr(os, 'O_DIRECTORY', 0))
            check_file(args[0], writing, directory_only)
        elif event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.chown',
                       'os.truncate', 'os.utime'):
            check_file(args[0], True)
        elif event in ('os.rename', 'os.link', 'os.symlink'):
            check_file(args[0], True); check_file(args[1], True)
    sys.addaudithook(audit)
    # stat/lstat do not emit audit events. Do not even probe legacy tmp paths
    # or the real home (other than our scratch if scratch happens to be there).
    def metadata_allowed(value):
        if isinstance(value, int):
            return
        try:
            text = os.path.abspath(os.fsdecode(value))
        except (ValueError, TypeError):
            return
        if text.startswith(('/tmp/model_proxy', '/private/tmp/model_proxy')):
            raise PermissionError('test guard: legacy runtime path denied')
        home = str(real_home)
        if (text == home or text.startswith(home + os.sep)) and not (text == str(scratch) or text.startswith(str(scratch) + os.sep)):
            raise PermissionError('test guard: real home metadata denied')
    for name in ('stat', 'lstat', 'access', 'readlink', 'listdir', 'scandir'):
        original = getattr(os, name)
        def wrapper(path='.', *args, _original=original, **kwargs):
            metadata_allowed(path)
            return _original(path, *args, **kwargs)
        setattr(os, name, wrapper)
    # Publish readiness only after every guard has been installed.
    os.environ['MP_TEST_GUARD_INSTALLED'] = '1'
