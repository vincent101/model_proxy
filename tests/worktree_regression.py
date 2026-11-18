"""Local-only lifecycle and narrow Anthropic mock regression (never legacy CLI)."""
import argparse
import hashlib
import http.client
import json
import os
import pty
import re
import select
import shutil
import socket
import stat
import subprocess
import sys
import threading
import termios
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from isolation_bootstrap import (DEFAULT_PATHS, Scratch, attestation_for_test, fake_config,
                                 resolved_default_paths)

CLIENT = 'fake-client-for-test-only'
UPSTREAM = 'fake-upstream-for-test-only'
FAKE_SOURCE = 'isolated temporary test project'
SOURCE_PROMPT = b'Installation/recovery source: '
YES_PROMPT = b'Type YES to confirm: '

A2R_CLIENT = 'fake-client-a2r'
R2A_CLIENT = 'fake-client-r2a'
A2R_INPUT, A2R_SYSTEM, A2R_OUTPUT = 'a2r-input-sentinel', 'a2r-system-sentinel', 'a2r-output-sentinel'
R2A_INPUT, R2A_SYSTEM, R2A_OUTPUT = 'r2a-input-sentinel', 'r2a-system-sentinel', 'r2a-output-sentinel'


def validate_mock_request(path, headers, body):
    """Reject wrong protocol, token, target, user text, instructions, or shape."""
    key = headers.get('x-api-key')
    cases = {UPSTREAM: 'a2a', 'fake-upstream-a2r': 'a2r', 'fake-upstream-r2a': 'r2a'}
    case = cases.get(key)
    if (not case or headers.get('Authorization') != 'Bearer ' + str(key)
            or headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json'
            or not isinstance(body, dict)):
        raise ValueError('invalid fake upstream credential or JSON')
    if not isinstance(body.get('stream'), bool):
        raise ValueError('stream flag missing or not boolean')
    if case == 'a2r':
        items = body.get('input')
        if (path != '/v1/responses' or body.get('model') != 'fixture-model-a2r'
                or body.get('instructions') != A2R_SYSTEM or not isinstance(items, list)
                or len(items) != 1 or not isinstance(items[0], dict) or items[0].get('role') != 'user'
                or items[0].get('type') != 'message'
                or items[0].get('content') != [{'type': 'input_text', 'text': A2R_INPUT}]):
            raise ValueError('invalid a2r Responses request')
    elif case == 'r2a':
        messages = body.get('messages')
        if (path != '/v1/messages' or body.get('model') != 'fixture-model-r2a'
                or body.get('system') != R2A_SYSTEM or messages != [{'role': 'user', 'content': R2A_INPUT}]):
            raise ValueError('invalid r2a Anthropic request')
    elif (path != '/v1/messages' or body.get('model') not in ('fixture-model', 'fixture-model-reloaded')
          or body.get('messages') != [{'role': 'user', 'content': 'hello'}]):
        raise ValueError('invalid a2a passthrough request')
    return case


def sse(event, data):
    return ('event: ' + event + '\ndata: ' + json.dumps(data, separators=(',', ':')) + '\n\n').encode()


