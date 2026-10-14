from datetime import datetime, timezone
from .associations import analyze
from .models import BrowseResults, SearchTile
from .tile_access import is_locked


def search_results(rows, query, session_id, search_id, generation):
    audit = analyze(rows, query)
    scenes = [
        s
        for s in audit["scenes"]
        if s["searchId"] == search_id and s["generation"] == generation
    ]
    if not scenes:
        raise ValueError("no matching valid search scene")
    scene = scenes[-1]
    tiles = []
    for gi, g in enumerate(scene["groups"]):
        for t in g["tiles"]:
            structural = t["candidateIds"]
            art = t["artworkCandidateIds"]
            candidates = structural or art
            status = (
                "structure_slot_artwork_correlated"
                if len(structural) == 1
                else (
                    "artwork_correlated"
                    if len(art) == 1
                    else "ambiguous" if art else "unresolved"
                )
            )
            # Only the stronger, multi-anchor join populates content_id. This is
            # an explicit correlation, never silently labelled action-verified.
            content = structural[0] if len(structural) == 1 else None
            tiles.append(
                SearchTile(
                    handle=f'{session_id}:{search_id}:{generation}:{gi}:{t["ordinal"]}',
                    collection=" / ".join(g["headings"]) or f"Collection {gi+1}",
                    position=t["ordinal"],
                    title=t["title"],
                    date_label=t["dateLabel"],
                    availability=(
                        t["availability"].lower() if t["availability"] else None
                    ),
                    content_id=content,
                    candidate_ids=tuple(candidates),
                    identity_status=status,
                    labels=tuple(t["labels"]),
                    artwork=tuple(t["artwork"]),
                    is_locked=is_locked(t["artwork"]),
                    bounds=t["bounds"],
                    intersects_viewport=t["intersectsViewport"],
                )
            )
    return BrowseResults(
        query=query,
        session_id=session_id,
        search_id=search_id,
        generation=generation,
        observed_at=datetime.now(timezone.utc).isoformat(),
        tiles=tuple(tiles),
        warnings=(
            "Loaded tiles only; no automatic scrolling or selection.",
            "Correlations are not action-verified; missing labels and IDs remain null.",
            "Dates are raw displayed labels; availability badges do not establish playability.",
        ),
    )
