"""Bounded query fallback shared by catalogue checks and authorized launches."""

from ..executor.models import ExecutorError
from .broadcasts import choose_broadcast
from .labels import search_queries
from .matching import selection_state


async def choose_search(request, timezone, matcher, search, fetch, progress, *, select=None):
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
    for query in queries:
        attempt = {"query": query, "state": "pending"}
        attempts.append(attempt)

        def report(audit):
            attempt["audit"] = audit
            combined = {**audit, "search_attempts": list(attempts)}
            progress(combined)
            return combined

        report({"match_status": "uncertain", "reason": "Searching Prime event labels"})
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
            report(attempt["audit"])
            raise
        state = selection_state(selected, audit)
        attempt["state"] = state
        uncertain |= state == "access_unknown"
        combined = report(audit)
        if audit.get("error"):
            return results, selected, combined
        if state in {"ready", "waiting_for_feed"}:
            return results, selected, combined
        if state in {"feeds_locked", "feeds_unavailable"}:
            # A positively identified event has an access outcome. Do not spend
            # more queries repeating that observation, or override earlier ambiguity.
            if not uncertain:
                return results, selected, combined
            break
    if uncertain:
        selected = None
        combined = report(
            {**audit, "match_status": "uncertain", "reason": "Some search alternatives remain unresolved"}
        )
    return results, selected, combined
