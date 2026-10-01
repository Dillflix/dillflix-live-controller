"""Use observed layout for direction and native input focus for each step.

Labels can identify an action or a row, never the sports event inside a card.
The final activation still needs a fresh, independent visual identity reading.
"""

import re
from dataclasses import dataclass

from .verification import BLOCKED, LIVE, identity_matches, route_matches


def label(value):
    return " ".join((value or "").split()).casefold()


def watch_live(value):
    # Only the observed English action contract, with optional variant text.
    # Do not let "Watch with Multiview", Resume or Rapid Recap satisfy it.
    return bool(re.match(r"^watch live(?:\s|$)", label(value)) and not BLOCKED.search(value or ""))


def input_focus(native):
    if not native or not native.get("usable"):
        return None
    channel = native.get("channels", {}).get("input", {})
    focus = channel.get("focus")
    return focus if channel.get("usable") and focus and focus.get("text") else None


def activation_agrees(scene, native):
    """A fresh native contradiction overrides a visual activation proposal."""
    focus = input_focus(native)
    if not focus:
        return True  # Visual-only validation remains available, explicitly separate.
    text, description = label(focus["text"]), label(focus.get("contentDescription"))
    keyboard = "search keyboard" in description or bool(re.fullmatch(r"[a-z0-9](?: [a-z]+)?", text))
    suggestion = "search suggestions" in description
    if (keyboard or suggestion) and scene.focus.role in {"event", "play_live"}:
        return False
    menu_action = bool(
        re.match(
            r"^(watch live|resume|rapid recap|watch with multiview|watch from beginning|more details)\b", text
        )
    )
    if menu_action:
        return bool(scene.focus.role == "play_live" and watch_live(text) and label(scene.focus.label) == text)
    if scene.focus.role == "play_live":
        return watch_live(text) and label(scene.focus.label) == text
    return True


def item_identity(menu, item):
    if not menu.identity:
        return None
    return menu.identity.model_copy(
        update={
            "provider": item.provider or menu.identity.provider,
            "language": item.language or menu.identity.language,
        }
    )


@dataclass(frozen=True)
class MenuPlan:
    labels: tuple[str, ...]
    target: int
    backward: str
    forward: str

    @classmethod
    def observe(cls, scene, frame, request, option, timezone):
        menu = scene.action_menu
        if (
            scene.blocker != "none"
            or not menu
            or menu.layout not in {"vertical", "horizontal"}
            or menu.availability != "live"
            or not menu.live_text
            or not LIVE.search(menu.live_text)
            or BLOCKED.search(menu.live_text)
            or not identity_matches(menu.identity, request["content_snapshot"], frame.captured_at, timezone)
        ):
            return None
        labels = tuple(label(item.label) for item in menu.items)
        if not labels or len(set(labels)) != len(labels):
            return None  # Duplicate names cannot locate one focused instance.
        eligible = [
            i
            for i, item in enumerate(menu.items)
            if watch_live(item.label) and route_matches(item_identity(menu, item), option)
        ]
        if len(eligible) != 1:
            return None
        backward, forward = ("UP", "DOWN") if menu.layout == "vertical" else ("LEFT", "RIGHT")
        return cls(labels, eligible[0], backward, forward)

    def step(self, native):
        focus = input_focus(native)
        current = label(focus["text"]) if focus else None
        if current not in self.labels:
            return None
        index = self.labels.index(current)
        if index == self.target:
            return ("SELECT", current)
        neighbor = index + (1 if index < self.target else -1)
        return (self.forward if index < self.target else self.backward, self.labels[neighbor])
