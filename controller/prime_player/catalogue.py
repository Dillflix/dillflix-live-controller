"""On-demand catalogue checks before committing a playback switch."""

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from ..executor.models import ExecutorError
from ..model_diagnostics import capture_model_calls
from .broadcasts import refine
from .client import rpc_context
from .labels import search_queries
from .matching import selection_state


def key(device_id):
    return "prime_catalogue_probe:" + device_id


def signature(device, item):
    return hashlib.sha256(
        json.dumps(
            [
                device["revision"],
                device["intent_version"],
                item["content_id"],
                item["snapshot"],
                item["viewing_options"],
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()


class CatalogueChecks:
    def prepare_selection(self, db, device, item):
        """Called under the staging transaction; never issues an RPC here."""
        token = signature(device, item)
        probe = self.db.meta(db, key(device["id"]), {})
        now = time.time()
        if probe.get("signature") == token:
            if probe.get("state") == "ready" and now < probe.get("expires", 0):
                probe["launch_intent"] = device["intent_version"] + 1
                self.db.set_meta(db, key(device["id"]), probe)
                return True
            if probe.get("state") == "pending" and now < probe.get("expires", 0):
                return False
            if now < probe.get("retry_at", 0):
                return False
        active = getattr(self, "catalogue_task", None)
        if active and not active.done():
            return False
        probe = {
            "id": uuid4().hex,
            "signature": token,
            "state": "pending",
            "device_id": device["id"],
            "content_id": item["content_id"],
            "revision": device["revision"],
            "intent": device["intent_version"],
            "requested_at": datetime.now(UTC).isoformat(),
            "expires": now + self.config.prime_search_timeout + self.config.model_timeout + 65,
            "request": {
                "content_snapshot": item["snapshot"],
                "allowed_viewing_options": item["viewing_options"],
            },
            "timezone": device["preferences"]["timezone"],
        }
        self.db.set_meta(db, key(device["id"]), probe)
        if self.loop and self.loop.is_running() and not self.stopping:

            def schedule():
                active = getattr(self, "catalogue_task", None)
                if active and not active.done():
                    return  # Next staging tick queues the newest request after this read settles.
                self.catalogue_task = asyncio.create_task(self.observe_catalogue(probe))

            self.loop.call_soon_threadsafe(schedule)
        return False

    async def observe_catalogue(self, probe):
        correlation = rpc_context.set({"catalogue_probe_id": probe["id"], "content_id": probe["content_id"]})
        evidence = {**probe, "model_calls": []}
        try:
            async with asyncio.timeout(self.config.prime_search_timeout + self.config.model_timeout + 50):
                async with self.input_lock:
                    # A queued check can become obsolete before it obtains the RPC lane.
                    if not self.catalogue_current(probe):
                        return
                    health = await self.check_session(capabilities=("search",))
                    if health.get("api_version", 0) < 11:
                        raise ExecutorError("prime_incompatible", "Prime Player API 11 or newer is required")
                    ownership = self.player.ownership(health, automatic=True)
                    query = search_queries(probe["request"]["content_snapshot"])[0]
                    results = await self.player.search(query, self.config.prime_search_timeout, ownership)
                if results.get("session_id") != health["session_id"] or results.get("query") != query:
                    raise ExecutorError(
                        "prime_stale_result", "Catalogue result belongs to another session/query"
                    )
                evidence.update(
                    query=query, results=results, session_id=health["session_id"], received_at=time.time()
                )
                with capture_model_calls(lambda value: evidence["model_calls"].append(value)):
                    selected, audit = await self.matcher.choose(probe["request"], results, probe["timezone"])

                async def fetch_broadcasts(content_id):
                    async with self.input_lock:
                        if not self.catalogue_current(probe):
                            raise ExecutorError(
                                "prime_stale_result", "Plan changed during broadcast selection"
                            )
                        current = await self.check_session(health["session_id"], capabilities=("broadcasts",))
                        return await self.player.broadcasts(
                            content_id, self.player.ownership(current, automatic=True)
                        )

                selected, audit = await refine(probe["request"], results, selected, audit, fetch_broadcasts)
                state = selection_state(selected, audit)
                evidence.update(state=state, selected=selected, audit=audit)
        except asyncio.CancelledError:
            evidence.update(
                state="access_unknown", error={"code": "cancelled", "message": "Catalogue check cancelled"}
            )
            raise
        except Exception as exc:
            error = (
                exc.detail()
                if isinstance(exc, ExecutorError)
                else {"code": type(exc).__name__, "message": str(exc)[:1000]}
            )
            evidence.update(state="access_unknown", error=error)
        finally:
            rpc_context.reset(correlation)
            self.finish_catalogue(probe, evidence)

    def catalogue_current(self, probe):
        with self.db.transaction() as db:
            device = self.db.device(db, probe["device_id"])
            saved = self.db.meta(db, key(device["id"]), {})
            return (
                saved.get("id") == probe["id"]
                and device["revision"] == probe["revision"]
                and device["intent_version"] == probe["intent"]
                and device["automation"] == "active"
                and not device.get("manual_control")
                and not device.get("input_handoff")
            )

    def finish_catalogue(self, probe, evidence):
        with self.db.transaction() as db:
            device = self.db.device(db, probe["device_id"])
            saved = self.db.meta(db, key(device["id"]), {})
            if saved.get("id") != probe["id"]:
                return
            valid = (
                device["revision"] == probe["revision"]
                and device["intent_version"] == probe["intent"]
                and device["automation"] == "active"
                and not device.get("manual_control")
                and not device.get("input_handoff")
            )
            now = datetime.now(UTC)
            state = evidence.get("state", "access_unknown") if valid else "discarded"
            evidence.update(
                state=state,
                finished_at=now.isoformat(),
                expires=evidence.get("received_at", time.time()) + 60,
                retry_at=time.time() + 60 if valid else 0,
            )
            self.db.set_meta(db, key(device["id"]), evidence)
            if not valid:
                return
            reasons = {
                "ready": "Prime catalogue reports an entitled live feed",
                "waiting_for_feed": "Prime feed is upcoming; catalogue check retries in 60 seconds",
                "access_unknown": "Prime entitlement or event state is unknown; catalogue check retries in 60 seconds",
                "feeds_locked": "Matching Prime feeds are not entitled; watch plan retained",
                "no_matching_feed": "No matching live or upcoming Prime feed found; watch plan retained",
            }
            reason = reasons[state]
            broadcast_decision = (evidence.get("audit") or {}).get("broadcast_selection") or {}
            if state == "no_matching_feed" and broadcast_decision.get("match_status") == "no_match":
                reason = "No English or unlabeled Prime broadcast on the permitted route; watch plan retained"
            device.setdefault("prime_access", {})[probe["content_id"]] = {
                "state": state,
                "reason": reason,
                "observed_at": now.isoformat(),
                "retry_after": (now + timedelta(seconds=60)).isoformat()
                if state in {"waiting_for_feed", "access_unknown"}
                else None,
                "options": probe["request"]["allowed_viewing_options"],
                "catalogue_probe_id": probe["id"],
            }
            self.db.save_device(db, device)
            self.db.log(
                db,
                now.isoformat(),
                reason,
                json.dumps(
                    {
                        "probe_id": probe["id"],
                        "content_id": probe["content_id"],
                        "selected": evidence.get("selected"),
                        "broadcast_selection": (evidence.get("audit") or {}).get("broadcast_selection"),
                        "error": evidence.get("error"),
                    }
                ),
                "prime_catalogue",
                device["id"],
            )

    def prepared_catalogue(self, request, session):
        with self.db.transaction() as db:
            probe = self.db.meta(db, key(request["device_id"]), {})
        if (
            probe.get("state") == "ready"
            and probe.get("launch_intent") == request["intent_version"]
            and probe.get("content_id") == request["content_id"]
            and probe.get("session_id") == session
            and time.time() < probe.get("expires", 0)
            and probe.get("request", {}).get("content_snapshot") == request["content_snapshot"]
            and probe.get("request", {}).get("allowed_viewing_options") == request["allowed_viewing_options"]
        ):
            return probe
        return None
