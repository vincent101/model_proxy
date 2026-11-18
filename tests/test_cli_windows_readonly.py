"""Read-only Windows menus must not inherit or create reload markers."""
import contextlib
import ctypes
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from core.cli import dispatch


class WindowsReadonlyTests(unittest.TestCase):
    def test_windows_stdin_requires_a_console_handle(self):
        stream = mock.Mock()
        stream.isatty.return_value = True
        stream.fileno.return_value = 17
        msvcrt = mock.Mock()
        msvcrt.get_osfhandle.return_value = 123
        kernel = mock.Mock()
        with mock.patch.object(dispatch.sys, 'platform', 'win32'), \
             mock.patch.dict(sys.modules, {'msvcrt': msvcrt}), \
             mock.patch.object(ctypes, 'windll', kernel, create=True):
            kernel.kernel32.GetConsoleMode.return_value = 1
            self.assertTrue(dispatch._interactive_stdin(stream))
            kernel.kernel32.GetConsoleMode.return_value = 0
            self.assertFalse(dispatch._interactive_stdin(stream))
            msvcrt.get_osfhandle.side_effect = OSError
            self.assertFalse(dispatch._interactive_stdin(stream))

    def test_readonly_helper_uses_sentinel_not_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'fake.json'
            config.write_text('{}', encoding='utf-8')
            output = io.StringIO()
            control = mock.Mock()
            response = subprocess.CompletedProcess([], 0, '显示列表\n__RELOAD__=no\n'.encode())
            with mock.patch.object(dispatch.sys, 'platform', 'win32'), \
                 mock.patch.dict(os.environ, {
                     'CONFIG_OPS_RELOAD_MARKER': 'untrusted',
                     'CONFIG_OPS_RELOAD_MARKER_FD': '999',
                 }), \
                 mock.patch.object(dispatch.subprocess, 'run', return_value=response) as run, \
                 contextlib.redirect_stdout(output):
                self.assertEqual(dispatch.run_helper('supply-list', [], config, control), 0)
            environment = run.call_args.kwargs['env']
            self.assertNotIn('CONFIG_OPS_RELOAD_MARKER', environment)
            self.assertNotIn('CONFIG_OPS_RELOAD_MARKER_FD', environment)
            self.assertEqual(environment['PYTHONIOENCODING'], 'utf-8')
            self.assertEqual(output.getvalue(), '显示列表\n')
            control.reload.assert_not_called()

    def test_sentinel_like_text_inside_list_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'fake.json'
            config.write_text('{}', encoding='utf-8')
            output = io.StringIO()
            control = mock.Mock()
            data = 'note:\n__RELOAD__=yes\n__RELOAD__=no\nlast data line\n'
            response = subprocess.CompletedProcess([], 0, (data + '__RELOAD__=no\n').encode())
            with mock.patch.object(dispatch.sys, 'platform', 'win32'), \
                 mock.patch.object(dispatch.subprocess, 'run', return_value=response), \
                 contextlib.redirect_stdout(output):
                self.assertEqual(dispatch.run_helper('strategy-list', [], config, control), 0)
            self.assertEqual(output.getvalue(), data)
            control.reload.assert_not_called()

    def test_readonly_sentinel_requires_a_complete_final_line(self):
        data = '第一行\r\n__RELOAD__=yes\n__RELOAD__=no\n用户数据\u2028仍是数据\r\n'
        cases = (
            ('unterminated', 'data\n__RELOAD__=no', 0, 1, 'data\n'),
            ('cr-only', 'data\n__RELOAD__=no\r', 0, 1, 'data\n'),
            ('lf', 'data\n__RELOAD__=no\n', 0, 0, 'data\n'),
            ('crlf', 'data\r\n__RELOAD__=no\r\n', 0, 0, 'data\r\n'),
            ('multiline-data', data + '__RELOAD__=no\r\n', 0, 0, data),
            ('not-separate-line', 'data__RELOAD__=no\n', 0, 1, 'data__RELOAD__=no\n'),
            ('bare-cr-boundary', 'data\r__RELOAD__=no\n', 0, 1, 'data\r__RELOAD__=no\n'),
            ('unicode-boundary', 'data\u2028__RELOAD__=no\n', 0, 1,
             'data\u2028__RELOAD__=no\n'),
            ('failed-helper', 'data\n__RELOAD__=no\n', 7, 7, 'data\n'),
        )
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'fake.json'
            config.write_text('{}', encoding='utf-8')
            for name, text, child_rc, expected_rc, expected_output in cases:
                with self.subTest(name=name):
                    output, error = io.StringIO(), io.StringIO()
                    control = mock.Mock()
                    response = subprocess.CompletedProcess([], child_rc, text.encode('utf-8'))
                    with mock.patch.object(dispatch.subprocess, 'run', return_value=response), \
                         contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                        result = dispatch.run_helper('route-list', [], config, control)
                    self.assertEqual(result, expected_rc)
                    self.assertEqual(output.getvalue(), expected_output)
                    if child_rc == 0 and expected_rc:
                        self.assertIn('read-only helper result was not confirmed', error.getvalue())
                    control.reload.assert_not_called()

    def test_real_fake_helper_sentinel_framing(self):
        data = '假列表\r\n__RELOAD__=yes\n__RELOAD__=no\n最后一行\n'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / 'fake.json'
            config.write_text('{}', encoding='utf-8')
            helper = root / 'core' / 'cli' / 'helper_entry.py'
            helper.parent.mkdir(parents=True)
            helper.write_text(
                'import sys\n'
                f'data = {data!r}\n'
                "ending = {'none': '', 'lf': '\\n', 'crlf': '\\r\\n'}[sys.argv[3]]\n"
                "sys.stdout.buffer.write((data + '__RELOAD__=no' + ending).encode('utf-8'))\n",
                encoding='utf-8',
            )
            for ending, expected in (('none', 1), ('lf', 0), ('crlf', 0)):
                with self.subTest(ending=ending):
                    output, error = io.StringIO(), io.StringIO()
                    control = mock.Mock()
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                        result = dispatch.run_helper('route-list', [ending], config, control, root=root)
                    self.assertEqual(result, expected)
                    self.assertEqual(output.getvalue(), data)
                    self.assertEqual(config.read_text(encoding='utf-8'), '{}')
                    control.reload.assert_not_called()

    def test_undecodable_helper_output_preserves_failure_status(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'fake.json'
            config.write_text('{}', encoding='utf-8')
            control = mock.Mock()
            for code, expected in ((130, 130), (-2, 130), (0, 1)):
                with self.subTest(code=code), \
                     mock.patch.object(dispatch.subprocess, 'run', return_value=
                                       subprocess.CompletedProcess([], code, b'\xff')), \
                     contextlib.redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(dispatch.run_helper('route-list', [], config, control),
                                     expected)
                    self.assertIn('read-only helper result was not confirmed', error.getvalue())
            control.reload.assert_not_called()

    def test_windows_interactive_menu_with_nonreconfigurable_limited_stream(self):
        class LimitedStream:
            encoding = 'cp1252'

            def __init__(self):
                self.value = ''
                self.flushes = 0

            def write(self, text):
                text.encode(self.encoding)
                self.value += text

            def flush(self):
                self.flushes += 1

        # Exercise menu text, an invalid operation, the edit prompt, usage,
        # and EOF cancellation without reaching a mutating helper.
        for kind in ('supply', 'route', 'strategy'):
            with self.subTest(kind=kind):
                output = LimitedStream()
                control = mock.Mock()
                with mock.patch.object(dispatch.sys, 'platform', 'win32'), \
                     mock.patch.object(dispatch, '_interactive_stdin', return_value=True), \
                     mock.patch.object(dispatch, 'run_helper', return_value=0) as helper, \
                     mock.patch('builtins.input', side_effect=['?', 'e', '', EOFError]), \
                     contextlib.redirect_stdout(output):
                    self.assertEqual(dispatch.menu(kind, 'fake.json', control), 130)
                self.assertIn(r'\u53ef\u9009\u64cd\u4f5c', output.value)
                self.assertIn(r'\u672a\u77e5\u64cd\u4f5c', output.value)
                self.assertIn(r'\u8981\u7f16\u8f91', output.value)
                self.assertIn(r'\u5df2\u53d6\u6d88', output.value)
                self.assertGreaterEqual(output.flushes, 4)
                self.assertTrue(all(call.args[0] == kind + '-list'
                                    for call in helper.call_args_list))
                control.reload.assert_not_called()


if __name__ == '__main__':
    unittest.main()
