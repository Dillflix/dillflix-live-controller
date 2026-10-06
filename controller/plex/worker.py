"""Event/deadline-driven delivery, outside playback locks and DB transactions."""

import asyncio
import json
import logging
import time
from uuid import uuid4

from .client import (
    PlexClient,
    PlexError,
    download_image,
    field_locked,
    same_image,
    target_identity,
)
from .state import asset_by_id, configuration, prune_assets, runtime, save_asset, save_runtime

log = logging.getLogger(__name__)


class ArtworkWorker:
    def __init__(self, integration):
        self.integration = integration
        self.service = integration.service
        self.lock = asyncio.Lock()
        self.client_factory = PlexClient
        self.download = download_image

    def client(self, config):
        return self.client_factory(
            config["base_url"], self.integration.credentials.read(config["credential_ref"])
        )

    def snapshot(self, device):
        with self.service.db.transaction() as db:
            self.integration.reconcile(db)
            return configuration(db, device), runtime(db, device)

    def current(self, device, generation):
        config, state = self.snapshot(device)
        return (
            state["generation"] == generation
            and state["pending"]
            and not state["hold"]
            and not state["blocked"]
            and (config["enabled"] or config.get("one_shot"))
        )

    async def check_target(self, client, config):
        identity = await client.identity()
        if identity["machine_id"] != config["machine_id"]:
            raise PlexError("Plex server identity changed; check the connection", permanent=True)
        item = await client.metadata(config["rating_key"])
        # JSON storage converts tuple pairs to lists.
        if json.dumps(target_identity(item), sort_keys=True) != json.dumps(config["target"], sort_keys=True):
            raise PlexError("Plex target identity changed; select the item again", permanent=True)
        return item

    def fail(self, device, error, generation=None):
        with self.service.db.transaction() as db:
            state = runtime(db, device)
            if generation is not None and state["generation"] != generation and not error.permanent:
                return
            state["failures"] += 1
            state["error"] = str(error)
            state["retry_at"] = time.time() + min(5 * 2 ** min(state["failures"] - 1, 6), 300)
            if error.permanent:
                state["blocked"] = str(error)
                state["retry_at"] = None
            save_runtime(db, device, state)
            self.service.db.log(
                db,
                self.integration.now().isoformat(),
                "Plex update pending",
                str(error),
                "plex",
                device,
            )

    async def resolve_in_flight(self, device, attempt):
        """A transmitted request may have succeeded even without an acknowledgement."""
        client = self.client(attempt["config"])
        try:
            item = await self.check_target(client, attempt["config"])
            if attempt.get("kind") == "title":
                matched = item.get("title") == attempt["title"] and field_locked(item, "title")
                with self.service.db.transaction() as db:
                    state = runtime(db, device)
                    if (state.get("in_flight") or {}).get("id") != attempt["id"]:
                        return
                    state["in_flight"] = None
                    if matched and state["generation"] == attempt["generation"]:
                        state["applied_title"] = self.title_receipt(attempt)
                    save_runtime(db, device, state)
                return
            served = await client.image(attempt["config"]["rating_key"], attempt["slot"], item)
            with self.service.db.transaction() as db:
                source = asset_by_id(db, attempt["digest"])
            matched = same_image(source, served)
            with self.service.db.transaction() as db:
                state = runtime(db, device)
                if (state.get("in_flight") or {}).get("id") != attempt["id"]:
                    return
                state["in_flight"] = None
                if matched and state["generation"] == attempt["generation"]:
                    state["applied"][attempt["slot"]] = {
                        "digest": source.digest,
                        "served_digest": served.digest,
                        "content_id": attempt["content_id"],
                        "at": self.integration.now().isoformat(),
                    }
                save_runtime(db, device, state)
        finally:
            await client.close()

    def title_receipt(self, attempt):
        return {
            "value": attempt["title"],
            "content_id": attempt["content_id"],
            "at": self.integration.now().isoformat(),
        }

    async def deliver_title(self, device, generation, config, desired, client):
        if not self.current(device, generation):
            return
        item = await self.check_target(client, config)
        if not self.current(device, generation):
            return
        attempt = {
            "id": uuid4().hex,
            "kind": "title",
            "title": desired["plex_title"],
            "generation": generation,
            "config": config,
            "content_id": desired["content_id"],
        }
        if item.get("title") != attempt["title"] or not field_locked(item, "title"):
            with self.service.db.transaction() as db:
                state = runtime(db, device)
                if state["generation"] != generation or state["hold"] or state["blocked"]:
                    return
                state["in_flight"] = attempt
                save_runtime(db, device, state)
            await client.set_title(config["rating_key"], item.get("librarySectionID"), attempt["title"])
            after = await client.metadata(config["rating_key"])
            if after.get("title") != attempt["title"] or not field_locked(after, "title"):
                raise PlexError("Plex has not served the intended title; verification will retry")
        with self.service.db.transaction() as db:
            state = runtime(db, device)
            state["in_flight"] = None
            if state["generation"] == generation:
                state["applied_title"] = self.title_receipt(attempt)
            save_runtime(db, device, state)

    async def deliver_slot(self, device, generation, config, desired, slot, client):
        source = desired["sources"][slot]
        download_error = None
        if source.get("url"):
            try:
                asset = await self.download(source["url"])
            except PlexError as exc:
                download_error = PlexError(str(exc))  # Source problems remain retryable.
                with self.service.db.transaction() as db:
                    applied = runtime(db, device)["applied"].get(slot)
                    digest = (
                        applied["digest"]
                        if applied and applied.get("content_id") == desired["content_id"]
                        else config["defaults"][slot]
                    )
                    asset = asset_by_id(db, digest)
        else:
            with self.service.db.transaction() as db:
                asset = asset_by_id(db, source.get("asset"))
        if not self.current(device, generation):
            return None
        item = await self.check_target(client, config)
        if not self.current(device, generation):
            return None
        try:
            served = await client.image(config["rating_key"], slot, item)
        except PlexError as exc:
            if not str(exc).startswith("Plex has no supported current"):
                raise
            served = None
        if not self.current(device, generation):
            return None
        if served is None or not same_image(asset, served):
            attempt = {
                "id": uuid4().hex,
                "slot": slot,
                "digest": asset.digest,
                "generation": generation,
                "config": config,
                "content_id": desired["content_id"],
            }
            with self.service.db.transaction() as db:
                state = runtime(db, device)
                if state["generation"] != generation or state["hold"] or state["blocked"]:
                    return None
                save_asset(db, asset)
                state["in_flight"] = attempt
                save_runtime(db, device, state)
            # Leave the durable attempt intact on cancellation, timeout or read-back failure.
            await client.set_image(config["rating_key"], slot, asset)
            after = await client.metadata(config["rating_key"])
            served = await client.image(config["rating_key"], slot, after)
            if not same_image(asset, served):
                raise PlexError("Plex has not served the intended artwork; verification will retry")
        with self.service.db.transaction() as db:
            state = runtime(db, device)
            state["in_flight"] = None
            if state["generation"] == generation:
                save_asset(db, asset)
                state["applied"][slot] = {
                    "digest": asset.digest,
                    "served_digest": served.digest,
                    "content_id": desired["content_id"],
                    "at": self.integration.now().isoformat(),
                }
            save_runtime(db, device, state)
        return download_error

    async def run_once(self, device):
        async with self.lock:
            config, state = self.snapshot(device)
            if state["blocked"] or (state["retry_at"] and state["retry_at"] > time.time()):
                return
            generation = state["generation"]
            try:
                if state["in_flight"]:
                    await self.resolve_in_flight(device, state["in_flight"])
                    config, state = self.snapshot(device)
                    generation = state["generation"]
                if not self.current(device, generation) or not state["desired"]:
                    return
                client = self.client(config)
                errors = []
                try:
                    await self.deliver_title(device, generation, config, state["desired"], client)
                    for slot in ("poster", "background"):
                        if not self.current(device, generation):
                            return
                        error = await self.deliver_slot(
                            device, generation, config, state["desired"], slot, client
                        )
                        if error:
                            errors.append(error)
                finally:
                    await client.close()
                if errors:
                    raise errors[0]
                with self.service.db.transaction() as db:
                    latest = runtime(db, device)
                    if latest["generation"] == generation:
                        latest.update(
                            pending=False,
                            retry_at=None,
                            failures=0,
                            error=None,
                            last_success=self.integration.now().isoformat(),
                        )
                        save_runtime(db, device, latest)
                        self.service.db.log(
                            db,
                            self.integration.now().isoformat(),
                            "Plex title and artwork updated",
                            state["desired"]["title"],
                            "plex",
                            device,
                        )
                    prune_assets(db)
            except PlexError as exc:
                self.fail(device, exc, generation)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.fail(device, PlexError("Unexpected artwork error; update will retry"), generation)

    async def run(self):
        integration = self.integration
        integration.loop = asyncio.get_running_loop()
        integration.event = asyncio.Event()
        while True:
            try:
                await self.cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.error("Plex artwork worker failed; retrying from durable state")
                await asyncio.sleep(5)

    async def cycle(self):
        integration = self.integration
        integration.event.clear()
        with self.service.db.transaction() as db:
            integration.reconcile(db)
            devices = [r[0] for r in db.execute("SELECT device_id FROM plex_settings")]
        for device in devices:
            await self.run_once(device)
        with self.service.db.transaction() as db:
            states = [runtime(db, d) for d in devices]
            refs = {configuration(db, d).get("credential_ref") for d in devices}
            refs.update(s["in_flight"]["config"]["credential_ref"] for s in states if s["in_flight"])
            integration.credentials.prune(refs)
        now = time.time()
        deadlines = []
        immediate = False
        for state in states:
            if state["fallback_at"] and state["fallback_at"] > now:
                deadlines.append(state["fallback_at"])
            if not state["blocked"] and ((state["pending"] and not state["hold"]) or state["in_flight"]):
                if state["retry_at"] and state["retry_at"] > now:
                    deadlines.append(state["retry_at"])
                else:
                    immediate = True
        if immediate:
            await asyncio.sleep(0)
            return
        delay = max(0, min(deadlines) - time.time()) if deadlines else None
        try:
            async with asyncio.timeout(delay):
                await integration.event.wait()
        except TimeoutError:
            pass
