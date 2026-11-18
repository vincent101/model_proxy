"""Real Whisky API diagnostic on a fresh fake directory; not native proof."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import platform_windows as win


def diagnostic():
    output = {'environment': 'Wine/Whisky; not native Windows'}
    with tempfile.TemporaryDirectory(prefix='mp-private-create-fixture-') as directory:
        for label, path, operation in (
                ('directory', os.path.join(directory, 'state-fixture'), win.ensure_private_directory),
                ('file', os.path.join(directory, 'log-fixture'), win.create_private_empty_file)):
            try:
                result = operation(path)
                output[label] = {'result': 'verified', 'created': result.created}
            except win.WindowsPrivateCreationError as exc:
                output[label] = {'result': exc.code, 'created': exc.created,
                                 'release_unknown': exc.release_unknown}
    return output


if __name__ == '__main__':
    if os.name != 'nt' or len(sys.argv) != 2 or not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise SystemExit('Windows Python with -I -S -B and a fresh result path required')
    result = diagnostic()
    with open(sys.argv[1], 'x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2)