def upstream_response(protocol, text, model):
    if protocol == 'responses':
        return {'id': 'resp_mock', 'object': 'response', 'status': 'completed', 'model': model,
                'output': [{'id': 'msg_mock', 'type': 'message', 'status': 'completed',
                            'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}],
                'usage': {'input_tokens': 2, 'output_tokens': 2}}
    return {'id': 'msg_mock', 'type': 'message', 'role': 'assistant', 'model': model,
            'content': [{'type': 'text', 'text': text}], 'stop_reason': 'end_turn',
            'stop_sequence': None, 'usage': {'input_tokens': 2, 'output_tokens': 2}}


def upstream_sse(protocol, text, model):
    """A complete minimal text stream, split immediately after the text delta."""
    response = upstream_response(protocol, text, model)
    if protocol == 'responses':
        first = [sse('response.created', {'type': 'response.created',
                 'response': {'id': 'resp_mock', 'status': 'in_progress', 'model': model}}),
                 sse('response.output_item.added', {'type': 'response.output_item.added', 'output_index': 0,
                     'item': {'id': 'msg_mock', 'type': 'message', 'status': 'in_progress',
                              'role': 'assistant', 'content': []}}),
                 sse('response.content_part.added', {'type': 'response.content_part.added', 'item_id': 'msg_mock',
                     'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': ''}}),
                 sse('response.output_text.delta', {'type': 'response.output_text.delta', 'item_id': 'msg_mock',
                     'output_index': 0, 'content_index': 0, 'delta': text})]
        tail = [sse('response.output_text.done', {'type': 'response.output_text.done', 'item_id': 'msg_mock', 'text': text}),
                sse('response.content_part.done', {'type': 'response.content_part.done', 'item_id': 'msg_mock',
                    'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': text}}),
                sse('response.output_item.done', {'type': 'response.output_item.done', 'output_index': 0,
                    'item': response['output'][0]}),
                sse('response.completed', {'type': 'response.completed', 'response': response})]
    else:
        first = [sse('message_start', {'type': 'message_start', 'message': {
                    'id': 'msg_mock', 'type': 'message', 'role': 'assistant', 'model': model,
                    'content': [], 'stop_reason': None, 'usage': {'input_tokens': 2, 'output_tokens': 0}}}),
                 sse('content_block_start', {'type': 'content_block_start', 'index': 0,
                     'content_block': {'type': 'text', 'text': ''}}),
                 sse('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                     'delta': {'type': 'text_delta', 'text': text}})]
        tail = [sse('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
                sse('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn',
                    'stop_sequence': None}, 'usage': {'output_tokens': 2}}),
                sse('message_stop', {'type': 'message_stop'})]
    return b''.join(first), b''.join(tail)


class Mock(BaseHTTPRequestHandler):
    calls = []
    gate = threading.Event()
    timed_out = False
    def log_message(self, *_):
        pass
    def do_POST(self):
        try:
            data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            case = validate_mock_request(self.path, self.headers, data)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            self.calls.append({'case': None, 'error': str(exc)})
            self.send_error(400); return
        self.calls.append({'case': case, 'stream': data['stream'], 'model': data['model']})
        protocol = 'responses' if case == 'a2r' else 'anthropic'
        text = {'a2a': 'fixture reply', 'a2r': A2R_OUTPUT, 'r2a': R2A_OUTPUT}[case]
        if data['stream']:
            first, tail = upstream_sse(protocol, text, data['model'])
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(first) + len(tail))); self.end_headers()
            self.wfile.write(first); self.wfile.flush()
            if not self.gate.wait(8):
                type(self).timed_out = True
                return
            self.wfile.write(tail); self.wfile.flush()
        else:
            content = json.dumps(upstream_response(protocol, text, data['model'])).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(content))); self.end_headers()
            self.wfile.write(content); self.wfile.flush()


def reserve(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('127.0.0.1', port)); sock.listen(1)
    except OSError:
        sock.close()
        raise RuntimeError('port occupied or unavailable: %d' % port)
    return sock


