"""Native tab discovery, row-major tiles, and page-scoped identity correlation."""

import hashlib
import re
from .scene import artwork_key, build_scene
from .errors import StaleResultError

CARDS = {
    "beard-supported-card-test-id",
    "standard-card-test-id",
    "small-card-test-id",
    "circular-card-test-id",
}
CHANNEL = re.compile(
    r"^top_nav_logo_button_(amzn1\.dv\.channel\.[A-Za-z0-9-]+):channel:(amzn1\.dv\.channel\.[A-Za-z0-9-]+)::\d+$"
)
# Route candidates are checked against the rendered destination on every call.
PRIMARY = {
    "adult-home": ("home", "home", "home"),
    "adult-movie": ("movies", "movie", "home"),
    "adult-tv": ("tv", "tv", "home"),
    "adult-sports": ("sports", "home", "Sports"),
}


def layout(record):
    d = record["data"]
    if (
        not d.get("success")
        or d.get("pageChanged")
        or d.get("pageAtRequest") != d.get("pageAtReceipt")
    ):
        raise StaleResultError("page changed or native inspection failed")
    nodes = build_scene(record)["nodes"]
    by = {n["key"]: n for n in nodes}

    def subtree(n):
        return nodes[n["sceneIndex"] : n["subtreeEndExclusive"]]

    def texts(n):
        return [x["raw"]["text"] for x in subtree(n) if x["raw"].get("text")]

    def ancestors(n):
        while n.get("parent"):
            n = by[n["parent"]]
            yield n

    tabs, rows, seen, groups = [], [], set(), {}
    rings = [
        n["raw"]["boundingBox"]
        for n in nodes
        if n["raw"]["id"]
        in ("matched-geometry-focus-ring", "container-list-focus-ring")
        and n["intersectsViewport"]
    ]
    for n in nodes:
        raw, nid = n["raw"], n["raw"]["id"]
        match = CHANNEL.fullmatch(nid)
        tab = None
        if nid.startswith("top_nav_text_button_"):
            suffix = nid.removeprefix("top_nav_text_button_")
            labels = [
                x["raw"]["text"]
                for x in subtree(n)
                if x["raw"]["id"] == "top_nav_text_label" and x["raw"].get("text")
            ]
            route = PRIMARY.get(suffix)
            tab = dict(
                id=route[0] if route else suffix.removeprefix("adult-"),
                title=labels[0] if len(labels) == 1 else None,
                title_hint=None,
                kind="primary",
                available=route is not None,
            )
        elif match and match[1] == match[2]:
            labels = texts(n)
            hints = set()
            if d["pageAtReceipt"].get("pageId") == "channel::" + match[1]:
                hints = {
                    m[1]
                    for x in nodes
                    for m in [
                        re.fullmatch(
                            r"Included with your (.+) subscription",
                            x["raw"].get("text") or "",
                        )
                    ]
                    if m and x["raw"]["id"] == "entitlement-label-text-test-id"
                }
            # Logo hints are display conveniences, never subscription or content evidence.
            sources = " ".join(x["raw"].get("source") or "" for x in subtree(n)).lower()
            logo = next(
                (
                    name
                    for marker, name in [
                        ("dazn", "DAZN"),
                        ("sportsnet", "Sportsnet+"),
                        ("tsn", "TSN"),
                    ]
                    if marker in sources
                ),
                None,
            )
            tab = dict(
                id=match[1],
                title=labels[0] if len(labels) == 1 else None,
                title_hint=next(iter(hints)) if len(hints) == 1 else logo,
                kind="channel",
                available=True,
            )
        if tab and tab["id"] not in seen:
            seen.add(tab["id"])
            tabs.append(dict(tab, position=len(tabs) + 1))
        if nid not in CARDS:
            continue
        anc = list(ancestors(n))
        if any(x["raw"]["id"] in CARDS for x in anc):
            continue
        carousel = next((x for x in anc if x["raw"]["id"] == "basic-carousel"), None)
        if carousel is None:
            continue
        if carousel["key"] not in groups:
            headings = [
                x["raw"]["text"]
                for x in subtree(carousel)
                if x["raw"]["id"] == "basic-carousel-header" and x["raw"].get("text")
            ]
            if not headings and carousel.get("parent"):
                headings = [
                    x["raw"]["text"]
                    for x in subtree(by[carousel["parent"]])
                    if x["raw"]["id"] == "container-title" and x["raw"].get("text")
                ]
            # Native instance IDs are used only within this browse operation.
            parents = list(ancestors(carousel))
            native = next(
                (a["raw"]["id"] for a in parents if a["raw"]["id"].startswith("V2=")),
                None,
            )
            if native is None:
                native = next(
                    (
                        a["raw"]["id"]
                        for a in parents
                        if a["raw"]["id"].startswith("NodeId - ")
                    ),
                    carousel["key"],
                )
            group = dict(
                key=hashlib.sha256(native.encode()).hexdigest()[:20],
                title=headings[0] if headings else "",
                tiles=[],
                position=len(rows) + 1,
            )
            groups[carousel["key"]] = group
            rows.append(group)
        group = groups[carousel["key"]]
        labels = texts(n)
        badge = next(
            (
                x
                for x in labels
                if x in ("LIVE", "UPCOMING", "REPLAY", "ENDED", "ON NOW", "UNAVAILABLE")
            ),
            None,
        )
        title = date = None
        bug = next(
            (x for x in subtree(n) if x["raw"]["id"] == "score_bug-test-id"), None
        )
        if bug:
            name = next(
                (
                    x
                    for x in subtree(bug)
                    if x["raw"]["id"] == "participant_name-test-id"
                ),
                None,
            )
            names = texts(by[name["parent"]]) if name and name.get("parent") else []
            if len(names) == 2:
                title = " vs. ".join(names)
        elif badge and labels and labels[0] == badge:
            title = labels[1] if len(labels) > 1 else None
            date = labels[2] if len(labels) > 2 else None
        elif labels:
            title = labels[0]
        assets = list(
            dict.fromkeys(
                x["raw"]["source"]
                for x in subtree(n)
                if x["raw"].get("source")
            )
        )
        group["tiles"].append(
            dict(
                collection=group["title"],
                position=len(group["tiles"]) + 1,
                title=title,
                date_label=date,
                availability=badge.lower() if badge else None,
                content_id=None,
                candidate_ids=(),
                identity_status="unresolved",
                labels=tuple(labels),
                artwork=tuple(assets),
                bounds=raw["boundingBox"],
                intersects_viewport=n["intersectsViewport"],
            )
        )
    return dict(page=d["pageAtReceipt"], tabs=tabs, rows=rows, rings=rings)


