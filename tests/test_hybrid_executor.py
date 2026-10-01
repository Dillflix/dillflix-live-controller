"""Hybrid decision boundaries: supplied labels, varied layouts, and runtime continuity."""

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from test_real_executor import Device, Vision, identity, payload, scene, settings

from controller.executor.models import ActionMenu, ExecutorError, PlaybackReport
from controller.executor.navigation import MenuPlan, activation_agrees
from controller.executor.verification import playback_sample, progression
from controller.service import Controller


def native(text, revision=1, description=None):
    focus = {"text": text, "contentDescription": description}
    return {
        "usable": True,
        "focus": focus,
        "channels": {"input": {"usable": True, "focus": focus}},
        "validity": {"generation": revision, "revision": revision, "actionId": revision},
        "captureAssociation": {"status": "unchanged-during-capture"},
    }


def menu_scene(items, focused, *, layout="vertical"):
    observed = scene(menu=True)
    observed.action_menu = ActionMenu.model_validate(
        {
            "identity": identity(),
            "availability": "live",
            "live_text": "LIVE",
            "layout": layout,
            "items": [
                {"label": item, "language": "English" if "English" in item else None, "provider": None}
                for item in items
            ],
        }
    )
    observed.focus.label = items[focused]
    observed.focus.role = "play_live" if items[focused].startswith("Watch Live") else "unknown"
    observed.focus.identity.language = "English" if "English" in items[focused] else None
    return observed


class MenuDevice(Device):
    def __init__(self, items, index):
        super().__init__()
        self.menu, self.items, self.index, self.revision = True, items, index, 1
        self.unexpected = None

    def native_focus(self):
        return native(self.items[self.index], self.revision) if self.menu else None

    def validate_native(self, snapshot):
        if snapshot["validity"] != self.native_focus()["validity"]:
            raise ExecutorError("stale_navigation_focus", "Fixture changed before input")

    async def key(self, action):
        self.revision += 1
        if action in {"UP", "DOWN", "LEFT", "RIGHT"}:
            self.actions.append(action)
            self.index = max(
                0, min(len(self.items) - 1, self.index + (1 if action in {"DOWN", "RIGHT"} else -1))
            )
            if self.unexpected:
                self.items[self.index] = self.unexpected
            return
        await super().key(action)

    async def capture(self):
        frame = await super().capture()
        fields = json.loads(frame.image)
        fields.update(items=self.items, index=self.index)
        image = json.dumps(fields).encode()
        return replace(
            frame, image=image, sha256=hashlib.sha256(image).hexdigest(), native_focus=self.native_focus()
        )


class MenuVision(Vision):
    async def observe(self, frame):
        fields = json.loads(frame.image)
        if fields["menu"]:
            return menu_scene(fields["items"], fields["index"])
        result = await super().observe(frame)
        result.player.live_edge = None  # A LIVE badge alone does not prove playhead position.
        return result

    async def decide(self, *args, **kwargs):
        raise AssertionError("Observed menu navigation needs no actor call")


def prepared(tmp_path, *, device=None, vision=None, **config):
    service = Controller(settings(tmp_path, **config))
    engine = service.executor
    engine.device, engine.vision = device or Device(), vision or Vision()
    request = payload()
    report, _ = engine.store.submit(request)
    with engine.db.transaction() as db:
        row = dict(engine.store.get_row(db, report["token"]))
    return service, engine, request, row


async def play(engine, request, row):
    assert await engine.navigate_query(
        row["token"], request, request["allowed_viewing_options"][0], "Jets Lions", "UTC"
    )


@pytest.mark.parametrize(
    "items,index,expected",
    [
        (["Watch Live\nEnglish Broadcast"], 0, []),
        (["Resume", "Watch Live\nEnglish Broadcast"], 0, ["DOWN"]),
        (["More details", "Resume", "Rapid Recap", "Watch Live\nEnglish Broadcast"], 0, ["DOWN"] * 3),
        (
            [
                "Resume",
                "Watch Live\nEnglish Broadcast",
                "Rapid Recap",
                "More details",
                "Watch from beginning",
            ],
            4,
            ["UP"] * 3,
        ),
        (["Watch Live\nEnglish Broadcast", *[f"Visible option {i}" for i in range(16)]], 16, ["UP"] * 16),
    ],
)
async def test_watch_live_uses_observed_order_and_length_without_per_arrow_model_calls(
    tmp_path, items, index, expected
):
    service, engine, request, row = prepared(tmp_path, device=MenuDevice(items, index), vision=MenuVision())
    try:
        await play(engine, request, row)
        assert engine.device.actions == expected + ["SELECT"]
        assert engine.device.captures <= 4
        report = engine.store.report(row["token"])
        assert report["runtime"]["live_mode"] == "watch_live_selected"
        assert report["runtime"]["runtime_media_id"] == "fixture-media"
        assert report["runtime"]["bound_content_id"] == "fixture-game"
        assert report["runtime"]["live_edge"] == "unmeasured"
        PlaybackReport.model_validate(report)
    finally:
        await service.stop()


