"""Install boot services and persist Compose settings without displaying secrets."""

import argparse
import importlib.util
import os
import pwd
import re
import shutil
import subprocess
from pathlib import Path


def update_env(text, values):
    pattern = re.compile(r"^(?:export\s+)?(" + "|".join(map(re.escape, values)) + r")\s*=")
    kept = [line for line in text.splitlines() if not pattern.match(line)]
    for key, value in values.items():
        if any(c in str(value) for c in '\n\r"\\$`'):
            raise ValueError("Unsupported environment value for " + key)
        kept.append(f'{key}="{value}"')
    return "\n".join(kept) + "\n"


def controller_unit(repo, docker):
    for value in (str(repo), docker):
        if any(c in value for c in '\n\r"\\%'):
            raise ValueError("Unsupported service path")
    return f'''[Unit]
Description=Dillflix Controller Compose stack
Requires=docker.service
Wants=dillflix-prime-player.service
After=docker.service dillflix-prime-player.service network-online.target
StartLimitIntervalSec=0

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory={repo}
ExecStart="{docker}" compose -f compose.yaml -f compose.prime-player.yaml up -d --no-build
ExecStop="{docker}" compose -f compose.yaml -f compose.prime-player.yaml stop
TimeoutStartSec=120
TimeoutStopSec=120
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--player-repo", required=True, type=Path)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--user", default=os.environ.get("SUDO_USER"))
    parser.add_argument("--server-binary")
    args = parser.parse_args()
    if os.geteuid() != 0 or not args.user or args.user == "root":
        parser.error("Run with sudo as the deployment account, or specify --user")
    repo = Path(__file__).resolve().parents[1]
    envfile = repo / ".env"
    docker = shutil.which("docker")
    if not docker or not envfile.exists():
        parser.error("Docker Compose and an existing .env with TEAMARR_URL are required")
    account = pwd.getpwnam(args.user)
    spec = importlib.util.spec_from_file_location(
        "player_install", args.player_repo / "deploy/install-service.py"
    )
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    # Retain the existing Teamarr URL and all credentials; reject empty setup.
    source_env = envfile.read_text()
    url = re.search(r"^TEAMARR_URL\s*=\s*(.+)$", source_env, re.MULTILINE)
    if not url or not url.group(1).strip().strip("\"'"):
        parser.error("Configure TEAMARR_URL in .env before installation")
    state = Path(account.pw_dir) / ".local/state/dillflix-prime-player"
    state, gid = installer.install(args.user, args.serial, args.player_repo, state, args.server_binary)
    text = update_env(
        envfile.read_text(),
        {
            "PRIME_PLAYER_STATE_DIR": state,
            "PRIME_PLAYER_SOCKET_GID": gid,
            "CONTROLLER_MODE": "teamarr",
            "SCREEN_ADB_SERIAL": args.serial,
            "NAVIGATION_TIMEOUT_SECONDS": 300,
            "COMPOSE_FILE": "compose.yaml:compose.prime-player.yaml",
        },
    )
    envfile.write_text(text)
    os.chown(envfile, account.pw_uid, account.pw_gid)
    os.chmod(envfile, 0o600)
    # Use the same project directory/name and named volume as previous commands.
    command = [docker, "compose", "-f", "compose.yaml", "-f", "compose.prime-player.yaml"]
    subprocess.run(command + ["config", "--quiet"], cwd=repo, check=True)
    subprocess.run(command + ["build"], cwd=repo, check=True)
    Path("/etc/systemd/system/dillflix-controller.service").write_text(controller_unit(repo, docker))
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "docker.service"], check=True)
    subprocess.run(["systemctl", "enable", "dillflix-controller.service"], check=True)
    subprocess.run(["systemctl", "restart", "dillflix-controller.service"], check=True)
    print("Installed boot services. Logs: journalctl -u dillflix-prime-player -f")


if __name__ == "__main__":
    main()
