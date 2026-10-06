"""Settings and transaction hooks for a single device-scoped Plex binding."""

import hashlib
import logging
from datetime import UTC, datetime

from fastapi import HTTPException

from .client import base_url, rating_key, target_identity
from .coordinator import reconcile
from .state import (
    Credentials,
    asset_by_id,
    configuration,
    prune_assets,
    receipt,
    record_receipt,
    runtime,
    save_asset,
    save_configuration,
    save_runtime,
)
from .worker import ArtworkWorker

log = logging.getLogger(__name__)


class PlexIntegration:
    def __init__(self, service):
        self.service = service
        self.credentials = Credentials(service.settings.database)
        self.worker = ArtworkWorker(self)
        self.loop = None
        self.event = None

    @staticmethod
    def now():
        return datetime.now(UTC)

    def wake(self):
        if self.loop and not self.loop.is_closed() and self.event:
            self.loop.call_soon_threadsafe(self.event.set)

    def reconcile(self, db):
        changed = False
        for row in db.execute("SELECT device_id FROM plex_settings").fetchall():
            changed = reconcile(self.service, db, row[0], now=self.now()) or changed
        if changed:
            db.plex_changed = True
        return changed

    def before_commit(self, db):
        # Covers device, lifecycle and catalogue writes, including executor-side
        # completion. No nested connections, network I/O or activity-log parsing.
        db.execute("SAVEPOINT plex_projection")
        try:
            changed = self.reconcile(db)
            db.execute("RELEASE plex_projection")
            return changed or getattr(db, "plex_changed", False)
        except Exception:
            db.execute("ROLLBACK TO plex_projection")
            db.execute("RELEASE plex_projection")
            log.error("Plex artwork reconciliation failed; playback transaction preserved")
            return True  # Worker can recover from durable controller state.

    def status(self, device):
        with self.service.db.transaction() as db:
            self.service.db.device(db, device)
            config = configuration(db, device)
            state = runtime(db, device)
            images = {}
            for slot, digest in config["defaults"].items():
                asset = asset_by_id(db, digest)
                images[slot] = {"digest": digest, "width": asset.width, "height": asset.height}
            safe = {k: v for k, v in config.items() if k not in {"credential_ref", "target"}}
            safe["credential_configured"] = bool(config.get("credential_ref"))
            safe["default_images"] = images
            safe["real_playback"] = (
                self.service.settings.mode == "teamarr"
                and self.service.settings.executor.mode == "prime-player"
            )
            safe["status"] = {
                k: state.get(k)
                for k in (
                    "generation",
                    "pending",
                    "blocked",
                    "error",
                    "retry_at",
                    "last_success",
                    "fallback_at",
                    "hold",
                    "applied",
                )
            }
            safe["status"]["in_flight"] = bool(state["in_flight"])
            safe["status"]["desired"] = {k: v for k, v in (state["desired"] or {}).items() if k != "sources"}
            return safe

    async def inspect(self, device, request):
        with self.service.db.transaction() as db:
            self.service.db.device(db, device)
            config = configuration(db, device)
        token = (
            request.token.get_secret_value()
            if request.token
            else self.credentials.read(config["credential_ref"])
        )
        url, key = base_url(request.base_url), rating_key(request.rating_key)
        client = self.worker.client_factory(url, token)
        try:
            identity = await client.identity()
            item = await client.metadata(key)
            return {
                **identity,
                "title": item.get("title", ""),
                "library": item.get("librarySectionTitle", ""),
                "base_url": url,
                "rating_key": key,
                "target": target_identity(item),
            }, token
        finally:
            await client.close()

    def preflight(self, device, request, payload):
        with self.service.db.transaction() as db:
            self.service.db.device(db, device)
            return receipt(db, device, request.command_id, request.expected_revision, payload)[1]

    async def save(self, device, request):
        payload = request.model_dump(mode="json")
        if request.token:
            payload["token"] = hashlib.sha256(request.token.get_secret_value().encode()).hexdigest()
        prior = self.preflight(device, request, payload)
        if prior:
            return prior
        # Removing credentials and disabling must work even if Plex is offline.
        old = self.status(device)
        target_changed = (
            base_url(request.base_url) != old["base_url"]
            or rating_key(request.rating_key) != old["rating_key"]
        )
        checked = None
        ref = None
        if not request.remove_token and (target_changed or request.token or request.enabled):
            checked, token = await self.inspect(device, request)
            if request.token:
                ref = self.credentials.write(token)
        try:
            with self.service.db.transaction() as db:
                digest, prior = receipt(db, device, request.command_id, request.expected_revision, payload)
                if prior:
                    return prior
                config = configuration(db, device)
                if request.enabled and (
                    self.service.settings.mode != "teamarr"
                    or self.service.settings.executor.mode != "prime-player"
                ):
                    raise HTTPException(409, "Automatic Plex artwork requires real Prime Player playback")
                if request.enabled and (
                    request.remove_token or set(config["defaults"]) != {"poster", "background"}
                ):
                    raise HTTPException(409, "Configure a token and both default images before enabling")
                config.update(
                    base_url=base_url(request.base_url),
                    rating_key=rating_key(request.rating_key),
                    enabled=request.enabled,
                    one_shot=False,
                    revision=config["revision"] + 1,
                )
                if checked:
                    config.update({k: v for k, v in checked.items() if k not in {"base_url", "rating_key"}})
                if request.remove_token:
                    config["credential_ref"] = None
                elif ref:
                    config["credential_ref"] = ref
                if request.enabled and not old["enabled"]:
                    config["enabled_at"] = self.now().timestamp()
                for row in db.execute("SELECT device_id FROM plex_settings WHERE device_id<>?", (device,)):
                    other = configuration(db, row[0])
                    if (
                        other["machine_id"] == config["machine_id"]
                        and other["rating_key"] == config["rating_key"]
                    ):
                        raise HTTPException(409, "Another device is already bound to this Plex item")
                state = runtime(db, device)
                if target_changed:
                    state.update(applied={}, desired=None, last_confirmed_at=None)
                state.update(blocked=None, error=None, retry_at=None)
                save_runtime(db, device, state)
                save_configuration(db, device, config)
                self.service.db.log(
                    db, self.now().isoformat(), "Plex artwork settings saved", "", "plex", device
                )
                return record_receipt(db, device, request.command_id, digest, config["revision"])
        finally:
            if ref:
                self.credentials.release(ref)

    def change(self, device, request, action, *, assets=None):
        payload = {
            "action": action,
            **request.model_dump(),
            "assets": {k: v.digest for k, v in (assets or {}).items()},
        }
        with self.service.db.transaction() as db:
            self.service.db.device(db, device)
            digest, prior = receipt(db, device, request.command_id, request.expected_revision, payload)
            if prior:
                return prior
            config = configuration(db, device)
            state = runtime(db, device)
            if assets:
                for slot, asset in assets.items():
                    config["defaults"][slot] = save_asset(db, asset)
            else:
                if not config.get("credential_ref") or set(config["defaults"]) != {"poster", "background"}:
                    raise HTTPException(409, "Configure the Plex connection and both default images first")
                if action == "restore-defaults":
                    if (
                        self.service.settings.mode != "teamarr"
                        or self.service.settings.executor.mode != "prime-player"
                    ):
                        raise HTTPException(409, "Plex writes require real Prime Player mode")
                    config.update(enabled=False, one_shot=True)
                elif not config["enabled"] and not config.get("one_shot"):
                    raise HTTPException(409, "Enable artwork updates before requesting a resync")
            config["revision"] += 1
            state.update(blocked=None, error=None, retry_at=None, failures=0, pending=True)
            save_configuration(db, device, config)
            save_runtime(db, device, state)
            self.reconcile(db)
            prune_assets(db)
            self.service.db.log(
                db, self.now().isoformat(), "Plex artwork request saved", action, "plex", device
            )
            return record_receipt(db, device, request.command_id, digest, config["revision"])

    async def capture(self, device, request):
        # Preflight avoids network work for stale requests; the receipt uses a
        # stable command payload rather than hashes of changing remote images.
        payload = {"action": "capture", **request.model_dump()}
        prior = self.preflight(device, request, payload)
        if prior:
            return prior
        with self.service.db.transaction() as db:
            config = configuration(db, device)
        client = self.worker.client(config)
        try:
            item = await self.worker.check_target(client, config)
            assets = {
                slot: await client.image(config["rating_key"], slot, item)
                for slot in ("poster", "background")
            }
        finally:
            await client.close()
        with self.service.db.transaction() as db:
            digest, prior = receipt(db, device, request.command_id, request.expected_revision, payload)
            if prior:
                return prior
            config = configuration(db, device)
            for slot, asset in assets.items():
                config["defaults"][slot] = save_asset(db, asset)
            config["revision"] += 1
            save_configuration(db, device, config)
            self.reconcile(db)
            prune_assets(db)
            self.service.db.log(
                db, self.now().isoformat(), "Plex default artwork captured", "", "plex", device
            )
            return record_receipt(db, device, request.command_id, digest, config["revision"])
