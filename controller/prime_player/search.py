"""Bounded query fallback shared by catalogue checks and authorized launches."""

from ..executor.models import ExecutorError
from ..model_diagnostics import report_progress
from .broadcasts import choose_broadcast
from .labels import search_queries
from .matching import selection_state


async def choose_search(request, timezone, matcher, search, fetch, progress, *, select=None,
                        exhaust_denials=False):
    """Search alternative labels without relaxing identity, access or ownership.

    Callers fence every RPC and impose one overall deadline for the entire scan.
    Transport errors stop the scan; uncertain matching cannot become a definite
    exclusion merely because a later query returned no candidates.
    """
    queries = search_queries(request["content_snapshot"])
    if not queries:
        raise ExecutorError("prime_invalid_search", "Event has no usable search labels")
    attempts, identity = [], None
    uncertain = False
    denied_outcome = None
    trial_outcome = None
    for query in queries:
        attempt = {"query": query, "state": "pending"}
        attempts.append(attempt)

        async def report(audit):
            attempt["audit"] = audit
            combined = {**audit, "search_attempts": list(attempts)}
            await report_progress(progress, combined)
            return combined

        await report({"match_status": "uncertain", "reason": "Searching Prime event labels"})
        try:
            results = await search(query)
            current = (results.get("session_id"), results.get("generation"))
            if results.get("query") != query or (identity is not None and current != identity):
                raise ExecutorError("prime_stale_result", "Prime search session, generation or query changed")
            identity = current
            attempt.update({k: results.get(k) for k in ("search_id", "generation", "coverage", "complete")})

            async def broadcasts(content_id):
                return await fetch(results, content_id)

            selected, audit = (
                await select(request, results, timezone)
                if select else
                await choose_broadcast(request, results, timezone, matcher, broadcasts, report)
            )
        except Exception as exc:
            attempt["state"] = "access_unknown"
            attempt["error"] = (
                exc.detail()
                if isinstance(exc, ExecutorError)
                else {"code": type(exc).__name__, "message": str(exc)[:1000]}
            )
            await report(attempt["audit"])
            raise
        state = selection_state(selected, audit)
        attempt["state"] = state
        uncertain |= state == "access_unknown"
        combined = await report(audit)
        if audit.get("error"):
            return results, selected, combined
        if trial_outcome:
            prior = trial_outcome[1]
            parent_id = prior.get("parent_content_id") or prior["content_id"]
            if any(item.get("content_id") == parent_id
                   for container in results["containers"] for item in container["items"]):
                # A new observation of this parent replaces the earlier trial,
                # even if its entitlement, lifecycle or broadcast choices changed.
                trial_outcome = None
        if state == "ready" and selected.get("unavailable_trial"):
            # Eligible for a real trial is not evidence of a playable feed.
            # Prefer an available alternative from the remaining bounded labels.
            trial_outcome = (results, selected, audit)
            continue
        if trial_outcome and state in {"waiting_for_feed", "feeds_locked", "feeds_unavailable"}:
            continue
        if state in {"ready", "waiting_for_feed"}:
            return results, selected, combined
        if state == "feeds_locked" and exhaust_denials:
            # Background entitlement seeks any accessible feed. A denied match
            # cannot stop the remaining queries from discovering another provider.
            denied_outcome = (results, selected, audit)
            continue
        if state in {"feeds_locked", "feeds_unavailable"}:
            # A positively identified event has an access outcome. Do not spend
            # more queries repeating that observation, or override earlier ambiguity.
            if not uncertain:
                return results, selected, combined
            break
    if uncertain:
        selected = None
        combined = await report(
            {**audit, "match_status": "uncertain", "reason": "Some search alternatives remain unresolved"}
        )
    elif trial_outcome or denied_outcome:
        results, selected, audit = trial_outcome or denied_outcome
        combined = {**audit, "search_attempts": list(attempts), "search_queries_exhausted": True}
        await report_progress(progress, combined)
    combined = {**combined, "search_queries_exhausted": len(attempts) == len(queries)}
    return results, selected, combined
