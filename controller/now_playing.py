"""Compatibility HTTP projection; internal consumers use current_playback."""

from .artwork import matchup_thumbnail  # noqa: F401
from .current_playback import NowPlaying, current_playback  # noqa: F401


def now_playing(service, device_id):
    with service.db.transaction() as db:
        return current_playback(service, db, device_id)
