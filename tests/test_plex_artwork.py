import asyncio
import io
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from xml.sax.saxutils import quoteattr

import httpx
import pytest
from PIL import Image

from controller.database import encode
from controller.diagnostic_log import scrub
from controller.plex.client import (
    PlexClient,
    PlexError,
    image_asset,
)
from controller.plex.state import configuration, runtime, save_configuration, save_runtime
from controller.prime_player.diagnostics import redact

PATH = "/api/v1/devices/living-room/plex"
TOKEN = "private-plex-token-123456"


def picture(color):
    output = io.BytesIO()
    Image.new("RGB", (160, 120), color).save(output, "PNG")
    return output.getvalue()


class Plex:
    def __init__(self):
        self.images = {"thumb": picture("black"), "art": picture("gray")}
        self.calls = []
        self.machine = "server-1"
        self.duration = "7200000"
        self.bitrate = "20000"
        self.title = "Dillflix Live - Stream 1"
        self.sort_title = self.title
        self.title_locked = False
        self.sort_locked = False
        self.title_requests = []
        self.title_status = 200
        self.ignore_title = False
        self.title_timeout = False
        self.title_drift = None
        self.during_title = None
        self.fail_art = False
        self.drift = False
        self.timeout_after_write = False
        self.during_write = None
        self.version = 1
        self.fail_read = False
        self.fail_identity = False

    def metadata(self):
        return f'''<MediaContainer><Video ratingKey="8" type="movie" guid="local://stream1"
        title={quoteattr(self.title)} titleSort={quoteattr(self.sort_title)} summary="Live stream" duration="{self.duration}"
        librarySectionID="1" librarySectionTitle="Streams" thumb="/library/metadata/8/thumb/{self.version}"
        art="/library/metadata/8/art/{self.version}"><Media id="9" duration="{self.duration}"
        bitrate="{self.bitrate}" videoCodec="h264" width="1920" height="1080">
        <Part id="10" file="/stream.mkv" duration="{self.duration}" size="1000">
        <Stream id="11" codec="h264" bitrate="19000" streamType="1"/></Part></Media>
        <Field name="title" locked="{int(self.title_locked)}"/>
        <Field name="titleSort" locked="{int(self.sort_locked)}"/>
        </Video></MediaContainer>'''

    async def handle(self, request):
        assert request.headers.get("X-Plex-Token") == TOKEN
        assert "X-Plex-Token" not in str(request.url)
        self.calls.append((request.method, request.url.path, request.content))
        path = request.url.path
        if path == "/identity":
            if self.fail_identity:
                return httpx.Response(401)
            return httpx.Response(
                200, text=f'<MediaContainer machineIdentifier="{self.machine}" version="1.2.3"/>'
            )
        if path == "/library/metadata/8":
            return httpx.Response(200, text=self.metadata())
        if request.method == "PUT":
            assert path == "/library/sections/1/all"
            params = dict(request.url.params)
            assert set(params) == {"id", "type", "title.value", "title.locked"}
            assert params["id"] == "8" and params["type"] == "1" and params["title.locked"] == "1"
            assert not request.content
            self.title_requests.append(params)
            if self.title_status != 200:
                return httpx.Response(self.title_status)
            if self.ignore_title:
                return httpx.Response(200)
            if self.during_title:
                await self.during_title()
            self.title = params["title.value"]
            self.title_locked = True
            if not self.sort_locked:
                self.sort_title = self.title
            if self.title_drift:
                setattr(self, self.title_drift, "1")
            if self.title_timeout:
                self.title_timeout = False
                raise httpx.ReadTimeout("private title timeout details", request=request)
            return httpx.Response(200)
        slot = path.split("/")[4] if path.startswith("/library/metadata/8/") else None
        if request.method == "POST":
            # Independently model PlexAPI's upload contract; singular image paths return 404.
            uploads = {"/library/metadata/8/posters": "thumb", "/library/metadata/8/arts": "art"}
            if path not in uploads:
                return httpx.Response(404)
            slot = uploads[path]
            assert request.url.query == b""
            assert request.headers["content-type"] == "image/png"
            if self.during_write:
                await self.during_write(slot)
            if slot == "art" and self.fail_art:
                return httpx.Response(503)
            self.images[slot] = request.content
            self.version += 1
            if self.drift:
                self.duration = "1"
            if self.timeout_after_write:
                self.timeout_after_write = False
                raise httpx.ReadTimeout("secret should not reach logs", request=request)
            return httpx.Response(200)
        if slot in self.images:
            if self.fail_read:
                return httpx.Response(503)
            return httpx.Response(200, content=self.images[slot])
        return httpx.Response(404)

    def writes(self):
        return [c for c in self.calls if c[0] == "POST"]

    def mutations(self):
        return [c for c in self.calls if c[0] in {"POST", "PUT", "DELETE"}]


@pytest.fixture
def setup(rig):
    browser, service, settings = rig
    plex = Plex()
    service.plex.worker.client_factory = lambda url, token: PlexClient(
        url, token, httpx.MockTransport(plex.handle)
    )
    request = {
        "command_id": "settings-1",
        "expected_revision": 0,
        "base_url": "http://plex:32400",
        "rating_key": "8",
        "token": TOKEN,
        "enabled": False,
    }
    response = browser.put(PATH, json=request)
    assert response.status_code == 200, response.text
    assert (
        browser.post(
            PATH + "/defaults/capture", json={"command_id": "capture", "expected_revision": 1}
        ).status_code
        == 200
    )
    return browser, service, plex


