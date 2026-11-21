"""Graceful in-process release handover: systemd's main PID remains unchanged."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


class HandlerDrain:
    def __init__(self, bot):
        self.pending = 0
        self.condition = threading.Condition()
        pool = getattr(bot, "worker_pool", None)
        if pool is None:
            return
        original = pool.put

        def put(function, *args, **kwargs):
            with self.condition:
                self.pending += 1

            def run():
                try:
                    return function(*args, **kwargs)
                finally:
                    with self.condition:
                        self.pending -= 1
                        self.condition.notify_all()

            try:
                original(run)
            except BaseException:
                with self.condition:
                    self.pending -= 1
                    self.condition.notify_all()
                raise

        pool.put = put

    def wait(self):
        with self.condition:
            self.condition.wait_for(lambda: self.pending == 0)


def runtime_directory():
    raw = os.environ.get("VPN_SHOP_RUNTIME_DIR")
    return Path(raw).resolve() if raw else None


def join_polling_threads():
    # TeleBot 4.x stops its polling worker without joining the outstanding getUpdates call.
    # It may still enqueue handlers until that call returns, so drain only after joining it.
    for thread in threading.enumerate():
        if thread.name == "PollingThread" and thread is not threading.current_thread():
            thread.join()


def publish_health():
    runtime = runtime_directory()
    if not runtime:
        return
    state = runtime / "state"
    state.mkdir(parents=True, exist_ok=True)
    destination = state / "health.json"
    temporary = state / f".health-{os.getpid()}.tmp"
    temporary.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "release_id": os.environ.get("VPN_SHOP_RELEASE_ID"),
                "ready_at": time.time(),
                "hot_reload": True,
            }
        ),
        encoding="utf-8",
    )
    temporary.replace(destination)


def exec_current_release(last_update_id=0):
    runtime = runtime_directory()
    if runtime is None:
        raise RuntimeError("Hot updates require the managed launcher")
    release = (runtime / "current").resolve(strict=True)
    if release.parent != runtime / "releases":
        raise RuntimeError("Release path is outside the release directory")
    python = release / "venv/bin/python"
    if not python.is_file() or not (release / "src/vpnshopbot/app.py").is_file():
        raise RuntimeError("Release is incomplete")
    env = dict(
        os.environ,
        VPN_SHOP_RELEASE_ID=release.name,
        VPN_SHOP_LAST_UPDATE_ID=str(int(last_update_id)),
    )
    os.chdir(release / "src")
    os.execve(str(python), [str(python), "-m", "vpnshopbot.app"], env)
