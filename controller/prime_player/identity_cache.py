"""Reuse validated identity decisions within one bounded catalogue check."""

import copy
import json


class EventMatchCache:
    def __init__(self, matcher):
        self.matcher = matcher
        self.decisions = {}
        self.uncertain_pools = {}

    async def _choose(self, request, results, timezone, *, eligible):
        # Route text is identity evidence too. Changed titles, timing, provider
        # messaging, target or runtime must go through the matcher again. Access
        # flags may change independently; callers always read them from this response.
        scope = json.dumps([
            request["content_snapshot"], request["allowed_viewing_options"], timezone,
            results.get("session_id"), results.get("generation"), results.get("network_context_id"),
        ], sort_keys=True)
        keyed = [(item, (scope, json.dumps({
            k: v for k, v in item.items()
            if k not in {"entitlement_status", "event_state", "availability_status"}
        }, sort_keys=True))) for item in eligible]
        audit = {"candidates": eligible, "coverage": results.get("coverage"),
                 "complete": results.get("complete"), "warnings": results.get("warnings", [])}
        for item, key in keyed:
            choice = self.decisions.get(key)
            if choice:
                choice = copy.deepcopy(choice)
                return choice, {**audit, **choice, "method": "reused",
                                "reused_content_ids": [item["content_id"]]}

        unseen = [item for item, key in keyed if key not in self.decisions]
        reused = [item["content_id"] for item, key in keyed if key in self.decisions]
        if not unseen:
            return None, {**audit, "method": "reused", "match_status": "no_match",
                          "reason": "Candidates already ruled out for this event and route",
                          "reused_content_ids": reused}
        pool_key = tuple(sorted(key for _, key in keyed if key not in self.decisions))
        if pool_key in self.uncertain_pools:
            return None, {**audit, **copy.deepcopy(self.uncertain_pools[pool_key]), "method": "reused",
                          "reused_content_ids": [i["content_id"] for i in eligible]}
        choice, audit = await self.matcher._choose(request, results, timezone, eligible=unseen)
        if choice:
            key = next(key for item, key in keyed if item["content_id"] == choice["content_id"])
            self.decisions[key] = copy.deepcopy(choice)
        elif audit.get("match_status") == "no_match":
            # no_match explicitly rules out every supplied candidate. A matched
            # choice says nothing about the others; uncertain decisions are not exclusions.
            for _, key in keyed:
                self.decisions[key] = None
        elif audit.get("match_status") == "uncertain":
            # Reuse only the exact unresolved pool, never treat it as a set of
            # individual rejections. Added/changed candidates need a fresh decision.
            self.uncertain_pools[pool_key] = {
                k: copy.deepcopy(audit[k]) for k in ("match_status", "reason", "evidence") if k in audit
            }
        if reused:
            audit = {**audit, "reused_content_ids": reused}
        return choice, audit
