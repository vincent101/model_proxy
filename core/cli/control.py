"""Injectable CLI lifecycle and same-connection administration.

Records alone are not authorization. MacControl fails closed without a supplied
authority; dispatch normally supplies the Mac implementation. Windows stop
and force are not authorized by this module.
"""
from __future__ import annotations

import contextlib
import errno
import http.client
import ipaddress
import json
import os
import re
import socket
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import ContextManager, Protocol

from core import platform_process
from core.discovery import (IDENTITY_LEGACY_SAME_BOOT, IDENTITY_SAME, _record_schema_ok,
                            inspect_runtime_identities, lock_identity_matches_observed,
                            lock_identity_requires_same_boot, verify_fixed_state_directory,
                            PreflightError)
from core.listen_config import resolve_listen, ListenConfigError
from core.runtime_paths import PATH_KEYS, read_runtime_paths, RuntimePathsError


class ControlError(RuntimeError):
    def __init__(self, code: str, detail: str = ''):
        self.code, self.detail = code, detail
        super().__init__(code)


class StartupPending(RuntimeError):
    """Authority says its own launched child has not published a complete set.

    Only valid during a bounded post-spawn wait; never authorizes a signal,
    request or another spawn. Permission/identity failures must not be mapped
    to this exception.
    """


@dataclass(frozen=True)
class Instance:
    pid: int
    start: dict
    executable: str
    nonce: str
    host: str
    port: int
    active: dict
    last_good: dict


INSTALL_INITIALIZED = 'INITIALIZED'
INSTALL_UNINITIALIZED_OR_RECORD_LOST = 'UNINITIALIZED_OR_RECORD_LOST'
INSTALL_IDENTITY_UNVERIFIED = 'IDENTITY_UNVERIFIED'
INSTALL_CHANGED = 'IDENTITY_CHANGED'


class AuthorityLease(Protocol):
    """Held under the installation's verified fixed control.lock.

    All methods must fail closed on permission, replacement, incomplete record,
    unknown process or lock identity. records() must read stable protected files;
    it may raise StartupPending only while a verified new child is publishing a
    transient incomplete record set, never on permission/identity failures.
    verify_active() must independently check install, actual PID creation/image,
    service entry and effective config against the protected record and root.
    For running/READY verification the fixed anchor must also have a verified
    flock conflict; that conflict does not prove its holder's PID. Recovery
    stop is separately gated and does not depend on attributing a flock owner.
    The user accepts same-user impersonation and the identity-check/signal race.
    Except for plain last-good off, the config must match config_path passed
    to locked(). Plain off uses the protected installation/instance records
    and launch identity without reading the mutable business configuration.
    Control lock covers CLI operations; direct service entry separately uses
    the instance locks.
    assert_startable() must prove correct installation, paths, both locks free,
    absence of any live/unknown old instance, and valid stable launch config.
    assert_restart_supported() must preflight configuration drift and reject
    unprovable same-port wildcard expansion before any process is signalled.
    stop_lock_evidence(instance) must record the exact fixed lock objects
    (device+inode) that the caller is about to stop, before any signal. A
    missing, replaced, unreadable or non-fixed object must fail closed.
    assert_stopped(instance) must prove this instance exited and both original
    objects recorded by stop_lock_evidence were released and are re-acquirable
    without conflict; re-acquiring replacement files at the same paths is not
    proof. Missing evidence is rejected, never re-captured after the signal.
    With require_identity_evidence=False (plain off) a recorded lock identity
    is not compared; the fixed paths, protection, object evidence and liveness
    still are. admin_token()
    must read the SAME verified effective server config securely; no token may
    be included in error text or logs. In particular process image=python alone
    is insufficient to identify the script or its effective MODEL_PROXY_CONFIG.
    installation_state() must read the protected schema-2/4 install/active/last
    set under the fixed short lock and return only the four constants above, or
    raise on corruption, partial records, uncertain lock/ACL, or other UNKNOWN.
    INITIALIZED requires a consistent protected install set and a stable root
    identity (uuid+inode; a schema-2 record is accepted only in its own boot).
    IDENTITY_UNVERIFIED means the directory inode is unchanged but the record's
    boot/volume differs; only the interactive rebaseline may rewrite it.
    Installation identity is judged only by startup paths (installation_state,
    assert_startable) and the credential-bearing installation_identity() gate;
    plain status/off must neither collect a volume UUID nor compare a recorded
    inode, and verify_active/assert_stopped are called there with
    require_identity_evidence=False. Process, connected-peer, port and lock
    liveness checks still apply on every path.
    UNINITIALIZED_OR_RECORD_LOST requires all three records absent, a verified
    fixed anchor demonstrably free, and safe path/directory protection; it is
    not proof of a new installation. A held anchor with no record is UNKNOWN
    after bounded waiting, never permission to initialize or launch.

    initialize_interactively(stdin, stdout) is only called for the explicit
    UNINITIALIZED_OR_RECORD_LOST state and matching real TTY descriptors. It
    must acquire and retain fixed anchor then legacy lock and the actual bound
    listener throughout: full read-only preflight; authorized local inspection
    of known legacy entrypoints/locks, listener and permissions; prompt for a
    nonempty installation/recovery source and explicit YES covering old
    starters, shared ledger, synchronized files and disabled autostart; repeat
    config/path/identity/lock/listener checks after input; atomically initialize
    using core's verified initialization primitive. Unprovable checks, EOF,
    cancellation, changed state or denied confirmation must not write an install
    record. Release anchor/legacy/listener before returning to the launcher;
    keep the short control lock through spawn. The server independently repeats
    its own checks. Returning True without these guarantees is unsafe.
    rebaseline_interactively(stdin, stdout) is the only path for
    IDENTITY_UNVERIFIED: same TTY/locks/listener protocol, unchanged inode and
    project root required, install_id preserved, bounded history appended.
    Methods returning bool must return the literal True to authorize.
    """
    def installation_state(self) -> str: ...
    def installation_identity(self) -> str | None: ...
    def initialize_interactively(self, stdin, stdout) -> bool: ...
    def rebaseline_interactively(self, stdin, stdout) -> bool: ...
    def records(self) -> tuple[dict | None, dict | None]: ...
    def verify_active(self, instance: Instance, *,
                      require_identity_evidence: bool = True) -> bool: ...
    def assert_startable(self) -> bool: ...
    def stop_lock_evidence(self, instance: Instance) -> dict: ...
    def assert_stopped(self, instance: Instance, *,
                       require_identity_evidence: bool = True) -> bool: ...
    def assert_restart_supported(self) -> bool: ...
    def admin_token(self) -> str: ...
    def verify_admin_token(self) -> bool: ...


