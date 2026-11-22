"""Status identities must be checked only when the real formatter displays them."""
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from core import windows_process
from core.cli import _format_ops
from core.cli import windows_control as wc
from tests.test_windows_control_a_contract import ROOT, CONFIG, PATHS, RECORD


def session(*, route='r1', tokens=None, routes=None, last_route=None, supply='s1', when=None):
    return {'n': 1, 'fail': 0, 'fo': 0, 'last_ts': when or datetime.now(),
            'last_status': '200', 'last_route': route if last_route is None else last_route,
            'last_tier': 'sonnet', 'last_supply': supply, 'last_error': '',
            'last_req_id': 'fake-request', 'builtin_only': False,
            'chains': {('sonnet', supply): 1}, 'routes': routes if routes is not None else {route: 1},
            'tokens': tokens if tokens is not None else {'cc': 1}}


class VisibleStatusIdentityTests(unittest.TestCase):
    def run_status(self, *, token='0', data=None, cfg=None, sessions=None, health=None, names=None):
        data = data if data is not None else {'supplies': [], 'routes': [], 'strategies': [], 'cooldown': {}}
        cfg = cfg if cfg is not None else {'routes': [], 'strategies': []}
        sessions = sessions if sessions is not None else {'sessions': {}, 'truncated': False, 'log_missing': True}
        health = health or {}
        with tempfile.TemporaryDirectory(prefix='mp-visible-identity-') as temp:
            root = Path(temp); config = root / 'config.json'
            config.write_text(json.dumps(cfg), encoding='utf-8')
            paths = dict(PATHS, totals=str(root/'totals'), log=str(root/'log'))
            active = dict(RECORD, config=str(config), runtime_paths=paths)
            out, err = io.StringIO(), io.StringIO()
            control = wc.WindowsControl(ROOT, config_path=CONFIG, out=out, err=err)
            control.config_path = str(config)
            with patch.object(wc.os, 'name', 'nt'), \
                 patch.object(control, '_configuration', return_value=(paths, RECORD['listen'])), \
                 patch.object(control, '_locator_hint', return_value=True), \
                 patch.object(control, '_active', return_value=active), \
                 patch.object(windows_process, 'recorded_process_state', return_value='running'), \
                 patch.object(control, '_request', return_value=(data, token)), \
                 patch.object(_format_ops, 'load_supply_health', return_value=health), \
                 patch.object(_format_ops, 'load_session_names', return_value=names or {}), \
                 patch.object(_format_ops, 'load_active_sessions', return_value=sessions):
                rc = control.status()
            with patch.object(_format_ops, 'load_supply_health', return_value=health), \
                 patch.object(_format_ops, 'load_session_names', return_value=names or {}), \
                 patch.object(_format_ops, 'load_active_sessions', return_value=sessions):
                baseline = _format_ops._format_status_from_json(data, str(config), paths['totals'], paths['log'])
            return rc, out.getvalue(), err.getvalue(), '\n'.join(baseline)

    def test_hidden_response_route_is_not_a_visible_identity(self):
        data = {'supplies': [{'id': 's1'}], 'routes': [{'id': '0'}],
                'strategies': [{'client_token': 'cc', 'route_id': '0'}], 'cooldown': {}}
        cfg = {'routes': [{'id': '0', 'tiers': {'sonnet': ['s1']}}],
               'strategies': [{'client_token': 'cc', 'route_id': '0'}]}
        rc, out, err, baseline = self.run_status(data=data, cfg=cfg)
        self.assertEqual(rc, 0, err)
        self.assertIn('config 1 supplies / 1 routes / 1 strategies', out)
        self.assertIn('supply health', out)
        self.assertNotIn('0(cc) sonnet', out)
        self.assertNotIn('0(cc) sonnet', baseline)

    def test_hidden_response_supply_and_strategy_are_not_visible_identities(self):
        data = {'supplies': [{'id': '0'}], 'routes': [{'id': 'r1'}],
                'strategies': [{'client_token': '0', 'route_id': 'r1'}], 'cooldown': {}}
        cfg = {'routes': [{'id': 'r1', 'tiers': {'sonnet': ['0']}}],
               'strategies': [{'client_token': '0', 'route_id': 'r1'}]}
        rc, out, err, _ = self.run_status(data=data, cfg=cfg)
        self.assertEqual(rc, 0, err)
        self.assertIn('supply health', out)
        self.assertNotIn('r1(0) sonnet', out)

    def test_hidden_response_route_does_not_block_an_unrelated_visible_reference(self):
        data = {'supplies': [{'id': 's1'}, {'id': 's2'}], 'routes': [{'id': '0'}], 'strategies': [],
                'cooldown': {'s1': {'remain': 3, 'reason': '429'}}}
        cfg = {'routes': [{'id': '0', 'tiers': {'sonnet': ['s2']}},
                          {'id': 'r1', 'tiers': {'sonnet': ['s1']}}],
               'strategies': [{'client_token': 'cc', 'route_id': 'r1'}]}
        rc, out, err, _ = self.run_status(data=data, cfg=cfg)
        self.assertEqual(rc, 0, err)
        self.assertIn('r1(cc) sonnet', out)

    def test_visible_cooldown_supply_identity_still_fail_closed(self):
        data = {'supplies': [{'id': '0'}], 'routes': [], 'strategies': [],
                'cooldown': {'0': {'remain': 3, 'reason': '429'}}}
        rc, out, err, baseline = self.run_status(data=data, health={})
        self.assertEqual(rc, 1)
        self.assertEqual(out, '')
        self.assertIn('status_sensitive_conflict', err)
        self.assertIn('  0', baseline)

    def test_supplies_section_never_renders_supply_identity_when_unreferenced(self):
        """供给段只渲染被 route 引用的 id；未引用摘要也受同一脱敏守卫。

        2026-11-22 起供给段不再显示引用关系，因此「供给 id 撞 token」只在
        cooldown 段与未引用摘要两处可能泄露——前者由既有用例覆盖，后者在此。
        """
        data = {'supplies': [{'id': '0'}], 'routes': [], 'strategies': [],
                'cooldown': {}}
        # 供给清单来自磁盘配置（与 _supply_refs 同源）：让它真的含有 id=0，
        # 否则这条用例连渲染路径都走不到。
        cfg = {'supplies': [{'id': '0'}], 'routes': [], 'strategies': []}
        rc, out, err, _ = self.run_status(data=data, cfg=cfg, health={})
        self.assertEqual(rc, 1, '未引用摘要里的 0 必须 fail-closed')
        self.assertEqual(out, '')
        self.assertIn('status_sensitive_conflict', err)

    def test_visible_disk_route_reference_still_fails_closed(self):
        data = {'supplies': [{'id': 's1'}], 'routes': [], 'strategies': [],
                'cooldown': {'s1': {'remain': 3, 'reason': '429'}}}
        cfg = {'routes': [{'id': '0', 'tiers': {'sonnet': ['s1']}}],
               'strategies': [{'client_token': 'cc', 'route_id': '0'}]}
        rc, out, err, baseline = self.run_status(data=data, cfg=cfg)
        self.assertEqual(rc, 1)
        self.assertEqual(out, '')
        self.assertIn('status_sensitive_conflict', err)
        self.assertIn('0(cc) sonnet', baseline)

    def test_visible_session_route_still_fails_closed(self):
        sessions = {'sessions': {'session1': session(route='0')}, 'truncated': False, 'log_missing': False}
        rc, out, err, baseline = self.run_status(sessions=sessions)
        self.assertEqual(rc, 1)
        self.assertEqual(out, '')
        self.assertIn('status_sensitive_conflict', err)
        self.assertIn('  0 sonnet/s1', baseline)

    def test_unselected_session_route_and_response_route_do_not_block(self):
        data = {'supplies': [], 'routes': [{'id': '0'}], 'strategies': [], 'cooldown': {}}
        one = session(route='r1', routes={'r1': 3, '0': 1}, last_route='0')
        sessions = {'sessions': {'session1': one}, 'truncated': False, 'log_missing': False}
        rc, out, err, _ = self.run_status(data=data, sessions=sessions)
        self.assertEqual(rc, 0, err)
        self.assertIn('  r1 sonnet/s1', out)
        self.assertNotIn('  0 sonnet/s1', out)

    def test_undisplayed_twenty_first_session_does_not_block(self):
        now = datetime.now()
        items = {f'session-{i:02d}': session(route='0' if i == 20 else 'r1',
                 tokens={'0': 1} if i == 20 else {'cc': 1}, when=now-timedelta(seconds=i))
                 for i in range(21)}
        sessions = {'sessions': items, 'truncated': False, 'log_missing': False}
        rc, out, err, _ = self.run_status(sessions=sessions)
        self.assertEqual(rc, 0, err)
        self.assertIn('另有 1 个 session', out)
        self.assertNotIn('[0]', out)

    def test_visible_shortened_supply_and_session_name_still_fail_closed(self):
        sessions = {'sessions': {'session1': session(supply='0-sankuai')},
                    'truncated': False, 'log_missing': False}
        rc, out, err, baseline = self.run_status(sessions=sessions)
        self.assertEqual(rc, 1)
        self.assertEqual(out, '')
        self.assertIn('status_sensitive_conflict', err)
        self.assertIn('sonnet/0', baseline)
        sessions['sessions']['session1'] = session()
        rc, out, err, baseline = self.run_status(sessions=sessions, names={'session1': '0'})
        self.assertEqual(rc, 1)
        self.assertEqual(out, '')
        self.assertIn('status_sensitive_conflict', err)
        self.assertIn('0 ·', baseline)

    def test_client_tokens_are_compared_as_identities_not_split_rendered_text(self):
        sessions = {'sessions': {'session1': session(tokens={'cc,codex': 1})},
                    'truncated': False, 'log_missing': False}
        rc, out, err, _ = self.run_status(token='cc', sessions=sessions)
        self.assertEqual(rc, 0, err)
        self.assertIn('[cc,codex]', out)
        sessions['sessions']['session1'] = session(tokens={'a,b': 1})
        rc, out, err, _ = self.run_status(token='a,b', sessions=sessions)
        self.assertEqual(rc, 1)
        self.assertEqual(out, '')
        self.assertIn('status_sensitive_conflict', err)

    def test_numeric_cooldown_reason_is_not_a_user_identity(self):
        data = {'supplies': [{'id': 's1'}], 'routes': [], 'strategies': [],
                'cooldown': {'s1': {'remain': 3, 'reason': '0'}}}
        rc, out, err, _ = self.run_status(data=data)
        self.assertEqual(rc, 0, err)
        self.assertIn('cooldown (剩余秒)', out)
        self.assertIn('(0)', out)


if __name__ == '__main__':
    unittest.main()
