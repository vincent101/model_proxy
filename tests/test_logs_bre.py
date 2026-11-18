"""Synthetic log comparisons against macOS BSD grep -G; no service or real log."""
from __future__ import annotations

import contextlib
import io
import os
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.cli.bre import BREError, compile_bre
from core.cli import dispatch


class LogsBRETests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='logs-bre-', dir=os.environ.get('TMPDIR') or tempfile.gettempdir())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / 'synthetic.log'
        self.env = mock.patch.dict(os.environ, {
            'HOME': str(self.root), 'TMPDIR': str(self.root), 'LC_ALL': 'C.UTF-8',
            'MODEL_PROXY_CONFIG': str(self.root / 'nonexistent-config.json'),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.network = mock.patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.lines = [
            '2026-01 req_id=a+ ACCESS INFO event=a+',
            '2026-02 req_id=aaa ACCESS INFO event=aaa',
            '2026-03 req_id=abc ACCESS ERROR event=a?',
            '2026-04 req_id=xyz INFO event=a{2}|b(c)',
            '2026-05 req_id=ab WARNING event=ab',
            '2026-06 req_id=foo+ ERROR event=a^b a$b',
            'x', 'a', 'b', 'aa', 'aaa', 'a$', 'xb', 'a?b',
            'literal [brackets] \\ and *', 'é ١', 'CRLF\r',
        ]
        self.log.write_bytes(('\n'.join(self.lines) + '\n').encode())

    def run_logs(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = dispatch.logs(self.log, list(args))
        return rc, out.getvalue(), err.getvalue()

    def grep(self, pattern, count=30, locale_name='C.UTF-8'):
        if sys.platform != 'darwin' or not Path('/usr/bin/grep').is_file():
            self.skipTest('differential test requires macOS /usr/bin/grep')
        result = subprocess.run(
            ['/usr/bin/grep', '-G', '-e', pattern, str(self.log)],
            capture_output=True, check=False,
            env={'HOME': str(self.root), 'TMPDIR': str(self.root), 'LC_ALL': locale_name},
        )
        self.assertIn(result.returncode, (0, 1), result.stderr)
        # grep output is LF delimited, not splitlines() (which also splits CR).
        lines = result.stdout.split(b'\n')[:-1]
        selected = lines[-count:] if count else []
        return b''.join(line + b'\n' for line in selected).decode('utf-8')

    def assert_grep(self, args, pattern, count=30):
        rc, out, err = self.run_logs(*args)
        self.assertEqual((rc, err), (0, ''), (args, err))
        self.assertEqual(out, self.grep(pattern, count), args)

    def test_field_and_count_argument_behavior(self):
        cases = [
            ([], ' ACCESS ', 30), (['1'], ' ACCESS ', 1),
            (['0'], ' ACCESS ', 0), (['unused', '2', '1'], ' ACCESS ', 1),
            (['req=a+'], 'req_id=a+', 30), (['level=ERROR'], ' ERROR ', 30),
            (['event=a+'], 'a+', 30), (['event=a?', '2'], 'a?', 2),
            (['req=a', 'level=INFO', 'event=ab', '1'], 'ab', 1),
            (['unknown=x', 'event=ab'], 'ab', 30),
            (['event=ab', 'event='], 'ab', 30),
            (['level=ERROR', '7', '0', '2'], ' ERROR ', 2),
            (['9' * 5000, '1'], ' ACCESS ', 1),
        ]
        for args, pattern, n in cases:
            with self.subTest(args=args):
                self.assert_grep(args, pattern, n)
        rc, out, err = self.run_logs('event=a', 'wrong=x')
        self.assertEqual((rc, out, err), (1, 'Unknown filter field: wrong (supported: req/level/event)\n', ''))

    def test_bre_and_bsd_extension_differential(self):
        patterns = [
            'a+', 'a?', '(a)', 'a{2}', 'a|b', 'a^b', 'a$b',
            '.', 'a.*b', '^a', 'a$', '^a$', '*a',
            '[ab]', '[^a]', '[a-z]', '[A-Za-z0-9_]', '[]a]', '[^]]',
            '[-a]', '[a-]', r'[a\-c]', r'[\]]',
            r'a\+', r'a\?', r'a\|b', r'\(a\|b\)', r'\(ab\)\+',
            r'a\{2\}', r'a\{2,3\}', r'a\{0,\}', r'a\{001,002\}', r'\(a\)\{1,2\}',
            r'\.', r'\*', r'\[', r'\]', r'\^', r'\$', r'\\', r'\}',
            r'\(^a\|^b\)', r'a\|^b', r'a$\|b', r'\(a$\|b\)',
        ]
        for pattern in patterns:
            with self.subTest(pattern=pattern):
                self.assert_grep(['event=' + pattern, '100'], pattern, 100)
        # Prefix/suffix change where BRE anchors are special.
        for args, pattern in [(['req=^a'], 'req_id=^a'),
                              (['level=ERROR$'], ' ERROR$ '),
                              ([r'req=\(a\|b\)'], r'req_id=\(a\|b\)')]:
            with self.subTest(args=args):
                self.assert_grep(args, pattern)

    def test_explicit_rejection_not_silent_python_regex(self):
        unsupported = [r'\(a\)\1', r'\w', r'\d', r'\s', r'\b', r'\<a\>',
                       '[[:digit:]]', '[[.a.]]', '[[=a=]]', '[é-ÿ]',
                       r'a\{256\}', r'a\{2,1\}', r'a\{,2\}', r'a\{2',
                       r'\(a', r'a\)', r'a\|', r'\|a', 'a**', '[abc',
                       'a\\', r'\q', 'a\nb', 'a\rb', 'a\0b']
        unsupported.append(r'a\{' + '9' * 5000 + r'\}')
        unsupported.append(r'\(' * 1000 + 'a' + r'\)' * 1000)
        for pattern in unsupported:
            with self.subTest(pattern=pattern):
                with self.assertRaises(BREError):
                    compile_bre(pattern)
                rc, out, err = self.run_logs('event=' + pattern)
                self.assertEqual(rc, 1)
                self.assertEqual(out, '')
                self.assertIn('Error:', err)

    def test_leading_dash_pattern_fails_closed(self):
        rc, out, err = self.run_logs('event=-n')
        self.assertEqual((rc, out), (1, ''))
        self.assertIn('starting with dash', err)
        # Prefixes mean these grep patterns do not begin with a dash.
        self.assert_grep(['req=-x'], 'req_id=-x')
        self.assert_grep(['level=-x'], ' -x ')

    def test_c_locale_matches_grep_byte_semantics(self):
        self.log.write_text('é\néé\n中\na\n', encoding='utf-8')
        for locale_name in ('C', 'C.UTF-8'):
            with mock.patch.dict(os.environ, {'LC_ALL': locale_name}):
                for pattern in ('^.$', '^..$', '^...$', 'é*', '[é]'):
                    with self.subTest(locale=locale_name, pattern=pattern):
                        rc, out, err = self.run_logs('event=' + pattern)
                        self.assertEqual((rc, err), (0, ''))
                        self.assertEqual(out, self.grep(pattern, locale_name=locale_name))
        with mock.patch.dict(os.environ, {'LC_ALL': 'en_US.ISO8859-1'}):
            rc, out, err = self.run_logs('event=.')
            self.assertEqual((rc, out), (1, ''))
            self.assertIn('unsupported', err)

    def test_c_posix_classes_against_all_ascii(self):
        # Each legal ASCII log byte is its own line. NUL is rejected by logs;
        # LF is the line separator. Include UTF-8 multibyte data as well.
        contents = [bytes([c]) for c in range(1, 128) if c != 10]
        contents.extend(s.encode('utf-8') for s in ('é', '١', '中'))
        self.log.write_bytes(b'\n'.join(contents) + b'\n')
        names = ('alnum', 'alpha', 'blank', 'cntrl', 'digit', 'graph',
                 'lower', 'print', 'punct', 'space', 'upper', 'xdigit')
        patterns = ['^[[:' + name + ':]]$' for name in names]
        patterns += ['^[^[:' + name + ':]]$' for name in names]
        patterns += ['^[[:digit:]a-f]$', '^[[:digit:][:alpha:]_]$',
                     '^[-[:digit:]]$', '^[[:digit:]-]$',
                     '^[][:digit:]]$', '^[^][:digit:]]$']
        for locale_name in ('C', 'POSIX'):
            with mock.patch.dict(os.environ, {'LC_ALL': locale_name}):
                for pattern in patterns:
                    with self.subTest(locale=locale_name, pattern=pattern):
                        rc, out, err = self.run_logs('event=' + pattern, '300')
                        self.assertEqual((rc, err), (0, ''))
                        self.assertEqual(out, self.grep(pattern, 300, locale_name))

    def test_c_classes_and_fixed_backrefs_without_grep(self):
        # Portable regression also runs on platforms without macOS grep.
        examples = [
            (r'^[[:digit:]]$', b'5', True),
            (r'^[[:digit:]]$', '١'.encode(), False),
            (r'^[[:space:]]$', b'\x1c', False),
            (r'^[[:space:]]$', b'\t', True),
            (r'^[^[:alpha:]]$', b'5', True),
            (r'^[^[:alpha:]]$', b'A', False),
            (r'^\(.\)\1$', b'aa', True),
            (r'^\(.\)\1$', 'éé'.encode(), False),
            (r'^\(é\)\1$', 'éé'.encode(), True),
            (r'^\(a\)\10$', b'aa0', True),
        ]
        for pattern, subject, expected in examples:
            with self.subTest(pattern=pattern, subject=subject):
                self.assertEqual(bool(compile_bre(pattern, byte_mode=True).search(subject)),
                                 expected)

    def test_c_fixed_width_backrefs_against_grep(self):
        contents = ('aa\naaa\nab\nabab\nababab\nabba\naa0\n11\n22\n'
                    'éé\néaa\n中aa\nééa\naéé\n')
        self.log.write_bytes(contents.encode('utf-8'))
        patterns = [
            r'\(a\)\1', r'\(.\)\1', r'\([0-9]\)\1',
            r'\([[:digit:]]\)\1', r'\(ab\)\1*', r'\(a\)\1\{2\}',
            r'\(a\)\1\+', r'\(a\)\1\?', r'\(a\)\10',
            r'\(a\)\(b\)\2\1', r'.*\(a\)\1$',
            r'\(é\)\1', r'\([é]\)\1', r'\([ab]\)\1\1',
        ]
        for locale_name in ('C', 'POSIX'):
            with mock.patch.dict(os.environ, {'LC_ALL': locale_name}):
                for pattern in patterns:
                    with self.subTest(locale=locale_name, pattern=pattern):
                        rc, out, err = self.run_logs('event=' + pattern, '100')
                        self.assertEqual((rc, err), (0, ''))
                        self.assertEqual(out, self.grep(pattern, 100, locale_name))

    def test_unsupported_backrefs_and_class_syntax_fail_closed_in_c(self):
        unsupported = [
            r'\1', r'\(a\)\2', r'\(a*\)\1', r'\(a\)*\1',
            r'\(a\)\?\1', r'\(a\)\{1\}\1', r'\(a\|ab\)\1',
            r'\(a\)\1\|b', r'\(\(a\)\)\1', r'\(a\)\(\1\)',
            r'[[:word:]]', r'[[:digit]]', r'[[:digit:]-a]',
            r'[a-[:digit:]]', r'[[.a.]]', r'[[=a=]]',
        ]
        with mock.patch.dict(os.environ, {'LC_ALL': 'C'}):
            for pattern in unsupported:
                with self.subTest(pattern=pattern):
                    with self.assertRaises(BREError):
                        compile_bre(pattern, byte_mode=True)
                    rc, out, err = self.run_logs('event=' + pattern)
                    self.assertEqual((rc, out), (1, ''))
                    self.assertIn('unsupported', err)

    def test_utf8_classes_and_backrefs_are_rejected_not_python_semantics(self):
        # On this BSD grep, Unicode character-class membership differs from
        # Python's \s; its UTF-8 backreference behavior also differs on éé and
        # on an ASCII capture after a multibyte prefix. Do not emulate it with
        # Python re or extrapolate this observation to other OS versions.
        self.log.write_bytes('\x1c\n\u0085\n\u00a0\n١\néé\néaa\naa\n'.encode())
        for locale_name in ('C.UTF-8', 'en_US.UTF-8', 'zh_CN.UTF-8'):
            with mock.patch.dict(os.environ, {'LC_ALL': locale_name}):
                for pattern in ('[[:digit:]]', '[[:space:]]', '[[:alpha:]]',
                                '[^[:alpha:]]', r'\(.\)\1', r'\(a\)\1'):
                    with self.subTest(locale=locale_name, pattern=pattern):
                        rc, out, err = self.run_logs('event=' + pattern)
                        self.assertEqual((rc, out), (1, ''))
                        self.assertIn('require C/POSIX', err)
                self.assertTrue(re.search(r'\s', '\x1c'))
                self.assertNotIn('\x1c\n', self.grep('[[:space:]]', locale_name=locale_name))
                self.assertNotIn('éé\n', self.grep(r'\(.\)\1', locale_name=locale_name))
                self.assertNotIn('éaa\n', self.grep(r'\(a\)\1', locale_name=locale_name))
                self.assertIn('aa\n', self.grep(r'\(a\)\1', locale_name=locale_name))

    def test_final_unterminated_line_and_crlf(self):
        self.log.write_bytes(b'event=a+\r\nevent=a+')
        self.assert_grep(['event=a+'], 'a+')
        self.assertEqual(self.run_logs('event=a+')[1], 'event=a+\r\nevent=a+\n')

    def test_binary_or_invalid_utf8_is_error(self):
        for data in (b'a+\x00b\n', b'a+\xff\n'):
            with self.subTest(data=data):
                self.log.write_bytes(data)
                rc, out, err = self.run_logs('event=a+')
                self.assertEqual((rc, out), (1, ''))
                self.assertIn('binary', err)

    def test_main_dispatch_uses_only_injected_synthetic_path(self):
        out = io.StringIO()
        with mock.patch.object(dispatch, 'runtime_paths', return_value={'log': self.log}) as paths, \
             contextlib.redirect_stdout(out):
            rc = dispatch.main(['logs', 'event=a+'], root=self.root, control=object())
        paths.assert_called_once_with(self.root)
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), self.grep('a+'))


if __name__ == '__main__':
    unittest.main()
