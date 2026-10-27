import asyncio
from datetime import UTC, datetime, timedelta
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

import controller.api as api_module
from controller.config import Settings
from controller.live_streams import MAX_CONNECTIONS, MAX_JSON, LiveStreams, MediaResponse

ROOT = "/api/public/v1/streams"
SESSION = "browser_0123456789abcdef"
MEDIA = ROOT + f"/sessions/{SESSION}/hls/"
KEY = "server-only-live-key"
USER = {"x-dillflix-proxy-key": "proxy-key", "x-dillflix-role": "user", "x-dillflix-user": "viewer"}
ORIGIN = {"origin": "http://testserver"}


def feed(stream_id="sports0", status="live"):
    return {"id": stream_id, "label": stream_id, "status": status, "default_tier": 2,
            "tiers": [{"id": 0, "label": "1080p60", "status": status},
                      {"id": 2, "label": "720p30", "status": status}]}


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.parts = chunks
        self.closed = False

    async def __aiter__(self):
        for part in self.parts:
            yield part

    async def aclose(self):
        self.closed = True


@pytest.fixture
def live_client(tmp_path, monkeypatch):
    @contextmanager
    def build(handler, *, enabled=True, public_mode="guest", device_map=None, public_origin=""):
        settings = Settings(
            database=str(tmp_path / "live.sqlite"), public_auth_mode=public_mode,
            public_origin=public_origin,
            proxy_secret="proxy-key", live_browser_api_url="http://live-lb:8000" if enabled else "",
            live_browser_api_key=KEY if enabled else "",
            live_browser_device_map=device_map or {}, simulation_delay=0,
        )
        gateway = LiveStreams(settings, transport=httpx.MockTransport(handler))
        monkeypatch.setattr(api_module, "LiveStreams", lambda _: gateway)
        app = api_module.create_app(settings, start_workers=False)
        with TestClient(app) as client:
            yield client, gateway, app.state.controller
        assert gateway.client is None or gateway.client.is_closed
    return build


@pytest.mark.parametrize("stream_id", ["sports0", "sports1"])
def test_https_public_origin_supports_stream_creation_and_release(live_client, stream_id):
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"session_id": SESSION, "expires_in": 120})
        return httpx.Response(204)

    origin = "https://live-controller.example.com"
    headers = {"host": "live-controller.example.com", "origin": origin, "sec-fetch-site": "same-origin"}
    with live_client(handler, public_origin=origin) as (client, _, _):
        response = client.post(ROOT + "/sessions", headers=headers, json={"stream_id": stream_id, "tier": 2})
        assert response.status_code == 200, response.text
        assert response.json()["playlist_url"] == MEDIA + "index.m3u8"
        assert client.delete(ROOT + f"/sessions/{SESSION}", headers=headers).status_code == 204
        assert [request.method for request in calls] == ["POST", "DELETE"]


def test_disabled_catalog_does_not_contact_live_service(live_client):
    def handler(_):
        pytest.fail("Disabled configuration contacted upstream")

    with live_client(handler, enabled=False) as (client, _, _):
        assert client.get(ROOT).json() == {"enabled": False, "streams": []}
        assert client.post(ROOT + "/sessions", headers=ORIGIN,
                           json={"stream_id": "sports0", "tier": 2}).status_code == 503


