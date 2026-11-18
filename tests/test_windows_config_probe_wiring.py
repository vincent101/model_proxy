"""Windows snapshot/authorization around real probe call flow; network mocked."""
import contextlib
import io
import json
import os
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from core.cli import _config_ops as ops
from core import windows_secure_files as secure


class Snapshot:
    def __init__(self, data):
        self.contents = json.dumps(data).encode()
        self.valid = True
        self.checks = 0
    def revalidate(self):
        self.checks += 1
        if not self.valid:
            raise secure.SecureSnapshotError('target_changed')


class WindowsProbeWiring(unittest.TestCase):
    def setUp(self):
        self.path = 'fake-config'
        self.supply = {'id': 's1', 'protocol': 'anthropic', 'url': 'https://fixture.invalid/v1/messages',
                       'target_model': 'fake', 'appkey': 'fake-key'}
        self.snap = Snapshot({'supplies': [self.supply]})
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.multiple(ops, os=types.SimpleNamespace(name='nt', path=os.path),
            _WRITE_BASELINE=self.snap, _WRITE_INPUT_PATH=self.path, _WRITE_AUTHORIZATION=True,
            _WRITE_PREFLIGHT=lambda: True, _MARKER_REQUIRED=True))
        self.stack.enter_context(patch.object(ops, '_verify_marker_binding'))
        self.stack.enter_context(patch.object(ops, 'confirm', return_value=True))
        self.stack.enter_context(patch.object(ops, 'done'))
        self.write = self.stack.enter_context(patch.object(ops, 'atomic_write'))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        def fake_request(req, timeout):
            body = b'{"error":{"message":"expected one of `low`, `high`"}}'
            raise urllib.error.HTTPError(req.full_url, 400, 'fake', None, io.BytesIO(body))
        self.request = self.stack.enter_context(patch.object(ops.urllib.request, 'urlopen', side_effect=fake_request))

    def test_add_check_and_edit_reprobe_reach_request_with_valid_windows_snapshot(self):
        for operation in ('add', 'check', 'edit'):
            with self.subTest(operation=operation):
                self.request.reset_mock()
                self.write.reset_mock()
                if operation == 'add':
                    self.snap.contents = json.dumps({'supplies': []}).encode()
                    inputs = ['new', self.supply['url'], 'anthropic', 'fake-key', 'fake', '', '']
                    with patch('builtins.input', side_effect=inputs):
                        ops.supply_add(self.path)
                elif operation == 'check':
                    self.snap.contents = json.dumps({'supplies': [self.supply]}).encode()
                    with patch('builtins.input', return_value=''):
                        ops.supply_check(self.path, 's1')
                else:
                    self.snap.contents = json.dumps({'supplies': [self.supply]}).encode()
                    with patch('builtins.input', side_effect=['', '', '', '', '', '']):
                        ops.supply_edit(self.path, 's1')
                self.request.assert_called_once()
                self.write.assert_called_once()
                self.assertGreater(self.snap.checks, 0)

    def test_changed_snapshot_or_denied_preflight_never_sends_request(self):
        self.snap.valid = False
        with self.assertRaises(secure.SecureSnapshotError):
            ops.probe_effort(self.supply)
        self.request.assert_not_called()
        self.snap.valid = True
        with patch.object(ops, '_WRITE_PREFLIGHT', return_value=False):
            with self.assertRaises(ops.WritePreflightFailed):
                ops.probe_effort(self.supply)
        self.request.assert_not_called()
        with patch.object(ops, '_WRITE_AUTHORIZATION', None):
            with self.assertRaises(ops.WritePreflightFailed):
                ops.probe_effort(self.supply)
        self.request.assert_not_called()

    def test_change_during_preflight_is_rechecked_before_request(self):
        def preflight():
            self.snap.valid = False
            return True
        with patch.object(ops, '_WRITE_PREFLIGHT', side_effect=preflight):
            with self.assertRaises(secure.SecureSnapshotError):
                ops.probe_effort(self.supply)
        self.request.assert_not_called()

    def test_second_preflight_failure_stops_request_at_last_check(self):
        with patch.object(ops, '_WRITE_PREFLIGHT', side_effect=[True, False]):
            with self.assertRaises(ops.WritePreflightFailed):
                ops.probe_effort(self.supply)
        self.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
