"""Resolve image sources without inferring event identity."""

from urllib.parse import urlsplit, urlunsplit


def image_url(value):
    """Accept an upstream HTTP(S) image URL without changing its identity/query."""
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return None
        _ = parsed.port
        return value
    except ValueError:
        return None


def matchup_thumbnail(artwork):
    """Use Teamarr's game-thumbs matchup identity, never infer teams or a host."""
    logo = image_url(artwork.get("matchup_logo_url"))
    if not logo:
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
        cover = urlsplit(image_url(artwork.get("cover_url")) or "")
        if (
            cover.scheme == source.scheme
            and cover.netloc == source.netloc
            and cover.path in {prefix + "/cover", prefix + "/cover.png"}
        ):
            source = cover
        return urlunsplit(source._replace(path=prefix + "/thumb.png"))
    except ValueError:
        return None


def playback_thumbnail(artwork):
    """Prefer a matchup thumbnail, then the provider's supplied event/coverage art."""
    # Tennis day/court coverage has a DAZN cover URL and no pair of competitors.
    # Keep that provider URL verbatim; it is not a game-thumbs cover endpoint.
    return matchup_thumbnail(artwork) or image_url(artwork.get("cover_url"))