def test_catalog_projects_all_feeds_without_private_fields(live_client):
    def handler(request):
        assert str(request.url) == "http://live-lb:8000/api/browser/v1/streams"
        assert request.headers["x-dillflix-live-key"] == KEY
        return httpx.Response(200, json={"streams": [
            {**feed(), "upstream_url": "http://private/secret", "api_key": KEY},
            feed("sports1", "offline"), feed("sports_future", "unknown")], "secret": KEY})

    with live_client(handler) as (client, _, _):
        response = client.get(ROOT)
        assert response.status_code == 200
        assert [s["id"] for s in response.json()["streams"]] == ["sports0", "sports1", "sports_future"]
        assert "secret" not in response.text and "http://" not in response.text
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("mapped_feed", ["sports0", "sports1"])
def test_catalog_title_is_verified_device_scoped_and_read_only(live_client, mapped_feed, monkeypatch):
    feeds = [feed(), feed("sports1"), feed("mirror"), feed("missing")]
    mapping = {mapped_feed: "living-room", "mirror": "living-room", "missing": "absent-device"}
    with live_client(lambda _: httpx.Response(200, json={"streams": feeds}), device_map=mapping) as (client, _, service):
        assert all(item["now_playing"] is None for item in client.get(ROOT).json()["streams"])
        service.tick()
        with service.db.transaction() as db:
            device = service.db.device(db)
            device["observed"]["simulated"] = False
            service.db.save_device(db, device)
            before = list(db.iterdump())

        def forbidden(*args, **kwargs):
            pytest.fail("Feed titles must not issue playback commands or poll the device")

        monkeypatch.setattr(service, "tick", forbidden)
        monkeypatch.setattr(service.playback, "observe", forbidden)
        response = client.get(ROOT)
        assert response.status_code == 200
        streams = {item["id"]: item for item in response.json()["streams"]}
        title = streams[mapped_feed]["now_playing"]
        assert title["title"] == "NFL RedZone"
        assert set(title) == {"title", "valid_until"}
        assert datetime.fromisoformat(title["valid_until"]) > datetime.now(UTC)
        assert streams["mirror"]["now_playing"] == title
        assert streams["sports1" if mapped_feed == "sports0" else "sports0"]["now_playing"] is None
        assert streams["missing"]["now_playing"] is None
        assert "living-room" not in response.text
        with service.db.snapshot() as db:
            assert list(db.iterdump()) == before


@pytest.mark.parametrize("condition", ["expired", "unverified", "manual", "completed", "simulated", "offline", "unknown"])
def test_catalog_does_not_label_a_feed_without_verified_live_playback(live_client, condition):
    item = feed(status=condition if condition in {"offline", "unknown"} else "live")
    # An upstream title must not bypass the controller's verified projection.
    item["now_playing"] = {"title": "Untrusted upstream title"}
    with live_client(lambda _: httpx.Response(200, json={"streams": [item]}),
                     device_map={"sports0": "living-room"}) as (client, _, service):
        service.tick()
        with service.db.transaction() as db:
            device = service.db.device(db)
            observed = device["observed"]
            observed["simulated"] = condition == "simulated"
            if condition == "expired":
                observed["valid_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            elif condition == "unverified":
                observed["verified"] = False
            elif condition == "manual":
                device["manual_control"] = {"id": "owner"}
            elif condition == "completed":
                device["manual_completions"][observed["content_id"]] = datetime.now(UTC).isoformat()
            service.db.save_device(db, device)
        response = client.get(ROOT)
        assert response.status_code == 200
        assert response.json()["streams"][0]["now_playing"] is None


@pytest.mark.parametrize("mapping", [None, [], "sports0", {"../feed": "living-room"},
                                      {"sports0": ""}, {"sports0": ["living-room"]}])
def test_invalid_feed_device_mapping_is_rejected(mapping):
    with pytest.raises(ValueError, match="LIVE_BROWSER_DEVICE_MAP"):
        Settings(live_browser_device_map=mapping)


def test_feed_device_mapping_is_loaded_from_environment(monkeypatch):
    monkeypatch.setenv("LIVE_BROWSER_DEVICE_MAP", '{"sports1":"living-room"}')
    assert Settings.from_env().live_browser_device_map == {"sports1": "living-room"}
    monkeypatch.setenv("LIVE_BROWSER_DEVICE_MAP", "")
    assert Settings.from_env().live_browser_device_map == {}


@pytest.mark.parametrize("bad", [
    {**feed(), "id": "../admin"}, {**feed(), "id": "https://evil.example"},
    {**feed(), "tiers": [{"id": True, "label": "x", "status": "live"}]},
    {**feed(), "status": "playing"}, {**feed(), "label": "bad\nlabel"},
    {**feed(), "default_tier": 5}, {**feed(), "tiers": []},
])
def test_invalid_catalog_fails_closed(live_client, bad):
    with live_client(lambda _: httpx.Response(200, json={"streams": [bad]})) as (client, _, _):
        response = client.get(ROOT)
        assert response.status_code == 502
        assert KEY not in response.text


def test_bounded_upstream_json_and_duplicate_streams(live_client):
    for response in (httpx.Response(200, content=b" " * (MAX_JSON + 1)),
                     httpx.Response(200, json={"streams": [feed(), feed()]})):
        with live_client(lambda _, response=response: response) as (client, gateway, _):
            assert client.get(ROOT).status_code == 502
            assert gateway.slots._value == MAX_CONNECTIONS


@pytest.mark.parametrize("status,expected", [(302, 502), (401, 502), (403, 502), (404, 404),
                                            (409, 409), (429, 429), (500, 502), (503, 503)])
def test_upstream_errors_are_generic_and_redirects_not_followed(live_client, status, expected):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=KEY, headers={"Location": "http://private/secret", "Set-Cookie": KEY})

    with live_client(handler) as (client, gateway, _):
        response = client.get(ROOT)
        assert response.status_code == expected
        assert len(calls) == 1
        assert KEY not in response.text and "private" not in response.text
        assert "location" not in response.headers and "set-cookie" not in response.headers
        assert gateway.slots._value == MAX_CONNECTIONS


