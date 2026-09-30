import hashlib
import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_controller import command, overview
from test_playback import jobs

from controller import ops
from controller.config import Settings
from controller.database import Database
from controller.models import AutomationUpdate
from controller.service import Controller
from controller.storage_lock import database_guard


def test_running_backup_captures_committed_wal_and_all_user_state(rig, tmp_path):
    c, s, settings = rig
    reader = sqlite3.connect(settings.database)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM devices").fetchall()
    try:
        command(c, {"type": "play_now", "content_id": "demo:golf"})
        s.tick()
        before = overview(c)
        assert Path(settings.database + "-wal").stat().st_size > 0
        destination = tmp_path / "backup.sqlite3"
        report = ops.backup(settings.database, destination)
        assert report["sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
        assert destination.stat().st_mode & 0o777 == 0o600
        assert not Path(str(destination) + "-wal").exists()
        with ops.readonly(destination) as db:
            assert Database.device(db) == before["device"]
            assert report["counts"]["jobs"] == db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM edit_history").fetchone()[0] == 1
        assert overview(c)["device"] == before["device"]
    finally:
        reader.close()


def test_backup_never_overwrites_or_creates_a_missing_source(rig, tmp_path):
    _, _, settings = rig
    destination = tmp_path / "existing.sqlite3"
    destination.write_bytes(b"keep me")
    with pytest.raises(FileExistsError):
        ops.backup(settings.database, destination)
    assert destination.read_bytes() == b"keep me"
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        ops.backup(missing, tmp_path / "other.sqlite3")
    assert not missing.exists()


@pytest.mark.parametrize("damage", ["truncated", "future_schema", "bad_json", "wrong_database"])
def test_verify_rejects_invalid_backups_without_changing_them(rig, tmp_path, damage):
    _, _, settings = rig
    path = tmp_path / "bad.sqlite3"
    ops.backup(settings.database, path)
    if damage == "truncated":
        path.write_bytes(path.read_bytes()[:512])
    else:
        with sqlite3.connect(path) as db:
            if damage == "future_schema":
                db.execute("PRAGMA user_version=99")
            elif damage == "bad_json":
                db.execute("UPDATE devices SET payload='broken'")
            else:
                db.execute("DROP TABLE jobs")
    original = path.read_bytes()
    with pytest.raises((ValueError, sqlite3.Error)):
        ops.verify(path)
    assert path.read_bytes() == original


def test_restore_refuses_a_running_controller_even_without_worker_leases(rig, tmp_path):
    c, _, settings = rig
    path = tmp_path / "backup.sqlite3"
    ops.backup(settings.database, path)
    before = overview(c)["device"]
    with pytest.raises(RuntimeError, match="in use"):
        ops.restore(path, settings.database, replace=True)
    assert overview(c)["device"] == before


async def test_restore_round_trip_preserves_configuration_history_and_starts_paused(rig, tmp_path):
    c, s, settings = rig
    command(c, {"type": "play_now", "content_id": "demo:golf"})
    s.tick()
    command(c, {"type": "add", "content_id": "demo:jays"})
    await s.refresh_status(force=True)
    before = overview(c)
    source = tmp_path / "backup.sqlite3"
    ops.backup(settings.database, source)
    original = source.read_bytes()
    source.chmod(0o444)
    target = tmp_path / "restored.sqlite3"
    result = ops.restore(source, target, expected_mode="demo")
    assert result["automation"] == "paused"
    assert result["rollback_backup"] is None
    assert source.read_bytes() == original
    assert not Path(str(source) + ".lock").exists()
    restored = Controller(replace(settings, database=str(target)))
    try:
        restored.tick()
        after = restored.overview()
        for key in ("plan", "rules", "team_ranks", "preferences"):
            assert after["device"][key] == before["device"][key]
        assert after["device"]["automation"] == "paused"
        assert after["device"]["observed"] is None
        assert after["device"]["revision"] > before["device"]["revision"]
        assert after["undo"] == before["undo"]
        with restored.db.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == result["counts"]["jobs"]
            assert db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == result["counts"]["commands"]
        d = after["device"]
        restored.automation_command(
            "living-room",
            AutomationUpdate(command_id="resume", expected_revision=d["revision"], mode="active"),
        )
        await restored.refresh_status(force=True)
        restored.tick()
        assert restored.overview()["device"]["observed"]["content_id"] == "demo:golf"
    finally:
        await restored.stop()


async def test_replace_saves_rollback_and_fences_the_newer_target_intent(rig, tmp_path):
    c, s, settings = rig
    command(c, {"type": "play_now", "content_id": "demo:golf"})
    s.tick()
    source = tmp_path / "old.sqlite3"
    ops.backup(settings.database, source)
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    s.tick()
    newer = overview(c)["device"]
    await s.stop()
    result = ops.restore(source, settings.database, replace=True)
    with ops.readonly(result["rollback_backup"]) as db:
        assert Database.device(db) == newer
    with ops.readonly(settings.database) as db:
        restored = Database.device(db)
        assert restored["intent_version"] > newer["intent_version"]
        assert restored["revision"] > newer["revision"]
        assert restored["plan"][0]["content_id"] == "demo:golf"


async def test_restored_pending_work_is_cancelled_without_being_resubmitted(rig, tmp_path):
    c, s, settings = rig
    object.__setattr__(settings, "simulation_delay", 100)
    command(c, {"type": "play_now", "content_id": "demo:lions"})
    s.tick()
    old = jobs(c)[0]
    source = tmp_path / "pending.sqlite3"
    ops.backup(settings.database, source)
    target = tmp_path / "restored.sqlite3"
    ops.restore(source, target)
    restored = Controller(replace(settings, database=str(target)))
    try:
        restored.tick()
        with restored.db.transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (old["id"],)).fetchone()
            assert job["state"] == "cancelled"
            assert job["delivery_attempts"] == 0
            assert job["cancel_sent"] == 1
        assert restored.playback.submit(old["payload"])["state"] == "cancelled"
        assert restored.overview()["device"]["observed"] is None
    finally:
        await restored.stop()


