import io
import json
import os
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from vpnshopbot.release import allowed_path, build_bundle, digest, extract_bundle, switch
from vpnshopbot.runtime import HandlerDrain, exec_current_release, publish_health
from vpnshopbot.setup import render_service_unit


def test_release_version_matches_package():
    import vpnshopbot

    root = Path(__file__).resolve().parent.parent
    assert (root / "VERSION").read_text().strip() == vpnshopbot.__version__


def test_preflight_failure_leaves_running_release_pointer_untouched(source, tmp_path, monkeypatch):
    from vpnshopbot import release

    (source / ".env").write_text(
        "BOT_TOKEN=123:fake-token\nBOT_DB_URL=sqlite://\nPANEL_DB_URL=sqlite://\n"
        "SUBSCRIPTION_BASE_URL=https://panel.test/subscribe?token=\nCARD_NUMBER=CHANGE_ME\nCARD_HOLDER=Test\n"
    )
    current = source / ".runtime/releases/old"
    current.mkdir(parents=True)
    switch(source / ".runtime", "current", current)
    output = tmp_path / "update.tar.gz"
    build_bundle(source, output, "1.0.0")
    monkeypatch.setattr(release, "health", lambda runtime: {"hot_reload": True, "pid": 100})

    def fail(*args):
        raise ValueError("preflight rejected")

    monkeypatch.setattr(release, "prepare_runtime", fail)
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("Live service must not be touched"),
    )
    args = SimpleNamespace(
        adopt=False, rollback=False, version=None, patch=None, bundle=str(output), sha256=None
    )
    with pytest.raises(ValueError, match="preflight"):
        release.update(source, args)
    assert (source / ".runtime/current").resolve() == current


def test_handover_uses_signal_and_ready_marker_without_restart(tmp_path, monkeypatch):
    from vpnshopbot import release

    runtime = tmp_path / ".runtime"
    candidate = runtime / "releases/new"
    candidate.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(
        release, "health", lambda runtime: {"release_id": "new", "ready_at": 201, "pid": 100}
    )
    monkeypatch.setattr(release.time, "time", lambda: 200)

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="active\n", returncode=0)

    monkeypatch.setattr(release.subprocess, "run", run)
    report = release.handover(runtime, "test_bot.service", candidate)
    assert report["pid"] == 100
    assert any("--signal=HUP" in command for command in calls)
    assert not any("restart" in command or "stop" in command for command in calls)


@pytest.fixture
def source(tmp_path):
    project = tmp_path / "project"
    (project / "vpnshopbot").mkdir(parents=True)
    (project / "vpnshopbot/app.py").write_text("print('release')\n")
    (project / "VERSION").write_text("1.0.0\n")
    (project / "pyproject.toml").write_text("[project]\nname='test-release'\nversion='1.0.0'\n")
    return project


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "../.env",
        "/etc/passwd",
        "vpnshopbot/../../bad.py",
        "vpnshopbot/.env",
        "vpnshopbot/__pycache__/secret.py",
        ".runtime/health.json",
        "vpnshopbot\\evil.py",
    ],
)
def test_release_paths_block_secrets_and_traversal(path):
    assert not allowed_path(path)


def test_bundle_round_trip_omits_secrets_and_venv(source, tmp_path):
    # A broken configuration must never even be read as a source file.
    (source / ".venv").mkdir()
    (source / ".venv/secret.py").write_text("private")
    (source / ".env.local").write_text("private")
    output = tmp_path / "v1.tar.gz"
    metadata = build_bundle(source, output, "1.0.0")
    assert set(metadata["files"]) == {"VERSION", "pyproject.toml", "vpnshopbot/app.py"}
    result = extract_bundle(output, tmp_path / "extracted", digest(output))
    assert result == metadata
    assert (tmp_path / "extracted/vpnshopbot/app.py").read_text() == "print('release')\n"
    assert output.with_suffix(".gz.sha256").is_file()


def test_wrong_checksum_never_extracts(source, tmp_path):
    output = tmp_path / "v1.tar.gz"
    build_bundle(source, output, "1.0.0")
    with pytest.raises(ValueError, match="SHA-256"):
        extract_bundle(output, tmp_path / "extracted", "0" * 64)
    assert not (tmp_path / "extracted").exists()


