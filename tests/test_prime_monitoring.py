"""Regression against Prime's real native-status collector, with a paced runtime.

Native identity/position queries are serialized and drained by runtime polls.
The clock advances without real sleeps; no device or playback mutation is used.
"""

from types import SimpleNamespace

import pytest

pytest.importorskip("dillflix_prime_player")
from dillflix_prime_player import playback as prime_playback

from controller.executor.config import ExecutorConfig


class PacedRuntime:
    session_id = "paced-service"

    def __init__(self):
        self.clock = 1000.0
        self.rows = []
        self.pending = []
        self.commands = []
        self.page = {"pageType": "RUST_PLAYBACK", "pageId": "live-title"}

    @property
    def cursor(self):
        return len(self.rows)

    def records(self, after):
        return self.rows[after:]

    def command(self, op, timeout):
        assert timeout > 0
        self.commands.append(op)
        self.clock += 1
        if op == "playback-status":
            return {"result": {
                "page": self.page, "epoch": 3, "timeMs": self.clock * 1000,
                "event": {"name": "PLAYBACK_STATE_CHANGE", "rawState": "Playing",
                          "pageId": "live-title", "epoch": 3, "timeMs": 900000},
            }}
        assert op == "inspect-player", "monitoring must never navigate"
        inspection = str(len(self.commands))
        for delay, method, value in (
            (2, "getTitleId", "live-title"),
            (4, "getCurrentPosition", (self.clock + 4) * 1000),
        ):
            self.pending.append((self.clock + delay, {
                "kind": "native_query",
                "data": {"plugin": "app-player-plugin", "inspectionId": inspection,
                         "method": method, "result": value, "success": True,
                         "pageChanged": False, "pageAtRequest": self.page,
                         "pageAtReceipt": self.page,
                         "receivedTimeMs": (self.clock + delay) * 1000},
            }))
        return {"id": inspection}

    def wait(self, predicate, timeout):
        end = self.clock + timeout
        while True:
            for at, record in list(self.pending):
                if at <= self.clock:
                    self.rows.append(record)
                    self.pending.remove((at, record))
            value = predicate()
            if value:
                return value
            if self.clock >= end:
                raise TimeoutError("runtime observation deadline exceeded")
            self.clock = min(end, self.clock + 0.5)


@pytest.mark.parametrize("timeout,state,samples", [
    (10, "unknown", 1),
    (ExecutorConfig().prime_status_timeout, "playing", 2),
])
def test_serial_native_status_exceeds_old_budget_but_fits_configured_budget(
    monkeypatch, timeout, state, samples
):
    runtime = PacedRuntime()
    monkeypatch.setattr(prime_playback, "time", SimpleNamespace(monotonic=lambda: runtime.clock))
    attempt = {"attempt_id": "a" * 32, "requested_id": "event-title",
               "resolved_id": "live-title", "playback_epoch": 3}
    status = prime_playback.observe_playback(runtime, attempt, timeout)
    assert status["state"] == state
    assert len(status["evidence"]["samples"]) == samples
    assert set(runtime.commands) == {"playback-status", "inspect-player"}
    if state == "playing":
        assert runtime.clock - 1000 == 14
        assert status["matches_attempt"] is True and status["is_playing"] is True
        assert status["error"] is None
    else:
        assert status["error"] == "runtime observation deadline exceeded"