async def test_failed_restore_and_wrong_mode_leave_target_untouched(rig, tmp_path, monkeypatch):
    c, s, settings = rig
    before = overview(c)["device"]
    source = tmp_path / "backup.sqlite3"
    ops.backup(settings.database, source)
    await s.stop()
    with pytest.raises(FileExistsError):
        ops.restore(source, settings.database)
    with pytest.raises(ValueError, match="mode"):
        ops.restore(source, settings.database, replace=True, expected_mode="teamarr")

    def fail(*_):
        raise OSError("Simulated write failure")

    monkeypatch.setattr(ops, "prepare_restore", fail)
    with pytest.raises(OSError):
        ops.restore(source, settings.database, replace=True)
    with ops.readonly(settings.database) as db:
        assert Database.device(db) == before
    assert not list(tmp_path.glob(".controller-*"))


def test_database_open_is_blocked_during_restore_guard(tmp_path):
    path = tmp_path / "database.sqlite3"
    with database_guard(path, exclusive=True):
        with pytest.raises(RuntimeError, match="in use"):
            Controller(Settings(database=str(path)))
    assert not path.exists()


def test_cli_backup_and_verify_report_errors_with_nonzero_exit(rig, tmp_path):
    _, _, settings = rig
    path = tmp_path / "cli.sqlite3"
    cmd = [sys.executable, "-m", "controller.ops", "--database", settings.database]
    saved = subprocess.run([*cmd, "backup", str(path)], capture_output=True, text=True)
    assert saved.returncode == 0, saved.stderr
    assert json.loads(saved.stdout)["mode"] == "demo"
    checked = subprocess.run([*cmd, "verify", str(path)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
    blocked = subprocess.run([*cmd, "restore", str(path), "--replace"], capture_output=True, text=True)
    assert blocked.returncode == 1
    assert "in use" in blocked.stderr


def test_cli_restore_can_receive_a_backup_over_stdin(rig, tmp_path):
    _, _, settings = rig
    source, target = tmp_path / "source.sqlite3", tmp_path / "target.sqlite3"
    ops.backup(settings.database, source)
    result = subprocess.run(
        [sys.executable, "-m", "controller.ops", "--database", str(target), "restore", "-", "--mode", "demo"],
        input=source.read_bytes(),
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout)["automation"] == "paused"
    assert ops.verify(target)["mode"] == "demo"
