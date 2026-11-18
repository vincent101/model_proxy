"""Local Win32_Process command-line observation and shared-entry validation.

WindowsControl brackets this observation with a pinned process/peer handle.
Native Windows and PowerShell/CIM behavior remain unvalidated. Results do not
prove process memory or protect against same-user impersonation. There is no
shell and no termination except for this invocation's independently checked,
directly spawned PowerShell helper. Unconfirmed cleanup retains the gate.
"""
from __future__ import annotations

import base64
import binascii
import ctypes
import math
import ntpath
import os
import queue
import re
import subprocess
import sys
import threading
import time
from typing import Any

from core import windows_process
from core.cli.windows_control import _local_absolute


class CommandLineObservationError(RuntimeError):
    """Only fixed error codes; never include paths, command lines or OS text."""
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def parse_windows_argv(command_line: str) -> tuple[str, ...]:
    """Parse the actual Windows command line using the native argv parser."""
    if (not isinstance(command_line, str) or not command_line or "\0" in command_line
            or len(command_line) > 32767 or os.name != "nt"):
        raise CommandLineObservationError("command_line_unavailable")
    try:
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        shell.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
        shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        count = ctypes.c_int()
        result = shell.CommandLineToArgvW(command_line, ctypes.byref(count))
        if not result:
            raise ValueError("argv unavailable")
        try:
            if not 1 <= count.value <= 32:
                raise ValueError("argv invalid")
            return tuple(result[i] for i in range(count.value))
        finally:
            if kernel.LocalFree(ctypes.cast(result, ctypes.c_void_p)):
                raise ValueError("argv release unavailable")
    except Exception:
        raise CommandLineObservationError("command_line_unavailable") from None


def verify_shared_launch_argv(argv, *, entry: str, config: str, port: int) -> None:
    """Validate parsed service entry and explicit options, not raw serialization.

    The executable's actual image and process creation time must independently
    be checked through a held process handle by the caller.
    """
    def same_path(left, right):
        return (_local_absolute(left) and _local_absolute(right)
                and ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(ntpath.normpath(right)))

    try:
        if (not isinstance(argv, (tuple, list)) or len(argv) < 6
                or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
                or not _local_absolute(argv[0]) or type(port) is not int
                or not 1 <= port <= 65535):
            raise ValueError("invalid")
        i = 1
        while i < len(argv) and argv[i] in ("-B", "-I", "-S"):
            i += 1
        if i >= len(argv) or not same_path(argv[i], entry):
            raise ValueError("entry mismatch")
        items = argv[i + 1:]
        if len(items) != 4 or set(items[::2]) != {"--config", "--port"}:
            raise ValueError("options mismatch")
        options = dict(zip(items[::2], items[1::2]))
        if (not same_path(options["--config"], config)
                or options["--port"] != str(port)):
            raise ValueError("options mismatch")
    except (KeyError, TypeError, ValueError):
        raise CommandLineObservationError("launch_mismatch") from None


def verify_shared_launch(command_line: str, *, entry: str, config: str, port: int) -> None:
    verify_shared_launch_argv(parse_windows_argv(command_line), entry=entry, config=config, port=port)


