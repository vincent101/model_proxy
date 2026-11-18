"""Real status formatting with temporary old/new config; no real data or processes."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import windows_process
from core.cli import _format_ops
from core.cli import windows_control as wc
from tests.test_windows_control_a_contract import ROOT, CONFIG, PATHS, RECORD


_MISSING = object()


class DiskConfigReferenceTests(unittest.TestCase):
    def run_status(self, *, token, route_id='route1', client='cc', tier='sonnet',
                   visible='cooldown', pool=False, disk_admin='other-admin', extras=False):
        with tempfile.TemporaryDirectory(prefix='mp-disk-ref-') as temp:
            root = Path(temp)
            config = root / 'config.json'
            old_route = {'id': 'old-route', 'tiers': {tier: ['s1']}}
            old_strategy = {'client_token': 'old-client-safe', 'route_id': 'old-route'}
            config.write_text(json.dumps({'admin_token': token, 'routes': [old_route],
                                          'strategies': [old_strategy]}), encoding='utf-8')
            # This response is the authenticated server's still-unreloaded
            # state, deliberately distinct from the newly edited disk config.
            data = {'supplies': [{'id': 's1'}], 'routes': [old_route], 'strategies': [old_strategy],
                    'cooldown': {'s1': {'remain': 3, 'reason': '429'}} if visible == 'cooldown' else {}}
            route = {'tiers': {tier: ['s1']}}
            if route_id is not _MISSING:
                route['id'] = route_id
            target = '?' if route_id is _MISSING else route_id
            strategy = ({'route_pool': [{'route_id': target}]} if pool else {'route_id': target})
            if client is not _MISSING:
                strategy['client_token'] = client
            disk = {'admin_token': disk_admin, 'routes': [route], 'strategies': [strategy]}
            if extras:
                route['note'] = token
                strategy['note'] = token
                disk['supplies'] = [{'id': 'ignored', 'url': token, 'appkey': token}]
                disk['metadata'] = {'note': token}
            config.write_text(json.dumps(disk), encoding='utf-8')
            paths = dict(PATHS, totals=str(root / 'totals'), log=str(root / 'access.log'))
            active = dict(RECORD, config=str(config), runtime_paths=paths)
            out, err = io.StringIO(), io.StringIO()
            control = wc.WindowsControl(ROOT, config_path=CONFIG, out=out, err=err)
            control.config_path = str(config)
            health = {'s1': {'requests': 10, 'fail': 4}} if visible == 'degraded' else {}
            sessions = {'sessions': {}, 'truncated': False, 'log_missing': True}
            with patch.object(wc.os, 'name', 'nt'), \
                 patch.object(control, '_configuration', return_value=(paths, RECORD['listen'])), \
                 patch.object(control, '_locator_hint', return_value=True), \
                 patch.object(control, '_active', return_value=active), \
                 patch.object(windows_process, 'recorded_process_state', return_value='running'), \
                 patch.object(control, '_request', return_value=(data, token)), \
                 patch.object(_format_ops, 'load_supply_health', return_value=health), \
                 patch.object(_format_ops, 'load_session_names', return_value={}), \
                 patch.object(_format_ops, 'load_active_sessions', return_value=sessions):
                rc = control.status()
            # The default formatter is the unchanged Mac behavior and should
            # keep displaying normal config-derived identities.
            with patch.object(_format_ops, 'load_supply_health', return_value=health):
                baseline = _format_ops._format_status_from_json(data, str(config), paths['totals'])
            return rc, out.getvalue(), err.getvalue(), '\n'.join(baseline)

    def test_visible_current_disk_route_id_equal_to_auth_token_fails_closed(self):
        for token, route_id, client, visible, pool, expected in (
                ('0', '0', 'cc', 'cooldown', False, '0(cc) sonnet'),
                ('cc', 'cc', 'normal-client', 'degraded', True, 'cc(normal-client) sonnet'),
                ('a,b', 'a,b', 'other', 'cooldown', False, 'a,b(other) sonnet'),
                ('0', 0, 'cc', 'cooldown', False, '0 sonnet')):
            with self.subTest(token=token, route_id=route_id, visible=visible):
                rc, out, err, baseline = self.run_status(token=token, route_id=route_id,
                    client=client, visible=visible, pool=pool)
                self.assertEqual(rc, 1)
                self.assertEqual(out, '')
                self.assertIn('status_sensitive_conflict', err)
                self.assertIn(expected, baseline)

    def test_current_disk_client_token_still_guarded_without_reload(self):
        for token, pool in (('0', False), ('cc', True), ('a,b', False)):
            with self.subTest(token=token, pool=pool):
                rc, out, err, baseline = self.run_status(token=token, route_id='route1',
                                                         client=token, pool=pool)
                self.assertEqual(rc, 1)
                self.assertEqual(out, '')
                self.assertIn('status_sensitive_conflict', err)
                self.assertIn(f'route1({token}) sonnet', baseline)

    def test_multiple_client_identities_guard_exact_one_without_masking_others(self):
        with tempfile.TemporaryDirectory(prefix='mp-ref-multiple-') as temp:
            root = Path(temp)
            cfg = {'routes': [{'id': 'r1', 'tiers': {'sonnet': ['s1']}}],
                   'strategies': [{'client_token': 'cc', 'route_id': 'r1'},
                                  {'client_token': 'codex', 'route_id': 'r1'}]}
            config = root / 'config.json'; config.write_text(json.dumps(cfg), encoding='utf-8')
            data = {'supplies': [{'id': 's1'}], 'routes': [], 'strategies': [],
                    'cooldown': {'s1': {'remain': 3, 'reason': '429'}}}
            with self.assertRaises(_format_ops.StatusReferenceConflict):
                _format_ops._format_status_from_json(data, str(config), str(root/'totals'),
                                                     forbidden_reference_token='codex')
            allowed = _format_ops._format_status_from_json(data, str(config), str(root/'totals'),
                                                            forbidden_reference_token='cod')
            self.assertIn('r1(cc,codex) sonnet', '\n'.join(allowed))
            baseline = _format_ops._format_status_from_json(data, str(config), str(root/'totals'))
            self.assertIn('r1(cc,codex) sonnet', '\n'.join(baseline))

    def test_matching_other_route_not_shown_does_not_block_visible_route(self):
        with tempfile.TemporaryDirectory(prefix='mp-ref-other-route-') as temp:
            root = Path(temp)
            cfg = {'routes': [{'id': '0', 'tiers': {'sonnet': ['s2']}},
                              {'id': 'r1', 'tiers': {'sonnet': ['s1']}}],
                   'strategies': [{'client_token': 'cc', 'route_id': 'r1'}]}
            config = root / 'config.json'; config.write_text(json.dumps(cfg), encoding='utf-8')
            data = {'supplies': [{'id': 's1'}, {'id': 's2'}], 'routes': [], 'strategies': [],
                    'cooldown': {'s1': {'remain': 3, 'reason': '429'}}}
            lines = _format_ops._format_status_from_json(data, str(config), str(root/'totals'),
                                                         forbidden_reference_token='0')
            output = '\n'.join(lines)
            self.assertIn('r1(cc) sonnet', output)
            self.assertNotIn('0 sonnet', output)

    def test_unreferenced_matching_route_does_not_block_zero_counts(self):
        rc, out, err, _ = self.run_status(token='0', route_id='0', visible='none')
        self.assertEqual(rc, 0, err)
        self.assertIn('cooldown 0/1', out)
        self.assertNotIn('0(cc)', out)

    def test_nonmatching_business_values_and_unused_metadata_stay_unchanged(self):
        rc, out, err, baseline = self.run_status(token='0', route_id='route0',
                                                 client='cc0', extras=True)
        self.assertEqual(rc, 0, err)
        self.assertIn('route0(cc0) sonnet', out)
        self.assertIn('cooldown 1/1', out)
        self.assertIn('route0(cc0) sonnet', baseline)

    def test_disk_admin_is_not_substituted_for_real_authenticated_token(self):
        rc, out, err, _ = self.run_status(token='real-auth-token', route_id='0',
                                         client='cc', disk_admin='0')
        self.assertEqual(rc, 0, err)
        self.assertIn('0(cc) sonnet', out)

    def test_constant_tier_label_does_not_match_unrelated_admin_token(self):
        rc, out, err, _ = self.run_status(token='opus', route_id='route1',
                                         client='cc', tier='opus')
        self.assertEqual(rc, 0, err)
        self.assertIn('route1(cc) opus', out)

    def test_missing_identity_placeholders_are_not_treated_as_real_tokens(self):
        rc, out, err, baseline = self.run_status(token='?', route_id=_MISSING,
                                                 client=_MISSING)
        self.assertEqual(rc, 0, err)
        self.assertIn('?(?) sonnet', out)
        self.assertIn('?(?) sonnet', baseline)

    def test_config_read_error_does_not_reveal_exact_token_path_component(self):
        with tempfile.TemporaryDirectory(prefix='mp-config-error-') as temp:
            root = Path(temp)
            missing = root / '0' / 'missing.json'
            missing.parent.mkdir()
            totals = str(root / 'totals')
            data = {'supplies': [], 'routes': [], 'strategies': [], 'cooldown': {}}
            with self.assertRaises(_format_ops.StatusReferenceConflict):
                _format_ops._format_status_from_json(data, str(missing), totals,
                                                     forbidden_reference_token='0')
            baseline = '\n'.join(_format_ops._format_status_from_json(data, str(missing), totals))
            self.assertIn(repr(str(missing)), baseline)  # default Mac behavior remains unchanged

    def test_config_read_error_ignores_unrelated_numeric_substrings(self):
        with tempfile.TemporaryDirectory(prefix='mp-config-error-') as temp:
            root = Path(temp)
            missing = root / 'route0' / 'missing.json'
            missing.parent.mkdir()
            data = {'supplies': [], 'routes': [], 'strategies': [], 'cooldown': {}}
            result = _format_ops._format_status_from_json(data, str(missing), str(root/'totals'),
                                                          forbidden_reference_token='0')
            output = '\n'.join(result)
            self.assertIn(repr(str(missing)), output)
            self.assertIn('cooldown 0/0', output)

    def test_explicit_placeholder_shaped_identities_are_guarded(self):
        for route_id, client in (('?', 'cc'), ('route1', '?')):
            with self.subTest(route_id=route_id, client=client):
                rc, out, err, _ = self.run_status(token='?', route_id=route_id, client=client)
                self.assertEqual(rc, 1)
                self.assertEqual(out, '')
                self.assertIn('status_sensitive_conflict', err)


if __name__ == '__main__':
    unittest.main()
