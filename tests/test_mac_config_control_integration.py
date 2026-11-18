"""Real helper/write/reload tests in disposable sources and fake HOME only."""
import json
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ISO = Path(os.environ.get('MODEL_PROXY_TEST_ISOLATION', '/invalid')).resolve()
if (Path(__file__).resolve().parents[1] != ISO / 'project'
        or not (ISO / '.model-proxy-isolation').is_file()
        or not Path(os.environ.get('HOME', '/invalid')).resolve().is_relative_to(ISO)):
    raise RuntimeError('isolated source and fake HOME required')
sys.path.insert(0, str(ISO / 'project'))
from core.runtime_paths import PATH_KEYS


@unittest.skipUnless(sys.platform == 'darwin', 'native Mac protection required')
class RealHelperIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ISO, prefix='helper-integration-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'project'
        for d in ('config', 'runtime', 'home', 'tmp', 'external'):
            (self.root / d).mkdir(parents=True, mode=0o700)
        shutil.copytree(ISO / 'project/core', self.root / 'core',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        self.target = self.root / 'config/model_proxy_config.json'
        self.target.write_text(json.dumps({'admin_token':'fake-admin', 'supplies':[],
                                           'routes':[{'id':'old'}, {'id':'new'}],
                                           'strategies':[{'client_token':'fake-client','route_id':'old'}]}))
        self.target.chmod(0o600)
        self.paths = {k: str(self.root / 'runtime' / k) for k in PATH_KEYS}
        self.runtime_file = self.root / 'config/runtime_paths.json'
        self.runtime_file.write_text(json.dumps(self.paths))
        self.driver = self.root / 'fake_driver.py'
        self.driver.write_text('''import sys\nfrom pathlib import Path\nroot=Path(__file__).resolve().parent\nsys.path.insert(0,str(root))\nfrom core.cli import dispatch\nclass FakeControl:\n def reload(self):\n  print("FAKE_RELOAD_ONCE",flush=True);return 0\nif sys.argv[1] == "switch":\n rc=dispatch.main(["switch","fake-client","new"],root=root,control=FakeControl())\nelif sys.argv[1] == "pipe-test":\n rc=dispatch.run_helper("switch",["fake-client","new"],dispatch.config_path(root),FakeControl(),root=root)\nelif sys.argv[1] == "pipe-close-test":\n from unittest.mock import patch\n import os,errno\n state={};original_run=dispatch.subprocess.run;original_close=os.close\n def run(*args,**kwargs):\n  state["target"]=kwargs["pass_fds"][0];return original_run(*args,**kwargs)\n def close(fd):\n  if fd == state.get("target") and "reused" not in state:\n   original_close(fd);state["reused"]=os.open(root/"home/reused",os.O_RDWR)\n   raise OSError(errno.EIO,"fake close after-effect")\n  if fd == state.get("reused"): state["retried"]=True\n  return original_close(fd)\n try:\n  with patch.object(dispatch.subprocess,"run",side_effect=run), patch.object(dispatch.os,"close",side_effect=close):\n   rc=dispatch.run_helper("switch",["fake-client","new"],dispatch.config_path(root),FakeControl(),root=root)\n  if state.get("reused") == state.get("target") and not state.get("retried") and os.pread(state["reused"],32,0) == b"fake-owned-data":\n   print("FAKE_REUSED_FD_STILL_OPEN",flush=True)\n finally:\n  if "reused" in state: original_close(state["reused"])\nelse:\n rc=dispatch.run_helper("route-del",["new"],dispatch.config_path(root),FakeControl(),root=root)\nsys.exit(rc)\n''')
        self.env = {'HOME': str(self.root / 'home'), 'TMPDIR': str(self.root / 'tmp'),
                    'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE':'1',
                    'MODEL_PROXY_CONFIG': str(self.target), 'LC_ALL':'C'}

    def child(self, action, **kwargs):
        return subprocess.Popen([sys.executable, '-I', '-S', '-B', str(self.driver), action],
                                cwd=self.root, env=self.env, **kwargs)

    def noninteractive(self, action='switch', *, timeout=12):
        p = self.child(action, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = p.communicate(timeout=timeout)
            return p.returncode, out + err
        finally:
            if p.poll() is None:
                p.kill()
                p.communicate(timeout=5)

    def interactive(self, action, prompt, reply, before_reply=None):
        master, slave = pty.openpty()
        p = self.child(action, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        data = b''
        try:
            deadline = time.monotonic() + 12
            while prompt not in data:
                if time.monotonic() >= deadline or p.poll() is not None:
                    self.fail('fake helper exited before expected prompt: ' + data.decode(errors='replace'))
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    chunk = os.read(master, 65536)
                    if not chunk:
                        self.fail('fake helper closed before expected prompt')
                    data += chunk
            if before_reply is not None:
                before_reply()
            os.write(master, reply + b'\n')
            while p.poll() is None and time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    try: chunk = os.read(master, 65536)
                    except OSError: break
                    if not chunk: break
                    data += chunk
            p.wait(timeout=5)
            while select.select([master], [], [], 0)[0]:
                try: chunk = os.read(master, 65536)
                except OSError: break
                if not chunk: break
                data += chunk
            return p.returncode, data
        finally:
            if p.poll() is None:
                p.kill(); p.wait(timeout=5)
            os.close(master)

    def assert_switched_once(self, target, output):
        self.assertEqual(json.loads(target.read_text())['strategies'][0]['route_id'], 'new')
        self.assertEqual(output.count(b'FAKE_RELOAD_ONCE'), 1)
        self.assertEqual(len(list(target.parent.glob(target.name + '.bak.*'))), 1)
        self.assertEqual(list((self.root / 'tmp').glob('model-proxy-cli-*')), [])

    def test_default_noninteractive_helper_commits_and_reloads_once(self):
        code, out = self.noninteractive()
        self.assertEqual(code, 0, out)
        self.assert_switched_once(self.target, out)

    def test_real_pass_fds_with_owned_fake_helper_has_no_filesystem_marker(self):
        # This exercises a real child and pipe; the helper is deliberately a
        # test double and does not claim to validate the real helper protocol.
        helper = self.root / 'core/cli/helper_entry.py'
        helper.write_text('''import os,stat\nfd=int(os.environ["CONFIG_OPS_RELOAD_MARKER_FD"])\nst=os.fstat(fd)\nassert stat.S_ISFIFO(st.st_mode) and st.st_nlink == 0\nassert "CONFIG_OPS_RELOAD_MARKER" not in os.environ\nos.write(fd,b"writing\\nyes\\nyes\\n")\nprint("FAKE_PIPE_CHILD_CHECKED",flush=True)\n''')
        before = self.target.read_bytes()
        code, out = self.noninteractive('pipe-test')
        self.assertEqual(code, 0, out)
        self.assertIn(b'FAKE_PIPE_CHILD_CHECKED', out)
        self.assertEqual(out.count(b'FAKE_RELOAD_ONCE'), 1)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(list((self.root / 'tmp').iterdir()), [])

    def test_real_child_invalid_frames_nonzero_and_interrupt_never_reload(self):
        helper = self.root / 'core/cli/helper_entry.py'
        before = self.target.read_bytes()
        cases = (
            ('yes-then-failure', "os.write(fd,%r);print('SENT_YES',flush=True);sys.exit(7)" %
             b'writing\nyes\nyes\n', b'SENT_YES'),
            ('partial-frame', "os.write(fd,%r);print('SENT_PARTIAL',flush=True)" %
             b'writing\nyes', b'SENT_PARTIAL'),
            ('invalid-frame', "os.write(fd,%r);print('SENT_INVALID',flush=True)" %
             b'writing\ninvalid\nyes\n', b'SENT_INVALID'),
            ('write-failure', "os.close(fd)\ntry: os.write(fd,b'yes\\n')\n"
             "except OSError: print('WRITE_FAILED',flush=True);sys.exit(7)", b'WRITE_FAILED'),
            ('interrupt', "os.write(fd,b'writing\\n');print('BEFORE_SIGINT',flush=True);"
             "signal.signal(signal.SIGINT,signal.SIG_DFL);os.kill(os.getpid(),signal.SIGINT)",
             b'BEFORE_SIGINT'),
        )
        for label, body, expected_output in cases:
            with self.subTest(label=label):
                helper.write_text('import os,sys,signal\nfd=int(os.environ["CONFIG_OPS_RELOAD_MARKER_FD"])\n'
                                  + body + '\n')
                code, out = self.noninteractive('pipe-test')
                self.assertNotEqual(code, 0, (label, out))
                self.assertIn(expected_output, out)
                self.assertNotIn(b'FAKE_RELOAD_ONCE', out)
                self.assertEqual(self.target.read_bytes(), before)

    def test_real_child_close_failure_does_not_reclose_reused_fd(self):
        helper = self.root / 'core/cli/helper_entry.py'
        helper.write_text('import os\nfd=int(os.environ["CONFIG_OPS_RELOAD_MARKER_FD"])\n'
                          'os.write(fd,%r)\n' % b'writing\nyes\nyes\n')
        reused = self.root / 'home/reused'
        reused.write_bytes(b'fake-owned-data')
        code, out = self.noninteractive('pipe-close-test')
        self.assertNotEqual(code, 0, out)
        self.assertIn(b'FAKE_REUSED_FD_STILL_OPEN', out)
        self.assertNotIn(b'FAKE_RELOAD_ONCE', out)
        self.assertIn(b'close not confirmed', out)
        self.assertEqual(reused.read_bytes(), b'fake-owned-data')

    def test_direct_write_helper_without_pipe_fails_before_backup(self):
        before = self.target.read_bytes()
        helper = self.root / 'core/cli/helper_entry.py'
        env = dict(self.env)
        env.pop('CONFIG_OPS_RELOAD_MARKER', None)
        env.pop('CONFIG_OPS_RELOAD_MARKER_FD', None)
        proc = subprocess.Popen([sys.executable, '-I', '-S', '-B', str(helper),
                                 'switch', str(self.target), 'fake-client', 'new'],
                                cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)
        self.assertNotEqual(proc.returncode, 0, out + err)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(list(self.target.parent.glob(self.target.name + '.bak.*')), [])

    def test_direct_write_helper_rejects_untrusted_marker_path_before_backup(self):
        before = self.target.read_bytes()
        ordinary = self.root / 'external/ordinary.json'
        ordinary.write_bytes(b'started'); ordinary.chmod(0o600)
        env = dict(self.env)
        env['CONFIG_OPS_RELOAD_MARKER'] = str(ordinary)
        env.pop('CONFIG_OPS_RELOAD_MARKER_FD', None)
        helper = self.root / 'core/cli/helper_entry.py'
        proc = subprocess.Popen([sys.executable, '-I', '-S', '-B', str(helper),
                                 'switch', str(self.target), 'fake-client', 'new'],
                                cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill(); proc.communicate(timeout=5)
        self.assertNotEqual(proc.returncode, 0, out + err)
        self.assertEqual(ordinary.read_bytes(), b'started')
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(list(self.target.parent.glob(self.target.name + '.bak.*')), [])

    def test_direct_write_helper_rejects_linked_regular_fd_before_backup(self):
        before = self.target.read_bytes()
        ordinary = self.root / 'external/ordinary.json'
        ordinary.write_bytes(b'started'); ordinary.chmod(0o600)
        fd = os.open(ordinary, os.O_RDWR | os.O_NOFOLLOW)
        try:
            env = dict(self.env)
            env['CONFIG_OPS_RELOAD_MARKER_FD'] = str(fd)
            env.pop('CONFIG_OPS_RELOAD_MARKER', None)
            helper = self.root / 'core/cli/helper_entry.py'
            proc = subprocess.Popen([sys.executable, '-I', '-S', '-B', str(helper),
                                     'switch', str(self.target), 'fake-client', 'new'],
                                    cwd=self.root, env=env, pass_fds=(fd,),
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                out, err = proc.communicate(timeout=15)
            finally:
                if proc.poll() is None:
                    proc.kill(); proc.communicate(timeout=5)
        finally:
            os.close(fd)
        self.assertNotEqual(proc.returncode, 0, out + err)
        self.assertEqual(ordinary.read_bytes(), b'started')
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(list(self.target.parent.glob(self.target.name + '.bak.*')), [])

    def test_default_config_larger_than_two_mib_remains_editable(self):
        cfg = json.loads(self.target.read_text())
        cfg['fake_padding'] = 'x' * (2 * 1024 * 1024 + 1024)
        self.target.write_text(json.dumps(cfg))
        code, out = self.noninteractive(timeout=60)
        self.assertEqual(code, 0, out)
        self.assert_switched_once(self.target, out)
        self.assertEqual(len(json.loads(self.target.read_text())['fake_padding']),
                         len(cfg['fake_padding']))

    def test_real_helper_pipe_with_protocol_gate(self):
        code, out = self.noninteractive('pipe-test', timeout=60)
        self.assertEqual(code, 0, out)
        self.assert_switched_once(self.target, out)

    def test_large_real_helper_pipe_with_protocol_gate(self):
        cfg = json.loads(self.target.read_text())
        cfg['fake_padding'] = 'x' * (2 * 1024 * 1024 + 1024)
        self.target.write_text(json.dumps(cfg))
        code, out = self.noninteractive('pipe-test', timeout=90)
        self.assertEqual(code, 0, out)
        self.assert_switched_once(self.target, out)
        self.assertEqual(len(json.loads(self.target.read_text())['fake_padding']),
                         len(cfg['fake_padding']))

    def test_internal_link_helper_preserves_link_and_reloads_once(self):
        link = self.root / 'config/alias.json'; link.symlink_to(self.target.name)
        self.env['MODEL_PROXY_CONFIG'] = str(link)
        code, out = self.noninteractive()
        self.assertEqual(code, 0, out)
        self.assertTrue(link.is_symlink())
        self.assert_switched_once(self.target, out)

    def external_link(self):
        external = self.root / 'external/business.json'
        external.write_bytes(self.target.read_bytes()); external.chmod(0o600)
        link = self.root / 'config/outside.json'; link.symlink_to(external)
        self.env['MODEL_PROXY_CONFIG'] = str(link)
        return external, link

    def test_external_link_noninteractive_never_writes_or_reloads(self):
        external, link = self.external_link(); before = external.read_bytes()
        code, out = self.noninteractive()
        self.assertNotEqual(code, 0)
        self.assertIn(b'external config target requires explicit terminal authorization', out)
        self.assertNotIn(b'FAKE_RELOAD_ONCE', out)
        self.assertEqual(external.read_bytes(), before)
        self.assertTrue(link.is_symlink())
        self.assertEqual(list(external.parent.glob(external.name + '.bak.*')), [])

    def test_external_link_tty_explicit_yes_commits_real_target(self):
        external, link = self.external_link()
        code, out = self.interactive('switch', '输入 YES 确认'.encode(), b'YES')
        self.assertEqual(code, 0, out)
        self.assertIn(os.fsencode(external), out)
        self.assertTrue(link.is_symlink())
        self.assert_switched_once(external, out)

    def test_external_real_helper_pipe_with_protocol_gate(self):
        external, link = self.external_link()
        code, out = self.interactive('pipe-test', '输入 YES 确认'.encode(), b'YES')
        self.assertEqual(code, 0, out)
        self.assertTrue(link.is_symlink())
        self.assert_switched_once(external, out)

    def test_external_retarget_with_protocol_gate_is_rejected(self):
        external, link = self.external_link()
        before = external.read_bytes()
        def replace_link():
            link.unlink()
            link.symlink_to(external)
        code, out = self.interactive('pipe-test', '输入 YES 确认'.encode(), b'YES',
                                     before_reply=replace_link)
        self.assertNotEqual(code, 0)
        self.assertNotIn(b'FAKE_RELOAD_ONCE', out)
        self.assertEqual(external.read_bytes(), before)
        self.assertEqual(list(external.parent.glob(external.name + '.bak.*')), [])

    def test_external_link_tty_denied_never_writes(self):
        external, link = self.external_link(); before = external.read_bytes()
        code, out = self.interactive('switch', '输入 YES 确认'.encode(), b'NO')
        self.assertNotEqual(code, 0)
        self.assertEqual(external.read_bytes(), before)
        self.assertNotIn(b'FAKE_RELOAD_ONCE', out)
        self.assertTrue(link.is_symlink())

    def test_external_chain_change_during_authorization_is_rejected(self):
        external, link = self.external_link(); before = external.read_bytes()
        def replace_link():
            link.unlink()
            link.symlink_to(external)
        code, out = self.interactive('switch', '输入 YES 确认'.encode(), b'YES',
                                     before_reply=replace_link)
        self.assertNotEqual(code, 0)
        self.assertNotIn(b'FAKE_RELOAD_ONCE', out)
        self.assertEqual(external.read_bytes(), before)
        self.assertEqual(list(external.parent.glob(external.name + '.bak.*')), [])

    def test_runtime_conflict_blocks_helper_before_reload(self):
        self.paths['log'] = str(self.target)
        self.runtime_file.write_text(json.dumps(self.paths))
        before = self.target.read_bytes()
        code, out = self.noninteractive()
        self.assertNotEqual(code, 0)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertNotIn(b'FAKE_RELOAD_ONCE', out)
        self.assertEqual(list(self.target.parent.glob(self.target.name + '.bak.*')), [])

    def test_preview_cancel_has_no_commit_or_reload(self):
        before = self.target.read_bytes()
        code, out = self.interactive('delete', b'[y/N]', b'n')
        self.assertNotEqual(code, 0)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertNotIn(b'FAKE_RELOAD_ONCE', out)
        self.assertEqual(list(self.target.parent.glob(self.target.name + '.bak.*')), [])

    def test_target_changed_during_preview_rejects_commit(self):
        external = b'{"external":true}'
        code, out = self.interactive('delete', b'[y/N]', b'y',
                                     before_reply=lambda: self.target.write_bytes(external))
        self.assertNotEqual(code, 0)
        self.assertEqual(self.target.read_bytes(), external)
        self.assertNotIn(b'FAKE_RELOAD_ONCE', out)
        self.assertEqual(list(self.target.parent.glob(self.target.name + '.bak.*')), [])


if __name__ == '__main__': unittest.main()
