import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "stack_install", Path(__file__).resolve().parents[1] / "tools/install-prime-stack.py"
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_persistent_configuration_preserves_secrets_and_removes_duplicate_managed_keys():
    before = "# retained\nTEAMARR_URL=http://teamarr:9195\nTEAMARR_TOKEN=secret\nPRIME_PLAYER_STATE_DIR=old\nPRIME_PLAYER_STATE_DIR=duplicate\n"
    values = {
        "PRIME_PLAYER_STATE_DIR": "/home/user/state",
        "COMPOSE_FILE": "compose.yaml:compose.prime-player.yaml",
    }
    after = installer.update_env(before, values)
    assert "TEAMARR_TOKEN=secret\n" in after
    assert after.count("PRIME_PLAYER_STATE_DIR=") == 1
    assert installer.update_env(after, values) == after


def test_boot_start_uses_existing_images_and_volumes_without_dependency_restart():
    unit = installer.controller_unit(Path("/appdata/dillflix-live-controller"), "/usr/bin/docker")
    assert "Wants=dillflix-prime-player.service" in unit
    assert "After=docker.service dillflix-prime-player.service" in unit
    assert "up -d --no-build" in unit
    assert "compose.yaml -f compose.prime-player.yaml stop" in unit
    assert " down" not in unit and "--volumes" not in unit