def route(tab):
    if tab["kind"] == "channel":
        return dict(pageType="channel", pageId=tab["id"])
    candidate = next((v for v in PRIMARY.values() if v[0] == tab["id"]), None)
    if candidate is None:
        raise ValueError("page route is not yet supported: " + tab["id"])
    return dict(pageType=candidate[1], pageId=candidate[2])


def fingerprint(tile):
    # Scores, countdowns and badges are dynamic. Ordinal distinguishes duplicate artwork.
    return tuple(dict.fromkeys(artwork_key(a) for a in tile["artwork"])) or (tile["title"], tile["date_label"])


def merge_rows(previous, current, max_rows):
    """Accept retained prefixes only; never guess an offset for a virtualized window."""
    current = current[:max_rows]
    for i, old in enumerate(previous):
        if (
            i >= len(current)
            or old["key"] != current[i]["key"]
            or old["title"] != current[i]["title"]
        ):
            raise StaleResultError("page row order changed during collection")
        new = current[i]
        overlap = min(len(old["tiles"]), len(new["tiles"]))
        if any(
            fingerprint(a) != fingerprint(b)
            for a, b in zip(old["tiles"][:overlap], new["tiles"][:overlap])
        ):
            raise StaleResultError("tile prefix shifted or reordered during collection")
        if len(new["tiles"]) < len(old["tiles"]):
            raise StaleResultError("native tile prefix was evicted during collection")
    # Replacing the snapshot deduplicates repeated observations without erasing repeated titles.
    return current


