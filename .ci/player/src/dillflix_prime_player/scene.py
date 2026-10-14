"""Preorder renderer validation. Test IDs identify types, not persistent nodes."""

import re


def artwork_key(value):
    if not isinstance(value, str):
        return None
    return re.sub(
        r"\.(?:png|jpg|jpeg|webp)$",
        "",
        value.split("?", 1)[0].split("._CLs", 1)[0],
        flags=re.I,
    )


def build_scene(record, capture_name=""):
    raw = record["data"]["result"]
    if not isinstance(raw, list) or not raw or len(raw) > 10000:
        raise ValueError("invalid renderer snapshot size")
    parents, children, end = {}, {}, {}

    def visit(i, depth=0):
        if depth > 256:
            raise ValueError("renderer depth exceeds limit")
        children[i] = []
        j = i + 1
        for label in raw[i]["childNodeIds"]:
            if j >= len(raw) or raw[j]["id"] != label:
                raise ValueError("renderer preorder mismatch")
            parents[j] = i
            children[i].append(j)
            j = visit(j, depth + 1)
        end[i] = j
        return j

    if visit(0) != len(raw):
        raise ValueError("unconsumed renderer nodes")
    root = raw[0]["boundingBox"]
    prefix = "s" + str(record["sequence"]) + "-n"

    def visible(n):
        b = n["boundingBox"]
        return (
            b["width"] > 0
            and b["height"] > 0
            and b["left"] < root["left"] + root["width"]
            and b["top"] < root["top"] + root["height"]
            and b["left"] + b["width"] > root["left"]
            and b["top"] + b["height"] > root["top"]
        )

    return {
        "nodes": [
            {
                "key": prefix + str(i),
                "sceneIndex": i,
                "parent": prefix + str(parents[i]) if i in parents else None,
                "subtreeEndExclusive": end[i],
                "raw": n,
                "intersectsViewport": visible(n),
            }
            for i, n in enumerate(raw)
        ]
    }