def test_service_timeout_releases_capacity(live_client):
    def handler(request):
        raise httpx.ReadTimeout("private-host-secret", request=request)

    with live_client(handler) as (client, gateway, _):
        response = client.get(ROOT)
        assert response.status_code == 503 and "private" not in response.text
        assert gateway.slots._value == MAX_CONNECTIONS


def test_session_and_media_do_not_change_scheduler_and_never_forward_viewer_credentials(live_client):
    calls = []
    segment = TrackedStream([b"\x47" * 188] * 3)
    def handler(request):
        calls.append((request.method, request.url.path))
        assert request.headers["x-dillflix-live-key"] == KEY
        assert "cookie" not in request.headers and "authorization" not in request.headers
        if request.method == "POST":
            assert request.content == b'{"stream_id":"sports1","tier":2}'
            return httpx.Response(201, json={"session_id": SESSION, "expires_in": 90,
                                             "playlist_url": "http://private/wrong", "secret": KEY})
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path.endswith("index.m3u8"):
            return httpx.Response(200, content=b"#EXTM3U\n#EXTINF:2,\nmedia-00001.ts\n")
        return httpx.Response(200, stream=segment, headers={"Content-Length": "564", "Set-Cookie": KEY})

    with live_client(handler) as (client, gateway, service):
        before = service.overview()["device"]
        response = client.post(ROOT + "/sessions", headers=ORIGIN,
                               json={"stream_id": "sports1", "tier": 2})
        assert response.status_code == 200
        assert response.json() == {"session_id": SESSION, "expires_in": 90,
                                   "playlist_url": MEDIA + "index.m3u8"}
        playlist = client.get(response.json()["playlist_url"])
        assert playlist.status_code == 200 and b"media-00001.ts" in playlist.content
        video = client.get(MEDIA + "media-00001.ts", headers={"Cookie": "viewer=private", "Authorization": "secret"})
        assert video.content == b"\x47" * 564
        assert video.headers["content-type"] == "video/mp2t"
        assert video.headers["cache-control"] == "no-store"
        assert "set-cookie" not in video.headers and KEY not in str(video.headers)
        assert client.delete(ROOT + f"/sessions/{SESSION}", headers=ORIGIN).status_code == 204
        assert service.overview()["device"] == before
        assert segment.closed and gateway.slots._value == MAX_CONNECTIONS
        assert len(calls) == 4


@pytest.mark.parametrize("body", [
    {"stream_id": "../sports0", "tier": 2}, {"stream_id": "sports0", "tier": True},
    {"stream_id": "sports0", "tier": 6}, {"stream_id": "sports0", "tier": "2"},
    {"stream_id": "sports0", "tier": 2, "url": "http://evil"}, [],
])
def test_session_input_is_strict_and_bounded(live_client, body):
    with live_client(lambda _: pytest.fail("Invalid input reached upstream")) as (client, _, _):
        assert client.post(ROOT + "/sessions", json=body, headers=ORIGIN).status_code == 400
        assert client.post(ROOT + "/sessions", content=b"x" * 4097, headers=ORIGIN).status_code == 413


