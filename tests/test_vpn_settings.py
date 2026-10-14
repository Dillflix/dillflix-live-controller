from uuid import uuid4


def test_vpn_settings_revision_idempotency_and_legacy_edit(rig):
    client, _, _ = rig
    path = "/api/v1/devices/living-room/rules"
    before = client.get(path).json()
    body = {**before, "command_id": str(uuid4()), "expected_revision": before.pop("revision")}
    body.pop("revision", None)
    body["preferences"]["vpn_policy"] = {
        "enabled": True,
        "locations": [{"target_id": "toronto", "team_cities": ["Toronto", "Ottawa"]}],
    }
    saved = client.put(path, json=body)
    assert saved.status_code == 200, saved.text
    assert client.put(path, json=body).json() == saved.json()
    stale = {**body, "command_id": str(uuid4())}
    assert client.put(path, json=stale).status_code == 409
    current = client.get(path).json()
    policy = current["preferences"].pop("vpn_policy")
    current["preferences"]["timezone"] = "America/Toronto"
    current["expected_revision"] = current.pop("revision")
    current["command_id"] = str(uuid4())
    assert client.put(path, json=current).status_code == 200
    assert client.get(path).json()["preferences"]["vpn_policy"] == policy
    assert client.get("/api/v1/devices/living-room/vpn-locations").json() == {"enabled": False, "locations": []}


def test_vpn_settings_reject_duplicate_locations(rig):
    client, _, _ = rig
    path = "/api/v1/devices/living-room/rules"
    current = client.get(path).json()
    current["expected_revision"] = current.pop("revision")
    current["command_id"] = str(uuid4())
    row = {"target_id": "toronto", "team_cities": ["Toronto"]}
    current["preferences"]["vpn_policy"] = {"enabled": True, "locations": [row, row]}
    assert client.put(path, json=current).status_code == 422
