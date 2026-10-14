"""Read-only capture analysis: structural slot correlation is not action proof."""

import argparse, collections, json, pathlib, re, tarfile
from .scene import artwork_key
from .scene import build_scene

CARD_IDS = {"small-card-test-id", "standard-card-test-id"}


def records(value):
    if isinstance(value, dict):
        if value.get("schema") == "prime-runtime-experiment/1":
            yield value
        else:
            for v in value.values():
                yield from records(v)
    elif isinstance(value, list):
        for v in value:
            yield from records(v)


def load(path):
    path = pathlib.Path(path)
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as t:
            names = [
                n for n in t.getnames() if n.endswith("/01-control-trace/probe.jsonl")
            ]
            if len(names) != 1:
                raise ValueError("expected one control-trace probe.jsonl")
            lines = t.extractfile(names[0]).read().decode().splitlines()
    else:
        lines = path.read_text().splitlines()
    return [r for line in lines for r in records(json.loads(line))]


def layout(record):
    g = build_scene(record, "capture")
    ns = g["nodes"]
    bykey = {n["key"]: n for n in ns}
    groups = {}
    for n in ns:
        if n["raw"]["id"] not in CARD_IDS:
            continue
        cur = n
        anc = []
        while cur.get("parent"):
            cur = bykey[cur["parent"]]
            anc.append(cur)
        container = next(
            (
                x
                for x in anc
                if x["raw"]["id"] in ("basic-carousel", "card-grid-column-list")
            ),
            None,
        )
        if container is None:
            continue
        group = groups.setdefault(
            container["sceneIndex"], {"container": container, "tiles": []}
        )
        raw = [x["raw"] for x in ns[n["sceneIndex"] : n["subtreeEndExclusive"]]]
        row = next((x for x in anc if x["raw"]["id"] == "card-grid-row"), None)
        group["tiles"].append(
            {
                "sceneIndex": n["sceneIndex"],
                "rowNode": row["sceneIndex"] if row else None,
                "labels": [x["text"] for x in raw if x.get("text")],
                "artwork": list(dict.fromkeys(x["source"] for x in raw if x.get("source"))),
                "assets": list(
                    dict.fromkeys(
                        artwork_key(x["source"]) for x in raw if x.get("source")
                    )
                ),
                "bounds": n["raw"]["boundingBox"],
                "intersectsViewport": n["intersectsViewport"],
            }
        )
    out = []
    for group in groups.values():
        c = group["container"]
        parent = bykey[c["parent"]]
        headings = [
            n["raw"]["text"]
            for n in ns[parent["sceneIndex"] : parent["subtreeEndExclusive"]]
            if n["raw"]["id"] == "container-title" and n["raw"].get("text")
        ]
        group.update(
            containerIndex=c["sceneIndex"],
            containerType=c["raw"]["id"],
            headings=headings,
        )
        rows = []
        counts = collections.Counter()
        for i, t in enumerate(group["tiles"], 1):
            row = t.pop("rowNode")
            if row is not None:
                if row not in rows:
                    rows.append(row)
                counts[row] += 1
                t["slot"] = [counts[row], rows.index(row) + 1]
            else:
                t["slot"] = None
            t["ordinal"] = i
        del group["container"]
        out.append(group)
    return out