def artwork_ids(tile):
    return {
        m[1]
        for asset in tile["artwork"]
        for m in [
            re.search(r"/le-target-images-prod/(amzn1\.dv\.gti\.[A-Za-z0-9-]+)/", asset)
        ]
        if m
    }


def asset_matches(entry, tile):
    return (entry["asset"] and entry["asset"] in {artwork_key(a) for a in tile["artwork"]}) or entry[
        "content_id"
    ] in artwork_ids(tile)


def bind(rows, evidence, *, browse_id, page_id, started_ms, ended_ms):
    impressions = []
    for record in evidence:
        d = record.get("data", {})
        if (
            record.get("kind") != "page_tile_metadata"
            or d.get("browseId") != browse_id
            or d.get("page", {}).get("pageId") != page_id
        ):
            continue
        metadata = d.get("metadata", {})
        if metadata.get("lossless") is not True:
            continue
        payload = metadata.get("value", {})
        item, event = payload.get("ImpressionEntryItem", {}), payload.get(
            "ImpressionEntryEvent", {}
        )
        timestamp = event.get("eventTimestamp")
        if (
            type(timestamp) not in (int, float)
            or not started_ms <= timestamp <= ended_ms
        ):
            continue
        ref = re.fullmatch(r"(.+)_(\d+)_(\d+)", item.get("itemReftag", ""))
        slot = re.fullmatch(r"(\d+),(\d+)", item.get("itemSlotId", ""))
        cid = item.get("itemContentId")
        if (
            not ref
            or not slot
            or not isinstance(cid, str)
            or not re.fullmatch(r"amzn1\.dv\.gti\.[A-Za-z0-9-]+", cid)
        ):
            continue
        # The reftag's row index changes during Sports lazy loading. Its prefix,
        # slot row and tile ordinal remain consistent in the captured transitions.
        if int(ref[3]) != int(slot[1]):
            continue
        impressions.append(
            dict(
                group=(ref[1], int(slot[2])),
                ordinal=int(slot[1]),
                content_id=cid,
                asset=artwork_key(item.get("itemCreativeId")),
            )
        )
    for row in rows:
        groups = []
        for key in {e["group"] for e in impressions}:
            entries = [e for e in impressions if e["group"] == key]
            anchors, contradictions = set(), False
            for t in row["tiles"]:
                matches = [e for e in entries if asset_matches(e, t)]
                # Reused artwork is not a positional anchor.
                unique = all(
                    sum(bool(asset_matches(e, x)) for x in row["tiles"]) == 1
                    for e in matches
                )
                if matches and unique:
                    if any(e["ordinal"] != t["position"] for e in matches):
                        contradictions = True
                    elif len({e["content_id"] for e in matches}) == 1:
                        anchors.add((t["position"], matches[0]["content_id"]))
            if (
                not contradictions
                and len(anchors) >= 2
                and len({a[1] for a in anchors}) >= 2
            ):
                groups.append(key)
        for t in row["tiles"]:
            t["content_id"] = None
            t["identity_status"] = "unresolved"
            candidates = artwork_ids(t) | {
                e["content_id"] for e in impressions if asset_matches(e, t)
            }
            t["candidate_ids"] = tuple(sorted(candidates))
            matching = [
                e
                for e in impressions
                if len(groups) == 1
                and e["group"] == groups[0]
                and e["ordinal"] == t["position"]
                and asset_matches(e, t)
            ]
            ids = {e["content_id"] for e in matching}
            if len(ids) == 1:
                t["content_id"] = next(iter(ids))
                t["identity_status"] = "structural_slot_correlation"
            elif candidates:
                t["identity_status"] = (
                    "ambiguous" if len(candidates) > 1 else "artwork_candidate"
                )
