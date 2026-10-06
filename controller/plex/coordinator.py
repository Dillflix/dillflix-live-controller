"""Reconcile desired artwork inside the controller's committing transaction."""

from datetime import UTC, datetime

from ..current_playback import current_playback
from ..database import encode
from ..planner import parse_time
from .state import configuration, runtime, save_runtime

DEFAULT_TITLE = "Dillflix Live"
RETIRED_MEDIA_CHECK_ERRORS = {
    "Protected Plex metadata changed; artwork updates suspended",
    "Protected Plex metadata changed; updates suspended",
}


def reconcile(service, db, device_id, *, now=None):
    now = now or datetime.now(UTC)
    config = configuration(db, device_id)
    state = runtime(db, device_id)
    before = encode(state)
    # Old attempts still need read-back recovery, but no longer carry a media fingerprint.
    if state.get("in_flight"):
        state["in_flight"].pop("protected", None)
        state["in_flight"].pop("preserve_sort", None)
    if state.get("blocked") in RETIRED_MEDIA_CHECK_ERRORS:
        state.update(blocked=None, error=None, retry_at=None, failures=0, pending=True)
    real = service.settings.mode == "teamarr" and service.settings.executor.mode == "prime-player"
    if not real or not (config["enabled"] or config.get("one_shot")):
        if state["desired"] is not None:
            state.update(desired=None, generation=state["generation"] + 1)
        state.update(pending=False, fallback_at=None, retry_at=None, hold=False)
    else:
        view = current_playback(service, db, device_id, now=now)
        confirmed = view.state == "playing" and view.simulated is False
        if confirmed:
            stamp = parse_time(view.observed_at).timestamp()
            state["last_confirmed_at"] = max(stamp, state["last_confirmed_at"] or stamp)
        due = (state["last_confirmed_at"] or config["enabled_at"] or now.timestamp()) + 300
        state["fallback_at"] = due if config["enabled"] else None
        state["hold"] = not confirmed and now.timestamp() < due and not config.get("one_shot")
        wanted = None
        if config.get("one_shot") or now.timestamp() >= due:
            wanted = {
                "mode": "defaults",
                "content_id": None,
                "title": "Default artwork",
                "plex_title": DEFAULT_TITLE,
                "sources": {
                    slot: {"asset": config["defaults"].get(slot)} for slot in ("poster", "background")
                },
            }
        elif confirmed:
            event = view.event
            wanted = {
                "mode": "event",
                "content_id": event.content_id,
                "title": event.title,
                "plex_title": event.title.strip() or DEFAULT_TITLE,
                "sources": {
                    "poster": {"url": event.thumbnail_url}
                    if event.thumbnail_url
                    else {"asset": config["defaults"].get("poster")},
                    "background": {"asset": config["defaults"].get("background")},
                },
            }
        if wanted is not None:
            wanted["revision"] = config["revision"]
            if wanted != state["desired"]:
                state.update(
                    desired=wanted,
                    generation=state["generation"] + 1,
                    pending=True,
                    retry_at=None,
                    failures=0,
                    error=None,
                )
    if before != encode(state):
        save_runtime(db, device_id, state)
        return True
    return False
