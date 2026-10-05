"""Small allowlisted public read model; never serialize admin state or routes."""


def public_overview(service, actor, device_id="living-room"):
    with service.db.transaction() as db:
        device = service.db.device(db, device_id)
        items = service.items(db, device_id)
        planned = {p["content_id"] for p in device["plan"]}
        observed = device.get("observed") or {}
        current_id = observed.get("content_id")
        events = []
        for item in items:
            if not item["active"] and item["content_id"] not in planned | {current_id}:
                continue
            card = {
                key: item[key]
                for key in (
                    "content_id",
                    "title",
                    "league",
                    "phase",
                    "kind",
                    "start_time",
                    "expected_end_time",
                    "scores",
                    "playable",
                    "active",
                )
            }
            card["teams"] = [
                {
                    key: team.get(key)
                    for key in ("key", "name", "city", "full_name", "abbreviation", "logo_url")
                }
                for team in item["teams"]
            ]
            card.update(
                state=item["lifecycle"]["state"],
                planned=item["content_id"] in planned,
                viewing_option_count=len(item["viewing_options"]),
            )
            events.append(card)
        events.sort(key=lambda e: (e["start_time"], e["content_id"]))
        access = device["public_access"]
        paused = device["automation"] == "paused" or bool(
            device.get("manual_control") or device.get("input_handoff")
        )
        job = db.execute(
            "SELECT * FROM jobs WHERE id=? AND device_id=?", (observed.get("request_id"), device_id)
        ).fetchone()
        current = next((e for e in events if e["content_id"] == current_id), None)
        verified = bool(
            job
            and current
            and current["state"] not in {"ended", "cancelled"}
            and device["playback_state"] == "verified"
            and not device.get("manual_control")
            and not device.get("input_handoff")
            and not service.observation_error(job, observed)
        )
        return {
            "revision": device["revision"],
            "viewer": {"name": actor["name"], "guest": actor["id"] == "guest:anonymous"},
            "timezone": device["preferences"]["timezone"],
            "permissions": {
                "play_now": access["play_now"] and not paused,
                "add_to_plan": access["add_to_plan"],
            },
            "actions_message": "The admin has paused public playback."
            if paused
            else "Admin selections take priority. Your requests stay in the shared watch plan.",
            "now_playing": {
                "event": current,
                "verified": verified,
                "simulated": bool(observed.get("simulated")),
                "switching": device["playback_state"] == "navigating",
            },
            "events": events,
        }
