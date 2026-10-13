import copy
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from controller.content_status import InvalidStatus, TeamarrContentStatusAdapter, validate_report
from controller.teamarr import TeamarrClient


def request(content_id="event:espn:college-football:401856808"):
    return {
        "schema_version": 1,
        "request_id": "check-1",
        "content_id": content_id,
        "catalog_seen_at": "2026-10-11T06:50:45+00:00",
        "content_snapshot": {
            "id": content_id,
            "kind": "event",
            "source": "games",
            "title": "Hawaii at Arizona State",
            "competition": "college-football",
            "start_time": "2026-10-11T02:30:00Z",
            "expected_end_time": "2026-10-11T06:00:00Z",
            "status": "live",
        },
    }


@pytest.mark.parametrize(
    "content_id,league,start",
    [
        ("event:espn:college-football:401856808", "college-football", "2026-10-11T02:30:00Z"),
        ("event:espn:nfl:401872993", "nfl", "2026-10-12T00:20:00Z"),
    ],
)
async def test_stale_out_of_window_event_fetches_exact_identity_from_historical_pages(
    content_id, league, start
):
    req = request(content_id)
    req["content_snapshot"].update(competition=league, start_time=start)
    req["catalog_seen_at"] = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    calls = []

    def respond(http_request):
        calls.append(http_request)
        if len(calls) == 1:
            params = http_request.url.params
            assert params["league"] == league and params["source"] == "games"
            assert params["lookback_hours"] == "0"
            expected = datetime.fromisoformat(start.replace("Z", "+00:00"))
            assert datetime.fromisoformat(params["start"]) == expected - timedelta(hours=1)
            assert datetime.fromisoformat(params["end"]) == expected + timedelta(hours=1)
            assert "as_of" not in params  # Ask for current status, not historical time simulation.
            item = {**req["content_snapshot"], "id": "other-event", "status": "final"}
            return httpx.Response(200, json={"schema_version": 1, "items": [item], "next_cursor": "next"})
        assert dict(http_request.url.params) == {"cursor": "next"}
        return httpx.Response(
            200, json={"schema_version": 1, "items": [{**req["content_snapshot"], "status": "final"}]}
        )

    adapter = TeamarrContentStatusAdapter(
        TeamarrClient("https://teamarr.invalid", transport=httpx.MockTransport(respond)), 120
    )
    before = copy.deepcopy(req)
    report = await adapter.lookup(req)
    observation = validate_report(req, report, datetime.now(UTC), 120)
    assert observation["state"] == "ended"
    assert observation["source"] == "teamarr_history"
    assert observation["observed_at"] is None
    assert not observation["simulated"]
    assert req == before and len(calls) == 2


@pytest.mark.parametrize("result", ["missing", "wrong_id", "live", "unknown", "error", "expired"])
async def test_history_does_not_infer_completion_from_age_or_missing_data(result):
    req = request()
    req["catalog_seen_at"] = (datetime.now(UTC) - timedelta(days=3)).isoformat()

    def respond(_):
        item = {**req["content_snapshot"], "status": "final"}
        if result == "error":
            return httpx.Response(503)
        if result == "wrong_id":
            item["id"] = "another-game"
        if result in {"live", "unknown"}:
            item["status"] = result
        if result == "expired":
            item["status_received_at"] = req["catalog_seen_at"]
        return httpx.Response(200, json={"schema_version": 1, "items": [] if result == "missing" else [item]})

    adapter = TeamarrContentStatusAdapter(
        TeamarrClient("https://teamarr.invalid", transport=httpx.MockTransport(respond)), 120
    )
    if result == "error":
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.lookup(req)
    else:
        report = await adapter.lookup(req)
        if result in {"missing", "wrong_id"}:
            assert report is None
        elif result == "expired":
            with pytest.raises(InvalidStatus, match="expired"):
                validate_report(req, report, datetime.now(UTC), 120)
        else:
            assert report["observation"]["state"] == result


async def test_fresh_schedule_status_needs_no_extra_network_call():
    req = request()
    req["catalog_seen_at"] = datetime.now(UTC).isoformat()

    def unexpected(_):
        raise AssertionError("Fresh feed should not trigger a second fetch")

    adapter = TeamarrContentStatusAdapter(
        TeamarrClient("https://teamarr.invalid", transport=httpx.MockTransport(unexpected)), 120
    )
    assert (await adapter.lookup(req))["observation"]["source"] == "teamarr_feed"