@pytest.mark.parametrize("body", [
    {"session_id": "../../private", "expires_in": 90}, {"session_id": SESSION, "expires_in": True},
    {"session_id": SESSION, "expires_in": -1}, {"session_id": SESSION, "expires_in": 9999999},
])
def test_malicious_upstream_session_never_returns_a_url(live_client, body):
    with live_client(lambda _: httpx.Response(200, json=body)) as (client, _, _):
        response = client.post(ROOT + "/sessions", headers=ORIGIN, json={"stream_id": "sports0", "tier": 2})
        assert response.status_code == 502 and "playlist_url" not in response.text


def test_authentication_and_origin_cover_sessions_playlists_and_segments(live_client):
    def handler(request):
        if request.url.path.endswith("/streams"):
            return httpx.Response(200, json={"streams": []})
        return httpx.Response(200, content=b"#EXTM3U\n" if request.url.path.endswith("m3u8") else b"media")

    with live_client(handler, public_mode="proxy") as (client, _, _):
        for path in (ROOT, MEDIA + "index.m3u8", MEDIA + "media-00001.ts"):
            assert client.get(path).status_code == 401
            assert client.get(path, headers=USER).status_code == 200
            assert client.get(path, headers={**USER, "x-dillflix-proxy-key": "forged"}).status_code == 401
        assert client.post(ROOT + "/sessions", headers=USER, json={"stream_id": "sports0", "tier": 2}).status_code == 403
        assert client.delete(ROOT + f"/sessions/{SESSION}", headers={**USER, "origin": "https://evil"}).status_code == 403
        assert client.get("/api/v1/overview", headers=USER).status_code == 403
    with live_client(handler, public_mode="guest") as (client, _, _):
        assert client.get(MEDIA + "media-00001.ts").status_code == 200
        assert client.post(ROOT + "/sessions", json={"stream_id": "sports0", "tier": 2}).status_code == 403


@pytest.mark.parametrize("reference", ["https://evil/segment.ts", "//evil/segment.ts", "../segment.ts",
                                        "segment.ts?key=secret", "other/index.m3u8", "segment%2fts"])
def test_playlist_references_must_stay_on_local_segment_route(live_client, reference):
    stream = TrackedStream([("#EXTM3U\n" + reference + "\n").encode()])
    with live_client(lambda _: httpx.Response(200, stream=stream)) as (client, gateway, _):
        assert client.get(MEDIA + "index.m3u8").status_code == 502
        assert stream.closed and gateway.slots._value == MAX_CONNECTIONS


@pytest.mark.parametrize("tag", ['#EXT-X-KEY:METHOD=AES-128,URI="https://evil/key"',
                                 '#EXT-X-MAP:URI="init.mp4"', '#EXT-X-STREAM-INF:BANDWIDTH=1'])
def test_nonlocal_or_unsupported_playlist_tags_are_rejected(live_client, tag):
    with live_client(lambda _: httpx.Response(200, content=("#EXTM3U\n" + tag + "\n").encode())) as (client, _, _):
        assert client.get(MEDIA + "index.m3u8").status_code == 502


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("byte_range", ["bytes=0-", "bytes=0-7", "bytes=-8", "bytes=1-2,4-5"])
def test_playlist_ranges_return_complete_response(live_client, method, byte_range):
    body = b"#EXTM3U\n#EXTINF:2,\nmedia-00001.ts\n"
    stream = TrackedStream([body] if method == "GET" else [])
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == method
        assert request.url.path.endswith("/hls/index.m3u8")
        assert "range" not in request.headers
        return httpx.Response(200, stream=stream, headers={
            "Content-Length": str(len(body)), "Accept-Ranges": "bytes"})

    with live_client(handler) as (client, gateway, _):
        response = client.request(method, MEDIA + "index.m3u8", headers={"Range": byte_range})
        assert response.status_code == 200
        assert response.content == (body if method == "GET" else b"")
        assert response.headers["content-type"] == "application/vnd.apple.mpegurl"
        assert response.headers["content-length"] == str(len(body))
        assert response.headers["accept-ranges"] == "none"
        assert "content-range" not in response.headers
        assert response.headers["cache-control"] == "no-store"
        assert len(calls) == 1
        assert stream.closed and gateway.slots._value == MAX_CONNECTIONS


