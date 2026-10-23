"""User-facing launch progress from retained executor and health evidence."""

PHASES = {
    "accepted": "Preparing playback",
    "searching": "Finding this event in Prime",
    "matching": "Selecting the event feed",
    "broadcasts": "Checking available broadcasts",
    "launching": "Opening the live feed",
    "verifying": "Verifying live playback",
    "network_pending": "Preparing a network change",
    "network_retiring_prime": "Closing Prime for the network change",
    "network_changing_network": "Connecting to the selected location",
    "network_reconciling_network": "Verifying the network connection",
    "network_starting_prime": "Starting Prime on the connected network",
    "network_settling_network": "Waiting for the network change to finish",
    "network_recovering": "Recovering Prime and its network connection",
}
STARTUP = {
    "device_wake": "Checking that the TV is awake",
    "frida_handshake": "Connecting to device control",
    "spawn_gating": "Preparing Prime startup",
    "retiring_prime": "Closing the previous Prime session",
    "prime_foreground": "Bringing Prime to the foreground",
    "runtime_readiness": "Waiting for Prime initialization",
    "bootstrap_complete": "Checking Prime readiness",
}


def launch_progress(report, network):
    operation = report.get("operation") or {}
    phase = operation.get("phase", "accepted")
    title = PHASES.get(phase, "Preparing playback")
    target = (network or {}).get("requested_target") or (network or {}).get("target_id")
    detail = None
    startup = (network or {}).get("startup") or {}
    if phase.startswith("network_"):
        if phase in {"network_starting_prime", "network_recovering"} and startup:
            detail = STARTUP.get(startup.get("phase"), "Initializing Prime")
        if target:
            title += " · " + target
    return {"phase": phase, "title": title, "detail": detail,
            "started_at": operation.get("phase_started_at") or operation.get("created_at"),
            "updated_at": operation.get("updated_at"),
            "startup_elapsed_seconds": startup.get("elapsed_seconds") if detail else None}