def real_playback(service):
    service.tick()
    service.settings = replace(
        service.settings, mode="teamarr", executor=replace(service.settings.executor, mode="prime-player")
    )
    now = datetime.now(UTC)
    service.plex.now = lambda: now
    with service.db.transaction() as db:
        d = service.db.device(db)
        assert d["observed"]
        d["observed"].update(
            simulated=False, observed_at=now.isoformat(), valid_until=(now + timedelta(minutes=5)).isoformat()
        )
        content_id = d["observed"]["content_id"]
        snap = json.loads(db.execute("SELECT snapshot FROM contents WHERE id=?", (content_id,)).fetchone()[0])
        snap["artwork"] = {"matchup_logo_url": "http://images/game/logo.png?style=6"}
        db.execute("UPDATE contents SET snapshot=? WHERE id=?", (encode(snap), content_id))
        service.db.save_device(db, d)
    return now, content_id


def enable(browser):
    response = browser.put(
        PATH,
        json={
            "command_id": "enable",
            "expected_revision": 2,
            "base_url": "http://plex:32400",
            "rating_key": "8",
            "enabled": True,
        },
    )
    assert response.status_code == 200, response.text


def state(service):
    with service.db.transaction() as db:
        return runtime(db, "living-room")


def ready_retry(service):
    with service.db.transaction() as db:
        s = runtime(db, "living-room")
        s["retry_at"] = None
        save_runtime(db, "living-room", s)


async def red_image(_url):
    return image_asset(picture("red"))


def event_title(service, content_id, title):
    with service.db.transaction() as db:
        snapshot = json.loads(
            db.execute("SELECT snapshot FROM contents WHERE id=?", (content_id,)).fetchone()[0]
        )
        snapshot["title"] = title
        db.execute("UPDATE contents SET snapshot=? WHERE id=?", (encode(snapshot), content_id))


def restart_plex(service, now):
    from controller.plex.integration import PlexIntegration

    previous = service.plex
    replacement = PlexIntegration(service)
    replacement.now = lambda: now
    replacement.worker.client_factory = previous.worker.client_factory
    replacement.worker.download = previous.worker.download
    service.plex = replacement
    service.db.before_commit = replacement.before_commit
    service.db.after_commit = replacement.wake
    return replacement.worker


@pytest.mark.asyncio
async def test_event_title_updates_without_an_image_then_restores_default(setup):
    browser, service, plex = setup
    now, cid = real_playback(service)
    title = 'Beijing Open: Day 7 — 女子 & "Final"?id=99#live'
    event_title(service, cid, title)
    with service.db.transaction() as db:
        snap = json.loads(db.execute("SELECT snapshot FROM contents WHERE id=?", (cid,)).fetchone()[0])
        snap["artwork"] = {}
        db.execute("UPDATE contents SET snapshot=? WHERE id=?", (encode(snap), cid))
    enable(browser)
    await service.plex.worker.run_once("living-room")
    assert plex.title == title and plex.title_locked
    assert plex.title_requests == [{"id": "8", "type": "1", "title.value": title, "title.locked": "1"}]
    assert not plex.writes()
    assert browser.get(PATH).json()["status"]["applied_title"]["value"] == title
    exported = browser.get("/api/v1/devices/living-room/diagnostics").json()
    assert exported["plex_artwork"]["applied_title"]["value"] == title
    with service.db.transaction() as db:
        d = service.db.device(db)
        d["observed"]["verified"] = False
        service.db.save_device(db, d)
    service.plex.now = lambda: now + timedelta(seconds=299)
    await service.plex.worker.run_once("living-room")
    assert plex.title == title and len(plex.title_requests) == 1
    service.plex.now = lambda: now + timedelta(seconds=300)
    await service.plex.worker.run_once("living-room")
    assert plex.title == "Dillflix Live"
    assert len(plex.title_requests) == 2 and not state(service)["pending"]


@pytest.mark.asyncio
async def test_renewal_and_title_change_do_not_reupload_matching_images(setup):
    browser, service, plex = setup
    now, cid = real_playback(service)
    enable(browser)
    service.plex.worker.download = red_image
    await service.plex.worker.run_once("living-room")
    service.plex.now = lambda: now + timedelta(seconds=60)
    with service.db.transaction() as db:
        d = service.db.device(db)
        d["observed"].update(
            observed_at=(now + timedelta(seconds=60)).isoformat(),
            valid_until=(now + timedelta(seconds=360)).isoformat(),
        )
        service.db.save_device(db, d)
    await service.plex.worker.run_once("living-room")
    assert len(plex.title_requests) == 1
    event_title(service, cid, "Beijing Open: Day 8")
    await service.plex.worker.run_once("living-room")
    assert plex.title == "Beijing Open: Day 8" and len(plex.title_requests) == 2
    assert len(plex.writes()) == 1


