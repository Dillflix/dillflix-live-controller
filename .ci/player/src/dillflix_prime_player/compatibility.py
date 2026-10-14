"""Evidence reports and offline discovery. Discovery never promotes an adapter."""

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile

OPERATION_CAPABILITIES = {
    "search-data": "search",
    "search": "renderer_search",
    "watch-search": "renderer_search",
    "search-results": "renderer_search",
    "play-content": "play",
    "play-live": "play",
    "resolve-content": "resolve",
    "resolve-live": "resolve",
    "inspect-native": "native_inspection",
    "inspect-selection": "native_inspection",
    "inspect-player": "native_inspection",
    "playback-status": "core",
    "watch-page": "core",
    "navigate": "javascript_navigation",
    "back": "javascript_navigation",
    "stop-playback": "javascript_navigation",
    "focus": "javascript_focus",
    "snapshot": "javascript_focus",
    "inspect-state": "core",
}


def report(contexts, owner, engine, *, degraded=False):
    """Never combine module contracts from different JavaScript contexts."""
    selected = contexts.get(owner) if owner else None
    # A manifest in a background VM supplies resource evidence, not app capabilities.
    evidence = selected or next(
        (s for s in contexts.values() if s.get("runtimeVersion")), {}
    )
    result = deepcopy(evidence)
    result.setdefault("schema", "prime-compatibility/1")
    result["ownerContext"] = owner
    result["mode"] = "degraded" if degraded else "observing"
    result.setdefault("layers", {})["engine"] = {
        "available": bool(engine),
        "reasons": [] if engine else ["engine_not_verified"],
        "evidence": deepcopy(engine),
    }
    caps = result.setdefault("capabilities", {})
    if not selected or degraded:
        for name in set(OPERATION_CAPABILITIES.values()) | {"playback_status"}:
            caps[name] = {
                "available": False,
                "reasons": ["owning_dispatcher_not_validated"],
            }
    elif any(x.get("available") for x in caps.values()):
        result["mode"] = (
            "partial" if any(not x.get("available") for x in caps.values()) else "ready"
        )
    for name in ("pages", "discover"):
        required = ("native_inspection", "javascript_navigation")
        missing = [
            dependency
            for dependency in required
            if not caps.get(dependency, {}).get("available")
        ]
        caps[name] = {
            "available": not missing,
            "reasons": ["requires_" + x for x in missing],
        }
    result["refreshPolicy"] = (
        "observe_and_gate; never restart or reload Prime automatically"
    )
    result["promotionPolicy"] = (
        "reviewed adapter release; static discovery cannot approve semantics"
    )
    return result


def archive_evidence(value, output):
    """Archive controller-produced identity metadata; no raw account config or code."""
    output = Path(output).expanduser().resolve()
    payload = json.dumps(value, indent=2, allow_nan=False).encode()
    if len(payload) > 2_200_000:
        raise ValueError("compatibility evidence exceeds archive limit")
    with output.open("xb") as stream, tarfile.open(
        fileobj=stream, mode="w:gz"
    ) as archive:
        for name, data in [
            ("compatibility.json", payload),
            (
                "README.txt",
                b"Read-only identities and module hints. Resources are advertised, not necessarily loaded. No source bytes, signed URLs or credentials are included. Candidate discovery does not authorize execution.\n",
            ),
        ]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(data))
    return output


def analyze_resources(paths):
    """Bounded static hints from caller-supplied decoded JavaScript; no eval/import."""
    if len(paths) > 32:
        raise ValueError("at most 32 resources per analysis")
    hints = (
        "getFocusedTestID",
        "getCurrentPage",
        "waitWithTimeout",
        "initialQuery",
        "playbackMetadata",
        "isUserSignedIn",
        "getState",
        "deepLinkTitleTransform",
    )
    results = []
    for filename in paths:
        path = Path(filename)
        with path.open("rb") as stream:
            data = stream.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            raise ValueError(f"resource exceeds 16 MiB: {path.name}")
        item = {
            "name": path.name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "semanticStatus": "unvalidated",
            "enabledCapabilities": [],
        }
        if path.suffix not in (".js", ".mjs", ".cjs"):
            item["reason"] = "binary_or_encoded_resource_requires_reviewed_decoder"
        else:
            source = data.decode("utf-8", errors="strict")
            item["hints"] = [
                {
                    "name": hint,
                    "offsets": [
                        m.start() for m in re.finditer(re.escape(hint), source)
                    ][:32],
                }
                for hint in hints
                if hint in source
            ]
            item["possibleModuleIds"] = re.findall(
                r"(?:^|[,{}])\s*(\d+)\s*:\s*(?:function\b|\([^)]{0,100}\)\s*=>)", source
            )[:512]
            item["numericCallDependencies"] = sorted(
                set(re.findall(r"\b\w+\(\s*(\d{1,9})\s*\)", source))
            )[:512]
            item["limits"] = (
                "Lexical hints only; dependency candidates may be false positives. Verify factory boundaries, export paths and dependency relationships with static review plus bounded owning-thread reads."
            )
        results.append(item)
    return {
        "schema": "prime-adapter-candidates/1",
        "resources": results,
        "promotion": "requires fingerprint-matched adapter, contract tests and behavioral evidence",
    }
