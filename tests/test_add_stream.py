import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location("add_stream", Path(__file__).resolve().parents[1] / "tools/add-stream.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

ENV = ('# deployment\nTEAMARR_TOKEN="retain-secret"\nSCREEN_ADB_SERIAL=first:5555\n'
       'COMPOSE_FILE=compose.yaml:compose.prime-player.yaml\n')


def prepare(tmp_path, env=ENV, **kwargs):
    return helper.prepare_update(env, tmp_path, device_id=kwargs.get("device_id", "stream-2"),
        name=kwargs.get("name", "Stream 2"), serial=kwargs.get("serial", "second:5555"))


def test_first_add_preserves_existing_primary_and_every_other_environment_setting(tmp_path):
    registry, env, records = prepare(tmp_path)
    assert registry == tmp_path / "controller-devices.json"
    assert env.startswith(ENV)
    assert helper.env_value(env, "CONTROLLER_DEVICES_FILE") == "/run/prime-player/controller-devices.json"
    assert records == [
        {"id": "living-room", "name": "Living room", "prime_player_socket": "/run/prime-player/player.sock", "adb_serial": "first:5555"},
        {"id": "stream-2", "name": "Stream 2", "prime_player_socket": "/run/prime-player/instances/stream-2/player.sock", "adb_serial": "second:5555"},
    ]
    assert not registry.exists()


def test_later_add_keeps_all_existing_rows_and_the_custom_registry_path(tmp_path):
    _, env, records = prepare(tmp_path)
    custom = tmp_path / "devices.json"
    custom.write_text(json.dumps(records))
    env = env.replace("controller-devices.json", "devices.json")
    registry, updated, expanded = prepare(tmp_path, env, device_id="stream-3", serial="third:5555")
    assert registry == custom
    assert expanded[:2] == records
    assert helper.env_value(updated, "CONTROLLER_DEVICES_FILE") == "/run/prime-player/devices.json"
    assert json.loads(custom.read_text()) == records


def test_inline_registry_moves_to_file_without_losing_rows(tmp_path):
    _, _, records = prepare(tmp_path)
    env = ENV + "CONTROLLER_DEVICES_JSON='" + json.dumps(records) + "'\n"
    _, updated, expanded = prepare(tmp_path, env, device_id="stream-3", serial="third:5555")
    assert expanded[:2] == records
    assert helper.env_value(updated, "CONTROLLER_DEVICES_JSON") == ""


@pytest.mark.parametrize("kwargs", [{"device_id": "living-room"}, {"serial": "first:5555"},
    {"device_id": "../escape"}, {"serial": "$SECOND_SERIAL"}, {"name": ""}])
def test_conflicts_fail_without_changing_configuration(tmp_path, kwargs):
    with pytest.raises((ValueError, TypeError)):
        prepare(tmp_path, **kwargs)
    assert list(tmp_path.iterdir()) == []


def test_unconfigured_registry_is_never_overwritten(tmp_path):
    path = tmp_path / "controller-devices.json"
    path.write_text("keep")
    with pytest.raises(ValueError, match="unconfigured"):
        prepare(tmp_path)
    assert path.read_text() == "keep"


@pytest.mark.parametrize("path", ["/config/devices.json", "/run/prime-player/../elsewhere.json"])
def test_registry_cannot_escape_existing_mount(tmp_path, path):
    with pytest.raises(ValueError, match="registry"):
        prepare(tmp_path, ENV + f'CONTROLLER_DEVICES_FILE="{path}"\n')


def test_success_backs_up_exact_previous_files_and_validates_before_install(tmp_path):
    envfile = tmp_path / ".env"
    envfile.write_text(ENV)
    registry, env, records = prepare(tmp_path)
    before = envfile.read_bytes()
    actions = []

    def validate():
        assert json.loads(registry.read_text()) == records
        assert envfile.read_text() == env
        actions.append("validate")

    backups = helper.apply_update(envfile, registry, env, records, validate=validate,
                                  install=lambda: actions.append("install"))
    assert actions == ["validate", "install"]
    assert len(backups) == 1 and backups[0].read_bytes() == before


@pytest.mark.parametrize("phase", ["validate", "install", "cleanup"])
def test_failure_restores_both_existing_files_even_if_service_cleanup_fails(tmp_path, phase):
    envfile = tmp_path / ".env"
    registry = tmp_path / "controller-devices.json"
    envfile.write_bytes(b"original env\r\n")
    registry.write_bytes(b"original registry\r\n")
    install = Mock(side_effect=RuntimeError("install failed") if phase in {"install", "cleanup"} else None)
    validate = Mock(side_effect=RuntimeError("validation failed") if phase == "validate" else None)
    cleanup = Mock(side_effect=RuntimeError("cleanup failed") if phase == "cleanup" else None)
    with pytest.raises(RuntimeError):
        helper.apply_update(envfile, registry, "replacement", [], validate=validate, install=install, cleanup=cleanup)
    assert envfile.read_bytes() == b"original env\r\n"
    assert registry.read_bytes() == b"original registry\r\n"
    assert len(list(tmp_path.glob("*.before-add-stream-*"))) == 2
    if phase == "validate":
        install.assert_not_called()
        cleanup.assert_not_called()
    else:
        cleanup.assert_called_once()


def test_failed_first_add_removes_only_new_registry_and_preserves_env(tmp_path):
    envfile = tmp_path / ".env"
    envfile.write_text(ENV)
    registry, env, records = prepare(tmp_path)
    with pytest.raises(RuntimeError):
        helper.apply_update(envfile, registry, env, records,
            validate=Mock(side_effect=RuntimeError("invalid compose")), install=Mock())
    assert not registry.exists()
    assert envfile.read_text() == ENV


def test_default_command_only_previews_unqualified_hardware(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    state.mkdir()
    envfile = tmp_path / ".env"
    original = ENV + f"PRIME_PLAYER_STATE_DIR='{state.as_posix()}'\n"
    envfile.write_text(original)
    monkeypatch.setattr(helper, "REPO", tmp_path)
    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(flock=Mock(), LOCK_EX=2, LOCK_NB=4))
    monkeypatch.setitem(sys.modules, "grp", SimpleNamespace(getgrnam=Mock()))
    monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace(getpwnam=lambda _: SimpleNamespace(pw_uid=123)))
    monkeypatch.setattr(helper.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(helper.shutil, "which", lambda _: "/usr/bin/docker")
    run = Mock(return_value=SimpleNamespace(stdout="not-found\n"))
    monkeypatch.setattr(helper.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["add-stream", "--id", "stream-2", "--name", "Stream 2",
        "--serial", "second:5555", "--player-repo", str(tmp_path / "not-installed"), "--user", "player"])
    helper.main()
    assert "not published" in capsys.readouterr().out
    assert envfile.read_text() == original
    assert not (state / "controller-devices.json").exists()
    assert [call.args[0] for call in run.call_args_list] == [[
        "systemctl", "show", "dillflix-prime-player@stream-2.service", "-p", "LoadState", "--value"]]
