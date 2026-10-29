import copy
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from controller.fixtures import fixtures
from controller.teamarr import TeamarrClient


def broadcast(competition):
    item = copy.deepcopy(next(x for x in fixtures("tennis")[0] if x["id"] == "demo:tennis"))
    title = {"nsl": "Philadelphia Freedom vs. Seattle Freeze",
             "beatdown": "Beatdown 15: James vs. Stoddart"}[competition]
    item.update(id=f"broadcast:dazn_sports:{competition}", source="dazn_sports",
                competition=competition, title=title,
                sports=["squash" if competition == "nsl" else "mma"])
    item["broadcast"].update(title=title, kind="event_coverage", competition_name=competition)
    item["viewing_options"][0]["stream_title"] = title
    return item


@pytest.mark.parametrize("competition", ["nsl", "beatdown"])
def test_dazn_broadcast_discovery_priority_and_plan_without_team_ids(rig, competition):
    client, service, _ = rig
    item = broadcast(competition)
    with service.db.transaction() as db:
        device = service.db.device(db)
        device["preferences"]["discovery_leagues"] = []
        device["rules"] = [{"id": "coverage", "name": competition, "league": competition,
                            "source": "dazn_sports", "phase": "any", "enabled": True}]
        service.db.save_device(db, device)
        service.replace_catalog(db, [item], "demo")
    view = client.get("/api/v1/overview").json()
    event = next(e for e in view["events"] if e["content_id"] == item["id"])
    assert event["teams"] == [] and event["priority"] == 0
    assert event["kind"] == "broadcast" and event["expected_end_time"] is None
    response = client.post("/api/v1/devices/living-room/watch-plan", json={
        "command_id": str(uuid4()), "expected_revision": view["device"]["revision"],
        "action": {"type": "add", "content_id": item["id"], "priority": "first"},
    })
    assert response.status_code == 200, response.text
    assert client.get("/api/v1/overview").json()["device"]["plan"][0]["content_id"] == item["id"]


@pytest.mark.parametrize("competition", ["nsl", "beatdown"])
async def test_dazn_source_in_empty_discovery_and_historical_status(competition):
    item = broadcast(competition)
    item["status_received_at"] = datetime.now(UTC).isoformat()
    queries = []
    def handler(request):
        queries.append(request.url.params)
        return httpx.Response(200, json={"schema_version": 1, "items": [item]})
    client = TeamarrClient("http://teamarr", transport=httpx.MockTransport(handler))
    assert (await client.fetch_snapshot(leagues=[]))[0] == [item]
    assert "dazn_sports" in queries[0].get_list("source")
    assert await client.fetch_content_status(item) == item
    assert queries[-1].get_list("source") == ["dazn_sports"]
    assert queries[-1].get_list("league") == [competition]
