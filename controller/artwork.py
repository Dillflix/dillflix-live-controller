"""Resolve image sources without inferring event identity."""

from urllib.parse import urlsplit, urlunsplit


def matchup_thumbnail(artwork):
    """Use Teamarr's game-thumbs matchup identity, never infer teams or a host."""
    logo = artwork.get("matchup_logo_url")
    if not isinstance(logo, str):
        return None
    try:
        source = urlsplit(logo)
        if source.scheme not in {"http", "https"} or not source.netloc:
            return None
        prefix, _, filename = source.path.rpartition("/")
        if filename not in {"logo", "logo.png"}:
            return None
        # The companion cover has the event artwork style, while the logo
        # has the transparent-logo style. Only reuse a cover of this matchup.
        cover = urlsplit(artwork.get("cover_url") or "")
        if (
            cover.scheme == source.scheme
            and cover.netloc == source.netloc
            and cover.path in {prefix + "/cover", prefix + "/cover.png"}
        ):
            source = cover
        return urlunsplit(source._replace(path=prefix + "/thumb.png"))
    except ValueError:
        return None


