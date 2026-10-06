# Test the public branch directly on the LAN

These Bash commands assume the documented production installation at
`/appdata/dillflix-live-controller` and its `dillflix-controller.service` unit.
They use the existing Prime Player service for real playback. Leave
`dillflix-prime-player.service` running. Everyone who can reach this test server
can use its admin interface; neither app requires a login, nginx, or PlexSSO.

The branch upgrades the database schema. Use a separate checkout and a copy of
the stopped production data so rollback retains the original database and image.
Never run the production and test controllers against the device simultaneously.

## First launch

Run on the production Linux host with Docker access. The checkout and volume
names must be unused. If a previous test checkout already exists, use the update
section instead.

```bash
(
set -euo pipefail
PROD=/appdata/dillflix-live-controller
TEST=/appdata/dillflix-live-controller-public-test
PROJECT=dillflix-public-test

cd "$PROD"
git fetch origin codex/public-guest-mode
git worktree add --detach "$TEST" origin/codex/public-guest-mode
install -m 600 "$PROD/.env" "$TEST/.env"

python3 - "$TEST/.env" <<'PY'
import pathlib, re, sys
path = pathlib.Path(sys.argv[1])
lines = [line for line in path.read_text().splitlines() if not re.match(
    r"^\s*(?:export\s+)?CONTROLLER_(PUBLIC_AUTH_MODE|ADMIN_AUTH_MODE|PROXY_SECRET)\s*=", line
)]
lines += ["CONTROLLER_PUBLIC_AUTH_MODE=guest", "CONTROLLER_ADMIN_AUTH_MODE=trusted-lan",
          "CONTROLLER_PROXY_SECRET="]
path.write_text("\n".join(lines) + "\n")
PY

container=$(docker compose -f compose.yaml -f compose.prime-player.yaml ps -q controller)
test -n "$container"
volume=$(docker inspect --format \
  '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$container")
test -n "$volume"

cd "$TEST"
dc() {
  docker compose -p "$PROJECT" -f compose.yaml -f compose.prime-player.yaml "$@"
}
if docker volume inspect "${PROJECT}_controller-data" >/dev/null 2>&1; then
  echo "Test volume already exists; production has not been changed."
  exit 1
fi
dc build controller
sudo systemctl disable --now dillflix-controller.service

dc run --rm -T --no-deps --user 0 --entrypoint python \
  -v "${volume}:/production-data:ro" controller - <<'PY'
import os, shutil
shutil.copytree("/production-data", "/data", dirs_exist_ok=True)
for root, dirs, files in os.walk("/data"):
    os.chown(root, 10001, 10001)
    for name in dirs + files:
        os.chown(os.path.join(root, name), 10001, 10001)
PY

dc up -d --no-build --wait --wait-timeout 120
dc ps
)
```

If launch fails after production stops, use the rollback commands below.

Open `http://YOUR-SERVER:8790/` for admin and
`http://YOUR-SERVER:8790/public/` for guests. Use your existing port and bind address
if different. In the admin app, enable **Settings → Public app → Allow public
Play now / Allow public Add to plan**. Public actions default off.

## Update a test checkout created using the earlier instructions

This keeps its copied database and applies direct access without any proxy secret.
Run only against the disposable test checkout, with production stopped before
starting the test container:

```bash
(
set -euo pipefail
cd /appdata/dillflix-live-controller-public-test
git fetch origin codex/public-guest-mode
git switch --detach origin/codex/public-guest-mode

python3 - <<'PY'
import pathlib, re
path = pathlib.Path(".env")
lines = [line for line in path.read_text().splitlines() if not re.match(
    r"^\s*(?:export\s+)?CONTROLLER_(PUBLIC_AUTH_MODE|ADMIN_AUTH_MODE|PROXY_SECRET)\s*=", line
)]
lines += ["CONTROLLER_PUBLIC_AUTH_MODE=guest", "CONTROLLER_ADMIN_AUTH_MODE=trusted-lan",
          "CONTROLLER_PROXY_SECRET="]
path.write_text("\n".join(lines) + "\n")
PY

docker compose -p dillflix-public-test -f compose.yaml -f compose.prime-player.yaml build controller
sudo systemctl disable --now dillflix-controller.service
docker compose -p dillflix-public-test -f compose.yaml -f compose.prime-player.yaml \
  up -d --no-build --wait --wait-timeout 120
)
```

## Restore production

```bash
cd /appdata/dillflix-live-controller-public-test
docker compose -p dillflix-public-test -f compose.yaml -f compose.prime-player.yaml down &&
sudo systemctl enable --now dillflix-controller.service
sudo systemctl status dillflix-controller.service --no-pager
```

Test changes stay in the separate test volume. The production checkout and
database are unchanged. These commands have not been executed on the production
host; Docker and real playback require host acceptance testing.
