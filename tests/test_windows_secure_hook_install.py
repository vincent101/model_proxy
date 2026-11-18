"""Fake HOME and fake writer tests; not evidence of native Windows ACLs."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

from core.cli import _install_ops as install
from core import windows_secure_files as secure


class WindowsFakeInstallTests(unittest.TestCase):
    def test_windows_hook_migrates_only_exact_managed_commands(self):
        fake_os = types.SimpleNamespace(name='nt', path=os.path)
        with patch.object(install, 'os', fake_os):
            current = install._expected_hook_command()
            old = 'bash "${CLAUDE_PROJECT_DIR}/tools/model_proxy/hooker/ensure_model_proxy.sh"'
            custom = {'type': 'command', 'command': old + ' && echo custom'}
            entries = [
                {'matcher': 'startup', 'hooks': [
                    {'type': 'command', 'command': old}, custom]},
                {'hooks': [{'type': 'command', 'command': current}]},
            ]
            result = install._normalize_session_start(entries, Path('fake'))
            self.assertEqual(result, [
                {'matcher': 'startup', 'hooks': [
                    {'type': 'command', 'command': current}, custom]},
                {'hooks': [{'type': 'command', 'command': current}]},
            ])
            self.assertEqual(install._normalize_session_start(result, Path('fake')), result)

    def test_duplicate_removal_requires_equivalent_scope_and_hook_options(self):
        for platform in ('nt', 'posix'):
            with self.subTest(platform=platform), patch.object(
                    install, 'os', types.SimpleNamespace(name=platform, path=os.path)):
                cmd = install._expected_hook_command()
                hook = {'type': 'command', 'command': cmd}
                custom = {'type': 'command', 'command': 'unmanaged'}
                slow = dict(hook, timeout=120)
                entries = [
                    {'matcher': 'startup', 'hooks': [hook, custom]},
                    {'hooks': [hook]},
                    {'matcher': 'resume', 'hooks': [hook]},
                    {'matcher': 'startup', 'hooks': [hook, slow]},
                    {'hooks': [hook]},
                ]
                expected = [entries[0], entries[1], entries[2],
                            {'matcher': 'startup', 'hooks': [slow]}]
                result = install._normalize_session_start(entries, Path('fake'))
                self.assertEqual(result, expected)
                self.assertEqual(install._normalize_session_start(result, Path('fake')), result)

    def test_four_clients_and_hook_with_only_fake_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home, vault = root / 'fake home', root / 'fake vault'
            project = vault / 'tools' / 'model_proxy'
            for p in (home / '.claude', home / '.codex', home / '.hermes',
                      home / '.openclaw', vault / '.claude', vault / '.obsidian',
                      project / 'hooker'):
                p.mkdir(parents=True)
            (project / 'hooker' / 'ensure_model_proxy.bat').write_text('fake')
            settings = vault / '.claude' / 'settings.json'
            old_hook = 'bash "${CLAUDE_PROJECT_DIR}/tools/model_proxy/hooker/ensure_model_proxy.sh"'
            unrelated = {'type': 'command', 'command': 'fake-other-command'}
            settings.write_text(json.dumps({'hooks': {'SessionStart': [
                {'hooks': [{'type': 'command', 'command': old_hook}, unrelated]}]}}))
            paths = {name: home / folder / file for name, folder, file in (
                ('claude', '.claude', 'settings.json'),
                ('codex', '.codex', 'config.toml'),
                ('hermes', '.hermes', 'config.yaml'),
                ('openclaw', '.openclaw', 'openclaw.json'))}
            for name, text in (('claude', '{}'), ('codex', ''),
                               ('hermes', 'fake: true\n'), ('openclaw', '{}')):
                paths[name].write_text(text)
            onboarding = home / '.claude.json'
            onboarding.write_text('{}')
            sdk = {name: dict(value, config_path=paths[name])
                   for name, value in install.SDKS.items()}
            writes = []

            class FakeSnapshot:
                def __init__(self, path):
                    self.path = path
                    self.exists = Path(path).exists()
                    self.contents = Path(path).read_bytes() if self.exists else None
                    self._closed = False

                def revalidate(self):
                    if self._closed or (Path(self.path).read_bytes() if self.exists else None) != self.contents:
                        raise secure.SecureSnapshotError('target_changed')

                def close(self):
                    self._closed = True

            def capture(path, **kwargs):
                self.assertTrue(Path(path).is_relative_to(root))
                return FakeSnapshot(path)

            def write(snapshot, data, *, preflight):
                self.assertTrue(preflight())
                snapshot.revalidate()
                Path(snapshot.path).write_bytes(data)
                writes.append(Path(snapshot.path))
                snapshot.close()
                return secure.WriteOutcome('VERIFIED', None, False, 'NONE')

            fake_os = types.SimpleNamespace(name='nt', path=os.path)
            output = io.StringIO()
            with contextlib.ExitStack() as stack:
                for target, value in (
                    (patch.object(install, 'os', fake_os), None),
                    (patch.object(install, '_VAULT_ROOT', vault), None),
                    (patch.object(install, '_CLAUDE_SETTINGS', settings), None),
                    (patch.object(install, '_CLAUDE_ONBOARDING', onboarding), None),
                    (patch.object(install, 'SDKS', sdk), None),
                    (patch.object(Path, 'home', return_value=home), None),
                    (patch.object(install, 'load_config', return_value={}), None),
                    (patch.object(install, 'choose_token', return_value='fake-token'), None),
                    (patch.object(install, 'confirm', return_value=True), None),
                    (patch.object(install.urllib.request, 'urlopen',
                                  side_effect=OSError('fake offline')), None),
                    (patch.object(install, '_install_catalog_asset', return_value=False), None),
                    (patch.object(secure, 'capture_target', side_effect=capture), None),
                    (patch.object(secure, 'write_target', side_effect=write), None),
                    (patch('core.cli.dispatch.write_preflight', return_value=True), None),
                    (contextlib.redirect_stdout(output), None),
                    (contextlib.redirect_stderr(output), None),
                ):
                    stack.enter_context(target)
                self.assertEqual(install._recognize_vault(project), vault)
                result = install.cmd_install(str(project / 'fake-config.json'), '12345',
                                             list(paths), preflight_root=project)
                command = install._expected_hook_command()

            self.assertEqual(result, 0, output.getvalue())
            hooks = json.loads(settings.read_text(encoding='utf-8'))['hooks']['SessionStart'][0]['hooks']
            self.assertEqual(hooks, [{'type': 'command', 'command': command}, unrelated])
            self.assertEqual(json.loads(paths['claude'].read_text(encoding='utf-8'))['env']['ANTHROPIC_AUTH_TOKEN'], 'fake-token')
            self.assertIn('model_proxy', paths['codex'].read_text(encoding='utf-8'))
            self.assertEqual(paths['hermes'].read_text(encoding='utf-8'), 'fake: true\n')
            self.assertEqual(json.loads(paths['openclaw'].read_text(encoding='utf-8'))['models']['providers']['model_proxy']['apiKey'], 'fake-token')
            self.assertIn('hermes', output.getvalue())
            self.assertNotIn('fake-token', output.getvalue())
            self.assertEqual(set(writes), {settings, paths['claude'], onboarding,
                                           paths['codex'], paths['openclaw']})

    @unittest.skipIf(os.name == 'nt', 'fake POSIX shell diagnostic')
    def test_windows_hook_command_quotes_project_path_for_shell(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_cmd = root / 'cmd.exe'
            fake_cmd.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            fake_cmd.chmod(0o700)
            fake_os = types.SimpleNamespace(name='nt', path=os.path)
            with patch.object(install, 'os', fake_os):
                command = install._expected_hook_command()
            env = {'PATH': str(root), 'CLAUDE_PROJECT_DIR': str(root / 'space 中文')}
            result = subprocess.run(['/bin/bash', '-c', command], env=env,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.splitlines(), [
                '/d', '/c', f'"{env["CLAUDE_PROJECT_DIR"]}/tools/model_proxy/hooker/ensure_model_proxy.bat"'])


class WindowsHookSnapshotLifecycleTests(unittest.TestCase):
    """Exercise cmd_install with fake HOME, file snapshots and no real services."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home, self.vault = self.root / 'fake-home', self.root / 'fake-vault'
        self.project = self.vault / 'tools' / 'model_proxy'
        self.settings = self.vault / '.claude' / 'settings.json'
        self.client = self.home / '.openclaw' / 'openclaw.json'
        self.settings.parent.mkdir(parents=True)
        self.client.parent.mkdir(parents=True)
        self.client.write_text('{}', encoding='utf-8')
        self.snapshots, self.writes = [], []
        self.fail_close = self.fail_revalidate = False
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        fake_os = types.SimpleNamespace(name='nt', path=os.path)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for ctx in (
            patch.object(install, 'os', fake_os),
            patch.object(install, '_VAULT_ROOT', self.vault),
            patch.object(install, '_CLAUDE_SETTINGS', self.settings),
            patch.object(Path, 'home', return_value=self.home),
            patch.dict(install.SDKS['openclaw'], {'config_path': self.client}),
            patch.object(install, 'load_config', return_value={}),
            patch.object(install, 'choose_token', return_value='fake-token'),
            patch.object(install, 'confirm', return_value=True),
            patch.object(secure, 'capture_target', side_effect=self.capture),
            patch.object(secure, 'write_target', side_effect=self.write),
            patch('core.cli.dispatch.write_preflight', return_value=True),
            contextlib.redirect_stdout(self.stdout),
            contextlib.redirect_stderr(self.stderr),
        ):
            stack.enter_context(ctx)

    def capture(self, path, **kwargs):
        owner = self
        path = Path(path)
        self.assertTrue(path.is_relative_to(self.root))

        class Snapshot:
            def __init__(self):
                self.path = str(path)
                self.exists = path.exists()
                self.contents = path.read_bytes() if self.exists else None
                self._closed = False
                self.closes = self.revalidations = 0

            def revalidate(self):
                self.revalidations += 1
                if path == owner.settings and owner.fail_revalidate:
                    raise secure.SecureSnapshotError('target_changed')
                current = path.read_bytes() if path.exists() else None
                if self._closed or current != self.contents:
                    raise secure.SecureSnapshotError('target_changed')

            def close(self):
                self.closes += 1
                self._closed = True
                if path == owner.settings and owner.fail_close:
                    raise secure.SecureSnapshotError('release_unknown')

        snap = Snapshot()
        self.snapshots.append(snap)
        return snap

    def write(self, snap, data, *, preflight):
        self.assertTrue(preflight())
        snap.revalidate()
        Path(snap.path).write_bytes(data)
        self.writes.append(Path(snap.path))
        snap.close()
        return secure.WriteOutcome('VERIFIED', None, False, 'NONE')

    def run_install(self):
        return install.cmd_install(str(self.project / 'fake-config.json'), '12345',
                                   ['openclaw'], preflight_root=self.project)

    def set_matching_hook(self):
        self.settings.write_text(json.dumps({'hooks': {'SessionStart': [
            {'hooks': [{'type': 'command', 'command': install._expected_hook_command()}]},
        ]}}), encoding='utf-8')

    def test_missing_settings_revalidated_and_released_before_client(self):
        self.assertEqual(self.run_install(), 0)
        self.assertFalse(self.settings.exists())
        self.assertEqual([Path(s.path) for s in self.snapshots], [self.settings, self.client])
        self.assertEqual((self.snapshots[0].revalidations, self.snapshots[0].closes), (1, 1))
        self.assertEqual(self.writes, [self.client])

    def test_repeated_install_releases_unchanged_hook_before_each_client(self):
        self.set_matching_hook()
        original = self.settings.read_bytes()
        self.assertEqual(self.run_install(), 0)
        self.assertEqual(self.run_install(), 0)
        self.assertEqual([Path(s.path) for s in self.snapshots],
                         [self.settings, self.client, self.settings, self.client])
        for snap in (self.snapshots[0], self.snapshots[2]):
            self.assertEqual((snap.revalidations, snap.closes), (1, 1))
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertEqual(self.writes, [self.client])

    def test_close_unknown_stops_batch_on_both_read_only_paths(self):
        for exists in (False, True):
            with self.subTest(settings_exists=exists):
                if exists:
                    self.set_matching_hook()
                self.fail_close = True
                start = len(self.snapshots)
                self.assertEqual(self.run_install(), 1)
                captured = self.snapshots[start:]
                self.assertEqual([Path(s.path) for s in captured], [self.settings])
                self.assertEqual((captured[0].revalidations, captured[0].closes), (1, 1))
                self.assertEqual(self.writes, [])
                self.assertIn('快照释放状态未知', self.stderr.getvalue())

    def test_failed_revalidation_still_closes_and_stops_batch(self):
        self.fail_revalidate = True
        for exists in (False, True):
            with self.subTest(settings_exists=exists):
                if exists:
                    self.set_matching_hook()
                start = len(self.snapshots)
                self.assertEqual(self.run_install(), 1)
                captured = self.snapshots[start:]
                self.assertEqual([Path(s.path) for s in captured], [self.settings])
                self.assertEqual((captured[0].revalidations, captured[0].closes), (1, 1))
                self.assertEqual(self.writes, [])

    def test_percent_vault_warns_skips_hook_and_still_installs_client(self):
        vault = self.root / 'fake-vault-%FAKE%'
        settings = vault / '.claude' / 'settings.json'
        settings.parent.mkdir(parents=True)
        settings.write_text('{"fake":true}', encoding='utf-8')
        output = io.StringIO()
        with patch.object(install, '_VAULT_ROOT', vault), \
             patch.object(install, '_CLAUDE_SETTINGS', settings), \
             contextlib.redirect_stdout(output):
            self.assertEqual(self.run_install(), 0)
        self.assertIn('跳过自动配置', output.getvalue())
        self.assertIn('已有 hook 不会修改', output.getvalue())
        self.assertEqual(settings.read_text(encoding='utf-8'), '{"fake":true}')
        self.assertEqual([Path(s.path) for s in self.snapshots], [self.client])
        self.assertEqual(self.writes, [self.client])

    def test_percent_vault_still_rejects_an_unrelated_hook_path(self):
        with patch.object(install, '_VAULT_ROOT', self.root / 'fake-%NAME%'):
            with self.assertRaises(install.InstallFailure):
                install.ensure_session_hook(self.client)
        self.assertEqual(self.snapshots, [])


if __name__ == '__main__':
    unittest.main()