@pytest.mark.parametrize(
    "name,kind", [("../escaped.py", "file"), (".env", "file"), ("vpnshopbot/evil.py", "symlink")]
)
def test_malicious_archive_is_rejected(tmp_path, name, kind):
    bundle = tmp_path / "bad.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        metadata = json.dumps(
            {"format": 1, "schema": "additive-only", "version": "1.0.0", "files": {}}
        ).encode()
        member = tarfile.TarInfo("release.json")
        member.size = len(metadata)
        archive.addfile(member, io.BytesIO(metadata))
        member = tarfile.TarInfo(name)
        if kind == "symlink":
            member.type, member.linkname = tarfile.SYMTYPE, "/tmp/escape"
        archive.addfile(member)
    with pytest.raises(ValueError, match="Forbidden"):
        extract_bundle(bundle, tmp_path / "extracted")


def test_secret_scanner_rejects_embedded_telegram_token(source, tmp_path):
    token = "1234567890:" + "A" * 35
    (source / "vpnshopbot/secret.py").write_text(f"TOKEN = '{token}'\n")
    with pytest.raises(ValueError, match="secret"):
        build_bundle(source, tmp_path / "bad.tar.gz", "1.0.0")


def test_atomic_pointer_switch_does_not_change_env(tmp_path):
    runtime = tmp_path / ".runtime"
    release = runtime / "releases/v1"
    release.mkdir(parents=True)
    env = tmp_path / ".env"
    env.write_text("original")
    switch(runtime, "current", release)
    assert (runtime / "current").resolve() == release
    assert env.read_text() == "original"
    with pytest.raises(ValueError, match="outside"):
        switch(runtime, "current", tmp_path)


def test_drain_waits_for_queued_handlers_and_accounts_for_errors():
    queued = []
    bot = SimpleNamespace(worker_pool=SimpleNamespace(put=lambda task: queued.append(task)))
    drain = HandlerDrain(bot)
    bot.worker_pool.put(lambda: None)
    finished = threading.Event()
    waiter = threading.Thread(target=lambda: (drain.wait(), finished.set()))
    waiter.start()
    assert not finished.wait(0.02)
    queued.pop()()
    waiter.join(timeout=2)
    assert finished.is_set()

    def fail():
        raise ValueError("test error")

    bot.worker_pool.put(fail)
    with pytest.raises(ValueError):
        queued.pop()()
    assert drain.pending == 0


def test_hot_exec_preserves_polling_offset_and_process_environment(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime"
    release = runtime / "releases/v2"
    (release / "venv/bin").mkdir(parents=True)
    (release / "venv/bin/python").touch()
    (release / "src/vpnshopbot").mkdir(parents=True)
    (release / "src/vpnshopbot/app.py").touch()
    switch(runtime, "current", release)
    monkeypatch.setenv("VPN_SHOP_RUNTIME_DIR", str(runtime))
    changes = []
    monkeypatch.setattr(os, "chdir", lambda path: changes.append(path))
    monkeypatch.setattr(os, "execve", lambda python, args, env: changes.append((python, args, env)))
    exec_current_release(123)
    assert changes[0] == release / "src"
    assert changes[1][2]["VPN_SHOP_LAST_UPDATE_ID"] == "123"
    assert changes[1][2]["VPN_SHOP_RELEASE_ID"] == "v2"


def test_health_report_is_ready_for_managed_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("VPN_SHOP_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("VPN_SHOP_RELEASE_ID", "v1")
    publish_health()
    report = json.loads((tmp_path / "state/health.json").read_text())
    assert report["release_id"] == "v1" and report["hot_reload"]
    assert report["pid"] == os.getpid()


def test_managed_unit_has_reload_without_stop_command():
    unit = render_service_unit("test_bot.service", "alice", "alice", "/srv/bot", managed=True)
    assert "ExecStart=/srv/bot/deploy/run.sh" in unit
    assert "ExecReload=/bin/kill -HUP $MAINPID" in unit
    assert "TimeoutStopSec=180" in unit
