"""Local release bundles, staged installs and graceful systemd handover."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
import venv
from pathlib import Path, PurePosixPath

from .privileges import privileged

VERSION_RE = re.compile(r"v?\d+(?:\.\d+){0,2}(?:[-.][a-zA-Z0-9]+)*")
ROOT_FILES = {
    "VERSION",
    "README.md",
    "pyproject.toml",
    "brand_api.py",
    "start.sh",
    "update.sh",
    "make-release.sh",
    ".env.example",
    ".gitignore",
    "CHANGELOG.md",
    "requirements.lock",
    "install-prerequisites.sh",
}
PREFIXES = ("vpnshopbot/", "tests/", "deploy/", ".github/")
MAX_BYTES = 50 * 1024 * 1024


def allowed_path(raw):
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or "\\" in raw or str(path) != raw:
        return False
    if any(part.startswith(".env") for part in path.parts) and raw != ".env.example":
        return False
    if any(part in {".git", ".venv", "__pycache__", ".runtime"} for part in path.parts):
        return False
    if raw in ROOT_FILES:
        return True
    return raw.startswith(PREFIXES) and path.suffix in {
        ".py",
        ".sh",
        ".md",
        ".toml",
        ".yml",
        ".yaml",
        ".service",
    }


def source_files(source):
    files = [source / name for name in ROOT_FILES if (source / name).is_file()]
    for prefix in PREFIXES:
        directory = source / prefix.rstrip("/")
        if directory.is_dir() and not directory.is_symlink():
            files.extend(path for path in directory.rglob("*") if path.is_file())
    return sorted(
        path
        for path in files
        if not path.is_symlink() and allowed_path(path.relative_to(source).as_posix())
    )


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_no_secrets(source, configuration=None):
    known = []
    if configuration is not None and configuration.is_file():
        from sqlalchemy.engine import make_url

        from .config import Settings

        settings = Settings(_env_file=configuration)
        known = [settings.bot_token.get_secret_value(), settings.card_number]
        for url in (settings.bot_db_url, settings.panel_db_url):
            password = make_url(url.get_secret_value()).password
            if password:
                known.append(password)
    known = [value.encode() for value in known if len(value) >= 8 and value != "CHANGE_ME"]
    for path in source_files(source):
        content = path.read_bytes()
        if re.search(rb"\b[0-9]{8,12}:[A-Za-z0-9_-]{30,}\b", content) or any(
            value in content for value in known
        ):
            raise ValueError(f"Possible real secret in release file: {path.relative_to(source)}")


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def manifest(source, version):
    if not VERSION_RE.fullmatch(version):
        raise ValueError("Version must look like v1, 1.0.0 or 1.1.0")
    files = {path.relative_to(source).as_posix(): digest(path) for path in source_files(source)}
    if not {"vpnshopbot/app.py", "pyproject.toml", "VERSION"}.issubset(files):
        raise ValueError("Release is missing required files")
    return {"format": 1, "version": version, "schema": "additive-only", "files": files}


def build_bundle(source, output, version):
    assert_no_secrets(source, source / ".env")
    description = manifest(source, version)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vpn-release-") as scratch:
        metadata = Path(scratch) / "release.json"
        metadata.write_text(json.dumps(description), encoding="utf-8")
        with tarfile.open(output, "w:gz") as archive:
            archive.add(metadata, arcname="release.json", recursive=False)
            for name in description["files"]:
                archive.add(source / name, arcname=name, recursive=False)
    output.with_suffix(output.suffix + ".sha256").write_text(
        f"{digest(output)}  {output.name}\n", encoding="utf-8"
    )
    return description


def extract_bundle(bundle, target, checksum=None):
    if checksum and digest(bundle) != checksum.lower():
        raise ValueError("Bundle SHA-256 does not match")
    with tarfile.open(bundle, "r:gz") as archive:
        entries = archive.getmembers()
        if sum(item.size for item in entries) > MAX_BYTES or len(entries) > 3000:
            raise ValueError("Release archive is too large")
        names = [item.name for item in entries]
        if len(names) != len(set(names)) or "release.json" not in names:
            raise ValueError("Missing manifest or duplicate archive members")
        for item in entries:
            if not item.isfile() or (item.name != "release.json" and not allowed_path(item.name)):
                raise ValueError(f"Forbidden release path: {item.name}")
        description = json.load(archive.extractfile("release.json"))
        if description.get("format") != 1 or description.get("schema") != "additive-only":
            raise ValueError("Unsupported release format or schema compatibility")
        if not VERSION_RE.fullmatch(str(description.get("version", ""))):
            raise ValueError("Invalid release version")
        if set(names) != set(description["files"]) | {"release.json"}:
            raise ValueError("Manifest does not cover every file")
        target.mkdir(parents=True, exist_ok=True)
        for item in entries:
            if item.name == "release.json":
                continue
            data = archive.extractfile(item).read()
            if hashlib.sha256(data).hexdigest() != description["files"][item.name]:
                raise ValueError(f"Hash mismatch: {item.name}")
            destination = target / item.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            destination.chmod(0o755 if item.name.endswith(".sh") else 0o644)
    return description


def copy_source(source, target):
    target.mkdir(parents=True, exist_ok=True)
    for path in source_files(source):
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def candidate_env(project):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["VPN_SHOP_ENV_FILE"] = str(project / ".env")
    env.pop("VPN_SHOP_RUNTIME_DIR", None)
    env.pop("VPN_SHOP_RELEASE_ID", None)
    return env


def prepare_runtime(release, project):
    assert_no_secrets(release / "src", project / ".env")
    print("Preparing isolated dependencies; the current bot stays running...", flush=True)
    venv.EnvBuilder(with_pip=True).create(release / "venv")
    python = release / "venv/bin/python"
    env = candidate_env(project)
    env["PIP_PROGRESS_BAR"] = "off"
    dependency_lock = release / "src/requirements.lock"
    if dependency_lock.is_file():
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                str(dependency_lock),
            ],
            cwd=release / "src",
            env=env,
            check=True,
        )
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            *(["--no-deps"] if dependency_lock.is_file() else []),
            str(release / "src"),
        ],
        cwd=release / "src",
        env=env,
        check=True,
    )
    subprocess.run(
        [str(python), "-m", "compileall", "-q", "vpnshopbot"],
        cwd=release / "src",
        env=env,
        check=True,
    )
    # create_schema only adds absent tables. Destructive migrations are not supported by this updater.
    for argument in ("--init-db", "--check"):
        subprocess.run(
            [str(python), "-m", "vpnshopbot.app", argument],
            cwd=release / "src",
            env=env,
            check=True,
        )


def switch(runtime, name, release):
    release = release.resolve(strict=True)
    if release.parent != runtime / "releases":
        raise ValueError("Release path is outside managed releases")
    temporary = runtime / f".{name}-{uuid.uuid4().hex}"
    temporary.symlink_to(release, target_is_directory=True)
    temporary.replace(runtime / name)


def health(runtime):
    for path in (runtime / "state/health.json", runtime / "health.json"):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {}


def handover(runtime, service, release, timeout=180):
    started = time.time()
    switch(runtime, "current", release)
    subprocess.run(
        privileged(["systemctl", "kill", "--kill-who=main", "--signal=HUP", service]), check=True
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        report = health(runtime)
        if report.get("release_id") == release.name and report.get("ready_at", 0) >= started:
            result = subprocess.run(
                ["systemctl", "is-active", service], capture_output=True, text=True, check=False
            )
            if result.stdout.strip() == "active":
                return report
        time.sleep(1)
    raise TimeoutError(
        "New release did not become ready; do not kill in-flight financial operations"
    )


def new_release(runtime, version):
    if not VERSION_RE.fullmatch(version):
        raise ValueError("Invalid version")
    release = runtime / "releases" / f"{version}-{uuid.uuid4().hex[:10]}"
    release.mkdir(parents=True)
    return release


def prepare_initial(project):
    runtime = project / ".runtime"
    if (runtime / "current").is_symlink():
        return
    release = new_release(runtime, (project / "VERSION").read_text().strip())
    copy_source(project, release / "src")
    prepare_runtime(release, project)
    switch(runtime, "current", release)


def update(project, args):
    from .config import Settings
    from .setup import service_slug

    settings = Settings(_env_file=project / ".env")
    service = f"{service_slug(settings.brand_name)}_bot.service"
    runtime = project / ".runtime"
    runtime.mkdir(exist_ok=True)
    with (runtime / "update.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.adopt:
            from .setup import install_service

            prepare_initial(project)
            install_service(service, project_dir=project)
            print("Managed launcher installed. Future updates use HUP, not systemctl restart.")
            return
        if not (runtime / "current").is_symlink() or not health(runtime).get("hot_reload"):
            raise ValueError(
                "One-time launcher setup required: ./update.sh --adopt (controlled restart)"
            )
        current = (runtime / "current").resolve(strict=True)
        if args.rollback:
            candidate = (runtime / "previous").resolve(strict=True)
            if candidate.parent != runtime / "releases" or candidate == current:
                raise ValueError("No previous managed release is available")
        else:
            candidate = (
                new_release(runtime, args.version or "candidate")
                if args.version
                else runtime / "releases" / f"staged-{uuid.uuid4().hex[:10]}"
            )
            candidate.mkdir(parents=True, exist_ok=True)
            if args.patch:
                if not args.version:
                    raise ValueError("--patch requires --version")
                copy_source(current / "src", candidate / "src")
                patch = Path(args.patch).resolve(strict=True)
                subprocess.run(["git", "init", "-q", str(candidate / "src")], check=True)
                for options in (["--check"], []):
                    subprocess.run(
                        ["git", "-C", str(candidate / "src"), "apply", *options, str(patch)],
                        check=True,
                    )
                if any(
                    path.is_symlink()
                    or (
                        path.is_file()
                        and not allowed_path(path.relative_to(candidate / "src").as_posix())
                    )
                    for path in (candidate / "src").rglob("*")
                    if ".git" not in path.relative_to(candidate / "src").parts
                ):
                    raise ValueError("Patch introduced a forbidden file")
                description = manifest(candidate / "src", args.version)
            else:
                if not args.bundle:
                    raise ValueError("Provide a release bundle or --patch")
                description = extract_bundle(
                    Path(args.bundle).resolve(strict=True), candidate / "src", args.sha256
                )
            atomic_json(candidate / "release.json", description)
            prepare_runtime(candidate, project)
        previous_pid = health(runtime).get("pid")
        if os.geteuid() != 0:
            subprocess.run(["sudo", "-v"], check=True)
        try:
            report = handover(runtime, service, candidate)
        except Exception:
            print("Handover failed; restoring the previous release pointer...", flush=True)
            switch(runtime, "current", current)
            try:
                subprocess.run(
                    privileged(["systemctl", "kill", "--kill-who=main", "--signal=HUP", service]),
                    check=True,
                )
            except subprocess.CalledProcessError:
                print(
                    "systemd will retry the previous release through Restart=on-failure; inspect journalctl."
                )
            raise
        switch(runtime, "previous", current)
        print(
            f"Update ready: {candidate.name}; main PID {report['pid']} (previous {previous_pid})."
        )
        print("Configuration, root accounts, wallets and database records were preserved.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    bundle = commands.add_parser("bundle")
    bundle.add_argument("--output", type=Path, required=True)
    bundle.add_argument("--version")
    command = commands.add_parser("update")
    command.add_argument("bundle", nargs="?")
    command.add_argument("--sha256")
    command.add_argument("--patch")
    command.add_argument("--version")
    command.add_argument("--adopt", action="store_true")
    command.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    project = args.project.resolve(strict=True)
    if args.command == "bundle":
        version = args.version or (project / "VERSION").read_text().strip()
        result = build_bundle(project, args.output.resolve(), version)
        print(f"Release {result['version']}: {args.output}")
    else:
        if os.geteuid() == 0:
            os.umask(0o022)
        try:
            update(project, args)
        except (ValueError, OSError, subprocess.CalledProcessError, TimeoutError) as exc:
            raise SystemExit(f"Update aborted: {exc}") from exc


if __name__ == "__main__":
    main()