@pytest.mark.asyncio
async def test_upgrade_reconciles_title_for_already_applied_artwork(setup):
    browser, service, plex = setup
    now, cid = real_playback(service)
    event_title(service, cid, "Beijing Open: Day 7")
    enable(browser)
    service.plex.worker.download = red_image
    await service.plex.worker.run_once("living-room")
    # Old versions stored artwork success without a title intent/receipt.
    service.db.before_commit = None
    with service.db.transaction() as db:
        saved = runtime(db, "living-room")
        del saved["desired"]["plex_title"]
        saved.pop("applied_title")
        save_runtime(db, "living-room", saved)
    plex.title, plex.title_locked = "Dillflix Live - Stream 1", False
    plex.calls.clear()
    await restart_plex(service, now).run_once("living-room")
    assert plex.title == "Beijing Open: Day 7" and not plex.writes()
    assert not state(service)["pending"]


@pytest.mark.asyncio
async def test_uncertain_title_resumes_after_restart_without_duplicate_write(setup):
    browser, service, plex = setup
    now, _ = real_playback(service)
    enable(browser)
    service.plex.worker.download = red_image
    plex.title_timeout = True
    await service.plex.worker.run_once("living-room")
    assert state(service)["in_flight"]["kind"] == "title"
    assert not plex.writes()
    # Pruning assets during a settings edit must also support title attempts.
    response = browser.post(
        PATH + "/defaults/background",
        params={"command_id": "new-default", "expected_revision": 3},
        content=picture("blue"),
    )
    assert response.status_code == 200, response.text
    await restart_plex(service, now).run_once("living-room")
    assert len(plex.title_requests) == 1 and len(plex.writes()) == 2
    assert not state(service)["in_flight"] and not state(service)["pending"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["duration", "bitrate", "sort_title"])
async def test_title_edit_ignores_unrelated_property_changes(setup, field):
    browser, service, plex = setup
    real_playback(service)
    enable(browser)
    service.plex.worker.download = red_image
    plex.sort_locked = True
    plex.sort_title = "Live stream sort position"
    plex.title_drift = field
    await service.plex.worker.run_once("living-room")
    assert not state(service)["blocked"] and not state(service)["pending"]
    assert not state(service)["in_flight"]
    assert plex.title == state(service)["desired"]["plex_title"]
    assert len(plex.writes()) == 1


@pytest.mark.asyncio
async def test_title_retry_ignores_media_changes_after_timed_out_edit(setup):
    browser, service, plex = setup
    real_playback(service)
    enable(browser)
    service.plex.worker.download = red_image
    plex.title_timeout = True
    await service.plex.worker.run_once("living-room")
    plex.duration = "1"
    ready_retry(service)
    await service.plex.worker.run_once("living-room")
    assert not state(service)["blocked"] and not state(service)["pending"]
    assert len(plex.title_requests) == 1 and len(plex.writes()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["title", "image"])
async def test_upgrade_resumes_retired_media_suspension_without_duplicate_write(setup, kind):
    browser, service, plex = setup
    now, _ = real_playback(service)
    enable(browser)
    service.plex.worker.download = red_image
    plex.title_timeout = kind == "title"
    plex.timeout_after_write = kind == "image"
    await service.plex.worker.run_once("living-room")
    assert state(service)["in_flight"]
    # Persist the old version's suspension and fingerprint before new reconciliation runs.
    service.db.before_commit = None
    with service.db.transaction() as db:
        saved = runtime(db, "living-room")
        error = "Protected Plex metadata changed; " + (
            "updates suspended" if kind == "title" else "artwork updates suspended"
        )
        saved.update(blocked=error, error=error, retry_at=None)
        saved["in_flight"].update(protected={"old": "media fingerprint"}, preserve_sort=True)
        save_runtime(db, "living-room", saved)
    plex.duration = "1"
    await restart_plex(service, now).run_once("living-room")
    assert not state(service)["blocked"] and not state(service)["pending"]
    assert not state(service)["in_flight"]
    assert len(plex.title_requests) == 1 and len(plex.writes()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        "Plex returned HTTP 401 for GET /identity",
        "Plex target identity changed; select the item again",
    ],
)
async def test_upgrade_keeps_unrelated_suspensions(setup, error):
    browser, service, plex = setup
    now, _ = real_playback(service)
    enable(browser)
    with service.db.transaction() as db:
        saved = runtime(db, "living-room")
        saved.update(blocked=error, error=error, pending=True)
        save_runtime(db, "living-room", saved)
    plex.calls.clear()
    await restart_plex(service, now).run_once("living-room")
    assert state(service)["blocked"] == error
    assert not plex.calls


@pytest.mark.asyncio
async def test_new_title_supersedes_old_uncertain_edit(setup):
    browser, service, plex = setup
    now, cid = real_playback(service)
    enable(browser)
    service.plex.worker.download = red_image
    plex.title_timeout = True
    await service.plex.worker.run_once("living-room")
    event_title(service, cid, "Beijing Open: Day 8")
    await restart_plex(service, now).run_once("living-room")
    assert len(plex.title_requests) == 2 and plex.title == "Beijing Open: Day 8"
    assert state(service)["applied_title"]["value"] == "Beijing Open: Day 8"


@pytest.mark.asyncio
async def test_disable_during_title_write_stops_later_artwork(setup):
    browser, service, plex = setup
    real_playback(service)
    enable(browser)
    service.plex.worker.download = red_image

    async def disable():
        with service.db.transaction() as db:
            config = configuration(db, "living-room")
            config.update(enabled=False, revision=4)
            save_configuration(db, "living-room", config)

    plex.during_title = disable
    await service.plex.worker.run_once("living-room")
    assert len(plex.title_requests) == 1 and not plex.writes()
    assert not state(service)["pending"]
    await service.plex.worker.run_once("living-room")
    assert len(plex.title_requests) == 1