@pytest.mark.parametrize(
    "items",
    [
        ["Resume", "Rapid Recap"],  # Watch Live may be off-screen; no blind traversal.
        ["Watch Live", "Watch Live"],
        ["Watch Live\nEnglish", "Watch Live\nFrench"],  # No requested variant to disambiguate.
    ],
)
async def test_missing_duplicate_or_ambiguous_menu_target_requires_visual_navigation(items):
    frame = await Device().capture()
    request = payload()
    assert (
        MenuPlan.observe(menu_scene(items, 0), frame, request, request["allowed_viewing_options"][0], "UTC")
        is None
    )


async def test_variant_and_horizontal_order_are_observed_not_inferred():
    request = payload()
    option = {**request["allowed_viewing_options"][0], "language": "English"}
    frame = await Device().capture()
    items = ["Watch Live\nFrench", "Resume", "Watch Live\nEnglish"]
    observed = menu_scene(items, 1, layout="horizontal")
    observed.action_menu.items[0].language = "French"
    plan = MenuPlan.observe(observed, frame, request, option, "UTC")
    assert plan.step(native("Resume")) == ("RIGHT", "watch live english")


async def test_unexpected_label_after_one_step_stops_the_sequence_and_never_selects(tmp_path):
    device = MenuDevice(["Resume", "More details", "Watch Live"], 0)
    device.unexpected = "Rapid Recap"
    service, engine, request, row = prepared(tmp_path, device=device)
    try:
        frame = await device.capture()
        plan = MenuPlan.observe(
            menu_scene(device.items, 0), frame, request, request["allowed_viewing_options"][0], "UTC"
        )
        assert await engine.walk_menu(row["token"], frame, plan)
        assert device.actions == ["DOWN"]
    finally:
        await service.stop()


async def test_menu_native_focus_is_rechecked_inside_manual_input_gate(tmp_path):
    device = MenuDevice(["Resume", "Watch Live"], 0)
    service, engine, _, row = prepared(tmp_path, device=device)
    await engine.input_lock.acquire()
    task = asyncio.create_task(
        engine.input(
            row["token"],
            "DOWN",
            lambda: device.key("DOWN"),
            native=device.native_focus(),
            context_frame=await device.capture(),
        )
    )
    try:
        await asyncio.sleep(0)
        device.revision += 1
        engine.input_lock.release()
        with pytest.raises(ExecutorError, match="Fixture changed"):
            await task
        assert device.actions == []
    finally:
        if engine.input_lock.locked():
            engine.input_lock.release()
        await service.stop()


@pytest.mark.parametrize(
    "text,description",
    [
        ("g", None),
        ("a Alpha", "[Search Keyboard, Search] a Alpha"),
        ("Jets Lions", "[Search Suggestions] Jets Lions"),
        ("Resume", None),
        ("Rapid Recap", None),
        ("Watch with Multiview", None),
    ],
)
def test_native_keyboard_suggestion_and_other_actions_block_visual_event_activation(text, description):
    assert not activation_agrees(scene(), native(text, description=description))


async def test_stable_bound_session_refreshes_status_without_screenshots_or_llm(tmp_path):
    service, engine, request, row = prepared(tmp_path)
    try:
        await play(engine, request, row)
        captures, actions = engine.device.captures, list(engine.device.actions)
        first = engine.store.report(row["token"])
        await engine.monitor(row)
        report = engine.store.report(row["token"])
        assert engine.device.captures == captures and engine.device.actions == actions
        assert report["observation"]["verified"]
        assert report["observation"]["observed_at"] > first["observation"]["observed_at"]
        assert report["runtime"]["last_visual_at"] == first["runtime"]["last_visual_at"]
        assert (
            report["runtime"]["position_ms"] == first["runtime"]["position_ms"]
        )  # No position-progress shortcut.
    finally:
        await service.stop()


async def test_buffering_recovers_same_binding_without_launch_or_false_completion(tmp_path):
    service, engine, request, row = prepared(tmp_path)
    try:
        await play(engine, request, row)
        original, actions, captures = (
            engine.device.runtime,
            list(engine.device.actions),
            engine.device.captures,
        )

        async def buffering():
            sample = await original()
            sample["session"]["transport"] = "buffering"
            return sample

        engine.device.runtime = buffering
        await engine.monitor(row)
        report = engine.store.report(row["token"])
        assert not report["observation"]["verified"] and report["runtime"]["transport"] == "buffering"
        assert report["content_status"]["effective_state"] == "unknown"
        engine.device.runtime = original
        await engine.monitor(row)
        assert engine.store.report(row["token"])["observation"]["verified"]
        assert engine.device.actions == actions and engine.device.captures == captures
    finally:
        await service.stop()


