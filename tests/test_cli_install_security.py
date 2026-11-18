"""Install security tests. This module refuses to run outside an isolated copy."""
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ISOLATION = Path(os.environ.get('MODEL_PROXY_TEST_ISOLATION', '/invalid')).resolve()
if ROOT != ISOLATION / 'project' or not (ISOLATION / '.model-proxy-isolation').is_file():
    raise RuntimeError('isolated test project required')
if not Path(os.environ.get('HOME', '/invalid')).is_relative_to(ISOLATION):
    raise RuntimeError('isolated HOME required')

from core import secure_files as sf
from core.cli import _install_ops as original_install

@unittest.skipUnless(sys.platform == 'darwin', 'native macOS ACL verification required')
class InstallSecurity(unittest.TestCase):
    def setUp(self):
        self.case = Path(tempfile.mkdtemp(prefix='case-', dir=ISOLATION / 'cases'))
        self.home = self.case / 'home'
        self.home.mkdir(mode=0o700)
        self.env = mock.patch.dict(os.environ, {'HOME': str(self.home), 'USERPROFILE': str(self.home),
                                               'LOCALAPPDATA': str(self.case / 'appdata')})
        self.env.start()
        self.install = importlib.reload(original_install)
        self.config = self.case / 'proxy-config.json'
        self.config.write_text(json.dumps({'strategies': [], 'routes': [], 'supplies': []}))
        self.config.chmod(0o600)
        self.project = self.case / 'project'
        (self.project / 'config').mkdir(parents=True)
        (self.project / 'runtime').mkdir()
        (self.project / 'config' / 'runtime_paths.json').write_text(json.dumps({
            key: str(self.project / 'runtime' / key)
            for key in ('log', 'totals', 'lock', 'pid', 'ensure_log', 'start_lock')
        }))
        self.context_token = self.install._WRITE_CONTEXT.set(
            {'root': self.project, 'config': self.config, 'targets': []})
        self.url = mock.patch('urllib.request.urlopen', side_effect=OSError('fake offline; network prohibited'))
        self.url.start()

    def tearDown(self):
        self.install._WRITE_CONTEXT.reset(self.context_token)
        self.url.stop()
        self.env.stop()
        shutil.rmtree(self.case)

    def file(self, name, text, mode=0o600):
        path = self.home / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        path.chmod(mode)
        return path

    def snap(self, path):
        return sf.capture_target(path, authorized_root=self.home,
                                 protection_reader=sf.mac_private_protection, max_bytes=1024 * 1024)

    def toml_parser(self):
        try:
            import tomllib as parser
        except ImportError:
            try:
                import tomli as parser
            except ImportError:
                self.skipTest('TOML parser unavailable; no-parser behavior is tested separately')
        return parser

    def test_actual_existing_and_new_file_safe_commit_and_backup(self):
        path = self.file('.claude/settings.json', '{"old":1}')
        before = self.snap(path)
        outcome = sf.secure_replace(before, b'{"new":2}', max_bytes=1024 * 1024)
        self.assertEqual(outcome.state, 'VERIFIED')
        self.assertEqual(path.read_bytes(), b'{"new":2}')
        self.assertEqual(Path(outcome.backup).read_bytes(), b'{"old":1}')
        self.assertEqual(stat.S_IMODE(Path(outcome.backup).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        new = self.home / '.claude' / 'new.json'
        outcome = sf.secure_replace(self.snap(new), b'FAKE', max_bytes=1024 * 1024)
        self.assertEqual(outcome.state, 'VERIFIED')
        self.assertEqual(stat.S_IMODE(new.stat().st_mode), 0o600)
        self.assertEqual(new.read_bytes(), b'FAKE')

    def test_reject_symlink_hardlink_and_unsafe_mode(self):
        path = self.file('client/config.json', 'FAKE_ORIGINAL')
        alias = path.parent / 'alias'
        alias.symlink_to(path)
        with self.assertRaises(sf.SecureSnapshotError): self.snap(alias)
        dangling = path.parent / 'dangling'
        dangling.symlink_to(path.parent / 'not-there')
        with self.assertRaises(sf.SecureSnapshotError): self.snap(dangling)
        linked = path.parent / 'hardlink'
        os.link(path, linked)
        with self.assertRaises(sf.SecureSnapshotError): self.snap(path)
        linked.unlink()
        path.chmod(0o644)
        with self.assertRaises(sf.SecureSnapshotError): self.snap(path)
        self.assertEqual(path.read_text(), 'FAKE_ORIGINAL')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_client_target_outside_authorized_home_is_rejected_before_capture(self):
        outside = self.case / 'unrelated' / 'config.json'
        outside.parent.mkdir(mode=0o700)
        outside.write_text('FAKE_UNRELATED')
        outside.chmod(0o600)
        with mock.patch.object(self.install, 'capture_target') as capture:
            with self.assertRaises(self.install.InstallFailure):
                self.install._snapshot_text(outside)
            capture.assert_not_called()
        self.assertEqual(outside.read_text(), 'FAKE_UNRELATED')
        self.assertEqual(list(outside.parent.glob('*.bak.*')), [])

    def test_claude_install_rejects_public_existing_file_without_changes(self):
        target = self.file('.claude/settings.json', '{"env":{}}', mode=0o644)
        before = target.read_bytes()
        with mock.patch.object(self.install, 'confirm') as confirm:
            with self.assertRaises(self.install.InstallFailure):
                self.install.install_claude('FAKE_SENSITIVE_TOKEN', '28371')
            confirm.assert_not_called()
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        self.assertEqual(list(target.parent.glob('*.bak.*')), [])

    def test_real_extended_acl_rejected(self):
        path = self.file('client/acl.json', 'FAKE')
        proc = subprocess.run(['/bin/chmod', '+a', 'everyone allow read', str(path)],
                              capture_output=True, text=True)
        if proc.returncode:
            self.skipTest('native filesystem could not create a test ACL')
        with self.assertRaises(sf.SecureSnapshotError): self.snap(path)
        self.assertEqual(path.read_text(), 'FAKE')

    def test_change_after_confirmation_refuses_without_backup(self):
        path = self.file('client/config.json', 'FAKE_OLD')
        snap, old = self.install._snapshot_text(path)
        def change(*_):
            path.write_text('FAKE_RACING_CHANGE')
            return True
        with mock.patch.object(self.install, 'confirm', side_effect=change), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(self.install.InstallFailure):
                self.install.preview_confirm_write(path, old, 'FAKE_NEW', 'test', [], snapshot=snap)
        self.assertEqual(path.read_text(), 'FAKE_RACING_CHANGE')
        self.assertEqual(list(path.parent.glob('*.bak.*')), [])

    def test_change_after_backup_does_not_replace_and_backup_matches_snapshot(self):
        path = self.file('client/config.json', 'FAKE_OLD')
        snap = self.snap(path)
        original = sf.target_unchanged
        calls = 0
        def change_after_backup(*args, **kw):
            nonlocal calls
            calls += 1
            if calls == 2:
                path.write_text('FAKE_RACE')
            return original(*args, **kw)
        with mock.patch.object(sf, 'target_unchanged', side_effect=change_after_backup):
            with self.assertRaises(sf.SecureWriteError) as e:
                sf.secure_replace(snap, b'FAKE_NEW', max_bytes=1024 * 1024)
        self.assertEqual(e.exception.state, 'NOT_REPLACED')
        self.assertEqual(path.read_text(), 'FAKE_RACE')
        self.assertEqual(Path(e.exception.backup).read_text(), 'FAKE_OLD')

    def test_create_never_overwrites_concurrent_file(self):
        path = self.home / 'client' / 'new'
        path.parent.mkdir(mode=0o700)
        snap = self.snap(path)
        original = os.link
        def raced(src, dest, **kw):
            path.write_text('FAKE_OTHER')
            return original(src, dest, **kw)
        with mock.patch('os.link', side_effect=raced):
            with self.assertRaises(sf.SecureWriteError) as e:
                sf.secure_replace(snap, b'FAKE_NEW', max_bytes=1024 * 1024)
        self.assertEqual(e.exception.state, 'NOT_REPLACED')
        self.assertEqual(path.read_text(), 'FAKE_OTHER')

    def test_parent_symlink_is_not_followed(self):
        target = self.file('real/config.json', 'FAKE_CONTENT')
        alias = self.home / 'alias'
        alias.symlink_to(target.parent, target_is_directory=True)
        with self.assertRaises(sf.SecureSnapshotError):
            self.snap(alias / 'config.json')
        self.assertEqual(target.read_text(), 'FAKE_CONTENT')

    def test_codex_confirmation_shows_field_names_not_values_and_cancel_is_safe(self):
        self.toml_parser()
        original = ('model = "FAKE_OLD_SECRET" # keep\n[profiles.work]\n'
                    'model = "FAKE_PROFILE_SECRET"\n')
        path = self.file('.codex/config.toml', original)
        out = io.StringIO()
        with mock.patch.object(self.install, 'confirm', return_value=False), \
             mock.patch.object(self.install, '_install_catalog_asset') as catalog, \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            self.install.install_codex('FAKE_NEW_SECRET', '28371')
        catalog.assert_not_called()
        self.assertEqual(path.read_text(), original)
        self.assertEqual(list(path.parent.glob('*.bak.*')), [])
        self.assertIn('model: 更新（值隐藏）', out.getvalue())
        self.assertIn('model_providers.model_proxy.experimental_bearer_token: 更新（值隐藏）', out.getvalue())
        for secret in ('FAKE_OLD_SECRET', 'FAKE_PROFILE_SECRET', 'FAKE_NEW_SECRET'):
            self.assertNotIn(secret, out.getvalue())

    def test_unknown_summary_field_never_echoes_raw_value(self):
        path = self.file('.codex/config.toml', 'model = "old"\n')
        out = io.StringIO()
        with mock.patch.object(self.install, 'confirm', return_value=False), contextlib.redirect_stdout(out):
            self.assertFalse(self.install.preview_confirm_write(
                path, 'model = "old"\n', 'model = "new"\n', 'codex', [],
                field_changes=['FAKE_SECRET_DO_NOT_PRINT']))
        self.assertIn('配置字段更新（值隐藏）', out.getvalue())
        self.assertNotIn('FAKE_SECRET_DO_NOT_PRINT', out.getvalue())
        self.assertEqual(path.read_text(), 'model = "old"\n')

    def test_codex_profile_first_gets_root_fields_without_changing_profile(self):
        tomllib = self.toml_parser()
        original = ('# heading retained\n[profiles.work] # profile comment\n'
                    'model = "profile-model" # keep model comment\n'
                    'model_provider = "corp-provider" # keep provider comment\n')
        path = self.file('.codex/config.toml', original)
        output = io.StringIO()
        with mock.patch.object(self.install, 'confirm', return_value=True), \
             mock.patch.object(self.install, '_install_catalog_asset', return_value=False), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.install.install_codex('FAKE_PRIVATE_CREDENTIAL', '28371')
        actual = path.read_text()
        parsed = tomllib.loads(actual)
        self.assertEqual(parsed['model'], 'claude-sonnet')
        self.assertEqual(parsed['model_provider'], 'model_proxy')
        self.assertEqual(parsed['profiles']['work'], tomllib.loads(original)['profiles']['work'])
        self.assertTrue(actual.startswith('# heading retained\n'))
        self.assertIn(original.split('\n', 1)[1], actual)
        self.assertIn('model_providers.model_proxy.experimental_bearer_token', output.getvalue())
        self.assertIn('model_provider: 更新', output.getvalue())
        self.assertNotIn('FAKE_PRIVATE_CREDENTIAL', output.getvalue())
        self.assertNotIn('corp-provider', output.getvalue())

    def test_codex_root_and_quoted_provider_fields_preserve_comments_and_profiles(self):
        tomllib = self.toml_parser()
        original = ('"model" = "old-root" # root model\n'
                    "'model_provider' = 'old-provider' # root provider\n"
                    '[profiles.work]\nmodel = "work-model"\nmodel_provider = "work-provider"\n'
                    '[model_providers."model_proxy"] # managed table\n'
                    'base_url = "http://old" # url comment\n'
                    "wire_api = 'chat' # api comment\n"
                    'experimental_bearer_token = "FAKE_OLD_SECRET" # token comment\n'
                    'custom = "keep this" # custom comment\n'
                    '[model_providers.model_proxy.limits]\nmodel = "nested-model"\n')
        path = self.file('.codex/config.toml', original)
        out = io.StringIO()
        with mock.patch.object(self.install, 'confirm', return_value=True), \
             mock.patch.object(self.install, '_install_catalog_asset', return_value=False), \
             contextlib.redirect_stdout(out):
            self.install.install_codex('FAKE_NEW_SECRET', '28371')
        text = path.read_text()
        after = tomllib.loads(text)
        self.assertEqual(after['model'], 'claude-sonnet')
        self.assertEqual(after['model_provider'], 'model_proxy')
        self.assertEqual(after['profiles'], tomllib.loads(original)['profiles'])
        self.assertEqual(after['model_providers']['model_proxy']['custom'], 'keep this')
        self.assertEqual(after['model_providers']['model_proxy']['limits']['model'], 'nested-model')
        self.assertEqual(after['model_providers']['model_proxy']['experimental_bearer_token'], 'FAKE_NEW_SECRET')
        for comment in ('# root model', '# root provider', '# managed table', '# url comment',
                        '# api comment', '# token comment', '# custom comment'):
            self.assertIn(comment, text)
        self.assertNotIn('FAKE_OLD_SECRET', out.getvalue())
        self.assertNotIn('FAKE_NEW_SECRET', out.getvalue())
        self.assertNotIn('work-provider', out.getvalue())

    def test_codex_multiline_arrays_and_inline_profiles_are_not_rewritten(self):
        tomllib = self.toml_parser()
        original = ('# string contains fake headers and assignments\n'
                    'notice = """first line\n[profiles.fake]\nmodel = \"not-a-key\"\nlast"""\n'
                    'models = [\n  "string # not comment", # array comment\n'
                    '  { label = "[profile]", values = [1, 2, 3] },\n]\n'
                    'profiles = { work = { model = "inline-profile", model_provider = "corp" } }\n'
                    'model = "root-old" # root value\n')
        before = tomllib.loads(original)
        path = self.file('.codex/config.toml', original)
        with mock.patch.object(self.install, 'confirm', return_value=True), \
             mock.patch.object(self.install, '_install_catalog_asset', return_value=False), \
             contextlib.redirect_stdout(io.StringIO()):
            self.install.install_codex('FAKE_NEW_SECRET', '28371')
        text = path.read_text()
        after = tomllib.loads(text)
        self.assertEqual(after['notice'], before['notice'])
        self.assertEqual(after['models'], before['models'])
        self.assertEqual(after['profiles'], before['profiles'])
        self.assertEqual(after['model'], 'claude-sonnet')
        self.assertEqual(after['model_provider'], 'model_proxy')
        self.assertIn('# array comment', text)
        self.assertIn('model = "not-a-key"', text)
        self.assertIn('# root value', text)

    def test_codex_preserves_crlf_when_adding_root_and_provider_fields(self):
        tomllib = self.toml_parser()
        original = '# heading\r\n[profiles.work]\r\nmodel = "work" # preserved\r\n'
        path = self.file('.codex/config.toml', original)
        with mock.patch.object(self.install, 'confirm', return_value=True), \
             mock.patch.object(self.install, '_install_catalog_asset', return_value=False), \
             contextlib.redirect_stdout(io.StringIO()):
            self.install.install_codex('FAKE_SECRET', '28371')
        result = path.read_bytes().decode('utf-8')
        self.assertEqual(result.count('\n'), result.count('\r\n'))
        self.assertIn('[profiles.work]\r\nmodel = "work" # preserved\r\n', result)
        self.assertEqual(tomllib.loads(result)['model'], 'claude-sonnet')
        self.assertEqual(tomllib.loads(result)['profiles']['work']['model'], 'work')

    def test_codex_escaped_key_and_literal_multiline_are_preserved(self):
        tomllib = self.toml_parser()
        original = ('"mo\\u0064el" = "old-root" # escaped key\n'
                    "memo = '''literal\n[profiles.fake]\nmodel = \"ignored\"\n'''\n"
                    '[profiles.work]\nmodel = "work-value" # profile\n')
        before = tomllib.loads(original)
        path = self.file('.codex/config.toml', original)
        with mock.patch.object(self.install, 'confirm', return_value=True), \
             mock.patch.object(self.install, '_install_catalog_asset', return_value=False), \
             contextlib.redirect_stdout(io.StringIO()):
            self.install.install_codex('FAKE_NEW_SECRET', '28371')
        text = path.read_text()
        after = tomllib.loads(text)
        self.assertEqual(after['model'], 'claude-sonnet')
        self.assertEqual(after['memo'], before['memo'])
        self.assertEqual(after['profiles'], before['profiles'])
        self.assertIn('"mo\\u0064el" = "claude-sonnet" # escaped key', text)
        self.assertIn('# profile', text)

    def test_codex_semantic_crosscheck_rejects_wrong_span_before_write(self):
        self.toml_parser()
        original = ('model = "old-root"\n[profiles.work]\nmodel = "profile"\n')
        path = self.file('.codex/config.toml', original)
        before = path.read_bytes()
        real_scan = self.install._toml_statements
        def wrong_span(text):
            records = real_scan(text)
            root = next(x for x in records if x['kind'] == 'assignment' and x['table'] == () and x['key'] == ('model',))
            profile = next(x for x in records if x['kind'] == 'assignment' and x['table'] == ('profiles', 'work'))
            root['value_start'], root['value_end'] = profile['value_start'], profile['value_end']
            return records
        with mock.patch.object(self.install, '_toml_statements', side_effect=wrong_span), \
             mock.patch.object(self.install, 'confirm') as confirm:
            with self.assertRaises(self.install.InstallFailure):
                self.install.install_codex('FAKE_SECRET', '28371')
        confirm.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.glob('*.bak.*')), [])

    def test_codex_managed_multiline_value_keeps_adjacent_comment_and_other_profile(self):
        tomllib = self.toml_parser()
        original = ('[profiles.work]\nmodel = "profile"\n'
                    '[model_providers.model_proxy]\n'
                    'experimental_bearer_token = """old\n[not-a-table]\nsecret""" # keep comment\n'
                    'other = [1, 2, { x = "y#z" }] # preserve array\n')
        before = tomllib.loads(original)
        path = self.file('.codex/config.toml', original)
        with mock.patch.object(self.install, 'confirm', return_value=True), \
             mock.patch.object(self.install, '_install_catalog_asset', return_value=False), \
             contextlib.redirect_stdout(io.StringIO()):
            self.install.install_codex('FAKE_NEW_SECRET', '28371')
        text = path.read_text()
        after = tomllib.loads(text)
        self.assertEqual(after['profiles'], before['profiles'])
        self.assertEqual(after['model_providers']['model_proxy']['other'],
                         before['model_providers']['model_proxy']['other'])
        self.assertEqual(after['model_providers']['model_proxy']['experimental_bearer_token'], 'FAKE_NEW_SECRET')
        self.assertIn('# keep comment', text)
        self.assertIn('# preserve array', text)

    def test_codex_catalog_top_level_does_not_change_profile_key(self):
        tomllib = self.toml_parser()
        original = ('[profiles.work]\nmodel = "work"\n'
                    'model_catalog_json = "profile-catalog" # preserve\n')
        path = self.file('.codex/config.toml', original)
        with mock.patch.object(self.install, 'confirm', return_value=True), \
             mock.patch.object(self.install, '_install_catalog_asset', return_value=True), \
             contextlib.redirect_stdout(io.StringIO()):
            self.install.install_codex('FAKE_NEW_SECRET', '28371')
        after = tomllib.loads(path.read_text())
        self.assertEqual(after['profiles']['work'], tomllib.loads(original)['profiles']['work'])
        self.assertEqual(after['model_catalog_json'], '~/.codex/model-catalogs/model_proxy_catalog.json')
        self.assertIn('# preserve', path.read_text())

    def test_codex_uncertain_toml_fails_before_confirmation_or_writes(self):
        self.toml_parser()
        samples = [
            'model = [ "unterminated"\n',
            'model = 123\n[profiles.work]\nmodel = "work"\n',
            'model_catalog_json = ["unexpected"]\n[profiles.work]\nmodel = "work"\n',
            'model_providers = { model_proxy = { base_url = "old" } }\n',
            '[[model_providers.model_proxy]]\nbase_url = "old"\n',
        ]
        for original in samples:
            with self.subTest(original=original):
                path = self.file('.codex/config.toml', original)
                before = path.read_bytes()
                out = io.StringIO()
                with mock.patch.object(self.install, 'confirm') as confirm, \
                     mock.patch.object(self.install, '_install_catalog_asset') as catalog, \
                     contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    with self.assertRaises(self.install.InstallFailure):
                        self.install.install_codex('FAKE_PRIVATE_CREDENTIAL', '28371')
                confirm.assert_not_called()
                catalog.assert_not_called()
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(list(path.parent.glob('*.bak.*')), [])
                self.assertNotIn('FAKE_PRIVATE_CREDENTIAL', out.getvalue())

    def test_codex_without_either_toml_parser_fails_closed(self):
        path = self.file('.codex/config.toml', 'model = "old"\n')
        before = path.read_bytes()
        with mock.patch.dict(sys.modules, {'tomllib': None, 'tomli': None}), \
             mock.patch.object(self.install, 'confirm') as confirm:
            with self.assertRaises(self.install.InstallFailure) as caught:
                self.install.install_codex('FAKE_PRIVATE_CREDENTIAL', '28371')
        self.assertIn('3.11', str(caught.exception))
        confirm.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.glob('*.bak.*')), [])

    def test_toml_backport_is_used_when_stdlib_parser_is_missing(self):
        import types
        backport = types.ModuleType('tomli')
        backport.loads = mock.Mock(return_value={'model': 'fake'})
        with mock.patch.dict(sys.modules, {'tomllib': None, 'tomli': backport}):
            self.assertEqual(self.install._toml_load('model = "fake"'), {'model': 'fake'})
        backport.loads.assert_called_once_with('model = "fake"')

    def test_candidate_routes_distinguishable_without_any_secrets_or_notes(self):
        cfg = {'admin_token': 'FAKE_ADMIN_K92',
               'supplies': [{'id':'s', 'protocol':'anthropic', 'appkey':'FAKE_APPKEY_Z42',
                             'extra_headers': {'X-Secret':'FAKE_HEADER_R36'}}],
               'routes': [{'id':'blue-lane', 'tiers':{'opus':['s']}},
                          {'id':'green-lane', 'tiers':{'opus':['s']}}],
               'strategies': [{'client_token':'FAKE_CLIENT_Q71', 'route_id':'blue-lane',
                               'note':'FAKE_APPKEY_Z42'},
                              {'client_token':'FAKE_CLIENT_P63', 'route_id':'green-lane',
                               'note':'FAKE_CLIENT_Q71'}]}
        output = io.StringIO()
        with mock.patch('builtins.input', return_value='1'), contextlib.redirect_stdout(output):
            chosen = self.install.choose_token(cfg, 'anthropic', 'claude')
        self.assertEqual(chosen, 'FAKE_CLIENT_P63')
        self.assertIn('blue-lane', output.getvalue())
        self.assertIn('green-lane', output.getvalue())
        for secret in ('FAKE_ADMIN_K92','FAKE_APPKEY_Z42','FAKE_HEADER_R36',
                       'FAKE_CLIENT_Q71','FAKE_CLIENT_P63'):
            self.assertNotIn(secret, output.getvalue())

    def test_filtered_strategy_and_unrelated_supply_secrets_are_also_redacted(self):
        filtered = 'FILTERED_SECRET_Z73'
        unused_key = 'UNUSED_KEY_P6L9'
        cfg = {'supplies': [{'id':'s1', 'protocol':'anthropic', 'appkey':'SAFE_KEY'},
                            {'id':'s2', 'protocol':'responses', 'appkey':unused_key}],
               'routes': [{'id':'route-' + filtered, 'tiers':{'opus':['s1']}},
                          {'id':'route-' + unused_key, 'tiers':{'opus':['s1']}},
                          {'id':'filtered-route', 'tiers':{'opus':['s2']}}],
               'strategies': [{'client_token':'CANDIDATE_ONE_ABC', 'route_id':'route-' + filtered},
                              {'client_token':'CANDIDATE_TWO_DEF', 'route_id':'route-' + unused_key},
                              {'client_token':filtered, 'route_id':'filtered-route'}]}
        output = io.StringIO()
        with mock.patch('builtins.input', side_effect=['0', 'yes']), contextlib.redirect_stdout(output):
            result = self.install.choose_token(cfg, 'anthropic', 'claude')
        self.assertEqual(result, 'CANDIDATE_ONE_ABC')
        self.assertEqual(output.getvalue().count('<线路信息已隐藏>'), 2)
        for secret in (filtered, unused_key, 'CANDIDATE_ONE_ABC', 'CANDIDATE_TWO_DEF', 'Z73', 'P6L9'):
            self.assertNotIn(secret, output.getvalue())

    def test_supply_url_credentials_do_not_leak_via_route_labels(self):
        password = 'URL_PASSWORD_Z73'
        query_key = 'QUERY_SECRET_P6L9'
        cfg = {'supplies': [{'id':'s', 'protocol':'anthropic',
                             'url':'https://fake-user:' + password + '@example.invalid/v1/messages?api_key=' + query_key}],
               'routes': [{'id':'route-' + password, 'tiers':{'opus':['s']}},
                          {'id':'route-' + query_key, 'tiers':{'opus':['s']}}],
               'strategies': [{'client_token':'CLIENT_Q17', 'route_id':'route-' + password},
                              {'client_token':'CLIENT_Z73', 'route_id':'route-' + query_key}]}
        output = io.StringIO()
        with mock.patch('builtins.input', side_effect=['0', 'yes']), contextlib.redirect_stdout(output):
            self.assertEqual(self.install.choose_token(cfg, 'anthropic', 'claude'), 'CLIENT_Q17')
        self.assertEqual(output.getvalue().count('<线路信息已隐藏>'), 2)
        for secret in (password, query_key, 'Z73', 'P6L9'):
            self.assertNotIn(secret, output.getvalue())

    def test_unparseable_url_secret_hides_route_details(self):
        cfg = {'supplies': [{'id':'s', 'protocol':'anthropic',
                             'url':'https://example.invalid/v1/messages?api_key=%FF'}],
               'routes': [{'id':'blue-lane', 'tiers':{'opus':['s']}},
                          {'id':'green-lane', 'tiers':{'opus':['s']}}],
               'strategies': [{'client_token':'CLIENT_Q17', 'route_id':'blue-lane'},
                              {'client_token':'CLIENT_Z73', 'route_id':'green-lane'}]}
        output = io.StringIO()
        with mock.patch('builtins.input', side_effect=['0', 'yes']), contextlib.redirect_stdout(output):
            self.assertEqual(self.install.choose_token(cfg, 'anthropic', 'claude'), 'CLIENT_Q17')
        self.assertEqual(output.getvalue().count('<线路信息已隐藏>'), 2)
        self.assertNotIn('blue-lane', output.getvalue())
        self.assertNotIn('green-lane', output.getvalue())

    def test_route_with_credential_fragment_is_hidden_whole(self):
        cfg = {'supplies': [{'id':'s', 'protocol':'anthropic', 'appkey':'KEY_ABC987'}],
               'routes': [{'id':'route-ABC987-tail', 'tiers':{'opus':['s']}},
                          {'id':'safe-lane', 'tiers':{'opus':['s']}}],
               'strategies': [{'client_token':'CLIENT_Q17', 'route_id':'route-ABC987-tail'},
                              {'client_token':'CLIENT_Z73', 'route_id':'safe-lane'}]}
        output = io.StringIO()
        with mock.patch('builtins.input', side_effect=['1', 'yes']), contextlib.redirect_stdout(output):
            self.assertEqual(self.install.choose_token(cfg, 'anthropic', 'claude'), 'CLIENT_Z73')
        self.assertIn('<线路信息已隐藏>', output.getvalue())
        self.assertNotIn('ABC987', output.getvalue())
        self.assertNotIn('987', output.getvalue())

    def test_sensitive_route_and_unknown_appkey_are_hidden_and_require_confirmation(self):
        cfg = {'supplies':[{'id':'s','protocol':'anthropic','appkey':'FAKE_APPKEY_P6L9'}],
               'routes':[{'id':'safe-lane','tiers':{'opus':['s']}},
                         {'id':'route-FAKE_CLIENT_Z73','tiers':{'opus':['s']}}],
               'strategies':[{'client_token':'FAKE_CLIENT_Q17','route_id':'safe-lane',
                              'note':'FAKE_APPKEY_P6L9'},
                             {'client_token':'FAKE_CLIENT_Z73','route_id':'route-FAKE_CLIENT_Z73',
                              'note':'FAKE_CLIENT_Q17'}]}
        out = io.StringIO()
        with mock.patch('builtins.input', side_effect=['1','yes']), contextlib.redirect_stdout(out):
            chosen = self.install.choose_token(cfg, 'anthropic', 'claude')
        self.assertEqual(chosen, 'FAKE_CLIENT_Z73')
        self.assertIn('<线路信息已隐藏>', out.getvalue())
        for secret in ('FAKE_CLIENT_Q17','FAKE_CLIENT_Z73','FAKE_APPKEY_P6L9','Z73','P6L9'):
            self.assertNotIn(secret, out.getvalue())
        cfg['supplies'][0]['appkey_file'] = str(self.case / 'never-read-key')
        out = io.StringIO()
        with mock.patch('builtins.input', side_effect=['0','no']), contextlib.redirect_stdout(out):
            self.assertIsNone(self.install.choose_token(cfg, 'anthropic', 'claude'))
        self.assertNotIn('safe-lane', out.getvalue())
        self.assertNotIn(str(self.case), out.getvalue())

    def test_codex_escapes_unusual_token_without_output_leak(self):
        tomllib = self.toml_parser()
        path = self.file('.codex/config.toml', 'model = "old"\n')
        secret = 'FAKE_"quote"_\\_line\nend'
        out = io.StringIO()
        with mock.patch('urllib.request.urlopen', side_effect=OSError('fake offline')) as offline, \
             mock.patch.object(self.install, 'confirm', return_value=True), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            self.install.install_codex(secret, '28371')
        offline.assert_called_once()
        parsed = tomllib.loads(path.read_text())
        self.assertEqual(parsed['model_providers']['model_proxy']['experimental_bearer_token'], secret)
        self.assertNotIn(secret, out.getvalue())
        self.assertNotIn('FAKE_', out.getvalue())

    def test_postcheck_failure_reports_already_replaced_and_backup(self):
        path = self.file('client/config.json', 'FAKE_OLD')
        snap = self.snap(path)
        original = sf.capture_target
        calls = 0
        def fail_post(*args, **kw):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise sf.SecureSnapshotError('synthetic_postcheck')
            return original(*args, **kw)
        with mock.patch.object(sf, 'capture_target', side_effect=fail_post):
            with self.assertRaises(sf.SecureWriteError) as e:
                sf.secure_replace(snap, b'FAKE_NEW', max_bytes=1024 * 1024)
        self.assertEqual(e.exception.state, 'POSTCHECK_FAILED')
        self.assertEqual(path.read_text(), 'FAKE_NEW')
        self.assertEqual(Path(e.exception.backup).read_text(), 'FAKE_OLD')

    def test_install_four_clients_and_redact_all_output(self):
        self.toml_parser()
        claude = self.file('.claude/settings.json', '{"env":{}}')
        codex = self.file('.codex/config.toml', 'model = "old"\n')
        claw = self.file('.openclaw/openclaw.json', '{}')
        self.file('.hermes/config.yaml', 'model: fake\n')
        secret = 'FAKE_TOKEN_do_not_print'
        output = io.StringIO()
        with mock.patch('urllib.request.urlopen', side_effect=OSError('fake offline')) as offline, \
             mock.patch.object(self.install, 'choose_token', return_value=secret), \
             mock.patch.object(self.install, 'confirm', return_value=True), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            rc = self.install.cmd_install(str(self.config), '28371', ['claude', 'codex', 'hermes', 'openclaw'],
                                          preflight_root=self.project)
        offline.assert_called_once()
        self.assertEqual(rc, 0, output.getvalue())
        self.assertNotIn(secret, output.getvalue())
        self.assertIn(secret, claude.read_text())
        self.assertIn(secret, codex.read_text())
        self.assertIn(secret, claw.read_text())
        self.assertTrue(json.loads((self.home / '.claude.json').read_text())['hasCompletedOnboarding'])
        self.assertIn('<CLIENT_TOKEN>', output.getvalue())  # Hermes remains manual by design.
        self.assertIn('env.ANTHROPIC_AUTH_TOKEN: 更新（值隐藏）', output.getvalue())
        self.assertIn('models.providers.model_proxy.apiKey: 更新（值隐藏）', output.getvalue())
        self.assertNotIn('{"env":{}}', output.getvalue())
        self.assertIn('跳过 hook', output.getvalue())
        self.assertIn('VERIFIED', output.getvalue())
        for p in (claude, codex, claw):
            backups = list(p.parent.glob(p.name + '.bak.*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)

    def test_install_through_python_dispatch_with_real_selection(self):
        claude = self.file('.claude/settings.json', '{"env":{}}')
        secret = 'FAKE_DISPATCH_TOKEN'
        port = int(os.environ['MP_ISOLATED_PORT'])
        cfg = {'listen': {'host': '127.0.0.1', 'port': port},
               'supplies': [{'id': 's', 'protocol': 'anthropic'}],
               'routes': [{'id': 'r', 'tiers': {'opus': ['s']}}],
               'strategies': [{'client_token': secret, 'route_id': 'r'}]}
        self.config.write_text(json.dumps(cfg))
        env = os.environ.copy()
        env['MODEL_PROXY_CONFIG'] = str(self.config)
        proc = subprocess.run([sys.executable, str(ROOT / 'core/cli/main.py'), 'install'],
                              cwd=ROOT, env=env, input='0\n\ny\ny\n', text=True,
                              capture_output=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertNotIn(secret, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(claude.read_text())['env']['ANTHROPIC_AUTH_TOKEN'], secret)

    def test_codex_catalog_offline_mock_commits_and_preserves_existing_on_failure(self):
        self.toml_parser()
        path = self.file('.codex/config.toml', 'model_catalog_json = "old-catalog.json"\n')
        template = self.case / 'template.json'
        template.write_text(json.dumps({'models': [{'base_instructions': 'PLACEHOLDER'}]}))
        catalog = self.home / '.codex' / 'model-catalogs' / 'model_proxy_catalog.json'
        catalog.parent.mkdir(mode=0o700)
        output = io.StringIO()
        response = mock.Mock()
        response.read.return_value = b'FAKE_LOCAL_PROMPT'
        with mock.patch.object(self.install, '_CODEX_CATALOG_TEMPLATE', template), \
             mock.patch.object(self.install, '_CODEX_CATALOG_TARGET', catalog), \
             mock.patch('urllib.request.urlopen', return_value=response) as fetch, \
             mock.patch.object(self.install, 'confirm', return_value=True), \
             contextlib.redirect_stdout(output):
            self.install.install_codex('FAKE_CODEX_TOKEN', '28371')
        fetch.assert_called_once_with(self.install._PROMPT_MD_URL,
                                      timeout=self.install._PROMPT_MD_TIMEOUT)
        self.assertEqual(json.loads(catalog.read_text())['models'][0]['base_instructions'], 'FAKE_LOCAL_PROMPT')
        self.assertIn('~/.codex/model-catalogs/model_proxy_catalog.json', path.read_text())
        self.assertNotIn('FAKE_CODEX_TOKEN', output.getvalue())
        self.assertEqual(stat.S_IMODE(catalog.stat().st_mode), 0o600)
        path.write_text('model_catalog_json = "old-catalog.json"\n'); path.chmod(0o600)
        with mock.patch.object(self.install, '_CODEX_CATALOG_TEMPLATE', template), \
             mock.patch.object(self.install, '_CODEX_CATALOG_TARGET', catalog), \
             mock.patch('urllib.request.urlopen', side_effect=OSError('fake offline')), \
             mock.patch.object(self.install, 'confirm', return_value=True), \
             contextlib.redirect_stdout(io.StringIO()):
            self.install.install_codex('FAKE_CODEX_TOKEN', '28371')
        self.assertIn('model_catalog_json = "old-catalog.json"', path.read_text())

    def _assert_catalog_permissions_block_before_config(self, *, directory_mode, file_mode):
        path = self.file('.codex/config.toml', 'model = "old"\n')
        catalog = self.file('.codex/model-catalogs/model_proxy_catalog.json', '{}', mode=file_mode)
        catalog.parent.chmod(directory_mode)
        template = self.case / 'template.json'
        template.write_text('{"models": []}')
        before_config, before_catalog = path.read_bytes(), catalog.read_bytes()
        out = io.StringIO()
        with mock.patch.object(self.install, '_CODEX_CATALOG_TEMPLATE', template), \
             mock.patch.object(self.install, '_CODEX_CATALOG_TARGET', catalog), \
             mock.patch('urllib.request.urlopen', return_value=io.BytesIO(b'FAKE_PROMPT')) as fetch, \
             mock.patch.object(self.install, 'choose_token', return_value='FAKE_SECRET'), \
             mock.patch.object(self.install, 'confirm', return_value=True) as confirm, \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = self.install.cmd_install(str(self.config), '28371', ['codex'], preflight_root=self.project)
        self.assertEqual(rc, 1)
        self.assertIn('catalog', out.getvalue())
        self.assertIn('Codex 配置未修改', out.getvalue())
        self.assertNotIn('codex: VERIFIED', out.getvalue())
        self.assertNotIn('FAKE_SECRET', out.getvalue())
        self.assertEqual(path.read_bytes(), before_config)
        self.assertEqual(catalog.read_bytes(), before_catalog)
        self.assertEqual(stat.S_IMODE(catalog.parent.stat().st_mode), directory_mode)
        self.assertEqual(stat.S_IMODE(catalog.stat().st_mode), file_mode)
        self.assertEqual(list(path.parent.glob('config.toml.bak.*')), [])
        confirm.assert_not_called()
        fetch.assert_called_once_with(self.install._PROMPT_MD_URL,
                                      timeout=self.install._PROMPT_MD_TIMEOUT)

    def test_existing_group_writable_catalog_directory_blocks_before_config(self):
        self._assert_catalog_permissions_block_before_config(directory_mode=0o775, file_mode=0o600)

    def test_existing_world_readable_catalog_file_blocks_before_config(self):
        self._assert_catalog_permissions_block_before_config(directory_mode=0o700, file_mode=0o644)

    def _assert_catalog_preparation_fallback(self, template_text, *, offline,
                                             incomplete=False, online_if_unexpected=False):
        self.toml_parser()
        path = self.file('.codex/config.toml',
                         'model = "old"\nmodel_catalog_json = "old-catalog.json"\n')
        catalog = self.file('.codex/model-catalogs/model_proxy_catalog.json', '{}', mode=0o644)
        catalog.parent.chmod(0o775)
        template = self.case / 'template.json'
        template.write_text(template_text, encoding='utf-8')
        before_catalog = catalog.read_bytes()
        out = io.StringIO()
        if incomplete:
            import http.client
            response = mock.Mock()
            response.read.side_effect = http.client.IncompleteRead(b'FAKE_PARTIAL_PROMPT', 100)
            fetch_patch = mock.patch('urllib.request.urlopen', return_value=response)
        elif online_if_unexpected:
            fetch_patch = mock.patch('urllib.request.urlopen', return_value=io.BytesIO(b'FAKE_PROMPT'))
        else:
            fetch_patch = mock.patch('urllib.request.urlopen', side_effect=OSError('fake offline'))
        with mock.patch.object(self.install, '_CODEX_CATALOG_TEMPLATE', template), \
             mock.patch.object(self.install, '_CODEX_CATALOG_TARGET', catalog), \
             fetch_patch as fetch, \
             mock.patch.object(self.install, 'choose_token', return_value='FAKE_SECRET'), \
             mock.patch.object(self.install, 'confirm', return_value=True), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = self.install.cmd_install(str(self.config), '28371', ['codex'], preflight_root=self.project)
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn('FAKE_SECRET', path.read_text())
        self.assertIn('model_catalog_json = "old-catalog.json"', path.read_text())
        self.assertNotIn('FAKE_SECRET', out.getvalue())
        self.assertNotIn('FAKE_PARTIAL_PROMPT', out.getvalue())
        self.assertEqual(catalog.read_bytes(), before_catalog)
        self.assertEqual(stat.S_IMODE(catalog.parent.stat().st_mode), 0o775)
        self.assertEqual(stat.S_IMODE(catalog.stat().st_mode), 0o644)
        self.assertEqual(list(catalog.parent.glob('*.bak.*')), [])
        if offline:
            fetch.assert_called_once_with(self.install._PROMPT_MD_URL,
                                          timeout=self.install._PROMPT_MD_TIMEOUT)
            if incomplete:
                response.read.assert_called_once()
        else:
            fetch.assert_not_called()

    def test_offline_and_unsafe_catalog_still_updates_codex(self):
        self._assert_catalog_preparation_fallback('{"models": []}', offline=True)

    def test_incomplete_download_and_unsafe_catalog_still_updates_codex(self):
        self._assert_catalog_preparation_fallback('{"models": []}', offline=True, incomplete=True)

    def test_invalid_json_and_unsafe_catalog_still_updates_codex(self):
        self._assert_catalog_preparation_fallback('{broken', offline=False)

    def test_invalid_template_structure_skips_get_and_updates_codex(self):
        self._assert_catalog_preparation_fallback('{"models": [42]}', offline=False)

    def test_array_model_messages_skips_get_and_updates_codex(self):
        self._assert_catalog_preparation_fallback(
            '{"models": [{"model_messages": []}]}', offline=False, online_if_unexpected=True)

    def test_cancel_after_public_get_writes_nothing(self):
        self.toml_parser()
        path = self.file('.codex/config.toml', 'model = "old"\n')
        catalog = self.file('.codex/model-catalogs/model_proxy_catalog.json', '{}', mode=0o600)
        template = self.case / 'template.json'
        template.write_text('{"models": []}', encoding='utf-8')
        before_config, before_catalog = path.read_bytes(), catalog.read_bytes()
        out = io.StringIO()
        with mock.patch.object(self.install, '_CODEX_CATALOG_TEMPLATE', template), \
             mock.patch.object(self.install, '_CODEX_CATALOG_TARGET', catalog), \
             mock.patch('urllib.request.urlopen', return_value=io.BytesIO(b'FAKE_PROMPT')) as fetch, \
             mock.patch.object(self.install, 'choose_token', return_value='FAKE_SECRET'), \
             mock.patch.object(self.install, 'confirm', return_value=False) as confirm, \
             mock.patch.object(self.install, 'secure_replace') as writer, \
             mock.patch.object(self.install, '_create_catalog_directory') as mkdir, \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = self.install.cmd_install(str(self.config), '28371', ['codex'], preflight_root=self.project)
        self.assertEqual(rc, 0, out.getvalue())
        fetch.assert_called_once_with(self.install._PROMPT_MD_URL,
                                      timeout=self.install._PROMPT_MD_TIMEOUT)
        confirm.assert_called_once()
        writer.assert_not_called()
        mkdir.assert_not_called()
        self.assertEqual(path.read_bytes(), before_config)
        self.assertEqual(catalog.read_bytes(), before_catalog)
        self.assertEqual(list(path.parent.glob('*.bak.*')), [])
        self.assertEqual(list(catalog.parent.glob('*.bak.*')), [])
        self.assertNotIn('FAKE_SECRET', out.getvalue())

    def test_missing_template_skips_unsafe_catalog_without_blocking_config(self):
        self.toml_parser()
        path = self.file('.codex/config.toml', 'model = "old"\n')
        catalog = self.file('.codex/model-catalogs/model_proxy_catalog.json', '{}', mode=0o644)
        catalog.parent.chmod(0o775)
        template = self.case / 'missing-template.json'
        out = io.StringIO()
        with mock.patch.object(self.install, '_CODEX_CATALOG_TEMPLATE', template), \
             mock.patch.object(self.install, '_CODEX_CATALOG_TARGET', catalog), \
             mock.patch('urllib.request.urlopen') as fetch, \
             mock.patch.object(self.install, 'choose_token', return_value='FAKE_SECRET'), \
             mock.patch.object(self.install, 'confirm', return_value=True), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = self.install.cmd_install(str(self.config), '28371', ['codex'], preflight_root=self.project)
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn('FAKE_SECRET', path.read_text())
        self.assertNotIn('FAKE_SECRET', out.getvalue())
        self.assertEqual(catalog.read_text(), '{}')
        self.assertEqual(stat.S_IMODE(catalog.parent.stat().st_mode), 0o775)
        self.assertEqual(stat.S_IMODE(catalog.stat().st_mode), 0o644)
        fetch.assert_not_called()

    def test_catalog_permission_change_after_preflight_reports_partial_install(self):
        self.toml_parser()
        path = self.file('.codex/config.toml', 'model = "old"\n')
        catalog = self.file('.codex/model-catalogs/model_proxy_catalog.json', '{}', mode=0o600)
        template = self.case / 'template.json'
        template.write_text('{"models": []}')
        response = mock.Mock(); response.read.return_value = b'FAKE_PROMPT'
        out = io.StringIO()
        def confirm(_):
            # Simulate an independent change after the initial read-only check.
            catalog.chmod(0o644)
            return True
        with mock.patch.object(self.install, '_CODEX_CATALOG_TEMPLATE', template), \
             mock.patch.object(self.install, '_CODEX_CATALOG_TARGET', catalog), \
             mock.patch('urllib.request.urlopen', return_value=response) as fetch, \
             mock.patch.object(self.install, 'choose_token', return_value='FAKE_SECRET'), \
             mock.patch.object(self.install, 'confirm', side_effect=confirm), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = self.install.cmd_install(str(self.config), '28371', ['codex'], preflight_root=self.project)
        fetch.assert_called_once_with(self.install._PROMPT_MD_URL,
                                      timeout=self.install._PROMPT_MD_TIMEOUT)
        self.assertEqual(rc, 1)
        self.assertIn('codex: VERIFIED', out.getvalue())
        self.assertIn('主配置已保存并验证', out.getvalue())
        self.assertIn('FAKE_SECRET', path.read_text())
        self.assertNotIn('FAKE_SECRET', out.getvalue())
        self.assertEqual(catalog.read_text(), '{}')
        self.assertEqual(stat.S_IMODE(catalog.stat().st_mode), 0o644)

    def test_unrelated_hook_commands_are_preserved(self):
        own = {'type': 'command', 'command': self.install._expected_hook_command()}
        unrelated = {'type': 'command', 'command': 'bash "/tmp/unrelated/ensure_model_proxy.sh"'}
        chained = {'type': 'command', 'command': self.install._expected_hook_command() + ' && echo hi'}
        entries = [{'hooks': [own]}, {'hooks': [own, unrelated, chained]}]
        result = self.install._normalize_session_start(entries, self.case)
        self.assertEqual(result[0]['hooks'], [own])
        self.assertEqual(result[1]['hooks'], [unrelated, chained])

    def test_partial_failure_stops_and_reports_first_commit(self):
        claude = self.file('.claude/settings.json', '{"env":{}}')
        claw = self.file('.openclaw/openclaw.json', '{}', mode=0o644)
        output = io.StringIO()
        with mock.patch.object(self.install, 'choose_token', return_value='FAKE_TOKEN'), \
             mock.patch.object(self.install, 'confirm', return_value=True), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            rc = self.install.cmd_install(str(self.config), '28371', ['claude', 'openclaw'],
                                          preflight_root=self.project)
        self.assertEqual(rc, 1)
        self.assertIn('claude: VERIFIED', output.getvalue())
        self.assertIn('openclaw.json: PREPARE_FAILED', output.getvalue())
        self.assertNotIn('FAKE_TOKEN', output.getvalue())
        self.assertIn('FAKE_TOKEN', claude.read_text())
        self.assertEqual(claw.read_text(), '{}')

    def test_vault_layout_recognition_and_hook_only_in_fake_vault(self):
        vault = self.case / 'vault'
        (vault / '.obsidian').mkdir(parents=True, mode=0o700)
        (vault / '.claude').mkdir(mode=0o700)
        project = vault / 'tools' / 'model_proxy'
        (project / 'hooker').mkdir(parents=True, mode=0o700)
        (project / 'hooker' / 'ensure_model_proxy.sh').write_text('# fake\n')
        settings = vault / '.claude' / 'settings.json'
        settings.write_text('{}'); settings.chmod(0o600)
        self.assertEqual(self.install._recognize_vault(project), vault)
        self.assertIsNone(self.install._recognize_vault(ROOT))
        with mock.patch.object(self.install, '_VAULT_ROOT', vault), \
             mock.patch.object(self.install, '_CLAUDE_SETTINGS', settings), \
             mock.patch.object(self.install, 'confirm', return_value=True), \
             contextlib.redirect_stdout(io.StringIO()):
            self.install.ensure_session_hook()
        self.assertIn('SessionStart', json.loads(settings.read_text())['hooks'])
        self.assertFalse((self.case / '.claude' / 'settings.json').exists())

    def test_readonly_correct_hook_with_real_acl_is_still_rejected(self):
        vault = self.case / 'vault-readonly'
        (vault / '.claude').mkdir(parents=True, mode=0o700)
        settings = vault / '.claude' / 'settings.json'
        settings.write_text(json.dumps({'hooks': {'SessionStart': [
            {'hooks': [{'type': 'command', 'command': self.install._expected_hook_command()}]},
        ]}}), encoding='utf-8')
        settings.chmod(0o644)
        before = settings.read_bytes()
        proc = subprocess.run(['/bin/chmod', '+a', 'everyone allow read', str(settings)],
                              capture_output=True, text=True)
        if proc.returncode:
            self.skipTest('native filesystem could not create a test ACL')
        with mock.patch.object(self.install, '_VAULT_ROOT', vault), \
             mock.patch.object(self.install, '_CLAUDE_SETTINGS', settings), \
             mock.patch.object(self.install, 'confirm') as confirm:
            with self.assertRaises(self.install.InstallFailure):
                self.install.ensure_session_hook()
        confirm.assert_not_called()
        self.assertEqual(settings.read_bytes(), before)
        self.assertEqual(list(settings.parent.glob('*.bak.*')), [])

    def test_windows_acl_is_explicitly_fail_closed(self):
        path = self.file('client/config.json', 'FAKE')
        with mock.patch.object(sys, 'platform', 'win32'):
            with self.assertRaises(sf.SecureSnapshotError): self.snap(path)
        self.assertEqual(path.read_text(), 'FAKE')

if __name__ == '__main__':
    unittest.main()
