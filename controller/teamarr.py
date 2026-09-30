from datetime import UTC, datetime, timedelta

import httpx

from .planner import parse_time


class TeamarrClient:
    def __init__(self, url, token="", transport=None):
        self.url, self.token, self.transport = url.rstrip("/"), token, transport

    async def fetch_snapshot(self):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with httpx.AsyncClient(
            timeout=20, headers=headers, transport=self.transport, follow_redirects=False
        ) as client:
            for attempt in range(2):
                now = datetime.now(UTC)
                params = {
                    "start": now.isoformat(),
                    "end": (now + timedelta(days=3)).isoformat(),
                    "limit": 500,
                }
                entries, cursors = {}, set()
                schema_version = None
                for page_number in range(100):
                    response = await client.get(f"{self.url}/api/v1/events/feed", params=params)
                    if response.status_code == 410 and attempt == 0:
                        break
                    response.raise_for_status()
                    if len(response.content) > 16 * 1024 * 1024:
                        raise ValueError("Teamarr page exceeds 16 MiB")
                    body = response.json()
                    if body.get("schema_version") != 1 or not isinstance(body.get("items"), list):
                        raise ValueError("Unsupported Teamarr feed schema")
                    schema_version = body["schema_version"]
                    for item in body["items"]:
                        if not isinstance(item.get("id"), str) or not item["id"]:
                            raise ValueError("Feed entry is missing its content ID")
                        if item.get("kind") not in {"event", "session", "broadcast"} or not item.get("title"):
                            raise ValueError("Malformed feed entry")
                        if not parse_time(item["start_time"]):
                            raise ValueError("Feed entry requires an aware start_time")
                        if item.get("expected_end_time"):
                            parse_time(item["expected_end_time"])
                        if item["id"] in entries:
                            raise ValueError("Duplicate ID in Teamarr snapshot")
                        entries[item["id"]] = item
                    if len(entries) > 10_000:
                        raise ValueError("Teamarr snapshot exceeds 10,000 entries")
                    cursor = body.get("next_cursor")
                    if not cursor:
                        return list(entries.values()), schema_version
                    if cursor in cursors:
                        raise ValueError("Repeated Teamarr cursor")
                    cursors.add(cursor)
                    params = {"cursor": cursor}
                else:
                    raise ValueError("Too many Teamarr pages")
            raise ValueError("Teamarr snapshot expired repeatedly")
