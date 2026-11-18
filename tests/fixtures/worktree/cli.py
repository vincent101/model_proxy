"""Minimal CLI fixture, not the model_proxy CLI."""
import json
import os
import signal
import stat
import subprocess
import sys
import urllib.request
import uuid
from pathlib import Path

root = Path(__file__).resolve().parent
cfg = json.loads((root / 'config/model_proxy_config.json').read_text())
paths = {key: str(root / value) for key, value in
         json.loads((root / 'config/runtime_paths.json').read_text()).items()}
base = 'http://127.0.0.1:%d' % cfg['listen']['port']
cmd = sys.argv[1]
if cmd == 'on':
    a, b = os.fstat(sys.stdin.fileno()), os.fstat(sys.stdout.fileno())
    if (not sys.stdin.isatty() or not sys.stdout.isatty()
            or not stat.S_ISCHR(a.st_mode) or (a.st_dev, a.st_rdev) != (b.st_dev, b.st_rdev)):
        raise SystemExit('fixture requires matching TTY')
    proxy_port, mock_port = os.environ['MP_TEST_PORTS'].split(',')
    print('Confirm this newly created fake project uses its own independent ledger '
          'and test ports %s and %s.' % (proxy_port, mock_port))
    source = input('Installation/recovery source: ')
    confirm = input('Type YES to confirm: ')
    if source != 'isolated temporary test project' or confirm != 'YES':
        raise SystemExit('fixture initialization not confirmed')
    state = root / '.runtime'
    state.mkdir(mode=0o700, exist_ok=True)
    nonce = uuid.uuid4().hex
    install = {'record': 'install', 'project_root': str(root),
               'initialization_source': source, 'install_id': uuid.uuid4().hex,
               'ever_started': True, 'last_instance_nonce': nonce}
    (state / 'install.json').write_text(json.dumps(install))
    env = os.environ.copy()
    env['MODEL_PROXY_START_NONCE'] = nonce
    p = subprocess.Popen([sys.executable, str(root / 'service.py')],
                         env=env, start_new_session=True)
    Path(paths['pid']).write_text(str(p.pid))
elif cmd == 'status':
    with urllib.request.urlopen(base + '/status', timeout=3) as response:
        data = json.loads(response.read())
    print('service running on port %s  pid %s' % (cfg['listen']['port'], data['pid']))
    print('config %d supplies / %d routes / %d strategies' %
          (len(cfg['supplies']), len(cfg['routes']), len(cfg['strategies'])))
    print('health: fixture')
elif cmd == 'reload':
    req = urllib.request.Request(base + '/reload', method='POST')
    print(urllib.request.urlopen(req, timeout=3).read().decode())
elif cmd == 'off':
    os.kill(int(Path(paths['pid']).read_text()), signal.SIGTERM)
else:
    raise SystemExit(2)
