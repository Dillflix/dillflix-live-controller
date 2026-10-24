import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from controller.background import BackgroundWorker
from controller.config import Settings
from controller.fixtures import fixtures
from controller.service import Controller


@pytest.mark.parametrize("failure", ["lease", "error_record"])
async def test_catalog_worker_recovers_after_database_failure(tmp_path, monkeypatch, failure):
    service = Controller(Settings(database=str(tmp_path / "feed.sqlite"), mode="teamarr"))
    published = asyncio.Event()
    lease = service.db.lease
    attempts = 0
    fetches = 0

    async def idle():
        await asyncio.Event().wait()

    for name in ["run_worker", "run_status", "run_maintenance", "run_team_directory"]:
        monkeypatch.setattr(service, name, idle)
    monkeypatch.setattr(service.plex.worker, "run", idle)

    def claim(db, resource, *args):
        nonlocal attempts
        if resource == "catalog":
            attempts += 1
            if attempts == 1 and failure == "lease":
                raise sqlite3.OperationalError("database is locked")
        return lease(db, resource, *args)

    async def fetch(**kwargs):
        nonlocal fetches
        fetches += 1
        if fetches == 1 and failure == "error_record":
            raise ValueError("temporary invalid feed")
        return fixtures()[0][:1], 1

    set_meta = service.db.set_meta

    def record(db, key, value):
        if key == "feed_health" and value.get("state") == "degraded" and failure == "error_record":
            raise sqlite3.OperationalError("database is locked")
        set_meta(db, key, value)

    refresh = service.refresh_catalog

    async def publish():
        await refresh()
        published.set()

    monkeypatch.setattr(service.db, "lease", claim)
    monkeypatch.setattr(service.db, "set_meta", record)
    monkeypatch.setattr(service.client, "fetch_snapshot", fetch)
    monkeypatch.setattr(service, "refresh_catalog", publish)
    service.start()
    try:
        await asyncio.wait_for(published.wait(), 3)
        assert attempts == 2
        assert service.overview()["health"]["state"] == "ok"
        worker = service.background_status()["catalog"]
        assert worker["state"] == "running"
        assert worker["failures"] == 1
        assert worker["last_error"]["type"] == "OperationalError"
    finally:
        await service.stop()


async def wait_retry(worker):
    async with asyncio.timeout(1):
        while worker.status()["state"] != "retrying":
            await asyncio.sleep(0.001)


async def test_worker_failure_health_does_not_require_persisting_an_error(rig):
    client, service, _ = rig
    calls = 0

    async def fail():
        nonlocal calls
        calls += 1
        error = sqlite3.OperationalError("sensitive exception detail")
        error.sqlite_errorcode, error.sqlite_errorname = sqlite3.SQLITE_BUSY, "SQLITE_BUSY"
        raise error

    worker = BackgroundWorker("catalog", fail)
    service.workers = {"catalog": worker}
    task = asyncio.create_task(worker.run())
    try:
        await wait_retry(worker)
        response = client.get("/api/health").json()
        assert response["ok"] is False
        status = response["background_workers"]["catalog"]
        assert status["state"] == "retrying" and status["next_retry_at"]
        assert status["last_error"]["sqlite_errorname"] == "SQLITE_BUSY"
        assert "sensitive exception detail" not in str(response)
        # Liveness remains independent of application-worker readiness.
        assert client.get("/healthz").json() == {"ok": True}
        exported = client.get("/api/v1/devices/living-room/diagnostics").json()
        assert exported["controller_health"]["background_workers"]["catalog"]["failures"] == 1
    finally:
        before_cancel = calls
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls == before_cancel
    assert worker.status()["state"] == "stopped"
    assert worker.status()["next_retry_at"] is None


async def test_worker_return_restarts_without_spinning():
    calls = 0
    running = asyncio.Event()

    async def loop():
        nonlocal calls
        calls += 1
        if calls == 1:
            return
        running.set()
        await asyncio.Event().wait()

    worker = BackgroundWorker("catalog", loop)
    task = asyncio.create_task(worker.run())
    try:
        await wait_retry(worker)
        assert calls == 1
        assert worker.status()["last_error"]["type"] == "RuntimeError"
        await asyncio.wait_for(running.wait(), 2)
        assert calls == 2 and not task.done()
        assert worker.status()["state"] == "running"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("failure", ["overdue", "worker"])
async def test_feed_health_exposes_stale_success_and_recovers(tmp_path, failure):
    service = Controller(Settings(database=str(tmp_path / "stale.sqlite"), mode="teamarr", feed_interval=60))
    try:
        with service.db.transaction() as db:
            service.replace_catalog(db, fixtures()[0][:1], "teamarr")
            previous = service.db.meta(db, "feed_health")
            if failure == "overdue":
                previous["last_success"] = (datetime.now(UTC) - timedelta(hours=6)).isoformat()
                service.db.set_meta(db, "feed_health", previous)

        async def fail():
            raise sqlite3.OperationalError("database is locked")

        if failure == "worker":
            worker = BackgroundWorker("catalog", fail)
            service.workers = {"catalog": worker}
            service.tasks = [asyncio.create_task(worker.run())]
            await wait_retry(worker)
        health = service.overview()["health"]
        assert health["state"] == "degraded"
        assert health["last_success"] == previous["last_success"]
        assert health["overdue"] == (failure == "overdue")
        with service.db.snapshot() as db:
            # Reading health must not change the saved evidence or its age.
            assert service.db.meta(db, "feed_health") == previous
        if failure == "overdue":
            with service.db.transaction() as db:
                service.replace_catalog(db, fixtures()[0][:1], "teamarr")
            assert service.overview()["health"]["state"] == "ok"
    finally:
        await service.stop()