def test_playlist_range_does_not_bypass_authentication_or_validation(live_client):
    calls = []

    def handler(request):
        calls.append(request)
        assert "range" not in request.headers
        return httpx.Response(200, content=b"#EXTM3U\nhttps://private/segment.ts\n")

    with live_client(handler, public_mode="proxy") as (client, gateway, _):
        path = MEDIA + "index.m3u8"
        assert client.get(path, headers={"Range": "bytes=0-"}).status_code == 401
        assert not calls
        response = client.get(path, headers={**USER, "Range": "bytes=0-"})
        assert response.status_code == 502
        assert "private" not in response.text
        assert len(calls) == 1
        assert gateway.slots._value == MAX_CONNECTIONS


def test_head_and_single_byte_range_are_preserved(live_client):
    streams = []
    def handler(request):
        assert request.headers.get("range") == "bytes=0-187"
        stream = TrackedStream([b"\x47" * 188])
        streams.append(stream)
        return httpx.Response(206, stream=stream, headers={"Content-Length": "188", "Content-Range": "bytes 0-187/564",
                                                          "Accept-Ranges": "bytes"})

    with live_client(handler) as (client, gateway, _):
        for method in (client.head, client.get):
            response = method(MEDIA + "media-00001.ts", headers={"Range": "bytes=0-187"})
            assert response.status_code == 206
            assert response.headers["content-range"] == "bytes 0-187/564"
            assert response.headers["content-length"] == "188"
        for value in ("bytes=1-2,4-5", "nonsense"):
            assert client.get(MEDIA + "media-00001.ts", headers={"Range": value}).status_code == 416
        assert all(stream.closed for stream in streams)
        assert gateway.slots._value == MAX_CONNECTIONS


@pytest.mark.parametrize("headers", [{"Content-Length": str(33 * 1024 * 1024)},
                                    {"Content-Encoding": "gzip"}])
def test_oversized_or_encoded_media_is_closed_before_streaming(live_client, headers):
    stream = TrackedStream([b"media"])
    with live_client(lambda _: httpx.Response(200, stream=stream, headers=headers)) as (client, gateway, _):
        assert client.get(MEDIA + "media-00001.ts").status_code == 502
        assert stream.closed and gateway.slots._value == MAX_CONNECTIONS


async def test_upstream_capacity_is_bounded_without_opening_a_request():
    settings = Settings(live_browser_api_url="http://live-lb:8000", live_browser_api_key=KEY)
    gateway = LiveStreams(settings, transport=httpx.MockTransport(lambda _: pytest.fail("No slot available")))
    try:
        for _ in range(MAX_CONNECTIONS):
            await gateway.slots.acquire()
        with pytest.raises(Exception) as error:
            await gateway.streams()
        assert error.value.status_code == 429
        assert gateway.slots._value == 0
    finally:
        await gateway.close()


async def test_segment_response_releases_upstream_when_downstream_disconnects():
    stream = TrackedStream([b"media"])
    response = httpx.Response(200, stream=stream)
    released = []
    async def release(upstream):
        await upstream.aclose()
        released.append(True)
    async def send(_):
        raise asyncio.CancelledError()
    async def receive():
        await asyncio.sleep(100)
    result = MediaResponse(response, release, {})
    with pytest.raises(asyncio.CancelledError):
        await result({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert stream.closed and released == [True]


@pytest.mark.parametrize("url,key", [("https://user:pass@lb", KEY), ("file:///tmp", KEY),
                                      ("http://lb/path", KEY), ("http://lb?x=1", KEY),
                                      ("http://lb", ""), ("", KEY), ("http://lb", "bad\nkey")])
def test_configuration_rejects_ambiguous_or_credentialed_origins(url, key):
    with pytest.raises(ValueError):
        Settings(live_browser_api_url=url, live_browser_api_key=key)
