"""The two entry points share help without misleading Windows users."""
import contextlib
import io
import unittest
from unittest import mock

from core.cli import dispatch
from core.cli.help_text import HELP, help_for_platform, write_platform_text


class PlatformHelpTests(unittest.TestCase):
    def test_mac_and_other_platforms_keep_legacy_help(self):
        self.assertEqual(help_for_platform('darwin'), HELP)
        self.assertEqual(help_for_platform('linux'), HELP)

    def test_windows_help_names_bat_and_preserves_other_content(self):
        expected = HELP.replace('model_proxy_cli.sh', 'model_proxy_cli.bat', 1)
        self.assertEqual(help_for_platform('win32'), expected)

    def test_windows_dispatch_uses_windows_help_for_all_help_spellings(self):
        for args in ([], ['--help'], ['-h']):
            with self.subTest(args=args), mock.patch.object(dispatch.sys, 'platform', 'win32'):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    result = dispatch.main(args)
                self.assertEqual(result, 0)
                self.assertEqual(output.getvalue(), help_for_platform('win32'))

    def test_windows_help_is_utf8_when_pipe_cannot_encode_chinese(self):
        raw = io.BytesIO()
        output = io.TextIOWrapper(raw, encoding='cp1252', newline='\n')
        with mock.patch.object(dispatch.sys, 'platform', 'win32'), \
             contextlib.redirect_stdout(output):
            self.assertEqual(dispatch.main(['--help']), 0)
            output.flush()
        self.assertEqual(raw.getvalue().decode('utf-8'), help_for_platform('win32'))

    def test_windows_help_falls_back_for_non_reconfigurable_stream(self):
        class LimitedStream:
            encoding = 'ascii'

            def __init__(self):
                self.value = ''

            def write(self, value):
                value.encode(self.encoding)
                self.value += value

        output = LimitedStream()
        with mock.patch.object(dispatch.sys, 'platform', 'win32'), \
             contextlib.redirect_stdout(output):
            self.assertEqual(dispatch.main(['--help']), 0)
        self.assertIn('model_proxy_cli.bat', output.value)
        self.assertIn(r'\u7528\u6cd5', output.value)

    def test_windows_stream_uses_one_encoding_across_multiple_writes(self):
        raw = io.BytesIO()
        output = io.TextIOWrapper(raw, encoding='cp1252', newline='\n')
        write_platform_text('café\n', 'win32', output)
        write_platform_text('中文\n', 'win32', output)
        output.flush()
        self.assertEqual(raw.getvalue().decode('utf-8'), 'café\n中文\n')


if __name__ == '__main__':
    unittest.main()