def owns_listener(pid, port):
    lsof = shutil.which('lsof')
    if not lsof:
        raise RuntimeError('lsof required to verify listener ownership')
    result = subprocess.run([lsof, '-nP', '-a', '-p', str(pid),
                             '-iTCP@127.0.0.1:%d' % port, '-sTCP:LISTEN', '-t'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
    return result.returncode == 0 and str(pid) in result.stdout.splitlines()


def require_managed_listener(command, pid, port):
    """Require the same worker-owned, live child and its loopback listener."""
    live = command('__probe__', quiet=True)['children']
    if pid not in live:
        raise RuntimeError('managed service child exited before the operation')
    if not owns_listener(pid, port):
        raise RuntimeError('managed service child no longer owns the listener')


def read_local_json(path):
    """Read only a single-link regular file, without following a final symlink."""
    if not hasattr(os, 'O_NOFOLLOW'):
        raise RuntimeError('safe test file open unavailable')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError('test file must be a single-link regular file')
        with os.fdopen(fd, 'r', encoding='utf-8', closefd=False) as stream:
            return json.load(stream)
    finally:
        os.close(fd)


def write_local_json(path, value):
    """Update an existing test file only after validating its opened identity."""
    if not hasattr(os, 'O_NOFOLLOW'):
        raise RuntimeError('safe test file open unavailable')
    fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError('test file must be a single-link regular file')
        data = json.dumps(value).encode('utf-8')
        os.ftruncate(fd, 0)
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(data)
    finally:
        os.close(fd)


def validate_fresh_project(scratch, proxy_port, mock_port, *, preparing=False):
    """Only this newly-created fake project may receive the scripted answers."""
    root, project = scratch.root, scratch.project
    info = root.lstat()
    state = project / '.runtime'
    if (proxy_port == mock_port or 18889 in (proxy_port, mock_port)
            or root.is_symlink() or (info.st_dev, info.st_ino) != scratch.identity
            or root.parent != Path('/tmp').resolve() or project.is_symlink() or project.parent != root
            or not root.name.startswith('mp-regression-')):
        raise RuntimeError('initialization target is not a fresh test project')
    for directory in (project, project / 'config', state, root / 'home',
                      root / 'tmp', root / 'guard', scratch.venv):
        if directory.is_symlink() or not directory.is_dir() or not directory.is_relative_to(root):
            raise RuntimeError('isolated directory is unavailable or symlinked')
    # Before initialization the directory is empty; at the prompt the CLI may
    # already hold its test-only control, anchor and instance locks. No records or ledger may preexist.
    allowed = {'control.lock', 'anchor.lock', 'model_proxy.lock'} if preparing else set()
    for child in state.iterdir():
        if (child.name not in allowed or child.is_symlink() or not child.is_file()
                or child.stat().st_nlink != 1):
            raise RuntimeError('initialization target is not uninitialized')
    config_path = project / 'config/model_proxy_config.json'
    paths_path = project / 'config/runtime_paths.json'
    if (config_path.is_symlink() or paths_path.is_symlink()
            or config_path.stat().st_mode & 0o077):
        raise RuntimeError('test configuration must not be symlinked')
    config = read_local_json(config_path)
    paths = read_local_json(paths_path)
    if paths != DEFAULT_PATHS or config != fake_config(proxy_port, mock_port):
        raise RuntimeError('test configuration is not the generated fake-only configuration')


class TTYWorkerConstructionError(RuntimeError):
    """A failed construction still owns a direct child that needs cleanup."""
    def __init__(self, worker, reason, descriptor_errors=()):
        detail = 'worker construction failed: ' + str(reason)
        if descriptor_errors:
            detail += '; descriptor close failures: ' + ', '.join(str(error) for error in descriptor_errors)
        super().__init__(detail)
        self.worker = worker
        self.reason = reason


class TTYWorker:
    """Private PTY for CLI interaction; separate pipes carry worker protocol."""
    def __init__(self, scratch, entry, proxy_port, mock_port, *, fixture=True):
        self.attestation = attestation_for_test(proxy_port, mock_port, None if fixture else scratch.root.name)
        self.fixture = fixture
        self.scratch, self.proxy_port, self.mock_port = scratch, proxy_port, mock_port
        self.pending = b''
        self.prompts = 0
        self.process = None
        self.master = self.request_out = self.response_in = None
        child_fds = []
        start_error = None
        try:
            self.master, slave = pty.openpty()
            child_fds.append(slave)
            request_in, self.request_out = os.pipe()
            child_fds.append(request_in)
            self.response_in, response_out = os.pipe()
            child_fds.append(response_out)
            settings = termios.tcgetattr(slave)
            settings[3] &= ~termios.ECHO
            termios.tcsetattr(slave, termios.TCSANOW, settings)
            self.process = subprocess.Popen(
                [str(scratch.python), '-S', '-B', str(scratch.root / 'guard/worker.py'),
                 str(entry), str(request_in), str(response_out)],
                stdin=slave, stdout=slave, stderr=slave, pass_fds=(request_in, response_out),
                start_new_session=True, env=scratch.env(proxy_port, mock_port),
                cwd=str(scratch.project))
        except BaseException as exc:
            start_error = exc
        close_errors = []
        # Attempt all child-side closes; never retry a failed fd because an OS
        # may have closed it before reporting an error and reused its number.
        for fd in child_fds:
            try:
                os.close(fd)
            except BaseException as exc:
                close_errors.append(exc)
        if start_error is not None or close_errors:
            reason = start_error if start_error is not None else close_errors[0]
            if self.process is not None:
                # Transfer the still-owned Popen handle to run() even though
                # __init__ cannot return normally. No PID scanning or group kill.
                # Do not print here: an output failure must not lose the handle.
                raise TTYWorkerConstructionError(self, reason, close_errors) from reason
            for error in close_errors:
                print('ERROR: worker construction descriptor close failed: ' + str(error), file=sys.stderr)
            # No child exists. Close every successfully allocated parent fd.
            for fd in (self.request_out, self.response_in, self.master):
                if fd is not None:
                    try:
                        os.close(fd)
                    except BaseException as exc:
                        print('ERROR: worker construction descriptor close failed: ' + str(exc), file=sys.stderr)
            raise reason

    def command(self, name, quiet=False):
        os.write(self.request_out, json.dumps(name).encode() + b'\n')
        transcript = bytearray()
        deadline = time.monotonic() + (45 if name == 'on' else 20)
        prompt_step = 0
        response = None
        while response is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('CLI timed out: ' + name)
            ready, _, _ = select.select([self.master, self.response_in], [], [], remaining)
            if not ready:
                raise RuntimeError('CLI timed out: ' + name)
            if self.master in ready:
                data = os.read(self.master, 65536)
                if not data:
                    raise RuntimeError('test PTY closed')
                transcript.extend(data)
                if len(transcript) > 262144:
                    raise RuntimeError('CLI output exceeded test limit')
                for marker in (SOURCE_PROMPT, YES_PROMPT):
                    if transcript.count(marker) > 1:
                        raise RuntimeError('repeated initialization prompt')
                if SOURCE_PROMPT in transcript and prompt_step == 0:
                    expected = self.attestation + b'\r\n' + SOURCE_PROMPT
                    if name != 'on' or self.prompts or transcript != expected:
                        raise RuntimeError('unexpected initialization prompt')
                    validate_fresh_project(self.scratch, self.proxy_port, self.mock_port, preparing=True)
                    if not self.fixture:
                        self.scratch.assert_authority_unchanged()
                    os.write(self.master, FAKE_SOURCE.encode() + b'\n')
                    prompt_step = 1
                if YES_PROMPT in transcript and prompt_step == 1:
                    expected = (self.attestation + b'\r\n'
                                + SOURCE_PROMPT + YES_PROMPT)
                    if name != 'on' or transcript != expected:
                        raise RuntimeError('unexpected confirmation prompt')
                    validate_fresh_project(self.scratch, self.proxy_port, self.mock_port, preparing=True)
                    if not self.fixture:
                        self.scratch.assert_authority_unchanged()
                    os.write(self.master, b'YES\n')
                    prompt_step = 2
                elif YES_PROMPT in transcript and prompt_step == 0:
                    raise RuntimeError('confirmation prompt without fake source prompt')
            if self.response_in in ready:
                data = os.read(self.response_in, 65536)
                if not data:
                    raise RuntimeError('CLI worker exited; output: ' + transcript.decode(errors='replace')[-4000:])
                self.pending += data
                if b'\n' in self.pending:
                    line, self.pending = self.pending.split(b'\n', 1)
                    response = json.loads(line)
        # Worker flushes its terminal before sending its response.
        while select.select([self.master], [], [], 0)[0]:
            transcript.extend(os.read(self.master, 65536))
            if len(transcript) > 262144:
                raise RuntimeError('CLI output exceeded test limit')
        # Data can arrive just before the control reply but be drained only
        # afterward. Never answer after the CLI command has already returned.
        if any(transcript.count(marker) > 1 for marker in (SOURCE_PROMPT, YES_PROMPT)):
            raise RuntimeError('repeated initialization prompt')
        if name != 'on' and (SOURCE_PROMPT in transcript or YES_PROMPT in transcript):
            raise RuntimeError('unexpected initialization prompt')
        if YES_PROMPT in transcript and prompt_step == 0:
            raise RuntimeError('confirmation prompt without fake source prompt')
        if SOURCE_PROMPT in transcript and prompt_step == 0:
            raise RuntimeError('unexpected initialization prompt')
        output = transcript.decode('utf-8', errors='replace').replace('\r\n', '\n')
        if response['code'] != 0:
            raise RuntimeError('CLI %s failed: %s' % (name, output[-8000:]))
        if name == 'on':
            if prompt_step != 2:
                raise RuntimeError('first initialization did not complete both PTY prompts')
            self.prompts += 1
        response['output'] = output
        if not quiet:
            print('PASS cli ' + name, flush=True)
        return response

    def close(self):
        close_error = None
        try:
            if self.process.poll() is None:
                try:
                    os.write(self.request_out, b'"quit"\n')
                    self.process.wait(timeout=8)
                except (OSError, subprocess.TimeoutExpired):
                    if self.process.poll() is None:
                        self.process.terminate()
                        try:
                            self.process.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            pass
        except Exception as exc:
            close_error = exc
        for fd in (self.request_out, self.response_in, self.master):
            if fd is not None:
                try:
                    os.close(fd)
                except Exception as exc:
                    if close_error is None:
                        close_error = exc
                    else:
                        print('ERROR: worker descriptor close failed: ' + str(exc), file=sys.stderr)
        if close_error is not None:
            raise close_error


def _verify_identity(data, pid, port, project, *, complete=False):
    if not isinstance(data, dict):
        raise RuntimeError('status identity is not an object')
    present = set(data)
    if complete and not {'pid', 'project_root', 'listen'} <= present:
        raise RuntimeError('instance observation lacks identity fields')
    if 'pid' in data and (type(data['pid']) is not int or data['pid'] != pid):
        raise RuntimeError('status instance PID mismatch')
    if 'project_root' in data and data['project_root'] != str(project):
        raise RuntimeError('status project root mismatch')
    if 'listen' in data:
        listen = data['listen']
        if (not isinstance(listen, dict) or listen.get('host') != '127.0.0.1'
                or type(listen.get('port')) is not int or listen['port'] != port):
            raise RuntimeError('status listen address mismatch')
    if 'port' in data and (type(data['port']) is not int or data['port'] != port):
        raise RuntimeError('status port mismatch')
    if 'host' in data and data['host'] != '127.0.0.1':
        raise RuntimeError('status host mismatch')
    if 'runtime_paths' in data:
        expected = resolved_default_paths(project)
        if data['runtime_paths'] != expected:
            raise RuntimeError('status runtime paths mismatch')
    return 'pid' in data or {'project_root', 'listen'} <= present


def verify_status(output, pid, port, project):
    """Cross-check CLI status against the live child's test installation records."""
    try:
        status = json.loads(output)
    except (ValueError, TypeError):
        lines = output.strip().splitlines() if isinstance(output, str) else []
        header = re.fullmatch(r'service running on port ([0-9]+)\s+pid ([0-9]+)(?:\s+up [^\r\n]+)?',
                              lines[0].strip()) if lines else None
        contradictory = any(re.match(r'(?i)^\s*(?:service|model_proxy)\s+'
                                     r'(?:not running|stopped|offline|unknown|failed)\b', line)
                            for line in lines[1:])
        config_present = any(line.startswith('config ') for line in lines[1:])
        health_present = any(line.startswith('health:') for line in lines[1:])
        error_present = any(re.match(r'(?i)^\s*(?:error|fatal)\s*:', line) for line in lines[1:])
        if (not header or contradictory or not config_present or not health_present or error_present
                or int(header.group(1)) != port or int(header.group(2)) != pid):
            raise RuntimeError('status text is unsupported or reports a different instance')
        status = {'state': 'running', 'pid': int(header.group(2)), 'port': int(header.group(1))}
    if not isinstance(status, dict):
        raise RuntimeError('status content is not an object')
    states = [status[key] for key in ('state', 'status') if key in status]
    if (not states or any(not isinstance(state, str)
                          or state.lower() not in ('running', 'ready') for state in states)):
        raise RuntimeError('status does not report a running instance')
    _verify_identity(status, pid, port, project)

    state_dir = project / '.runtime'
    record_path = state_dir / 'active.json'
    install_path = state_dir / 'install.json'
    last_path = state_dir / 'last_good_paths.json'
    if state_dir.is_symlink() or any(path.is_symlink() for path in (record_path, install_path, last_path)):
        raise RuntimeError('instance observation must not be symlinked')
    try:
        observation = read_local_json(record_path)
        installation = read_local_json(install_path)
        last = read_local_json(last_path)
    except (OSError, ValueError) as exc:
        raise RuntimeError('test installation or active record unavailable or invalid') from exc
    if (not isinstance(observation, dict) or observation.get('record') != 'active'
            or observation.get('state') != 'READY' or 'runtime_paths' not in observation):
        raise RuntimeError('instance observation is not an active ready record')
    _verify_identity(observation, pid, port, project, complete=True)
    if (not isinstance(last, dict) or last.get('record') != 'last_good_paths'
            or last.get('project_root') != str(project)
            or last.get('listen') != observation.get('listen')
            or last.get('runtime_paths') != observation.get('runtime_paths')
            or last.get('install_id') != observation.get('install_id')
            or last.get('instance_nonce') != observation.get('instance_nonce')):
        raise RuntimeError('last-good record does not match the active instance')
    if (not isinstance(installation, dict) or installation.get('record') != 'install'
            or installation.get('project_root') != str(project)
            or installation.get('initialization_source') != FAKE_SOURCE
            or installation.get('ever_started') is not True
            or not isinstance(installation.get('install_id'), str)
            or not installation['install_id']
            or installation['install_id'] != observation.get('install_id')
            or not isinstance(observation.get('instance_nonce'), str)
            or not observation['instance_nonce']
            or installation.get('last_instance_nonce') != observation['instance_nonce']):
        raise RuntimeError('active record does not match the confirmed fake installation')
    for key in ('instance_nonce', 'process_start', 'executable'):
        if key in status and key in observation and status[key] != observation[key]:
            raise RuntimeError('status instance %s mismatch' % key)


def stop_managed(command, pid, port):
    # Do not call off if the original service is already dead or lost the port.
    require_managed_listener(command, pid, port)
    return command('off', quiet=True)


def read_sse_event(response):
    """Read one SSE frame without assuming chunk or TCP boundaries."""
    event, data = '', []
    while True:
        line = response.readline(65537)
        if len(line) > 65536:
            raise RuntimeError('SSE line exceeded limit')
        if not line:
            if data:
                raise RuntimeError('SSE ended in a partial frame')
            return None
        line = line.decode('utf-8').rstrip('\r\n')
        if not line:
            if not data:
                event = ''
                continue
            payload = '\n'.join(data)
            if payload == '[DONE]':
                event, data = '', []
                continue
            obj = json.loads(payload)
            if not isinstance(obj, dict):
                raise RuntimeError('SSE data must be a JSON object')
            kind = event or obj.get('type')
            if not kind or obj.get('type', kind) != kind:
                raise RuntimeError('SSE event/type mismatch')
            return kind, obj
        if line.startswith('event:'):
            event = line[6:].strip()
        elif line.startswith('data:'):
            data.append(line[5:].lstrip())


def verify_json_response(payload, api, expected):
    if not isinstance(payload, dict):
        raise RuntimeError('response is not a JSON object')
    if api == 'anthropic':
        if (payload.get('type') != 'message' or payload.get('role') != 'assistant'
                or payload.get('stop_reason') != 'end_turn'):
            raise RuntimeError('bad Anthropic response or stop reason')
        content = payload.get('content') or []
        texts = [x.get('text') for x in content if isinstance(x, dict) and x.get('type') == 'text']
    else:
        if payload.get('object') != 'response' or payload.get('status') != 'completed':
            raise RuntimeError('bad Responses object or terminal status')
        texts = [part.get('text') for item in payload.get('output', []) if isinstance(item, dict)
                 and item.get('type') == 'message' and item.get('role') == 'assistant'
                 for part in item.get('content', []) if isinstance(part, dict) and part.get('type') == 'output_text']
    if texts != [expected]:
        raise RuntimeError('response text mismatch')


def verify_stream_events(events, api, expected):
    if not events or any(kind in ('error', 'response.failed', 'response.incomplete') for kind, _ in events):
        raise RuntimeError('empty or failed SSE stream')
    terminal = 'message_stop' if api == 'anthropic' else 'response.completed'
    if events[-1][0] != terminal or sum(kind == terminal for kind, _ in events) != 1:
        raise RuntimeError('missing or invalid SSE terminal event')
    required = (['message_start', 'content_block_start', 'content_block_delta',
                 'content_block_stop', 'message_delta', 'message_stop'] if api == 'anthropic'
                else ['response.created', 'response.output_item.added', 'response.content_part.added',
                      'response.output_text.delta', 'response.output_text.done',
                      'response.content_part.done', 'response.output_item.done', 'response.completed'])
    position = 0
    for kind, _ in events:
        if position < len(required) and kind == required[position]:
            position += 1
    if position != len(required):
        raise RuntimeError('missing or out-of-order protocol SSE events')
    if api == 'anthropic':
        if not any(kind == 'content_block_start' and obj.get('content_block', {}).get('type') == 'text'
                   for kind, obj in events):
            raise RuntimeError('Anthropic stream has no text block')
        text = ''.join(obj.get('delta', {}).get('text', '') for kind, obj in events
                       if kind == 'content_block_delta' and obj.get('delta', {}).get('type') == 'text_delta')
        stops = [obj.get('delta', {}).get('stop_reason') for kind, obj in events if kind == 'message_delta']
        if stops != ['end_turn']:
            raise RuntimeError('Anthropic stream stop reason mismatch')
    else:
        if (not any(kind == 'response.output_item.added' and obj.get('item', {}).get('type') == 'message'
                    for kind, obj in events)
                or not any(kind == 'response.content_part.added'
                           and obj.get('part', {}).get('type') == 'output_text' for kind, obj in events)):
            raise RuntimeError('Responses stream has no output text message')
        text = ''.join(obj.get('delta', '') for kind, obj in events if kind == 'response.output_text.delta')
        if events[-1][1].get('response', {}).get('status') != 'completed':
            raise RuntimeError('Responses completed status mismatch')
    if text != expected:
        raise RuntimeError('SSE text mismatch')


def request_case(port, case, stream):
    if case == 'a2a':
        api, path, token, expected = 'anthropic', '/v1/messages', CLIENT, 'fixture reply'
        body = {'model': 'claude-sonnet', 'max_tokens': 16,
                'messages': [{'role': 'user', 'content': 'hello'}], 'stream': stream}
    elif case == 'a2r':
        api, path, token, expected = 'anthropic', '/v1/messages', A2R_CLIENT, A2R_OUTPUT
        body = {'model': 'claude-sonnet', 'max_tokens': 16, 'system': A2R_SYSTEM,
                'messages': [{'role': 'user', 'content': A2R_INPUT}], 'stream': stream}
    elif case == 'r2a':
        api, path, token, expected = 'responses', '/v1/responses', R2A_CLIENT, R2A_OUTPUT
        body = {'model': 'claude-sonnet', 'max_output_tokens': 16, 'instructions': R2A_SYSTEM,
                'input': R2A_INPUT, 'stream': stream}
    else:
        raise ValueError('unknown regression case')
    before = len(Mock.calls)
    Mock.gate.clear(); Mock.timed_out = False
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=12)
    try:
        headers = {'Content-Type': 'application/json', 'anthropic-version': '2023-06-01'}
        headers['x-api-key' if api == 'anthropic' else 'Authorization'] = token if api == 'anthropic' else 'Bearer ' + token
        connection.request('POST', path, json.dumps(body), headers)
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError('proxy returned HTTP %s: %s' % (response.status, response.read()[:600]))
        if stream:
            if 'text/event-stream' not in response.getheader('Content-Type', ''):
                raise RuntimeError('stream content type mismatch')
            events = []
            delta_type = 'content_block_delta' if api == 'anthropic' else 'response.output_text.delta'
            # The upstream withholds terminal events until the client receives
            # an actual text delta, proving progressive delivery rather than EOF buffering.
            while True:
                event = read_sse_event(response)
                if event is None:
                    raise RuntimeError('stream ended before a text delta')
                events.append(event)
                if len(events) > 128:
                    raise RuntimeError('too many SSE events')
                kind, obj = event
                delta = obj.get('delta', {})
                text = delta.get('text', '') if api == 'anthropic' and isinstance(delta, dict) else delta
                if kind == delta_type and text:
                    break
            Mock.gate.set()
            while True:
                event = read_sse_event(response)
                if event is None: break
                events.append(event)
                if len(events) > 128:
                    raise RuntimeError('too many SSE events')
            if Mock.timed_out:
                raise RuntimeError('text delta buffered until mock timeout')
            verify_stream_events(events, api, expected)
        else:
            if 'application/json' not in response.getheader('Content-Type', ''):
                raise RuntimeError('JSON content type mismatch')
            verify_json_response(json.loads(response.read()), api, expected)
        if (len(Mock.calls) != before + 1 or Mock.calls[-1].get('case') != case
                or Mock.calls[-1].get('stream') is not stream):
            raise RuntimeError('mock did not receive the expected protocol request: %r' % Mock.calls[before:])
    finally:
        Mock.gate.set()
        connection.close()


def run(fixture, proxy_port, mock_port):
    source = HERE.parent
    if proxy_port == mock_port or 18889 in (proxy_port, mock_port):
        raise RuntimeError('choose two different non-default ports')
    proxy_reservation = reserve(proxy_port)
    mock = None
    scratch = None
    worker = None
    mock_thread_started = False
    primary_error = None
    cleanup_errors = []
    Mock.calls = []
    Mock.gate.clear()
    Mock.timed_out = False
    try:
        # Binding mock while the proxy reservation is held detects either conflict before execution.
        try:
            mock = ThreadingHTTPServer(('127.0.0.1', mock_port), Mock)
        except OSError as exc:
            raise RuntimeError('mock port occupied or unavailable: %d' % mock_port) from exc
        scratch = Scratch(source)
        entry = scratch.stage(fixture, proxy_port, mock_port)
        validate_fresh_project(scratch, proxy_port, mock_port)
        if not fixture:
            scratch.assert_authority_unchanged()
            print('NOTICE: copied authority prompt replaced for this isolated fake project only; '
                  'not original-prompt/manual initialization acceptance, production, or real-worktree validation',
                  flush=True)
            print('NOTICE: authority SHA-256 source=%s copied-with-test-prompt=%s' % (
                hashlib.sha256(scratch.authority_snapshot[1]).hexdigest(),
                hashlib.sha256(scratch.patched_authority_snapshot[1]).hexdigest()), flush=True)
        thread = threading.Thread(target=mock.serve_forever, daemon=True)
        thread.start()
        mock_thread_started = True
        try:
            worker = TTYWorker(scratch, entry, proxy_port, mock_port, fixture=fixture)
        except TTYWorkerConstructionError as exc:
            # __init__ failed after Popen: retain that exact child handle for
            # the common finally cleanup and exit-identity check.
            worker = exc.worker
            if not isinstance(exc.reason, Exception):
                raise exc.reason
            raise
        command = worker.command
        proxy_reservation.close(); proxy_reservation = None
        result = command('on', quiet=True)
        if not result['children']:
            raise RuntimeError('CLI did not create an owned direct service child')
        deadline = time.monotonic() + 12
        service_pid = None
        while time.monotonic() < deadline:
            live = command('__probe__', quiet=True)['children']
            owners = [pid for pid in result['children'] if pid in live and owns_listener(pid, proxy_port)]
            if len(owners) == 1:
                service_pid = owners[0]
                break
            if len(owners) > 1:
                raise RuntimeError('proxy listener has multiple managed owners')
            if worker.process.poll() is not None:
                raise RuntimeError('worker exited during startup')
            time.sleep(.1)
        else:
            raise RuntimeError('test child did not own proxy listener; no request sent')
        print('PASS cli on: fake initialization on PTY and owned listener', flush=True)
        require_managed_listener(command, service_pid, proxy_port)
        status_result = command('status', quiet=True)
        require_managed_listener(command, service_pid, proxy_port)
        verify_status(status_result['output'], service_pid, proxy_port, scratch.project)
        print('PASS cli status: state and available identity verified', flush=True)
        for streaming in (False, True):
            if streaming:
                config_path = scratch.project / 'config/model_proxy_config.json'
                config = read_local_json(config_path)
                if config != fake_config(proxy_port, mock_port):
                    raise RuntimeError('test configuration changed before reload')
                config['supplies'][0]['target_model'] = 'fixture-model-reloaded'
                write_local_json(config_path, config)
                command('reload')
            require_managed_listener(command, service_pid, proxy_port)
            request_case(proxy_port, 'a2a', streaming)
            print('PASS anthropic local ' + ('incremental SSE response' if streaming else 'JSON response'), flush=True)
        if [call.get('model') for call in Mock.calls] != ['fixture-model', 'fixture-model-reloaded']:
            raise RuntimeError('passthrough mock did not observe the reloaded model')
        for case in ('a2r', 'r2a'):
            for streaming in (False, True):
                require_managed_listener(command, service_pid, proxy_port)
                request_case(proxy_port, case, streaming)
                print('PASS %s %s: mock request, text and terminal' %
                      (case, 'incremental SSE' if streaming else 'JSON'), flush=True)
        stop_managed(command, service_pid, proxy_port)
        deadline = time.monotonic() + 5
        while command('__probe__', quiet=True)['children']:
            if time.monotonic() >= deadline:
                raise RuntimeError('off did not stop the owned service child')
            time.sleep(.05)
        print('PASS cli off: previously live owned child stopped', flush=True)
        print('PASS mock received fake credentials, expected request semantics and reloaded model', flush=True)
        if not fixture:
            scratch.assert_authority_unchanged()
            print('NOTICE: source and copied authority unchanged at checked points; this does not prove the original prompt, '
                  'manual initialization, production, or a real worktree instance', flush=True)
    except BaseException as exc:
        # Keep the original exception active while attempting every cleanup.
        primary_error = exc
        raise
    finally:
        def cleanup_failed(label, error):
            detail = '%s: %s' % (label, error)
            cleanup_errors.append(detail)
            print('ERROR: ' + detail, file=sys.stderr)

        if proxy_reservation:
            try:
                proxy_reservation.close()
            except Exception as exc:
                cleanup_failed('proxy reservation cleanup failed', exc)
        if worker:
            try:
                worker.close()
            except Exception as exc:
                cleanup_failed('worker close failed', exc)
            # A failed close does not establish whether the worker exited.
            try:
                worker_status = worker.process.poll()
            except Exception as exc:
                cleanup_failed('worker status check failed', exc)
                worker_status = None
            if worker_status is None:
                print('ERROR: worker still alive or exit unconfirmed; scratch retained: '
                      + str(scratch.root), file=sys.stderr)
                scratch = None  # never delete without confirmed worker exit
                cleanup_errors.append('worker cleanup failed: worker still alive or exit unconfirmed')
            elif worker_status != 0:
                print('ERROR: worker exited abnormally; scratch retained: ' + str(scratch.root), file=sys.stderr)
                scratch = None  # cannot prove all descendants were reaped
                cleanup_errors.append('worker cleanup failed: worker exited abnormally')
        if mock:
            if mock_thread_started:
                try:
                    mock.shutdown()
                except Exception as exc:
                    cleanup_failed('mock shutdown failed', exc)
            try:
                mock.server_close()
            except Exception as exc:
                cleanup_failed('mock server close failed', exc)
        if scratch:
            try:
                scratch.cleanup()
            except Exception as exc:
                cleanup_failed('scratch cleanup failed', exc)
            else:
                print('PASS scratch cleanup', flush=True)
        # Never mask the primary error; individual cleanup errors were printed.
        if cleanup_errors and primary_error is None:
            raise RuntimeError('; '.join(cleanup_errors))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--fixture', action='store_true', help='exercise only self-contained test fixture')
    p.add_argument('--worktree-ready', action='store_true', help='explicitly confirm core/CLI changes are complete')
    p.add_argument('--proxy-port', type=int, required=True)
    p.add_argument('--mock-port', type=int, required=True)
    args = p.parse_args()
    if args.fixture == args.worktree_ready:
        p.error('select exactly one of --fixture or --worktree-ready')
    if any(x < 1024 or x > 65535 for x in (args.proxy_port, args.mock_port)):
        p.error('ports must be 1024..65535')
    try:
        run(args.fixture, args.proxy_port, args.mock_port)
    except Exception as exc:
        print('FAIL: ' + str(exc), file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
