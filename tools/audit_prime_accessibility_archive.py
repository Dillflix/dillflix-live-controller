"""Inventory saved search/focus evidence; never connect to a TV or infer playback."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(root):
    directory = root / "design/sports-search-production-2026-09-25"
    observations = []
    seen = set()
    for path in sorted(directory.glob("*.json")):
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or not isinstance(value.get("semanticFocus"), dict):
            continue
        if value["id"] in seen:
            raise ValueError(f"Duplicate observation ID: {value['id']}")
        seen.add(value["id"])
        semantic = value["semanticFocus"]
        focus = semantic.get("focus") or {}
        image = path.with_suffix(".png")
        observations.append(
            {
                "source": str(path.relative_to(root)),
                "source_sha256": digest(path),
                "observation_id": value["id"],
                "prepared_image_sha256": value.get("hash"),
                "native_png_present": image.is_file(),
                "native_png_sha256": digest(image) if image.is_file() else None,
                "last_action": semantic.get("lastInput", {}).get("action"),
                "label": focus.get("text"),
                "content_description": focus.get("contentDescription"),
                "usable_at_capture": semantic.get("usable"),
                "reason_at_capture": semantic.get("reason"),
                "channel_labels": {
                    name: (channel.get("focus") or {}).get("text")
                    for name, channel in semantic.get("channels", {}).items()
                },
                "association": semantic.get("association"),
                "capture_association": semantic.get("captureAssociation", {}).get("status"),
                "window_kind": (semantic.get("windowEvidence") or {}).get("kind"),
            }
        )
    if not observations:
        raise ValueError("No saved direct sports-search observations found")
    review_path = directory / "focus-review.json"
    reviews = json.loads(review_path.read_text())
    by_id = {row["observation_id"]: row for row in observations}
    review_checks = []
    for review in reviews:
        observation = by_id.get(review["observationId"])
        native = review["nativeFocus"]
        review_checks.append(
            {
                "fixture_id": review["fixtureId"],
                "observation_id": review["observationId"],
                "native_focus_reported_by_review": native,
                "playback_attempted_reported_by_review": review["playbackAttempted"],
                "saved_observation_present": observation is not None,
                "saved_native_png_present": (directory / review["screenshot"]).is_file(),
                "native_focus_agrees_with_saved_observation": None
                if observation is None
                else (
                    (observation["label"], observation["usable_at_capture"], observation["reason_at_capture"])
                    == (native["text"], native["usable"], native["reason"])
                ),
                "native_png_digest_agrees_with_review": None
                if observation is None or not observation["native_png_present"]
                else (observation["native_png_sha256"] == review.get("nativePngSha256")),
            }
        )
    return {
        "schema": "prime-accessibility-search-audit-v1",
        "scope": "Direct saved JSON observations in design/sports-search-production-2026-09-25 only",
        "limitations": [
            "Saved development observations; not new device capture or model evaluation.",
            "Usability is the original collector verdict at capture, not a new reliability measurement.",
            "Not the complete journals referenced by the report; do not interpret counts as coverage rates.",
            "Sports probes explicitly did not activate content or attempt playback.",
            "Review-only records are distinguished from available observation/image pairs.",
        ],
        "summary": {
            "direct_observations": len(observations),
            "native_pngs_present": sum(row["native_png_present"] for row in observations),
            "usable_at_capture": sum(row["usable_at_capture"] is True for row in observations),
            "reasons_at_capture": dict(
                sorted(Counter(row["reason_at_capture"] for row in observations).items())
            ),
            "last_actions": dict(sorted(Counter(row["last_action"] for row in observations).items())),
            "usable_labels_at_capture": dict(
                sorted(
                    Counter(row["label"] for row in observations if row["usable_at_capture"] is True).items()
                )
            ),
            "review_entries": len(reviews),
            "review_entries_with_saved_observation": sum(
                row["saved_observation_present"] for row in review_checks
            ),
        },
        "review_source": str(review_path.relative_to(root)),
        "review_source_sha256": digest(review_path),
        "review_checks": review_checks,
        "observations": observations,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive_root", type=Path, help="Extracted agent-control directory")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.archive_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(result["summary"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
