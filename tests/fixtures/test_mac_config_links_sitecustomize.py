"""Installed only in the temporary interpreter's test PYTHONPATH."""
import os
try:
    from test_mac_config_links_guard import install, service_checks
    role = os.environ['MP_LINKS_GUARD_ROLE']
    ports = os.environ['MP_LINKS_GUARD_PORTS'].split(',')
    checks = service_checks(int(os.environ['MP_LINKS_OWNER_PID']), int(ports[1])) if role == 'service' else (None, None)
    install(os.environ['MP_LINKS_GUARD_ROOT'], ports, role, listener_check=checks[0], peer_check=checks[1])
    path = os.path.join(os.environ['MP_LINKS_GUARD_ROOT'], 'guard', 'attest-%d' % os.getpid())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        import stat
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise RuntimeError('guard attestation protection unknown')
        if os.write(fd, (role + '\n').encode('ascii')) != len(role) + 1:
            raise RuntimeError('guard attestation write failed')
    finally:
        os.close(fd)
    os.environ['MP_LINKS_GUARD_ACTIVE'] = '1'
except BaseException:
    os._exit(126)
