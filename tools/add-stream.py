"""Add one player instance and controller stream without restarting existing streams."""

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from controller.config import DeviceSettings, Settings
from controller.executor.config import ExecutorConfig

MOUNT = PurePosixPath("/run/prime-player")
MANAGED = {"CONTROLLER_DEVICES_FILE", "CONTROLLER_DEVICES_JSON"}


def env_value(text, key):
    values = []
    for line in text.splitlines():
        match = re.match(r"^\s*(?:export\s+)?" + re.escape(key) + r"\s*=\s*(.*)$", line)
        if not match:
            continue
        raw = match[1].strip()
        if raw.startswith(("'", '"')):
            parsed = shlex.split(raw, comments=True)
            if len(parsed) != 1:
                raise ValueError(f"Invalid quoted {key}")
            values.append(parsed[0])
        else:
            values.append(raw.split(" #", 1)[0].strip())
    if len(set(values)) > 1:
        raise ValueError(f"Conflicting {key} values in .env")
    return values[-1] if values else ""


def validate_devices(records):
    if not isinstance(records, list) or not records:
        raise ValueError("Device configuration must be a nonempty JSON array")
    devices = tuple(DeviceSettings(**record) for record in records)
    Settings(devices=devices, executor=ExecutorConfig(mode="prime-player"))
    if devices[0].id != "living-room":
        raise ValueError("Keep the existing living-room stream first; this helper does not rename devices")
    return records


