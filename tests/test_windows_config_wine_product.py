"""Run the actual Windows helper and preflight on fresh fake files in Whisky.
No services, real client configs, credentials, or native Windows claims.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from core.cli import _config_ops as ops, dispatch
except Exception as exc:
    if len(sys.argv) == 2:
        with open(sys.argv[1], 'x', encoding='utf-8') as stream:
            json.dump({'environment': 'Wine/Whisky; not native Windows',
                       'import_error': type(exc).__name__, 'detail': str(exc)[:300]}, stream)
    raise SystemExit(1)


def diagnostic():
    with tempfile.TemporaryDirectory(prefix='mp-config-fixture-') as temp, \
         tempfile.TemporaryDirectory(prefix='model-proxy-cli-') as marker_dir:
        root = Path(temp)
        (root / 'config').mkdir()
        (root / 'runtime').mkdir()
        config = root / 'config' / 'fake-config.json'
        before = '{"supplies":[],"routes":[],"strategies":[]}'
        config.write_text(before, encoding='utf-8')
        keys = ('log', 'totals', 'lock', 'pid', 'ensure_log', 'start_lock')
        (root / 'config' / 'runtime_paths.json').write_text(
            json.dumps({k: str(root / 'runtime' / k) for k in keys}), encoding='utf-8')
        marker = Path(marker_dir) / 'reload'
        marker.write_text('started', encoding='ascii')
        dispatch.ROOT = root
        def edit(path):
            cfg = ops.load_config(path)
            cfg['routes'].append({'id': 'fake-only'})
            ops.atomic_write(path, cfg)
            ops.done(True)
        ops._DISPATCH['route-add'] = (edit, 0)
        ops.confirm = lambda _prompt: True
        sys.argv = ['_config_ops', 'route-add', str(config)]
        os.environ['CONFIG_OPS_RELOAD_MARKER'] = str(marker)
        os.environ.pop('CONFIG_OPS_RELOAD_MARKER_FD', None)
        messages = io.StringIO()
        with contextlib.redirect_stdout(messages), contextlib.redirect_stderr(messages):
            try:
                ops.main()
                code = 0
            except SystemExit as exc:
                code = int(exc.code or 0)
            except Exception as exc:
                code = 1
                print('exception:', type(exc).__name__, getattr(exc, 'code', 'unavailable'))
        return {'environment': 'Wine/Whisky; not native Windows',
                'exit_code': code, 'marker': marker.read_text(encoding='ascii'),
                'config_unchanged': config.read_text(encoding='utf-8') == before,
                'messages': messages.getvalue()[-2000:]}


if __name__ == '__main__':
    if os.name != 'nt' or len(sys.argv) != 2 or not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise SystemExit('Windows Python -I -S -B and one fresh result path required')
    result_path = sys.argv[1]
    result = diagnostic()
    with open(result_path, 'x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=True, indent=2)
