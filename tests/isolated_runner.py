"""Persistent owned-child worker with a real PTY and separate control pipes."""
import fcntl
import json
import os
import runpy
import signal
import stat
import sys
import termios
import traceback

# The parent starts this worker in a new session with the test PTY as 0/1/2.
if not (os.isatty(0) and os.isatty(1)):
    raise RuntimeError('worker requires a test PTY')
in_stat, out_stat = os.fstat(0), os.fstat(1)
if (not stat.S_ISCHR(in_stat.st_mode) or not stat.S_ISCHR(out_stat.st_mode)
        or (in_stat.st_dev, in_stat.st_rdev) != (out_stat.st_dev, out_stat.st_rdev)):
    raise RuntimeError('worker stdin/stdout do not share the test PTY')
fcntl.ioctl(0, termios.TIOCSCTTY, 0)

# Worker uses -S; the separate service starts in the pristine isolated venv
# with a fail-closed sitecustomize so its kernel argv stays unmodified.
import isolation_bootstrap
isolation_bootstrap.install_guard()


def main():
    if os.environ.get('MP_TEST_GUARD_INSTALLED') != '1':
        raise RuntimeError('test guard not installed')
    entry, read_fd, write_fd = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    children = sys._mp_test_children
    channel_in = os.fdopen(read_fd, 'r', encoding='utf-8')
    channel_out = os.fdopen(write_fd, 'w', encoding='utf-8', buffering=1)
    def interrupted(_sig, _frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        for line in channel_in:
            command = json.loads(line)
            if command == 'quit':
                break
            code = 0
            if command != '__probe__':
                try:
                    sys.argv = [entry, command]
                    try:
                        runpy.run_path(entry, run_name='__main__')
                    except SystemExit as exc:
                        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
                except Exception:
                    code = 1
                    traceback.print_exc(file=sys.stderr)
            sys.stdout.flush(); sys.stderr.flush()
            result = {'code': code, 'children': [p.pid for p in children if p.poll() is None]}
            channel_out.write(json.dumps(result) + '\n'); channel_out.flush()
    finally:
        for p in children:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except Exception:
                    p.kill()
                    p.wait(timeout=3)
        channel_in.close(); channel_out.close()


if __name__ == '__main__':
    main()