# This fixed program reads only a numeric PID on stdin. No user text is ever
# interpolated into PowerShell or shell syntax. No ComputerName/CimSession means
# a local CIM query. System installation and binary trust remain unverified.
_SCRIPT = r'''$ErrorActionPreference = 'Stop'
try {
    [Console]::OutputEncoding = [Text.ASCIIEncoding]::new()
    $s = [Console]::In.ReadLine()
    if ($null -eq $s -or $s -cnotmatch '^[1-9][0-9]{0,9}$') { exit 2 }
    $n = [UInt32]::Parse($s, [Globalization.CultureInfo]::InvariantCulture)
    Microsoft.PowerShell.Core\Import-Module -Name ($PSHOME + '\Modules\CimCmdlets\CimCmdlets.psd1') -ErrorAction Stop
    $items = @(CimCmdlets\Get-CimInstance -Namespace 'root/cimv2' -ClassName 'Win32_Process' -Filter ('ProcessId = ' + $s) -Property @('ProcessId','CommandLine') -OperationTimeoutSec 2 -ErrorAction Stop)
    if ($items.Count -ne 1 -or [UInt32]$items[0].ProcessId -ne $n) { exit 3 }
    $c = $items[0].CommandLine
    if ($null -eq $c -or $c -isnot [string] -or $c.Length -eq 0 -or $c.Length -gt 32767 -or $c.Contains([char]0)) { exit 4 }
    $b = [Convert]::ToBase64String(([Text.UnicodeEncoding]::new($false, $false, $true)).GetBytes($c))
    [Console]::Out.Write("MPW1`n" + $s + "`n" + $b + "`n")
    exit 0
} catch { exit 5 }
'''
_ENCODED_SCRIPT = base64.b64encode(_SCRIPT.encode('utf-16-le')).decode('ascii')
_MAX_OUTPUT = 4 + 1 + 10 + 1 + 4 * ((32767 * 2 + 2) // 3) + 1
_GATE = threading.Lock()
_DIRECT_POPEN = subprocess.Popen
_CLEANUP_TIMEOUT = 1.0
_CLEANUP_REPLY_WAIT = 1.25
_WORKER_CODES = frozenset({'platform_unavailable', 'system_directory_unavailable',
                           'command_line_response_invalid', 'command_line_query_failed',
                           'helper_start_unconfirmed', 'helper_ownership_unverified',
                           'helper_cleanup_unconfirmed'})


def _system_directory() -> str:
    """Obtain Windows' system directory from the OS, never from PATH/env."""
    if os.name != 'nt':
        raise CommandLineObservationError('platform_unavailable')
    try:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        fn = kernel.GetSystemDirectoryW
        fn.argtypes = [ctypes.POINTER(ctypes.c_wchar), ctypes.c_uint32]
        fn.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        size = fn(buffer, len(buffer))
        if not 0 < size < len(buffer):
            raise ValueError('unavailable')
        result = buffer.value
        if len(result) != size or not _local_absolute(result):
            raise ValueError('invalid')
        return result
    except CommandLineObservationError:
        raise
    except Exception:
        raise CommandLineObservationError('system_directory_unavailable') from None


def parse_observation(payload: Any, expected_pid: int) -> str:
    """Parse one exact bounded ASCII response; any ambiguity is an error."""
    if type(expected_pid) is not int or not 1 < expected_pid <= 0xffffffff:
        raise CommandLineObservationError('pid_invalid')
    try:
        if not isinstance(payload, bytes) or len(payload) > _MAX_OUTPUT:
            raise ValueError('invalid')
        match = re.fullmatch(rb'MPW1\n([1-9][0-9]{0,9})\n([A-Za-z0-9+/]+={0,2})\n', payload)
        if match is None or int(match.group(1)) != expected_pid:
            raise ValueError('invalid')
        data = base64.b64decode(match.group(2), validate=True)
        # Enforce canonical encoding, no alternate encodings or invalid UTF-16.
        if (not data or len(data) > 32767 * 2 or len(data) % 2
                or base64.b64encode(data) != match.group(2)):
            raise ValueError('invalid')
        text = data.decode('utf-16-le', errors='strict')
        if not text or '\0' in text:
            raise ValueError('invalid')
        return text
    except (ValueError, UnicodeError, binascii.Error, OverflowError):
        raise CommandLineObservationError('command_line_response_invalid') from None


def _is_windows() -> bool:
    return os.name == 'nt' and sys.platform == 'win32'


class _OwnedHelper:
    """The direct Popen result and identity read from its own Windows handle."""
    def __init__(self, process, service_pid, backend):
        self._process, self._service_pid, self._backend = process, service_pid, backend
        self._handle_obj = process._handle
        self._handle = int(self._handle_obj)
        self._pid = process.pid
        if (type(self._pid) is not int or not 1 < self._pid <= 0xffffffff
                or self._pid in (service_pid, os.getpid())
                or self._handle in (0, -1, ctypes.c_void_p(-1).value)):
            raise ValueError('unverified')
        if backend.process_id(self._handle) != self._pid:
            raise ValueError('unverified')
        self._ticks = backend.creation_ticks(self._handle)
        if type(self._ticks) is not int or self._ticks <= 0:
            raise ValueError('unverified')
        self._lock = threading.Lock()
        self._result = None

    def _check(self):
        try:
            p = self._process
            if (not _is_windows() or type(p) is not _DIRECT_POPEN or p.pid != self._pid
                    or p.pid in (self._service_pid, os.getpid())
                    or p._handle is not self._handle_obj or int(p._handle) != self._handle
                    or self._backend.process_id(self._handle) != self._pid
                    or self._backend.creation_ticks(self._handle) != self._ticks):
                raise ValueError('unverified')
        except BaseException:
            raise CommandLineObservationError('helper_ownership_unverified') from None

    def cleanup(self) -> str:
        """Only this handle; exactly one cleanup attempt, bounded by caller too."""
        with self._lock:
            if self._result is not None:
                return self._result
            try:
                deadline = time.monotonic() + _CLEANUP_TIMEOUT
                self._check()
                status = self._process.poll()
                if status is None:
                    # Recheck immediately before Popen.kill: on Windows it uses
                    # the retained child process handle, not a PID lookup.
                    self._check()
                    try:
                        self._process.kill()
                    except OSError:
                        # It may have exited concurrently; only a successful
                        # bounded wait below can establish that it was reaped.
                        pass
                elif type(status) is not int:
                    raise ValueError('unverified')
                status = self._process.wait(timeout=max(0.0, deadline - time.monotonic()))
                if type(status) is not int:
                    raise ValueError('unverified')
                self._result = 'reaped'
            except CommandLineObservationError:
                self._result = 'helper_ownership_unverified'
            except BaseException:
                self._result = 'helper_cleanup_unconfirmed'
            return self._result


def _capture_owned_helper(process, service_pid, direct, backend_factory):
    # Test-injected factories are not evidence of child ownership. Likewise,
    # never invoke Windows-handle termination on a non-Windows Popen.
    if not direct or not _is_windows() or type(process) is not _DIRECT_POPEN:
        return None
    try:
        return _OwnedHelper(process, service_pid, backend_factory())
    except BaseException:
        return None


class _HelperLifecycle:
    """Serialize timeout/spawn races and a single cleanup attempt."""
    def __init__(self):
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._cancelled = False
        self._spawning = False
        self._attached = False
        self._guard = None
        self._cleanup_started = False
        self._result = None

    def begin_spawn(self):
        with self._lock:
            if self._cancelled:
                self._result = 'not_spawned'
                self._event.set()
                return False
            self._spawning = True
            return True

    def spawn_failed(self):
        with self._lock:
            self._spawning = False
            self._result = 'helper_start_unconfirmed'
            self._event.set()

    def attach(self, guard):
        with self._lock:
            self._spawning = False
            self._attached = True
            self._guard = guard
            cancelled = self._cancelled
        if cancelled:
            self.request_cleanup()
        return not cancelled

    def note_reaped(self):
        """Avoid starting a timeout kill after the worker has already waited."""
        with self._lock:
            if not self._event.is_set() and not self._cleanup_started:
                self._result = 'reaped'
                self._event.set()

    def request_cleanup(self):
        with self._lock:
            self._cancelled = True
            if self._event.is_set() or self._cleanup_started:
                return
            if not self._attached:
                if not self._spawning:
                    self._result = 'not_spawned'
                    self._event.set()
                return
            if self._guard is None:
                self._result = 'helper_ownership_unverified'
                self._event.set()
                return
            self._cleanup_started = True
            guard = self._guard

        def cleanup():
            try:
                outcome = guard.cleanup()
            except BaseException:
                outcome = 'helper_cleanup_unconfirmed'
            with self._lock:
                self._result = outcome
                self._event.set()

        try:
            threading.Thread(target=cleanup, name='win-helper-cleanup', daemon=True).start()
        except BaseException:
            with self._lock:
                self._result = 'helper_cleanup_unconfirmed'
                self._event.set()

    def wait_cleanup(self, timeout=None):
        if not self._event.wait(_CLEANUP_REPLY_WAIT if timeout is None else timeout):
            return 'helper_cleanup_unconfirmed'
        with self._lock:
            return self._result

    def wait_for_inflight_cleanup(self):
        """Do not release single-flight while a cleanup thread still runs."""
        with self._lock:
            started = self._cleanup_started
        return not started or self._event.wait(_CLEANUP_REPLY_WAIT)


class PowerShellWmiReader:
    """Opt-in experimental local reader; Windows behavior remains untested.

    Dependency: Windows PowerShell 5.1 with CimCmdlets/COM available in the
    system directory. Timeout can stop only the verified directly spawned helper,
    using its held handle. Cleanup failure/uncertain ownership fails closed.
    If spawn or cleanup cannot be confirmed, a helper may remain and the
    per-interpreter single-flight gate stays occupied until it is reaped.
    Injectable private factories support mock-only, non-Windows tests.
    """
    def __init__(self, timeout: float = 5.0, *, _process_factory=None,
                 _directory_provider=None, _cleanup_backend_factory=None):
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= 30):
            raise CommandLineObservationError('timeout_invalid')
        self._timeout = float(timeout)
        self._direct = _process_factory is None
        self._process_factory = _DIRECT_POPEN if self._direct else _process_factory
        self._directory_provider = _system_directory if _directory_provider is None else _directory_provider
        self._cleanup_backend_factory = (windows_process.NativeBackend if _cleanup_backend_factory is None
                                         else _cleanup_backend_factory)

    def read_command_line(self, pid: int) -> str:
        if type(pid) is not int or not 1 < pid <= 0xffffffff:
            raise CommandLineObservationError('pid_invalid')
        gate = _GATE
        if not gate.acquire(blocking=False):
            raise CommandLineObservationError('observer_busy')
        result = queue.Queue(maxsize=1)
        lifecycle = _HelperLifecycle()
        finished = threading.Event()
        completion = {'clean': False}

        def worker():
            process = None
            decision = (False, 'command_line_query_failed')
            reaped = False
            spawn_uncertain = False
            cleanup_known = True
            try:
                directory = self._directory_provider()
                if not _local_absolute(directory):
                    raise CommandLineObservationError('system_directory_unavailable')
                root = ntpath.dirname(directory)
                if not _local_absolute(root):
                    raise CommandLineObservationError('system_directory_unavailable')
                psdir = ntpath.join(directory, 'WindowsPowerShell', 'v1.0')
                exe = ntpath.join(psdir, 'powershell.exe')
                args = [exe, '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', _ENCODED_SCRIPT]
                env = {'SystemRoot': root, 'PSModulePath': ntpath.join(psdir, 'Modules')}
                if not lifecycle.begin_spawn():
                    raise CommandLineObservationError('command_line_query_failed')
                try:
                    process = self._process_factory(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                    stderr=subprocess.DEVNULL, shell=False, env=env,
                                                    close_fds=True,
                                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    if process is None:
                        raise ValueError('unconfirmed')
                except BaseException:
                    spawn_uncertain = True
                    lifecycle.spawn_failed()
                    raise CommandLineObservationError('helper_start_unconfirmed') from None
                # Private test injection cannot establish that this is the
                # system WMI helper. Only the direct native providers qualify
                # for termination; injected observations stay evidence-only.
                direct_native = (self._direct and self._process_factory is _DIRECT_POPEN
                                 and self._directory_provider is _system_directory
                                 and self._cleanup_backend_factory is windows_process.NativeBackend)
                guard = _capture_owned_helper(process, pid, direct_native,
                                              self._cleanup_backend_factory)
                if not lifecycle.attach(guard):
                    raise CommandLineObservationError('command_line_query_failed')
                if guard is None and (self._direct or isinstance(process, _DIRECT_POPEN)):
                    # Never use a real/direct Popen result if its helper
                    # ownership could not be established. Pure injected fakes
                    # remain available for observation-only unit tests.
                    raise CommandLineObservationError('helper_ownership_unverified')
                process.stdin.write((str(pid) + '\n').encode('ascii'))
                process.stdin.close()
                payload = process.stdout.read(_MAX_OUTPUT + 1)
                if not isinstance(payload, bytes) or len(payload) > _MAX_OUTPUT:
                    raise CommandLineObservationError('command_line_response_invalid')
                returncode = process.wait()
                if type(returncode) is not int:
                    raise CommandLineObservationError('command_line_query_failed')
                reaped = True
                lifecycle.note_reaped()
                if returncode != 0:
                    raise CommandLineObservationError('command_line_query_failed')
                text = parse_observation(payload, pid)
                decision = (True, text)
            except BaseException as exc:
                code = (exc.code if isinstance(exc, CommandLineObservationError)
                        and exc.code in _WORKER_CODES else 'command_line_query_failed')
                if process is not None and not reaped:
                    lifecycle.request_cleanup()
                    outcome = lifecycle.wait_cleanup()
                    if outcome == 'reaped':
                        reaped = True
                    else:
                        code = (outcome if outcome in _WORKER_CODES
                                else 'helper_cleanup_unconfirmed')
                decision = (False, code)
            finally:
                if process is not None:
                    for name in ('stdin', 'stdout'):
                        try:
                            stream = getattr(process, name, None)
                            if stream is not None:
                                stream.close()
                                if getattr(stream, 'closed', True) is False:
                                    cleanup_known = False
                        except BaseException:
                            cleanup_known = False
                    # A bounded last wait; never look up or stop by PID.
                    if not reaped:
                        try:
                            reaped = type(process.wait(timeout=_CLEANUP_TIMEOUT)) is int
                        except BaseException:
                            pass
                try:
                    cleanup_finished = lifecycle.wait_for_inflight_cleanup()
                except BaseException:
                    cleanup_finished = False
                if not cleanup_finished:
                    cleanup_known = False
                # If reaping is uncertain, stay fail-closed rather than leak an
                # unbounded series of helpers on repeated calls.
                if cleanup_known and ((process is None and not spawn_uncertain) or reaped):
                    try:
                        gate.release()
                    except BaseException:
                        cleanup_known = False
                else:
                    cleanup_known = False
                if not cleanup_known and (decision[0] or decision[1] not in (
                        'helper_ownership_unverified', 'helper_start_unconfirmed')):
                    decision = (False, 'helper_cleanup_unconfirmed')
                completion['clean'] = cleanup_known
                finished.set()
                # A successful response must never precede final cleanup and
                # gate release. Failure with unknown cleanup keeps the gate.
                try:
                    result.put_nowait(decision)
                except queue.Full:
                    pass

        try:
            thread = threading.Thread(target=worker, name='win-command-line-observer', daemon=True)
            thread.start()
        except BaseException:
            gate.release()
            raise CommandLineObservationError('observer_unavailable') from None
        try:
            ok, text = result.get(timeout=self._timeout)
        except queue.Empty:
            cleanup_deadline = time.monotonic() + _CLEANUP_REPLY_WAIT
            lifecycle.request_cleanup()
            outcome = lifecycle.wait_cleanup(max(0.0, cleanup_deadline - time.monotonic()))
            if outcome == 'reaped':
                done = finished.wait(max(0.0, cleanup_deadline - time.monotonic()))
                code = 'observer_timeout' if done and completion['clean'] else 'helper_cleanup_unconfirmed'
            else:
                code = ('observer_timeout' if outcome == 'not_spawned' else
                        outcome if outcome in _WORKER_CODES else 'helper_cleanup_unconfirmed')
            raise CommandLineObservationError(code) from None
        if not ok:
            raise CommandLineObservationError(text)
        return text