@pytest.mark.asyncio
async def test_title_http_failure_omits_query_and_does_not_start_artwork(setup):
    browser, service, plex = setup
    real_playback(service)
    enable(browser)
    plex.title_status = 404
    await service.plex.worker.run_once("living-room")
    assert state(service)["blocked"] == "Plex returned HTTP 404 for PUT /library/sections/1/all"
    assert not plex.writes()


@pytest.mark.asyncio
async def test_title_acknowledgement_requires_readback(setup):
    browser, service, plex = setup
    real_playback(service)
    enable(browser)
    service.plex.worker.download = red_image
    plex.ignore_title = True
    await service.plex.worker.run_once("living-room")
    assert state(service)["pending"] and state(service)["in_flight"]
    assert not state(service)["applied_title"] and not plex.writes()
    plex.ignore_title = False
    ready_retry(service)
    await service.plex.worker.run_once("living-room")
    assert not state(service)["pending"]
    assert plex.title == state(service)["desired"]["plex_title"]


@pytest.mark.asyncio
async def test_title_client_rejects_multiple_targets_and_invalid_section():
    plex = Plex()
    client = PlexClient("http://plex:32400", TOKEN, httpx.MockTransport(plex.handle))
    try:
        with pytest.raises(ValueError):
            await client.set_title("8,9", "1", "Dillflix Live")
        with pytest.raises(PlexError):
            await client.set_title("8", "1?other=value", "Dillflix Live")
    finally:
        await client.close()
    assert not plex.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slot", "upload", "element"),
    [("poster", "posters", "thumb"), ("background", "arts", "art")],
)
async def test_upload_uses_plural_route_and_reads_current_image(slot, upload, element):
    plex = Plex()
    client = PlexClient("http://plex:32400", TOKEN, httpx.MockTransport(plex.handle))
    asset = image_asset(picture("red"))
    try:
        await client.set_image("8", slot, asset)
        assert (await client.image("8", slot)).digest == asset.digest
    finally:
        await client.close()
    assert plex.writes() == [("POST", f"/library/metadata/8/{upload}", asset.data)]
    assert ("GET", f"/library/metadata/8/{element}/2", b"") in plex.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["identity", "metadata", "poster", "background"])