class InstallationAuthority(Protocol):
    def locked(self, root: Path, config_path: Path) -> ContextManager[AuthorityLease]: ...


class UnavailableControl:
    def on(self):
        return self._unavailable('on', 'installation authority is not integrated')

    def off(self):
        return self._unavailable('off', 'installation authority or approved stop is unavailable')

    def restart(self):
        return self._unavailable('restart', 'installation authority or approved stop is unavailable')

    def status(self):
        return self._unavailable('status', 'installation/connected peer identity unavailable')

    def reload(self):
        return self._unavailable('reload', 'installation/connected peer identity unavailable')

    @staticmethod
    def _unavailable(command, reason):
        print(f'Error: {command} unavailable: {reason}; no signal or credential sent.', file=sys.stderr)
        return 1


def _endpoint(host: str, port: int) -> tuple[int, str, int]:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ControlError('invalid_listen')
    # Never resolve DNS or use proxy configuration. Map wildcard to local only.
    if host in ('127.0.0.1', '0.0.0.0') or isinstance(host, str) and host.lower() == 'localhost':
        return socket.AF_INET, '127.0.0.1', port
    if host in ('::1', '::'):
        return socket.AF_INET6, '::1', port
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_unspecified:
            if isinstance(ip, ipaddress.IPv6Address) and ip.scope_id is not None:
                raise ControlError('unsupported_listen')
            return ((socket.AF_INET, '127.0.0.1', port) if ip.version == 4
                    else (socket.AF_INET6, '::1', port))
        if platform_process.is_local_numeric_address(host):
            return (socket.AF_INET if ip.version == 4 else socket.AF_INET6, str(ip), port)
    except (ValueError, TypeError):
        pass
    raise ControlError('unsupported_listen')


def _check_mac_new_listener(listen) -> None:
    """Gate new Mac starts against the server's unchanged AF_INET listener."""
    if sys.platform != 'darwin':
        return
    family, _, _ = _endpoint(listen['host'], listen['port'])
    if family != socket.AF_INET:
        raise ControlError('mac_ipv6_listen_unsupported', 'no signal or credential sent')