def prepare_update(env_text, state_root, *, device_id, name, serial):
    """Build and validate an additive registry; leave every existing row intact."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", device_id):
        raise ValueError("Stream ID must be 1..48 lowercase letters, digits or hyphens")
    if not serial or any(c in serial for c in '\n\r"\\%$`') or serial != serial.strip():
        raise ValueError("Provide a literal ADB serial without whitespace or environment expansion")
    state_root = Path(state_root).resolve()
    inline = env_value(env_text, "CONTROLLER_DEVICES_JSON")
    configured = env_value(env_text, "CONTROLLER_DEVICES_FILE")
    if inline and configured:
        raise ValueError("Set only one device registry source")
    registry = state_root / "controller-devices.json"
    if configured:
        container_path = PurePosixPath(configured)
        if ".." in container_path.parts:
            raise ValueError("Device registry must stay inside the existing player mount")
        try:
            relative = container_path.relative_to(MOUNT)
        except ValueError as exc:
            raise ValueError("Device registry must be inside /run/prime-player for this helper") from exc
        registry = (state_root / str(relative)).resolve()
        if not registry.is_relative_to(state_root):
            raise ValueError("Device registry resolves outside the existing player state directory")
        records = json.loads(registry.read_text(encoding="utf-8"))
    elif inline:
        if registry.exists():
            raise ValueError("An unconfigured controller-devices.json already exists; review it before adding a stream")
        records = json.loads(inline)
    else:
        if registry.exists():
            raise ValueError("An unconfigured controller-devices.json already exists; review it before adding a stream")
        original_serial = env_value(env_text, "SCREEN_ADB_SERIAL")
        if not original_serial or any(c in original_serial for c in "$`\n\r"):
            raise ValueError("Existing .env must contain the first stick's literal SCREEN_ADB_SERIAL")
        records = [{"id": "living-room", "name": "Dillflix 1",
                    "prime_player_socket": str(MOUNT / "player.sock"), "adb_serial": original_serial}]
    validate_devices(records)
    original_serial = env_value(env_text, "SCREEN_ADB_SERIAL")
    if original_serial and records[0]["adb_serial"] != original_serial:
        raise ValueError("Existing living-room registry serial disagrees with SCREEN_ADB_SERIAL")
    records = [*records, {"id": device_id, "name": name,
        "prime_player_socket": str(MOUNT / "instances" / device_id / "player.sock"), "adb_serial": serial}]
    validate_devices(records)
    keep = [line for line in env_text.splitlines()
            if not re.match(r"^\s*(?:export\s+)?(?:CONTROLLER_DEVICES_FILE|CONTROLLER_DEVICES_JSON)\s*=", line)]
    target = str(MOUNT / registry.relative_to(state_root).as_posix())
    updated_env = "\n".join(keep + [f'CONTROLLER_DEVICES_FILE="{target}"']) + "\n"
    return registry, updated_env, records


def atomic_write(path, data, *, uid=None, gid=None, mode=0o600):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        if uid is not None and gid is not None:
            os.chown(temporary, uid, gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply_update(envfile, registry, updated_env, records, *, validate, install,
                 uid=None, gid=None, cleanup=None):
    """Publish validated config with backups; restore both files on any failure."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    old = {}
    backups = []
    for path in (envfile, registry):
        if path.exists():
            info = path.stat()
            old[path] = (path.read_bytes(), info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
            backup = path.with_name(path.name + ".before-add-stream-" + stamp)
            atomic_write(backup, old[path][0])
            backups.append(backup)
        else:
            old[path] = None
    installation_started = False
    try:
        atomic_write(registry, (json.dumps(records, indent=2) + "\n").encode(), uid=uid, gid=gid, mode=0o640)
        atomic_write(envfile, updated_env.encode(), uid=uid, gid=gid)
        validate()
        installation_started = True
        install()
    except BaseException:
        try:
            for path, snapshot in old.items():
                if snapshot is None:
                    path.unlink(missing_ok=True)
                else:
                    data, old_uid, old_gid, mode = snapshot
                    atomic_write(path, data, uid=old_uid if uid is not None else None,
                                 gid=old_gid if gid is not None else None, mode=mode)
        finally:
            if installation_started and cleanup is not None:
                cleanup()
        raise
    return backups


def main():
    import fcntl
    import grp
    import pwd

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True, dest="device_id")
    parser.add_argument("--name", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--player-repo", required=True, type=Path)
    parser.add_argument("--user", default=os.environ.get("SUDO_USER"))
    parser.add_argument("--network-config", type=Path)
    parser.add_argument("--server-binary", type=Path)
    parser.add_argument("--adb-key", type=Path)
    parser.add_argument("--activate", action="store_true",
                        help="Install/start a qualified second player and publish the registry; default only previews")
    args = parser.parse_args()
    if os.geteuid() != 0 or not args.user or args.user == "root":
        parser.error("Run with sudo from the existing deployment account, or specify --user")
    account = pwd.getpwnam(args.user)
    envfile = REPO / ".env"
    docker = shutil.which("docker")
    if not docker or not envfile.is_file():
        parser.error("An existing .env and Docker Compose installation are required")
    env_text = envfile.read_text(encoding="utf-8")
    source = env_value(env_text, "PRIME_PLAYER_STATE_DIR")
    if not source or not Path(source).is_absolute() or "$" in source or "`" in source:
        parser.error(".env must contain the literal absolute PRIME_PLAYER_STATE_DIR already mounted by Compose")
    state = Path(source).resolve()
    if not state.is_dir():
        parser.error("The existing player state directory is missing")
    with open(state / ".controller-add-stream.lock", "a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if envfile.read_text(encoding="utf-8") != env_text:
            parser.error("Controller configuration changed while preparing; rerun to use the latest settings")
        registry, updated_env, records = prepare_update(env_text, state, device_id=args.device_id,
                                                       name=args.name, serial=args.serial)
        compose_files = env_value(env_text, "COMPOSE_FILE").split(":")
        if "compose.prime-player.yaml" not in compose_files:
            parser.error("The existing COMPOSE_FILE must include compose.prime-player.yaml and its player-directory mount")
        service = "dillflix-prime-player@" + args.device_id + ".service"
        unit = Path("/etc/systemd/system") / service
        config = Path("/etc/dillflix-prime-player") / (args.device_id + ".env")
        load = subprocess.run(["systemctl", "show", service, "-p", "LoadState", "--value"],
                              capture_output=True, text=True, check=True).stdout.strip()
        if unit.exists() or config.exists() or load != "not-found":
            parser.error("That player instance already exists; this helper only adds a new instance")
        installed_configs = [Path("/etc/dillflix-prime-player.env"),
                             *Path("/etc/dillflix-prime-player").glob("*.env")]
        for installed in installed_configs:
            if installed.is_file() and env_value(installed.read_text(), "PRIME_PLAYER_SERIAL") == args.serial:
                parser.error("That ADB serial is already assigned to an installed player service")
        if not args.activate:
            print("Proposed device registry (not published):")
            print(json.dumps(records, indent=2))
            print("No player was installed, enabled or started; controller configuration is unchanged.")
            print("Qualify the second stick's model, architecture, root and Prime runtime before rerunning with --activate.")
            return
        player_repo = args.player_repo.resolve()
        spec = importlib.util.spec_from_file_location("stream_player_installer", player_repo / "deploy/install-service.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        import inspect
        if "instance" not in inspect.signature(installer.install).parameters:
            parser.error("Update the player checkout before adding a stream")
        group = grp.getgrnam("dillflix-prime")
        command = [docker, "compose", "--env-file", str(envfile)]
        compose_env = {k: v for k, v in os.environ.items()
                       if k not in MANAGED | {"COMPOSE_FILE", "PRIME_PLAYER_STATE_DIR", "PRIME_PLAYER_SOCKET_GID"}}

        def validate():
            subprocess.run(command + ["config", "--quiet"], cwd=REPO, env=compose_env, check=True)

        def install():
            installer.install(args.user, args.serial, player_repo, state / "instances" / args.device_id,
                              args.server_binary, instance=args.device_id, network_config=args.network_config,
                              adb_key=args.adb_key)

        def cleanup():
            # These exact instance paths were confirmed absent before installation.
            # Keep the unit for inspection if stopping it cannot be confirmed.
            if unit.exists():
                subprocess.run(["systemctl", "disable", "--now", service], check=True)
            unit.unlink(missing_ok=True)
            config.unlink(missing_ok=True)
            subprocess.run(["systemctl", "daemon-reload"], check=True)

        backups = apply_update(envfile, registry, updated_env, records, uid=account.pw_uid,
                               gid=group.gr_gid, validate=validate, install=install, cleanup=cleanup)
    print("Added stream:", args.device_id)
    for path in backups:
        print("Configuration backup:", path)
    print("Existing player services and controller were not restarted.")
    print("Apply controller configuration: docker compose up -d --build controller")
    print("Observe the new player: sudo journalctl -u " + service + " -f")
    print("API availability precedes runtime readiness; startup and adaptation can take several minutes.")


if __name__ == "__main__":
    main()