def analyze(rows, query):
    scenes = []
    for r in rows:
        d = r.get("data", {})
        if (
            r["kind"] != "native_query"
            or d.get("method") != "inspectPage"
            or not d.get("success")
            or d.get("pageChanged")
            or d.get("pageAtRequest", {}).get("pageType") != "RUST_SEARCH"
        ):
            continue
        # Scope using the latest watch, then only prior matching impressions. Never join future search runs.
        eligible = [
            x
            for x in rows
            if x["sequence"] < r["sequence"]
            and x["kind"] == "search_tile_metadata"
            and x["data"].get("query") == query
        ]
        watches = [
            x
            for x in rows
            if x["sequence"] < r["sequence"] and x["kind"] == "search_watch_started"
        ]
        watch = watches[-1]["data"] if watches else None
        if watch and watch.get("query") != query:
            continue
        search_id = (
            watch.get("searchId")
            if watch
            else (eligible[-1]["data"].get("searchId") if eligible else None)
        )
        eligible = [x for x in eligible if x["data"].get("searchId") == search_id]
        loads = [
            x
            for x in rows
            if x["sequence"] < r["sequence"]
            and x["kind"] == "search_query_evidence"
            and x["data"].get("searchId") == search_id
        ]
        generation = (
            loads[-1]["data"].get("generation", 0)
            if loads
            else max((x["data"].get("generation", 0) for x in eligible), default=0)
        )
        impressions = []
        for x in eligible:
            z = x["data"]
            item = z.get("metadata", {}).get("value", {}).get("ImpressionEntryItem", {})
            if z.get("generation", 0) != generation:
                continue
            m = re.fullmatch(r"(.+)_(\d+)_(\d+)", item.get("itemReftag", ""))
            slot = re.fullmatch(r"(\d+),(\d+)", item.get("itemSlotId", ""))
            if not m or not slot or not item.get("itemContentId"):
                continue
            impressions.append(
                {
                    "sequence": x["sequence"],
                    "group": m[1] + "_" + m[2],
                    "collectionOrdinal": int(m[2]),
                    "ordinal": int(m[3]),
                    "slot": [int(slot[1]), int(slot[2])],
                    "id": item["itemContentId"],
                    "asset": artwork_key(item.get("itemCreativeId")),
                    "refMarker": item["itemReftag"],
                }
            )
        groups = layout(r)
        for group in groups:
            plausible = []
            for key in sorted({e["group"] for e in impressions}):
                es = [e for e in impressions if e["group"] == key]
                anchors = []
                contradiction = False
                for tile in group["tiles"]:
                    matches = [
                        e for e in es if e["asset"] and e["asset"] in tile["assets"]
                    ]
                    # Unique artwork is an anchor only; require slot AND ordinal agreement.
                    if len({e["id"] for e in matches}) == 1 and matches:
                        for e in matches:
                            slot_ok = (
                                e["slot"] == tile["slot"]
                                if tile["slot"]
                                else e["slot"]
                                == [tile["ordinal"], e["collectionOrdinal"]]
                            )
                            if e["ordinal"] != tile["ordinal"] or not slot_ok:
                                contradiction = True
                        anchors.append(
                            {
                                "tileOrdinal": tile["ordinal"],
                                "id": matches[0]["id"],
                                "sequence": matches[0]["sequence"],
                            }
                        )
                if not contradiction and len({a["id"] for a in anchors}) >= 2:
                    plausible.append((key, anchors))
            group["anchorGroups"] = [{"group": k, "anchors": a} for k, a in plausible]
            for tile in group["tiles"]:
                art = [
                    e
                    for e in impressions
                    if e["asset"] and e["asset"] in tile["assets"]
                ]
                tile["artworkCandidateIds"] = sorted({e["id"] for e in art})
                matched = []
                if len(plausible) == 1:
                    key = plausible[0][0]
                    matched = [
                        e
                        for e in impressions
                        if e["group"] == key
                        and e["ordinal"] == tile["ordinal"]
                        and e["asset"] in tile["assets"]
                        and (
                            e["slot"] == tile["slot"]
                            if tile["slot"]
                            else e["slot"] == [tile["ordinal"], e["collectionOrdinal"]]
                        )
                    ]
                tile["candidateIds"] = sorted({e["id"] for e in matched})
                tile["identityStatus"] = (
                    "structure_slot_artwork_correlated"
                    if len(tile["candidateIds"]) == 1
                    else "unresolved"
                )
                tile["contentId"] = None
                tile["evidence"] = matched
                tile["title"] = None
                tile["dateLabel"] = None
                tile["availability"] = None
                if (
                    group["containerType"] == "basic-carousel"
                    and tile["labels"]
                    and tile["labels"][0] in ("LIVE", "REPLAY", "ENDED", "UPCOMING", "ON NOW", "UNAVAILABLE")
                ):
                    tile["availability"] = tile["labels"][0]
                    tile["title"] = (
                        tile["labels"][1] if len(tile["labels"]) > 1 else None
                    )
                    tile["dateLabel"] = (
                        tile["labels"][2] if len(tile["labels"]) > 2 else None
                    )
                tile["labelStatus"] = (
                    "renderer_text" if tile["title"] else "not_observed"
                )
        scenes.append(
            {
                "sequence": r["sequence"],
                "searchId": search_id,
                "generation": generation,
                "groups": groups,
            }
        )
    return {
        "schema": "prime-structural-search-associations/1",
        "query": query,
        "scenes": scenes,
        "limits": [
            "Slot and artwork correlation is not an observed action binding; contentId remains null.",
            "Two distinct artwork anchors required per container; conflicting anchors reject the group.",
            "Loaded renderer subset only; missing/offscreen impression IDs and absent labels remain null.",
            "Search instance and generation scoped, but individual query membership is not asserted.",
            "Slot layout semantics are inferred from this runtime and require independent validation.",
        ],
    }