@pytest.mark.parametrize("change", ["media", "session", "boot", "pause", "outage", "revision", "foreground"])
async def test_changed_runtime_withdraws_binding_and_requires_new_visual_live_evidence(tmp_path, change):
    service, engine, request, row = prepared(tmp_path)
    try:
        await play(engine, request, row)
        original, observer = engine.device.runtime, engine.vision.observe

        async def changed():
            sample = await original()
            if change == "media":
                sample["session"]["runtime_media_id"] = "other-media"
            if change == "session":
                sample["session"]["session_token"] = "other-session"
            if change == "boot":
                sample["boot_id"] = "other-boot"
            if change == "pause":
                sample["session"]["transport"] = "paused"
            if change == "outage":
                sample["source_health"] = "unavailable"
            if change == "revision":
                sample["identity_revision"] += 1
            if change == "foreground":
                sample["foreground"] = "another.app"
            return sample

        async def no_live_playhead(frame):
            observed = await observer(frame)
            observed.player.live_edge = None
            return observed

        engine.device.runtime, engine.vision.observe = changed, no_live_playhead
        actions, captures = list(engine.device.actions), engine.device.captures
        await engine.monitor(row)
        report = engine.store.report(row["token"])
        assert not report["observation"]["verified"]
        assert report["runtime"]["binding"] != "visually_associated"
        assert engine.device.captures > captures and engine.device.actions == actions
        assert report["content_status"]["effective_state"] == "unknown"
    finally:
        await service.stop()


async def test_periodic_visual_contradiction_overrides_unchanged_runtime_id(tmp_path):
    service, engine, request, row = prepared(tmp_path)
    try:
        await play(engine, request, row)
        observer = engine.vision.observe

        async def different_event(frame):
            observed = await observer(frame)
            observed.player.identity.teams = ["Cowboys", "Giants"]
            return observed

        engine.vision.observe = different_event
        engine.next_visual[row["token"]] = 0
        await engine.monitor(row)
        assert not engine.store.report(row["token"])["observation"]["verified"]
    finally:
        await service.stop()


async def test_unknown_visual_reading_has_bounded_original_identity_age(tmp_path):
    service, engine, request, row = prepared(tmp_path)
    try:
        await play(engine, request, row)

        async def unknown(frame):
            observed = scene(playing=True)
            observed.player.identity.kind = "unknown"
            observed.player.identity.teams = []
            observed.player.live_edge = None
            return observed

        engine.vision.observe = unknown
        engine.next_visual[row["token"]] = 0
        await engine.monitor(row)
        assert engine.store.report(row["token"])["observation"]["verified"]
        binding = engine.bindings[row["token"]]
        binding.sample = (
            replace(
                binding.sample[0],
                captured_at=datetime.now(UTC) - timedelta(seconds=engine.visual_max_age + 1),
            ),
            binding.sample[1],
        )
        await engine.monitor(row)
        assert not engine.store.report(row["token"])["observation"]["verified"]
    finally:
        await service.stop()


async def test_recovery_and_status_expiry_never_relabel_historical_evidence_as_current(tmp_path):
    service, engine, request, row = prepared(tmp_path)
    try:
        await play(engine, request, row)
        engine.store.recover()
        report = engine.store.report(row["token"])
        assert not report["observation"]["verified"]
        assert report["runtime"]["binding"] == "revalidation_required"
        with engine.db.transaction() as db:
            saved = json.loads(engine.store.get_row(db, row["token"])["report"])
            saved["runtime"]["valid_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            engine.store.write(db, row["token"], saved)
        expired = engine.store.report(row["token"])
        assert expired["runtime"]["source_health"] == "stale"
        assert expired["runtime"]["observed_at"] == report["runtime"]["observed_at"]
    finally:
        await service.stop()


async def test_media_position_never_proves_rendered_video_or_an_allowed_language():
    device = Device()
    device.playing = True
    first = await device.capture()
    second = replace(
        first,
        captured_at=first.captured_at + timedelta(seconds=2),
        sessions=[{**first.sessions[0], "position_ms": first.sessions[0]["position_ms"] + 2000}],
    )
    observed = scene(playing=True)
    observed.player.position_seconds = None
    assert not progression((first, observed), (second, observed))
    request = payload()
    option = {**request["allowed_viewing_options"][0], "language": "French"}
    observed.player.identity.language = "English"
    assert not playback_sample(observed, first, request, "UTC", option)