def _stop_record_host(host: str, port: int) -> None:
    """Validate a recorded endpoint without requiring its interface to remain up."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise ControlError('invalid_listen')
    if isinstance(host, str) and host.lower() == 'localhost':
        return
    try:
        ip = ipaddress.ip_address(host)
        if (ip.is_multicast or str(ip) == '255.255.255.255'
                or isinstance(ip, ipaddress.IPv6Address)
                and (ip.scope_id is not None or ip.ipv4_mapped is not None or ip.is_link_local)):
            raise ValueError('unsupported address')
    except (ValueError, TypeError):
        raise ControlError('unsupported_listen') from None


def _valid_record(root: Path, active, last_good, *, allow_unavailable_host=False,
                  require_identity_evidence=True, allow_boot_drift=False) -> Instance:
    if not isinstance(active, dict) or not isinstance(last_good, dict):
        raise ControlError('records_missing')
    # Schema is judged per record, never as a cross-record equality: a mixed
    # set is exactly the resumable state left by an interrupted migration
    # (install -> active -> last_good). Every other shared field must agree.
    common = ('project_root', 'instance_nonce', 'listen', 'runtime_paths', 'install_id')
    if any(key not in active or key not in last_good or active[key] != last_good[key] for key in common):
        raise ControlError('records_mismatch')

    def observation_only(record):
        # Schema 1 is the pre-stability observation form; it is accepted only
        # as a whole pair and never carries installation authority.
        return (type(record.get('schema')) is int and record['schema'] == 1
                and record.get('trust') == 'observation_only')

    def stable_record_ok(record, name):
        return (_record_schema_ok(record, name) and 'trust' not in record
                and isinstance(record.get('install_id'), str)
                and re.fullmatch(r'[0-9a-f]{32}', record['install_id']) is not None)

    if observation_only(active) or observation_only(last_good):
        valid = observation_only(active) and observation_only(last_good)
    else:
        valid = stable_record_ok(active, 'active') and stable_record_ok(last_good, 'last_good_paths')
    if (not valid or active.get('record') != 'active'
            or last_good.get('record') != 'last_good_paths'
            or active.get('state') != 'READY'
            or active['project_root'] != str(root)):
        raise ControlError('record_invalid')
    pid, start, executable = active.get('pid'), active.get('process_start'), active.get('executable')
    nonce, listen, paths = active['instance_nonce'], active['listen'], active['runtime_paths']
    if (type(pid) is not int or pid <= 1 or not isinstance(start, dict) or not start
            or not isinstance(executable, str) or not os.path.isabs(executable)
            or not isinstance(nonce, str) or not re.fullmatch(r'[0-9a-f]{32}', nonce)
            or not isinstance(listen, dict) or not isinstance(listen.get('host'), str)
            or not isinstance(paths, dict) or set(paths) != set(PATH_KEYS)
            or any(not isinstance(v, str) or not os.path.isabs(v) for v in paths.values())):
        raise ControlError('record_invalid')
    host, port = listen['host'], listen.get('port')
    if allow_unavailable_host:
        _stop_record_host(host, port)
    else:
        _endpoint(host, port)
    snapshot = inspect_runtime_identities(root, paths).as_dict()
    if any(snapshot[key].canonical != paths[key] for key in PATH_KEYS):
        raise ControlError('runtime_identity_changed')
    locks = active.get('locks')
    if not isinstance(locks, dict):
        raise ControlError('lock_identity_missing')
    for name, key in (('anchor', 'anchor.lock'), ('legacy', 'lock')):
        if not isinstance(locks.get(name), dict) or not snapshot[key].exists:
            raise ControlError('lock_identity_changed')
    if require_identity_evidence:
        # Only `on`/startup and the credential-bearing reload judge installation
        # identity here: the fixed state directory's volume UUID and each
        # recorded lock's stable identity. Plain status/off locate the recorded
        # instance structurally and rely on live process/port/lock evidence, so
        # they must never collect a UUID or compare a recorded inode.
        # ``allow_boot_drift`` is startup-only: it tolerates a schema-2 lock
        # record's boot-local device only after the install identity was
        # verified as schema 4 (interrupted-migration resume). Live checks and
        # schema-4 records keep the exact comparison.
        verify_fixed_state_directory(root / '.runtime')
        require_same_boot = (lock_identity_requires_same_boot(active)
                             and not allow_boot_drift)
        for name, key in (('anchor', 'anchor.lock'), ('legacy', 'lock')):
            if not lock_identity_matches_observed(
                    locks[name], snapshot[key], require_same_boot=require_same_boot):
                raise ControlError('lock_identity_changed')
    return Instance(pid, start, executable, nonce, host, port, active, last_good)


class MacControl:
    """Use as dispatch.main(..., control=MacControl(root, authority=...)).

    No authority is supplied by this constructor by default.
    """
    def __init__(self, root: str | os.PathLike[str], *, authority: InstallationAuthority | None = None,
                 config_path: str | os.PathLike[str] | None = None,
                 startup_timeout: float = 15.0, stop_timeout: float = 8.0,
                 request_timeout: float = 5.0, out=None, err=None,
                 terminal_in=None, terminal_out=None):
        self.root = Path(root).resolve(strict=True)
        self.authority = authority
        raw_config = os.fspath(config_path) if config_path is not None else (
            os.environ.get('MODEL_PROXY_CONFIG') or str(self.root / 'config' / 'model_proxy_config.json'))
        if not isinstance(raw_config, str) or not raw_config or '\0' in raw_config:
            raise ValueError('invalid configuration path')
        self._config_source = (raw_config if os.path.isabs(raw_config)
                               else os.getcwd() + '/' + raw_config)
        self.config_path = Path(self._config_source)
        # Relative MODEL_PROXY_CONFIG retains the caller's cwd as server does.
        self.config_path = self.config_path.absolute()
        if min(startup_timeout, stop_timeout, request_timeout) <= 0:
            raise ValueError('timeouts must be positive')
        self.startup_timeout, self.stop_timeout, self.request_timeout = startup_timeout, stop_timeout, request_timeout
        self.out, self.err = out or sys.stdout, err or sys.stderr
        self.terminal_in = sys.stdin if terminal_in is None else terminal_in
        self.terminal_out = sys.stdout if terminal_out is None else terminal_out

    def _run(self, command, action):
        if sys.platform != 'darwin':
            print(f'Error: {command}: unsupported platform; no signal or credential sent.', file=self.err)
            return 1
        if self.authority is None:
            print(f'Error: {command}: installation authority not integrated; no signal or credential sent.', file=self.err)
            return 1
        try:
            stop_lock = getattr(self.authority, 'locked_for_stop', None)
            lock = stop_lock if command == 'off' and callable(stop_lock) else self.authority.locked
            with lock(self.root, self._config_source) as lease:
                action(lease)
            return 0
        except ControlError as exc:
            codes = {'invalid_listen', 'unsupported_listen', 'records_missing', 'records_mismatch',
                     'record_invalid', 'runtime_identity_changed', 'lock_identity_missing',
                     'lock_identity_changed', 'installation_identity_unknown', 'instance_changed',
                     'port_unavailable', 'port_check_unknown', 'anchor_unverified', 'legacy_lock_unverified',
                     'mac_ipv6_listen_unsupported',
                     'start_unverified', 'start_state_changed',
                     'service_entry_unavailable', 'spawn_failed', 'startup_failed',
                     'startup_identity_changed', 'startup_identity_unknown', 'startup_unconfirmed',
                     'stop_timeout', 'release_unverified', 'lock_evidence_unavailable',
                     'admin_token_unavailable',
                     'wildcard_restart_requires_off',
                     'restart_preflight_unavailable',
                     'response_unknown', 'authentication_or_admin_unavailable', 'reload_failed',
                     'status_failed', 'running_configuration_changed',
                     'configuration_migration_required',
                     'initialization_authority_unavailable', 'installation_state_unknown',
                     'initialization_interactive_required', 'initialization_unconfirmed', 'initialization_interrupted',
                     'initialization_verification_failed', 'initialization_after_stop_forbidden',
                     'installation_identity_unverified', 'installation_changed',
                     'volume_identity_unavailable',
                     'python_version_unsupported'}
            details = {'no signal or credential sent', 'child exited', 'child may still be running',
                       'child may still be running; no signal sent',
                       'SIGTERM sent; exit unconfirmed, no further signal sent',
                       'process exited but lock release is unknown', 'no credential sent',
                       'request may have been sent; no retry', 'handler not confirmed',
                       'run on interactively after verifying installation and legacy risks',
                       'initialization not confirmed; no child started',
                       'initialization outcome unknown; no child started',
                       'installation identity changed after stop; no restart',
                       'configuration changed; no restart attempted',
                       'old instance stopped; configuration migration required',
                       'runtime paths changed; migration required; no process started',
                       'runtime paths changed; migration required; no restart attempted',
                       'installation identity unverified; run on interactively',
                       'installation changed; full reinitialization required'}
            details.update({'same-port wildcard expansion cannot be preflighted; use off then on',
                            'process exited; locks released; port occupied',
                            'process exited; locks released; port release unknown'})
            code = exc.code if isinstance(exc.code, str) and exc.code in codes else 'control_failed'
            detail = exc.detail if (isinstance(exc.detail, str) and (exc.detail in details
                or re.fullmatch(r'loaded=(yes|no|unknown), cooldown_cleared=(yes|no|unknown)'
                                r'(, error=[a-z_]{1,40})?', exc.detail))) else ''
            print(f'Error: {command}: {code}' + (f'; {detail}' if detail else '') + '.', file=self.err)
        except platform_process.ProcessEvidenceError as exc:
            codes = {'invalid_process_identity', 'process_identity_unavailable', 'process_identity_changed',
                     'process_state_unavailable', 'process_already_exited',
                     'stop_unsupported', 'signal_failed', 'process_liveness_unavailable',
                     'socket_family_unsupported', 'non_loopback_socket', 'non_local_socket',
                     'scoped_ipv6_unsupported',
                     'socket_endpoint_unavailable', 'socket_closed_or_unsolicited_data', 'socket_error',
                     'socket_unavailable', 'connected_peer_unsupported', 'libproc_unavailable',
                     'fd_list_unavailable', 'socket_fd_unavailable', 'ambiguous_socket_owner',
                     'socket_endpoint_changed', 'connected_peer_not_found',
                     'process_launch_unsupported', 'process_launch_unavailable',
                     'process_owner_unverified', 'local_address_unavailable'}
            code = str(exc) if str(exc) in codes else 'process_evidence_unavailable'
            print(f'Error: {command}: {code}; outcome may be unknown.', file=self.err)
        except (PreflightError, RuntimePathsError, ListenConfigError) as exc:
            safe_codes = {'record_changed', 'record_unavailable', 'records_changed',
                          'records_inconsistent', 'protection_changed', 'protection_unavailable',
                          'preflight_changed', 'locks_unverified', 'config_unverified',
                          'config_changed', 'runtime_changed', 'unknown_path', 'config_conflict',
                          'log_write_unverified', 'installation_changed',
                          'installation_identity_unverified', 'volume_identity_unavailable',
                          'identity_evidence_changed', 'record_backup_failed',
                          'record_invalid', 'record_cleanup_failed',
                          'lock_identity_changed', 'lock_identity_missing',
                          'lock_ownership_unverified', 'process_still_active',
                          'process_unverified'}
            code = getattr(exc, 'code', None)
            suffix = f' ({code})' if isinstance(code, str) and code in safe_codes else ''
            print(f'Error: {command}: identity or configuration unavailable{suffix}; outcome may be unknown.', file=self.err)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            # Never print exception contents: they may contain a credential/path.
            print(f'Error: {command}: control failed or state unknown.', file=self.err)
        except Exception:
            print(f'Error: {command}: control failed or state unknown.', file=self.err)
        return 1

    def _instance(self, lease, *, allow_unavailable_host=False,
                  require_identity_evidence=True) -> Instance:
        active, last_good = lease.records()
        instance = _valid_record(self.root, active, last_good,
                                 allow_unavailable_host=allow_unavailable_host,
                                 require_identity_evidence=require_identity_evidence)
        if lease.verify_active(
                instance, require_identity_evidence=require_identity_evidence) is not True:
            raise ControlError('installation_identity_unknown', 'no signal or credential sent')
        platform_process.check_identity(instance.pid, instance.start, instance.executable)
        return instance

    def _recheck(self, lease, instance, *, allow_unavailable_host=False,
                 require_identity_evidence=True):
        if self._instance(lease, allow_unavailable_host=allow_unavailable_host,
                          require_identity_evidence=require_identity_evidence) != instance:
            raise ControlError('instance_changed', 'no signal or credential sent')

    def _connection(self, lease, instance, *, require_identity_evidence=True):
        family, host, port = _endpoint(instance.host, instance.port)
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.request_timeout)
            sock.connect((host, port))
            platform_process.verify_connected_peer(sock, instance.pid, instance.start, instance.executable)
            self._recheck(lease, instance, require_identity_evidence=require_identity_evidence)
            # Reverify actual accepted connection after the last potentially
            # blocking authority check and immediately before token acquisition.
            platform_process.verify_connected_peer(sock, instance.pid, instance.start, instance.executable, timeout=0)
            return sock
        except BaseException:
            sock.close()
            raise

    @staticmethod
    def _port_free(host, port):
        family, local, port = _endpoint(host, port)
        # For wildcard listeners check the actual bind address, not only loopback.
        try:
            unspecified = ipaddress.ip_address(host).is_unspecified
        except ValueError:
            unspecified = False
        address = ('0.0.0.0' if family == socket.AF_INET else '::') if unspecified else local
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            raise ControlError('port_check_unknown') from None
        try:
            try:
                # Match the server's settings; TIME_WAIT is not an active owner.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0 if host == '::' else 1)
            except OSError:
                raise ControlError('port_check_unknown') from None
            try:
                sock.bind((address, port))
                sock.listen(1)
            except OSError as exc:
                code = 'port_unavailable' if exc.errno == errno.EADDRINUSE else 'port_check_unknown'
                raise ControlError(code) from None
        finally:
            try:
                sock.close()
            except OSError:
                raise ControlError('port_check_unknown') from None

    @staticmethod
    def _same_listen(instance, listen):
        actual, configured = instance.host, listen['host']
        if instance.port != listen['port']:
            return False
        if actual == configured or isinstance(configured, str) and configured.lower() == 'localhost' and actual == '127.0.0.1':
            return True
        try:
            return ipaddress.ip_address(actual) == ipaddress.ip_address(configured)
        except ValueError:
            return False

    @staticmethod
    def _listeners_overlap(instance, listen):
        """Whether the old listener can itself occupy the new bind address."""
        if instance.port != listen['port']:
            return False
        old_family, old_host, _ = _endpoint(instance.host, instance.port)
        new_family, new_host, _ = _endpoint(listen['host'], listen['port'])
        def wildcard(host):
            try:
                return ipaddress.ip_address(host).is_unspecified
            except ValueError:
                return False
        old_wild, new_wild = wildcard(instance.host), wildcard(listen['host'])
        if old_family == new_family:
            return old_wild or new_wild or old_host == new_host
        # The recorded old IPv6 wildcard may have lost its original spelling;
        # conservatively assume dual-stack. The new listener's setting is
        # known: the server uses dual-stack only for the exact spelling "::".
        return (old_family == socket.AF_INET6 and old_wild
                or new_family == socket.AF_INET6 and new_wild and listen['host'] == '::')

    def _configuration(self):
        paths = read_runtime_paths(self.root / 'config' / 'runtime_paths.json', project_root=self.root,
                                   local_appdata=os.environ.get('LOCALAPPDATA'))
        snapshot = inspect_runtime_identities(self.root, paths).as_dict()
        listen = resolve_listen(self._config_source, os.environ.get('MODEL_PROXY_PORT'))
        return {key: snapshot[key].canonical for key in PATH_KEYS}, listen

    def _prepare_initialization(self, lease, *, allow_initialize):
        classify = getattr(lease, 'installation_state', None)
        initialize = getattr(lease, 'initialize_interactively', None)
        if not callable(classify) or not callable(initialize):
            raise ControlError('initialization_authority_unavailable')
        state = classify()
        if state == INSTALL_INITIALIZED and type(state) is str:
            return
        if state == INSTALL_CHANGED and type(state) is str:
            raise ControlError('installation_changed',
                               'installation changed; full reinitialization required')
        if state != INSTALL_UNINITIALIZED_OR_RECORD_LOST and state != INSTALL_IDENTITY_UNVERIFIED \
                or type(state) is not str:
            raise ControlError('installation_state_unknown')
        if not allow_initialize:
            raise ControlError('initialization_after_stop_forbidden',
                               'installation identity changed after stop; no restart')
        try:
            input_fd, output_fd = self.terminal_in.fileno(), self.terminal_out.fileno()
            ins, outs = os.fstat(input_fd), os.fstat(output_fd)
            same_terminal = (os.isatty(input_fd) and os.isatty(output_fd)
                             and stat.S_ISCHR(ins.st_mode) and stat.S_ISCHR(outs.st_mode)
                             and (ins.st_dev, ins.st_rdev) == (outs.st_dev, outs.st_rdev))
        except (AttributeError, OSError, ValueError, TypeError):
            same_terminal = False
        if not same_terminal:
            raise ControlError('initialization_interactive_required',
                'run on interactively after verifying installation and legacy risks')
        if state == INSTALL_IDENTITY_UNVERIFIED:
            rebaseline = getattr(lease, 'rebaseline_interactively', None)
            if not callable(rebaseline):
                raise ControlError('initialization_authority_unavailable')
            try:
                rebound = rebaseline(self.terminal_in, self.terminal_out)
            except (EOFError, KeyboardInterrupt):
                raise ControlError('initialization_interrupted',
                    'initialization outcome unknown; no child started') from None
            if rebound is not True:
                raise ControlError('initialization_unconfirmed',
                                   'initialization not confirmed; no child started')
            if classify() != INSTALL_INITIALIZED:
                raise ControlError('initialization_verification_failed')
            return
        try:
            confirmed = initialize(self.terminal_in, self.terminal_out)
        except (EOFError, KeyboardInterrupt):
            raise ControlError('initialization_interrupted',
                'initialization outcome unknown; no child started') from None
        if confirmed is not True:
            raise ControlError('initialization_unconfirmed', 'initialization not confirmed; no child started')
        # An affirmative response is not proof that a safe record was committed.
        state = classify()
        if type(state) is not str or state != INSTALL_INITIALIZED:
            raise ControlError('initialization_verification_failed')

    def _start(self, lease, *, allow_initialize=True):
        # The service bootstrap requires Python >= 3.10. Never obtain operator
        # attestation or write an initialization record for a doomed child.
        if sys.version_info < (3, 10):
            raise ControlError('python_version_unsupported')
        paths, listen = self._configuration()
        # Must run before interactive initialization can publish records.
        _check_mac_new_listener(listen)
        self._prepare_initialization(lease, allow_initialize=allow_initialize)
        startable = lease.assert_startable() is True
        if not startable:
            # Idempotence is allowed only for this fully verified instance,
            # with the current six paths and configured listener unchanged.
            instance = self._instance(lease)
            if paths != instance.active['runtime_paths'] or not self._same_listen(instance, listen):
                raise ControlError('running_configuration_changed')
            with self._connection(lease, instance):
                pass
            print('model_proxy already running and verified.', file=self.out)
            return
        self._port_free(listen['host'], listen['port'])
        if lease.assert_startable() is not True:
            raise ControlError('start_state_changed')
        # Detect a removed/replaced initialization record before the detached
        # child, whose stdin/stdout/stderr intentionally have no terminal.
        state = lease.installation_state()
        if type(state) is not str or state != INSTALL_INITIALIZED:
            raise ControlError('initialization_verification_failed')
        entry = self.root / 'core' / 'cli' / 'model_proxy.py'
        if not entry.is_file() or entry.is_symlink():
            raise ControlError('service_entry_unavailable')
        for attempt in range(2):
            nonce = uuid.uuid4().hex
            env = os.environ.copy()
            env['MODEL_PROXY_START_NONCE'] = nonce
            env['MODEL_PROXY_CONFIG'] = self._config_source
            try:
                child = subprocess.Popen([sys.executable, str(entry)], cwd=os.getcwd(), env=env,
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
            except OSError:
                raise ControlError('spawn_failed') from None
            watch_start = getattr(lease, 'watch_start', None)
            if callable(watch_start):
                watch_start(child.pid, nonce)
            deadline = time.monotonic() + self.startup_timeout
            final_observation = False
            while True:
                if time.monotonic() >= deadline:
                    if final_observation:
                        raise ControlError('startup_unconfirmed',
                                           'child may still be running; no signal sent')
                    # A slow read can return the previous generation after the
                    # child became READY. One final fresh observation only.
                    final_observation = True
                returncode = child.poll()
                if returncode is not None:
                    retry = getattr(lease, 'retry_exited_child', None)
                    if (attempt == 0 and returncode == 1 and callable(retry)
                            and retry(child.pid, nonce, returncode) is True):
                        current_paths, current_listen = self._configuration()
                        if current_paths != paths or current_listen != listen:
                            raise ControlError('start_state_changed')
                        if lease.installation_state() != INSTALL_INITIALIZED:
                            raise ControlError('initialization_verification_failed')
                        self._port_free(listen['host'], listen['port'])
                        time.sleep(0.05)
                        if retry(child.pid, nonce, returncode) is not True:
                            raise ControlError('start_state_changed')
                        print('Warning: startup child exited; safe state rechecked, retrying once.',
                              file=self.err)
                        break
                    raise ControlError('startup_failed', 'child exited')
                try:
                    active, good = lease.records()
                    if isinstance(active, dict) and active.get('pid') == child.pid and active.get('state') == 'READY':
                        instance = _valid_record(self.root, active, good)
                        if (instance.nonce != nonce or not self._same_listen(instance, listen)
                                or instance.active['runtime_paths'] != paths):
                            raise ControlError('startup_identity_changed', 'child may still be running')
                        if lease.verify_active(instance) is not True:
                            raise ControlError('startup_identity_unknown', 'child may still be running')
                        platform_process.check_identity(instance.pid, instance.start, instance.executable)
                        with self._connection(lease, instance):
                            pass
                        print('model_proxy started and verified.', file=self.out)
                        return
                except StartupPending:
                    pass
                except PreflightError as exc:
                    retry = getattr(lease, 'retry_startup_observation', None)
                    if not callable(retry) or retry(exc) is not True:
                        raise
                if final_observation:
                    raise ControlError('startup_unconfirmed',
                                       'child may still be running; no signal sent')
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))

    def _stop(self, lease, *, allow_stale=False, require_identity_evidence=True):
        """Stop one recorded instance.

        ``require_identity_evidence`` is False only for plain off. Restart and
        every other caller keep the strict installation/lock identity gate on
        the initial read, both pre-signal rechecks and the release proof. The
        original lock objects are recorded before any signal and only those
        objects may be reported released.
        """
        stopping = getattr(lease, 'stopping', None)
        scope = stopping() if callable(stopping) else contextlib.nullcontext()
        with scope:
            if allow_stale:
                active, good = lease.records()
                instance = _valid_record(self.root, active, good, allow_unavailable_host=True,
                                         require_identity_evidence=require_identity_evidence)
                if platform_process.process_gone(instance.pid, instance.start, instance.executable):
                    self._capture_stop_lock_evidence(lease, instance)
                    if lease.assert_stopped(
                            instance,
                            require_identity_evidence=require_identity_evidence) is not True:
                        raise ControlError('release_unverified', 'process exited but lock release is unknown')
                    self._port_free(instance.host, instance.port)
                    print('model_proxy already stopped; locks and port verified free.', file=self.out)
                    return
            instance = self._instance(lease, allow_unavailable_host=True,
                                      require_identity_evidence=require_identity_evidence)
            # User-approved recovery: the protected record and actual process
            # entry are rechecked; plain off does not depend on live config,
            # a socket, or the installation identity. Process, lock and port
            # liveness are still verified. Restart keeps the full identity
            # gate here and in the release proof. There is a non-atomic
            # check-to-signal window.
            self._recheck(lease, instance, allow_unavailable_host=True,
                          require_identity_evidence=require_identity_evidence)
            self._recheck(lease, instance, allow_unavailable_host=True,
                          require_identity_evidence=require_identity_evidence)
            # Record the exact lock objects immediately before the signal.
            # assert_stopped reports release only for these objects, never for
            # replacement files that happen to occupy the same paths.
            self._capture_stop_lock_evidence(lease, instance)
            if not require_identity_evidence:
                self._report_stop_port_owner(instance)
            platform_process.signal_verified(instance.pid, instance.start, instance.executable)
            deadline = time.monotonic() + self.stop_timeout
            while not platform_process.process_gone(instance.pid, instance.start, instance.executable):
                if time.monotonic() >= deadline:
                    raise ControlError('stop_timeout', 'SIGTERM sent; exit unconfirmed, no further signal sent')
                time.sleep(0.05)
            if lease.assert_stopped(
                    instance,
                    require_identity_evidence=require_identity_evidence) is not True:
                raise ControlError('release_unverified', 'process exited but lock release is unknown')
            try:
                self._port_free(instance.host, instance.port)
            except ControlError as exc:
                if exc.code == 'port_unavailable':
                    raise ControlError('port_unavailable',
                        'process exited; locks released; port occupied') from None
                raise ControlError('port_check_unknown',
                    'process exited; locks released; port release unknown') from None
            except (platform_process.ProcessEvidenceError, OSError):
                raise ControlError('port_check_unknown',
                    'process exited; locks released; port release unknown') from None
            print('model_proxy stopped; locks and port verified free.', file=self.out)

    @staticmethod
    def _capture_stop_lock_evidence(lease, instance):
        capture = getattr(lease, 'stop_lock_evidence', None)
        if not callable(capture):
            raise ControlError('lock_evidence_unavailable', 'no signal or credential sent')
        evidence = capture(instance)
        # The production lease validates and consumes its own evidence inside
        # assert_stopped; only an explicitly absent capture is refused here.
        if evidence is None:
            raise ControlError('lock_evidence_unavailable', 'no signal or credential sent')
        return evidence

    def _report_stop_port_owner(self, instance):
        """Best-effort pre-signal listener ownership for plain off.

        The recorded PID, its start/image and both fixed lock conflicts were
        already verified. Port ownership is additional operator information:
        an unverifiable check warns and continues (user decision), while a
        confirmed other holder is reported and the existing post-stop port
        safety boundary still applies. No signal, credential or connection is
        affected by this probe.
        """
        host, port = instance.host, instance.port
        label = f'{host}:{port}'
        try:
            family, address, _ = _endpoint(host, port)
            owned = platform_process.verify_listening_owner(
                instance.pid, instance.start, instance.executable, family, address, port)
        except (ControlError, platform_process.ProcessEvidenceError, OSError):
            print(f'Warning: off: listening port {label} ownership could not be verified; '
                  'stopping the recorded process anyway.', file=self.err)
            return
        if owned:
            return
        try:
            self._port_free(host, port)
        except ControlError as exc:
            if exc.code == 'port_unavailable':
                print(f'Warning: off: listening port {label} is not held by recorded PID '
                      f'{instance.pid} and is occupied by another process; stopping the recorded '
                      'process; the post-stop port check still applies.', file=self.err)
            else:
                print(f'Warning: off: listening port {label} is not held by recorded PID '
                      f'{instance.pid}; whether another process holds it could not be verified; '
                      'stopping the recorded process anyway.', file=self.err)
        except (platform_process.ProcessEvidenceError, OSError):
            print(f'Warning: off: listening port {label} is not held by recorded PID '
                  f'{instance.pid}; whether another process holds it could not be verified; '
                  'stopping the recorded process anyway.', file=self.err)
        else:
            print(f'Warning: off: recorded PID {instance.pid} has no listener on {label} '
                  '(port currently unbound); stopping the recorded process.', file=self.err)

    def on(self):
        return self._run('on', self._start)

    def off(self):
        # Only plain off may stop without installation/lock identity evidence.
        return self._run('off', lambda lease: self._stop(
            lease, allow_stale=True, require_identity_evidence=False))

    def restart(self):
        def restart_locked(lease):
            check = getattr(lease, 'assert_restart_supported', None)
            if not callable(check):
                raise ControlError('restart_preflight_unavailable', 'no signal or credential sent')
            if check() is not True:
                raise ControlError('configuration_migration_required',
                                   'runtime paths changed; migration required; no restart attempted')
            never_started = getattr(lease, 'assert_never_started', None)
            if not callable(never_started) or never_started() is not True:
                # Restart reuses the stop path but never the plain-off
                # relaxation: the strict identity gate applies throughout.
                self._stop(lease, allow_stale=True)
            self._start(lease, allow_initialize=False)
        return self._run('restart', restart_locked)

    @staticmethod
    def _require_verified_identity(verdict):
        """reload is a live operation and must match this directory.

        A missing verdict (no records) is not a verified identity either, so it
        fails closed here. The production observation raises
        ``installation_changed`` from ``installation_identity()`` before
        returning a changed verdict, so only the cross-boot legacy verdict
        reaches this whitelist as a distinguishable rejection.
        """
        if verdict in (IDENTITY_SAME, IDENTITY_LEGACY_SAME_BOOT):
            return
        raise ControlError('installation_identity_unverified',
                           'installation identity unverified; run on interactively')

    def _request(self, lease, command):
        # Installation identity is judged only by startup paths and the
        # credential-bearing request below. status is read-only: it locates the
        # recorded instance structurally and proves it live (process, connected
        # peer, lock liveness) without collecting a volume UUID or comparing a
        # recorded inode, so an identity anomaly can never hide the service.
        status = command == 'status'
        if not status:
            identify = getattr(lease, 'installation_identity', None)
            self._require_verified_identity(identify() if callable(identify) else None)
        instance = self._instance(lease, require_identity_evidence=not status)
        with self._connection(lease, instance,
                              require_identity_evidence=not status) as sock:
            token = lease.admin_token()
            if (not isinstance(token, str) or not token or len(token) > 4096
                    or any(ord(ch) < 33 or ord(ch) > 126 for ch in token)):
                raise ControlError('admin_token_unavailable', 'no credential sent')
            family, host, port = _endpoint(instance.host, instance.port)
            # Host is a protocol header, not the socket destination.  The
            # server always accepts loopback Host values even when a specific
            # local LAN address is bound; the connected peer is checked below.
            host_header = f'[::1]:{port}' if family == socket.AF_INET6 else f'127.0.0.1:{port}'
            method = 'GET' if command == 'status' else 'POST'
            request = (f'{method} /model_proxy/{command} HTTP/1.1\r\nHost: {host_header}\r\n'
                       f'X-Proxy-Admin-Token: {token}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n').encode('ascii')
            self._recheck(lease, instance, require_identity_evidence=not status)
            platform_process.verify_connected_peer(sock, instance.pid, instance.start, instance.executable, timeout=0)
            # exec of the same Python image can retain PID/start/image and a
            # socket. Recheck the actual service entry/config *after* the final
            # peer check, immediately before sending. This is not atomic.
            self._recheck(lease, instance, require_identity_evidence=not status)
            # Bind the token to the original service and CLI chain snapshots,
            # rather than accepting a fresh, possibly different token snapshot.
            if lease.verify_admin_token() is not True:
                raise ControlError('admin_token_unavailable', 'no credential sent')
            try:
                # sendall may have written a prefix before raising. Do not retry.
                sock.sendall(request)
                response = http.client.HTTPResponse(sock)
                response.begin()
                body = response.read(2 * 1024 * 1024 + 1)
                if len(body) > 2 * 1024 * 1024 or response.length not in (None, 0):
                    raise ValueError('response too large or incomplete')
                data = json.loads(body.decode('utf-8'))
                if not isinstance(data, dict):
                    raise ValueError('invalid response')
            except (OSError, http.client.HTTPException, UnicodeError, ValueError, RecursionError):
                raise ControlError('response_unknown', 'request may have been sent; no retry') from None
            if response.status in (401, 403, 503):
                raise ControlError('authentication_or_admin_unavailable', 'handler not confirmed')
            if command == 'reload':
                if response.status == 200 and data.get('ok') is True:
                    print('Reload succeeded; configuration loaded and cooldown cleared.', file=self.out)
                    return
                loaded, cleared = data.get('loaded'), data.get('cooldown_cleared')
                def state(value):
                    return 'yes' if value is True else 'no' if value is False else 'unknown'
                # 只回显固定枚举错误码，绝不回显服务端原始文本（可能含路径/凭据）。
                error = data.get('error') or data.get('load_error')
                code = error if isinstance(error, str) and re.fullmatch(r'[a-z_]{1,40}', error) else ''
                detail = f'loaded={state(loaded)}, cooldown_cleared={state(cleared)}'
                if code:
                    detail += f', error={code}'
                raise ControlError('reload_failed', detail)
            if response.status != 200:
                raise ControlError('status_failed')
            # Redact before JSON escaping, including token-bearing keys.
            def redact(value):
                if isinstance(value, str):
                    return value.replace(token, '[REDACTED]')
                if isinstance(value, list):
                    return [redact(item) for item in value]
                if isinstance(value, dict):
                    return {redact(key): redact(item) for key, item in value.items()}
                return value
            try:
                from core.cli._format_ops import _format_status_from_json, _format_status_header
                paths = instance.active['runtime_paths']
                lines = _format_status_from_json(redact(data), str(self.config_path),
                                                 paths['totals'], paths['log'])
                started = instance.start.get('seconds') if instance.start.get('kind') == 'darwin_proc_bsdinfo' else None
                header = _format_status_header(instance.port, instance.pid, started)
                rendered = [header, *lines]
                if any(not isinstance(line, str) for line in rendered):
                    raise ValueError('invalid status formatting')
            except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
                raise ControlError('status_failed') from None
            for line in rendered:
                print(line.replace(token, '[REDACTED]'), file=self.out)

    def status(self):
        return self._run('status', lambda lease: self._request(lease, 'status'))

    def reload(self):
        return self._run('reload', lambda lease: self._request(lease, 'reload'))
