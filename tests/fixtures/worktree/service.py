"""Simple Anthropic-shaped fixture forwarding to the local mock, not production."""
import json
import os
import site
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

root = Path(__file__).resolve().parent
if (os.environ.get('MP_TEST_GUARD_INSTALLED') != '1' or sys.prefix == sys.base_prefix
        or site.ENABLE_USER_SITE or len(sys.orig_argv) != 2 or sys.orig_argv[1] != __file__):
    raise SystemExit('fixture service requires isolated interpreter and guard')
cfg = json.loads((root / 'config/model_proxy_config.json').read_text())
paths = {key: str(root / value) for key, value in
         json.loads((root / 'config/runtime_paths.json').read_text()).items()}
Path(paths['log']).write_text('fixture only\n')
install = json.loads((root / '.runtime/install.json').read_text())
active = {'record': 'active', 'state': 'READY', 'pid': os.getpid(),
          'project_root': str(root), 'listen': {'host': '127.0.0.1', 'port': cfg['listen']['port']},
          'runtime_paths': paths, 'install_id': install['install_id'],
          'instance_nonce': os.environ['MODEL_PROXY_START_NONCE']}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass
    def do_GET(self):
        status = {'state': 'running', 'pid': os.getpid(), 'project_root': str(root),
                  'listen': {'host': '127.0.0.1', 'port': cfg['listen']['port']}}
        body = json.dumps(status).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.end_headers(); self.wfile.write(body)
    def emit(self, kind, payload):
        wire = ('event: ' + kind + '\ndata: ' + json.dumps(payload) + '\n\n').encode()
        self.wfile.write(wire); self.wfile.flush()

    def do_POST(self):
        if self.path == '/reload':
            global cfg
            cfg = json.loads((root / 'config/model_proxy_config.json').read_text())
            self.send_response(200); self.end_headers(); self.wfile.write(b'fixture reloaded'); return
        token = self.headers.get('x-api-key') or self.headers.get('Authorization', '').removeprefix('Bearer ')
        selection = {'fake-client-for-test-only': (0, '/v1/messages', 'a2a'),
                     'fake-client-a2r': (1, '/v1/messages', 'a2r'),
                     'fake-client-r2a': (2, '/v1/responses', 'r2a')}.get(token)
        if not selection or self.path != selection[1]:
            self.send_error(401); return
        idx, _, mode = selection
        incoming = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        supply = cfg['supplies'][idx]
        if mode == 'a2r':
            body = {'model': supply['target_model'], 'instructions': incoming['system'],
                    'input': [{'type': 'message', 'role': 'user', 'content': [
                        {'type': 'input_text', 'text': incoming['messages'][0]['content']}]}],
                    'max_output_tokens': incoming['max_tokens'], 'stream': incoming['stream']}
        elif mode == 'r2a':
            body = {'model': supply['target_model'], 'system': incoming['instructions'],
                    'messages': [{'role': 'user', 'content': incoming['input']}],
                    'max_tokens': incoming['max_output_tokens'], 'stream': incoming['stream']}
        else:
            body = dict(incoming, model=supply['target_model'])
        req = urllib.request.Request(supply['url'], data=json.dumps(body).encode(), headers={
            'x-api-key': supply['appkey'], 'Authorization': 'Bearer ' + supply['appkey'],
            'content-type': 'application/json'})
        with urllib.request.urlopen(req, timeout=12) as resp:
            if mode == 'a2a':
                self.send_response(resp.status)
                self.send_header('Content-Type', resp.headers['Content-Type'])
                if resp.headers.get('Content-Length'):
                    self.send_header('Content-Length', resp.headers['Content-Length'])
                self.end_headers()
                while True:
                    chunk = resp.readline()
                    if not chunk: break
                    self.wfile.write(chunk); self.wfile.flush()
                return
            if not incoming['stream']:
                upstream = json.loads(resp.read())
                if mode == 'a2r':
                    text = upstream['output'][0]['content'][0]['text']
                    downstream = {'type': 'message', 'role': 'assistant',
                                  'content': [{'type': 'text', 'text': text}], 'stop_reason': 'end_turn'}
                else:
                    text = upstream['content'][0]['text']
                    downstream = {'object': 'response', 'status': 'completed',
                                  'output': [{'type': 'message', 'role': 'assistant',
                                              'content': [{'type': 'output_text', 'text': text}]}]}
                payload = json.dumps(downstream).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload))); self.end_headers()
                self.wfile.write(payload); return
            # This is a deliberately narrow semantic fixture, not production conversion code.
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
            event_name, data_lines, opened, collected = '', [], False, ''
            while True:
                line = resp.readline()
                if not line: break
                line = line.decode().rstrip('\r\n')
                if line.startswith('event:'):
                    event_name = line[6:].strip()
                elif line.startswith('data:'):
                    data_lines.append(line[5:].strip())
                elif not line and data_lines:
                    event = json.loads('\n'.join(data_lines)); data_lines = []
                    if mode == 'a2r' and event_name == 'response.created':
                        self.emit('message_start', {'type': 'message_start', 'message': {'role': 'assistant'}})
                    elif mode == 'a2r' and event_name == 'response.output_text.delta':
                        if not opened:
                            self.emit('content_block_start', {'type': 'content_block_start', 'index': 0,
                                      'content_block': {'type': 'text', 'text': ''}}); opened = True
                        self.emit('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                                  'delta': {'type': 'text_delta', 'text': event['delta']}})
                    elif mode == 'a2r' and event_name == 'response.completed':
                        self.emit('content_block_stop', {'type': 'content_block_stop', 'index': 0})
                        self.emit('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}})
                        self.emit('message_stop', {'type': 'message_stop'})
                    elif mode == 'r2a' and event_name == 'message_start':
                        self.emit('response.created', {'type': 'response.created', 'response': {'status': 'in_progress'}})
                    elif mode == 'r2a' and event_name == 'content_block_start':
                        self.emit('response.output_item.added', {'type': 'response.output_item.added',
                                  'item': {'type': 'message', 'role': 'assistant'}})
                        self.emit('response.content_part.added', {'type': 'response.content_part.added',
                                  'part': {'type': 'output_text', 'text': ''}})
                    elif mode == 'r2a' and event_name == 'content_block_delta':
                        collected += event['delta']['text']
                        self.emit('response.output_text.delta', {'type': 'response.output_text.delta',
                                  'delta': event['delta']['text']})
                    elif mode == 'r2a' and event_name == 'content_block_stop':
                        self.emit('response.output_text.done', {'type': 'response.output_text.done', 'text': collected})
                        self.emit('response.content_part.done', {'type': 'response.content_part.done',
                                  'part': {'type': 'output_text', 'text': collected}})
                        self.emit('response.output_item.done', {'type': 'response.output_item.done',
                                  'item': {'type': 'message', 'role': 'assistant'}})
                    elif mode == 'r2a' and event_name == 'message_stop':
                        self.emit('response.completed', {'type': 'response.completed',
                                  'response': {'status': 'completed'}})

server = HTTPServer(('127.0.0.1', cfg['listen']['port']), Handler)
last = {key: active[key] for key in ('project_root', 'listen', 'runtime_paths', 'install_id', 'instance_nonce')}
last['record'] = 'last_good_paths'
(root / '.runtime/last_good_paths.json').write_text(json.dumps(last))
(root / '.runtime/active.json').write_text(json.dumps(active))
server.serve_forever()
