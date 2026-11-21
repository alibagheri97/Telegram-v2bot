"""Root-aware installer commands and an unprivileged bot service identity."""

import getpass
import grp
import os
import pwd
import re
import subprocess


def privileged(command):
    return list(command) if os.geteuid() == 0 else ["sudo", *command]


def service_identity(service_name, project):
    if os.geteuid() != 0:
        return getpass.getuser(), grp.getgrgid(os.getgid()).gr_name
    username = (
        "vpnbot_" + re.sub(r"[^a-z0-9_]", "_", service_name.removesuffix(".service").lower())
    )[:31]
    try:
        account = pwd.getpwnam(username)
    except KeyError:
        subprocess.run(
            [
                "useradd",
                "--system",
                "--user-group",
                "--no-create-home",
                "--home-dir",
                "/nonexistent",
                "--shell",
                "/usr/sbin/nologin",
                username,
            ],
            check=True,
        )
        account = pwd.getpwnam(username)
    if account.pw_uid == 0:
        raise ValueError("The service account must not be root")
    group = grp.getgrgid(account.pw_gid).gr_name
    runtime = project / ".runtime"
    configuration = project / ".env"
    if runtime.is_symlink() or configuration.is_symlink() or not configuration.is_file():
        raise ValueError("Runtime/config must be local project paths, not symlinks")
    runtime.mkdir(exist_ok=True)
    runtime.chmod(0o755)
    state = runtime / "state"
    if state.is_symlink():
        raise ValueError("Runtime state directory must not be a symlink")
    state.mkdir(exist_ok=True)
    state.chmod(0o700)
    os.chown(state, account.pw_uid, account.pw_gid)
    os.chown(configuration, 0, account.pw_gid)
    configuration.chmod(0o640)
    result = subprocess.run(
        ["runuser", "-u", username, "--", "test", "-r", str(configuration)], check=False
    )
    if result.returncode:
        raise ValueError(
            "The bot user cannot traverse the project path. Check directory permissions or use a directory outside the web root."
        )
    return username, group
