"""Observed card decoration, not an account subscription inventory."""

from urllib.parse import unquote


LOCKED = "BottomLeftCardGradientWithShoppingBag16x9.png"
UNLOCKED = {"FullGradient16x9.png", "BottomLeftCardGradient16x9.png"}


def is_locked(artwork):
    """Recognize complete card compositions; bare artwork has unknown access.

    A right-hand gradient/provider logo alone says nothing about the lock.
    Conflicting compositions (for example across discovery pages) stay unknown.
    """
    observations = set()
    for asset in artwork:
        if not isinstance(asset, str):
            continue
        asset = unquote(asset)
        if "._CLs|" not in asset:
            continue
        composition = asset.split("._CLs|", 1)[1].split("|", 2)
        if len(composition) != 3:
            continue
        overlays = {path.rsplit("/", 1)[-1] for path in composition[1].split(",")
                    if path.startswith("/G/bundle/")}
        if LOCKED in overlays:
            observations.add(True)
        elif overlays & UNLOCKED:
            observations.add(False)
    return observations.pop() if len(observations) == 1 else None
