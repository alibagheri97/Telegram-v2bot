import os
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from vpnshopbot import privileges
from vpnshopbot.release import health


def test_privileged_commands_do_not_require_sudo_when_root(monkeypatch):
    monkeypatch.setattr(privileges.os, "geteuid", lambda: 0)
    assert privileges.privileged(["systemctl", "daemon-reload"]) == ["systemctl", "daemon-reload"]
    monkeypatch.setattr(privileges.os, "geteuid", lambda: 1000)
    assert privileges.privileged(["systemctl", "daemon-reload"]) == [
        "sudo",
        "systemctl",
        "daemon-reload",
    ]


def test_root_installer_uses_unprivileged_identity_and_private_state(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("not a real secret")
    monkeypatch.setattr(privileges.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        privileges.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=1234, pw_gid=2345)
    )
    monkeypatch.setattr(
        privileges.grp, "getgrgid", lambda gid: SimpleNamespace(gr_name="vpnbot_test")
    )
    ownership = []
    monkeypatch.setattr(privileges.os, "chown", lambda *args: ownership.append(args))
    monkeypatch.setattr(
        privileges.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0)
    )
    user, group = privileges.service_identity("starlight_bot.service", tmp_path)
    assert user == "vpnbot_starlight_bot" and user != "root"
    assert group == "vpnbot_test"
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o640
    assert stat.S_IMODE((tmp_path / ".runtime/state").stat().st_mode) == 0o700
    assert (tmp_path / ".env", 0, 2345) in ownership


def test_existing_service_account_cannot_be_root(tmp_path, monkeypatch):
    monkeypatch.setattr(privileges.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        privileges.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=0, pw_gid=0)
    )
    with pytest.raises(ValueError, match="must not be root"):
        privileges.service_identity("test.service", tmp_path)


def test_legacy_health_is_readable_during_launcher_upgrade(tmp_path):
    (tmp_path / "health.json").write_text('{"pid": 100, "hot_reload": true}')
    assert health(tmp_path)["pid"] == 100
    (tmp_path / "state").mkdir()
    (tmp_path / "state/health.json").write_text('{"pid": 200, "hot_reload": true}')
    assert health(tmp_path)["pid"] == 200


def test_prepare_only_installs_dependencies_without_starting_setup(tmp_path):
    root = Path(__file__).resolve().parent.parent
    shutil.copy2(root / "start.sh", tmp_path / "start.sh")
    venv = tmp_path / ".venv/bin"
    venv.mkdir(parents=True)
    python = venv / "python"
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_SETUP_LOG"\n')
    python.chmod(0o755)
    log = tmp_path / "commands.log"
    env = dict(os.environ, PYTHON_BIN=str(python), TEST_SETUP_LOG=str(log))
    result = subprocess.run(
        ["bash", str(tmp_path / "start.sh"), "--prepare-only"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "No bot, database setup or service was started" in result.stdout
    commands = log.read_text()
    assert "-m pip install" in commands
    assert "vpnshopbot.setup" not in commands
    assert not (tmp_path / ".env").exists()


def test_prerequisite_script_syntax_only():
    root = Path(__file__).resolve().parent.parent
    subprocess.run(["bash", "-n", str(root / "install-prerequisites.sh")], check=True)