async def test_http_error_identifies_operation_without_credentials_or_response_body(operation):
    def missing(_request):
        return httpx.Response(404, text=f"Private server body: {TOKEN}")

    client = PlexClient("http://private-plex:32400", TOKEN, httpx.MockTransport(missing))
    try:
        with pytest.raises(PlexError) as failure:
            if operation == "identity":
                await client.identity()
            elif operation == "metadata":
                await client.metadata("8")
            else:
                await client.set_image("8", operation, image_asset(picture("red")))
    finally:
        await client.close()
    request = {
        "identity": "GET /identity",
        "metadata": "GET /library/metadata/8",
        "poster": "POST /library/metadata/8/posters",
        "background": "POST /library/metadata/8/arts",
    }[operation]
    assert str(failure.value) == f"Plex returned HTTP 404 for {request}"
    assert failure.value.permanent
    assert TOKEN not in str(failure.value) and "private-plex" not in str(failure.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_drift", [False, True])
async def test_retry_after_legacy_upload_404_checks_saved_attempt_before_writing(
    setup, monkeypatch, metadata_drift
):
    from controller.plex.integration import PlexIntegration

    browser, service, plex = setup
    now, _ = real_playback(service)
    enable(browser)
    service.plex.worker.download = red_image

    async def legacy_upload(client, key, slot, asset):
        element = {"poster": "thumb", "background": "art"}[slot]
        await client._request("POST", f"/library/metadata/{key}/{element}", data=asset.data, mime=asset.mime)

    with monkeypatch.context() as old:
        old.setattr(PlexClient, "set_image", legacy_upload)
        await service.plex.worker.run_once("living-room")
    assert state(service)["blocked"] == "Plex returned HTTP 404 for POST /library/metadata/8/thumb"
    assert state(service)["in_flight"] and state(service)["pending"]
    assert plex.images["thumb"] == picture("black")
    # Upgrade/restart preserves the old suspension and durable uncertain request.
    with service.db.transaction() as db:
        saved = runtime(db, "living-room")
        saved.update(blocked="Plex returned HTTP 404", error="Plex returned HTTP 404")
        save_runtime(db, "living-room", saved)
    replacement = PlexIntegration(service)
    replacement.now = lambda: now
    replacement.worker.client_factory = service.plex.worker.client_factory
    replacement.worker.download = red_image
    service.plex = replacement
    service.db.before_commit = replacement.before_commit
    service.db.after_commit = replacement.wake
    plex.calls.clear()
    await replacement.worker.run_once("living-room")
    assert not plex.calls
    if metadata_drift:
        plex.duration = "1"
    response = browser.post(PATH + "/retry", json={"command_id": "retry-404", "expected_revision": 3})
    assert response.status_code == 200, response.text
    await replacement.worker.run_once("living-room")
    assert not state(service)["blocked"] and not state(service)["pending"]
    assert not state(service)["in_flight"]
    assert plex.writes() == [("POST", "/library/metadata/8/posters", picture("red"))]
    assert plex.calls[:3] == [
        ("GET", "/identity", b""),
        ("GET", "/library/metadata/8", b""),
        ("GET", "/library/metadata/8/thumb/1", b""),
    ]
    assert plex.images["thumb"] == picture("red")


def test_settings_capture_and_token_do_not_leak(setup):
    browser, service, plex = setup
    response = browser.get(PATH)
    assert response.status_code == 200
    assert response.json()["credential_configured"]
    assert set(response.json()["defaults"]) == {"poster", "background"}
    assert not plex.writes()
    for path in (
        PATH,
        "/api/v1/overview",
        "/api/v1/devices/living-room/configuration/export",
        "/api/v1/devices/living-room/diagnostics",
    ):
        assert TOKEN not in browser.get(path).text
    with service.db.transaction() as db:
        assert TOKEN not in "\n".join(db.iterdump())
        ref = configuration(db, "living-room")["credential_ref"]
    assert os.stat(service.plex.credentials.directory / ref).st_mode & 0o777 == 0o600
    assert scrub({"X-Plex-Token": TOKEN})["X-Plex-Token"] == "[redacted]"
    assert redact({"plex_token": TOKEN})["plex_token"] == "[redacted]"
    assert TOKEN not in str(scrub("http://plex/?X-Plex-Token=" + TOKEN))


def test_settings_revision_validation_and_idempotency(setup):
    browser, service, plex = setup
    response = browser.put(
        PATH,
        json={
            "command_id": "bad",
            "expected_revision": 0,
            "base_url": "http://plex:32400",
            "token": TOKEN,
            "enabled": False,
        },
    )
    assert response.status_code == 409
    assert (
        browser.post(
            PATH + "/defaults/capture", json={"command_id": "capture", "expected_revision": 1}
        ).status_code
        == 200
    )
    response = browser.post(PATH + "/test", json={"base_url": "http://plex", "token": {"value": TOKEN}})
    assert response.status_code == 422 and TOKEN not in response.text
    response = browser.post(
        PATH + "/test",
        json={"base_url": "http://plex", "token": TOKEN},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert not plex.writes()


@pytest.mark.asyncio
async def test_event_upload_artwork_only_and_no_reupload_on_renewal(setup):
    browser, service, plex = setup
    now, cid = real_playback(service)
    enable(browser)
    service.plex.worker.download = red_image
    await service.plex.worker.run_once("living-room")
    assert [c[1] for c in plex.writes()] == ["/library/metadata/8/posters"]
    assert not state(service)["pending"]
    gen = state(service)["generation"]
    service.plex.now = lambda: now + timedelta(seconds=60)
    with service.db.transaction() as db:
        d = service.db.device(db)
        d["observed"]["observed_at"] = (now + timedelta(seconds=60)).isoformat()
        d["observed"]["valid_until"] = (now + timedelta(seconds=360)).isoformat()
        service.db.save_device(db, d)
    assert state(service)["generation"] == gen
    assert state(service)["fallback_at"] == (now + timedelta(seconds=360)).timestamp()
    await service.plex.worker.run_once("living-room")
    assert len(plex.writes()) == 1


@pytest.mark.asyncio
async def test_existing_verified_tennis_replaces_default_with_provider_image(setup, monkeypatch):
    browser, service, plex = setup
    _, cid = real_playback(service)
    provider = "https://image.discovery.indazn.com/ca/v2/ca/image?id=day_7&width=666&height=374&format=jpg"
    with service.db.transaction() as db:
        snapshot = json.loads(db.execute("SELECT snapshot FROM contents WHERE id=?", (cid,)).fetchone()[0])
        snapshot.update(
            title="Beijing Open: Day 7",
            kind="broadcast",
            competition="tennis",
            event=None,
            artwork={"matchup_logo_url": None, "cover_url": provider},
        )
        db.execute("UPDATE contents SET snapshot=? WHERE id=?", (encode(snapshot), cid))
    # Reproduce persisted 0.15.0 state: the verified event had only its defaults applied.
    with monkeypatch.context() as old:
        old.setattr("controller.current_playback.playback_thumbnail", lambda _artwork: None)
        enable(browser)
        await service.plex.worker.run_once("living-room")
    assert not state(service)["pending"]
    assert plex.images["thumb"] == picture("black")
    generation = state(service)["generation"]
    downloaded = []

    async def download(url):
        downloaded.append(url)
        return image_asset(picture("green"))

    service.plex.worker.download = download
    # Startup reconciliation needs neither a new play request nor an HTTP status poll.
    await service.plex.worker.run_once("living-room")
    assert state(service)["generation"] > generation
    assert downloaded == [provider]
    assert plex.images == {"thumb": picture("green"), "art": picture("gray")}
    assert [c[1] for c in plex.writes()] == ["/library/metadata/8/posters"]
    view = browser.get("/api/v1/devices/living-room/now-playing").json()
    assert view["state"] == "playing" and view["simulated"] is False
    assert view["event"]["title"] == "Beijing Open: Day 7"
    assert view["event"]["thumbnail_url"] == provider
    assert not state(service)["pending"]


@pytest.mark.asyncio
async def test_fallback_deadline_is_from_sample_not_last_read(setup):
    browser, service, plex = setup
    now, _ = real_playback(service)
    enable(browser)
    service.plex.worker.download = red_image
    await service.plex.worker.run_once("living-room")
    due = state(service)["fallback_at"]
    service.plex.now = lambda: now + timedelta(seconds=299)
    with service.db.transaction() as db:
        service.plex.reconcile(db)
    assert state(service)["fallback_at"] == due
    assert state(service)["desired"]["mode"] == "event"
    service.plex.now = lambda: now + timedelta(seconds=300)
    await service.plex.worker.run_once("living-room")
    assert state(service)["desired"]["mode"] == "defaults"
    assert plex.images["thumb"] == picture("black")
    assert len(plex.writes()) == 2


@pytest.mark.asyncio
async def test_take_control_holds_then_restores_without_new_grace(setup):
    browser, service, plex = setup
    now, _ = real_playback(service)
    enable(browser)
    service.plex.worker.download = red_image
    await service.plex.worker.run_once("living-room")
    service.plex.now = lambda: now + timedelta(seconds=250)
    with service.db.transaction() as db:
        d = service.db.device(db)
        d.update(observed=None, manual_control={"id": "manual"})
        service.db.save_device(db, d)
    assert state(service)["hold"]
    service.plex.now = lambda: now + timedelta(seconds=301)
    await service.plex.worker.run_once("living-room")
    assert plex.images["thumb"] == picture("black")


@pytest.mark.asyncio
async def test_upload_failure_has_uncertain_marker_and_reads_before_retry(setup):
    browser, service, plex = setup
    real_playback(service)
    enable(browser)
    service.plex.worker.download = red_image
    plex.timeout_after_write = True
    await service.plex.worker.run_once("living-room")
    assert state(service)["in_flight"] and state(service)["pending"]
    ready_retry(service)
    await service.plex.worker.run_once("living-room")
    assert not state(service)["in_flight"] and not state(service)["pending"]
    assert len(plex.writes()) == 1


@pytest.mark.asyncio
async def test_artwork_update_ignores_media_property_changes(setup):
    browser, service, plex = setup
    real_playback(service)
    enable(browser)
    service.plex.worker.download = red_image
    plex.drift = True
    await service.plex.worker.run_once("living-room")
    assert not state(service)["blocked"] and not state(service)["pending"]
    assert len(plex.writes()) == 1
    await service.plex.worker.run_once("living-room")
    assert len(plex.writes()) == 1


@pytest.mark.asyncio
async def test_new_generation_during_download_discards_old_asset(setup):
    browser, service, plex = setup
    real_playback(service)
    enable(browser)

    async def downloading(_url):
        with service.db.transaction() as db:
            config = configuration(db, "living-room")
            config.update(enabled=False, revision=4)
            save_configuration(db, "living-room", config)
        return image_asset(picture("red"))

    service.plex.worker.download = downloading
    await service.plex.worker.run_once("living-room")
    assert not plex.writes()


@pytest.mark.asyncio
async def test_slow_plex_does_not_hold_database_or_playback_lock(setup):
    browser, service, plex = setup
    real_playback(service)
    enable(browser)
    service.plex.worker.download = red_image
    entered, released = asyncio.Event(), asyncio.Event()

    async def slow(_slot):
        entered.set()
        await released.wait()

    plex.during_write = slow
    task = asyncio.create_task(service.plex.worker.run_once("living-room"))
    await asyncio.wait_for(entered.wait(), 2)

    def change():
        with service.playback_lock:
            with service.db.transaction() as db:
                d = service.db.device(db)
                d["reason"] = "Concurrent playback continues"
                service.db.save_device(db, d)

    await asyncio.wait_for(asyncio.to_thread(change), 2)
    released.set()
    await task


@pytest.mark.asyncio
async def test_simulator_and_disabled_never_write(setup):
    browser, service, plex = setup
    # Inject a stale enabled config to exercise the backend guard, not just UI validation.
    with service.db.transaction() as db:
        config = configuration(db, "living-room")
        config.update(enabled=True, enabled_at=0)
        save_configuration(db, "living-room", config)
    await service.plex.worker.run_once("living-room")
    assert not plex.mutations()


@pytest.mark.asyncio
async def test_auth_and_server_identity_failures_suspend(setup):
    browser, service, plex = setup
    real_playback(service)
    enable(browser)
    service.plex.worker.download = red_image
    plex.machine = "wrong-server"
    await service.plex.worker.run_once("living-room")
    assert "identity" in state(service)["blocked"] and not plex.writes()


@pytest.mark.asyncio
async def test_missing_source_uses_default_and_failed_source_is_retryable(setup):
    browser, service, plex = setup
    real_playback(service)
    enable(browser)
    service.plex.worker.download = red_image
    await service.plex.worker.run_once("living-room")
    with service.db.transaction() as db:
        d = service.db.device(db)
        cid = d["observed"]["content_id"]
        snap = json.loads(db.execute("SELECT snapshot FROM contents WHERE id=?", (cid,)).fetchone()[0])
        snap["artwork"] = {}
        db.execute("UPDATE contents SET snapshot=? WHERE id=?", (encode(snap), cid))
    await service.plex.worker.run_once("living-room")
    assert plex.images["thumb"] == picture("black")


def test_defaults_upload_and_preview_are_validated(setup):
    browser, service, plex = setup
    params = {"command_id": "upload", "expected_revision": 2}
    assert browser.post(PATH + "/defaults/poster", params=params, content=b"not an image").status_code == 502
    response = browser.post(PATH + "/defaults/poster", params=params, content=picture("blue"))
    assert response.status_code == 200, response.text
    preview = browser.get(PATH + "/images/default/poster")
    assert preview.content == picture("blue")
    assert preview.headers["cache-control"] == "no-store"
    assert not plex.writes()


def test_transaction_rollback_does_not_publish_work(setup):
    browser, service, plex = setup
    real_playback(service)
    enable(browser)
    before = state(service)
    with pytest.raises(ValueError):
        with service.db.transaction() as db:
            config = configuration(db, "living-room")
            config.update(enabled=False)
            save_configuration(db, "living-room", config)
            raise ValueError("rollback")
    assert state(service) == before


@pytest.mark.asyncio
async def test_partial_background_failure_retries_without_reuploading_poster(setup):
    browser, service, plex = setup
    real_playback(service)
    enable(browser)
    assert (
        browser.post(
            PATH + "/defaults/background",
            params={"command_id": "blue", "expected_revision": 3},
            content=picture("blue"),
        ).status_code
        == 200
    )
    service.plex.worker.download = red_image
    plex.fail_art = True
    await service.plex.worker.run_once("living-room")
    assert set(state(service)["applied"]) == {"poster"}
    assert state(service)["pending"]
    plex.fail_art = False
    ready_retry(service)
    await service.plex.worker.run_once("living-room")
    assert not state(service)["pending"]
    assert [c[1] for c in plex.writes()].count("/library/metadata/8/posters") == 1
    assert plex.images["art"] == picture("blue")


@pytest.mark.asyncio
async def test_failing_new_event_image_applies_default_then_retries(setup):
    from controller.plex.client import PlexError

    browser, service, plex = setup
    real_playback(service)
    enable(browser)
    plex.images["thumb"] = picture("green")

    async def unavailable(_url):
        raise PlexError("Image unavailable")

    service.plex.worker.download = unavailable
    await service.plex.worker.run_once("living-room")
    assert plex.images["thumb"] == picture("black")
    assert state(service)["pending"] and not state(service)["blocked"]
    service.plex.worker.download = red_image
    ready_retry(service)
    await service.plex.worker.run_once("living-room")
    assert plex.images["thumb"] == picture("red")


@pytest.mark.asyncio
async def test_disabled_while_poster_in_flight_stops_background(setup):
    browser, service, plex = setup
    real_playback(service)
    enable(browser)
    assert (
        browser.post(
            PATH + "/defaults/background",
            params={"command_id": "blue", "expected_revision": 3},
            content=picture("blue"),
        ).status_code
        == 200
    )
    service.plex.worker.download = red_image

    async def disable(_slot):
        with service.db.transaction() as db:
            c = configuration(db, "living-room")
            c.update(enabled=False, revision=5)
            save_configuration(db, "living-room", c)

    plex.during_write = disable
    await service.plex.worker.run_once("living-room")
    assert [c[1] for c in plex.writes()] == ["/library/metadata/8/posters"]
    assert not state(service)["pending"]


@pytest.mark.asyncio
async def test_restore_defaults_disables_auto_and_is_one_shot(setup):
    browser, service, plex = setup
    real_playback(service)
    enable(browser)
    service.plex.worker.download = red_image
    await service.plex.worker.run_once("living-room")
    response = browser.post(
        PATH + "/restore-defaults", json={"command_id": "restore-defaults", "expected_revision": 3}
    )
    assert response.status_code == 200
    await service.plex.worker.run_once("living-room")
    assert plex.images["thumb"] == picture("black")
    assert plex.title == "Dillflix Live"
    title_writes = len(plex.title_requests)
    count = len(plex.writes())
    await service.plex.worker.run_once("living-room")
    assert len(plex.writes()) == count
    assert len(plex.title_requests) == title_writes
    assert not browser.get(PATH).json()["enabled"]


def test_actual_transaction_change_and_rollback_notifications(setup):
    browser, service, plex = setup
    real_playback(service)
    enable(browser)
    calls = []
    service.db.after_commit = lambda: calls.append("wake")
    response = browser.post(PATH + "/resync", json={"command_id": "resync", "expected_revision": 3})
    assert response.status_code == 200 and calls == ["wake"]
    calls.clear()
    with pytest.raises(ValueError):
        with service.db.transaction() as db:
            c = configuration(db, "living-room")
            c["enabled"] = False
            save_configuration(db, "living-room", c)
            service.plex.reconcile(db)
            raise ValueError()
    assert not calls


def test_projection_failure_does_not_rollback_playback(setup, monkeypatch):
    browser, service, plex = setup

    def broken(_db):
        raise ValueError("unexpected projection problem")

    monkeypatch.setattr(service.plex, "reconcile", broken)
    with service.db.transaction() as db:
        d = service.db.device(db)
        d["reason"] = "Playback remains committed"
        service.db.save_device(db, d)
    with service.db.transaction() as db:
        assert service.db.device(db)["reason"] == "Playback remains committed"


@pytest.mark.asyncio
async def test_stored_defaults_and_disabled_state_survive_database_restore(setup, tmp_path):
    from controller import ops
    from controller.database import Database

    browser, service, plex = setup
    source, target = tmp_path / "backup.sqlite", tmp_path / "restored.sqlite"
    ops.backup(service.settings.database, source)
    ops.restore(source, target, expected_mode="demo")
    restored = Database(str(target))
    with restored.transaction() as db:
        c = configuration(db, "living-room")
        s = runtime(db, "living-room")
        assert set(c["defaults"]) == {"poster", "background"}
        assert not c["enabled"] and not s["pending"] and not s["in_flight"]
        assert "restored" in s["blocked"]
        assert db.execute("SELECT COUNT(*) FROM plex_assets").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_restart_keeps_deadline_and_does_not_reassert_old_event(setup):
    from controller.plex.integration import PlexIntegration

    browser, service, plex = setup
    now, _ = real_playback(service)
    enable(browser)
    service.plex.worker.download = red_image
    await service.plex.worker.run_once("living-room")
    with service.db.transaction() as db:
        d = service.db.device(db)
        d["observed"]["verified"] = False
        service.db.save_device(db, d)
    replacement = PlexIntegration(service)
    replacement.now = lambda: now + timedelta(seconds=301)
    replacement.worker.client_factory = service.plex.worker.client_factory
    service.plex = replacement
    service.db.before_commit = replacement.before_commit
    service.db.after_commit = replacement.wake
    await replacement.worker.run_once("living-room")
    assert state(service)["fallback_at"] == (now + timedelta(seconds=300)).timestamp()
    assert plex.images["thumb"] == picture("black")


@pytest.mark.asyncio
async def test_worker_wakes_for_changes_then_sleeps_without_polling(setup):
    browser, service, plex = setup
    real_playback(service)
    enable(browser)
    service.plex.worker.download = red_image
    completed = asyncio.Event()
    actual = service.plex.worker.run_once

    async def wrapped(device):
        await actual(device)
        if not state(service)["pending"]:
            completed.set()

    service.plex.worker.run_once = wrapped
    task = asyncio.create_task(service.plex.worker.run())
    await asyncio.wait_for(completed.wait(), 2)
    for _ in range(5):
        await asyncio.sleep(0)
    calls = len(plex.calls)
    await asyncio.sleep(0.05)
    assert len(plex.calls) == calls
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_auth_failures_and_missing_credentials_do_not_leak_tokens(setup):
    browser, service, plex = setup
    real_playback(service)
    enable(browser)
    service.plex.worker.download = red_image
    plex.fail_identity = True
    await service.plex.worker.run_once("living-room")
    assert "401" in state(service)["blocked"] and TOKEN not in json.dumps(state(service))
    assert not plex.writes()


def test_invalid_upload_query_is_422_not_server_error(setup):
    browser, _, _ = setup
    response = browser.post(
        PATH + "/defaults/poster", params={"command_id": "", "expected_revision": -1}, content=picture("red")
    )
    assert response.status_code == 422


def switch_event(service, new_id="demo:lions"):
    with service.db.transaction() as db:
        d = service.db.device(db)
        assert d["observed"]["content_id"] != new_id
        d["observed"]["content_id"] = new_id
        db.execute("UPDATE jobs SET content_id=? WHERE id=?", (new_id, d["observed"]["request_id"]))
        snapshot = json.loads(db.execute("SELECT snapshot FROM contents WHERE id=?", (new_id,)).fetchone()[0])
        snapshot["artwork"] = {"matchup_logo_url": "http://images/game/logo.png?style=6"}
        db.execute("UPDATE contents SET snapshot=? WHERE id=?", (encode(snapshot), new_id))
        service.db.save_device(db, d)


@pytest.mark.asyncio
async def test_new_event_reusing_url_is_fetched_again_and_supersedes_old_download(setup):
    browser, service, plex = setup
    real_playback(service)
    enable(browser)
    calls = []

    async def changing(url):
        calls.append(url)
        if len(calls) == 1:
            switch_event(service)
            return image_asset(picture("red"))
        return image_asset(picture("blue"))

    service.plex.worker.download = changing
    await service.plex.worker.run_once("living-room")
    assert not plex.writes()
    await service.plex.worker.run_once("living-room")
    assert len(calls) == 2 and calls[0] == calls[1]
    assert plex.images["thumb"] == picture("blue")
    assert state(service)["desired"]["content_id"] == "demo:lions"


@pytest.mark.asyncio
async def test_desired_event_and_paused_automation_do_not_replace_observed_art(setup):
    browser, service, plex = setup
    now, cid = real_playback(service)
    enable(browser)
    with service.db.transaction() as db:
        d = service.db.device(db)
        d.update(desired="demo:lions", playback_state="navigating", automation="paused")
        service.db.save_device(db, d)
    assert state(service)["desired"]["content_id"] == cid
    assert state(service)["fallback_at"] == (now + timedelta(seconds=300)).timestamp()


def test_schema_eight_migration_preserves_device_and_defaults_to_disabled(rig):
    from controller.database import Database

    _, service, settings = rig
    with service.db.transaction() as db:
        before = service.db.device(db)
        for table in ("plex_settings", "plex_runtime", "plex_assets", "plex_commands"):
            db.execute("DROP TABLE " + table)
        db.execute("PRAGMA user_version=8")
    upgraded = Database(settings.database)
    with upgraded.transaction() as db:
        assert upgraded.device(db) == before
        assert not configuration(db, "living-room")["enabled"]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 10


@pytest.mark.asyncio
async def test_source_download_does_not_send_plex_credentials():
    from controller.plex.client import download_image

    seen = []

    def get(request):
        seen.append(request)
        assert "X-Plex-Token" not in request.headers
        return httpx.Response(200, content=picture("red"))

    result = await download_image("http://images/game.png", httpx.MockTransport(get))
    assert result.digest == image_asset(picture("red")).digest and len(seen) == 1
