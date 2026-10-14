"""Bounded manual input, independent of an injected Prime runtime."""
import shlex
import subprocess


def manual_input(adb, *, key=None, text=None):
    keys = {
        "up": 19,
        "down": 20,
        "left": 21,
        "right": 22,
        "select": 23,
        "back": 4,
        "home": 3,
        "menu": 82,
        "play_pause": 85,
        "backspace": 67,
        "wake": 224,
    }
    if key in keys and text is None:
        command = ["input", "keyevent", str(keys[key])]
    elif (
        key is None
        and isinstance(text, str)
        and 1 <= len(text) <= 200
        and all(32 <= ord(c) <= 126 for c in text)
        and "%s" not in text
    ):
        command = ["input", "text", text.replace(" ", "%s")]
    else:
        raise ValueError(
            "unsupported key or text; printable ASCII only, literal %s unsupported"
        )
    subprocess.run(
        adb + ["shell", shlex.join(command)],
        capture_output=True,
        check=True,
        timeout=8,
    )
