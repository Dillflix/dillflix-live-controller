# Plex artwork

The optional **Settings → Plex artwork** integration updates an existing Plex
video library item's poster and background from accepted controller playback.
It is disabled by default. It adds no separate service, now-playing HTTP polling,
player status checks, scans, analysis, or media-metadata editing operations.

## Configure

1. Deploy the controller with your normal Compose files. Keep the
   existing `.env`, Prime Player socket configuration and persistent volume.
2. Open **Settings → Plex artwork**. Enter the Plex server URL reachable from
   the controller container, a Plex token with artwork-edit permission, and the
   numeric item ID (for example `8`) or the item's Plex link.
3. Use **Test Plex connection** to verify read access and the target title, then
   **Save Plex settings**. This test performs no artwork writes and does not
   prove edit permission.
4. Use **Use current Plex artwork as defaults**, or upload both default images.
   PNG, JPEG and static WebP images are limited to 8 MiB and 32 megapixels each.
   Captured defaults are saved image bytes, independent of subsequent Plex edits.
5. With real Prime Player playback configured, enable **Automatic artwork
   updates** and save. Observe the first controlled change, the two per-image
   previews and status before leaving the integration unattended.

Do not use `localhost` for a Plex server outside the controller container.
A LAN address such as `http://PLEX_HOST:32400` works when reachable from that
container. Existing nginx authentication protects the settings and previews.

## Behavior

- The poster prefers the current event's game-thumbs matchup thumbnail,
  retaining its URL/style and landscape proportions. Without a matchup, it uses
  Teamarr's supplied `artwork.cover_url` unchanged. DAZN tennis day/court coverage
  supplies a landscape provider image here; it does not define two competitors.
  Provider URLs are never rewritten into game-thumbs paths. Images are not cropped.
- The background uses its saved default until a distinct upstream background
  source is implemented. The two slots are tracked independently.
- A desired/queued event does not replace the currently verified event's art.
- A new accepted positive observation renews the fallback deadline. Re-reading
  an old sample does not. Defaults are due **five minutes after the last fresh
  accepted confirmation**, without an extra grace period after evidence expiry.
- Brief lost verification, pause, manual handoff, or completion holds the current
  artwork until that deadline. Automation pause alone does not invalidate
  continuing verified playback.
- A new event without an image uses the default poster. A failed image download
  for a new event also uses its default and retries; a failed refresh for the
  same event retains that event's last good image.
- Content changes, changed source URLs, changed defaults and explicit resync
  trigger work. A source silently changing bytes behind an unchanged URL needs
  **Resync artwork now**; there is no image polling loop.
- Demo/simulator mode cannot write Plex artwork, including fallback images.
- Disabling prevents new writes and leaves current images in place. A request
  already transmitted can still complete. **Restore defaults and disable**
  explicitly disables automation and queues one application of both defaults.

The worker uses committed desired state and deadline/retry wake-ups. Ordinary
controller restart retains the existing deadline and resolves uncertain writes
before proceeding. A database **restore** suspends the integration and discards
historical pending jobs; review the restored settings before enabling it again.

For inspection, `GET /api/v1/devices/living-room/now-playing` returns the accepted
playback and selected `event.thumbnail_url`. This is a read-only diagnostic view
of the same internal projection; the artwork worker never polls it. See the
[endpoint contract](now-playing.md). A 0.15.0 deployment returned 404 because that
route was removed; it is restored in 0.15.1.

## Media protection and verification

The only outbound writes are raw image POSTs to the configured single item's
`/library/metadata/{id}/posters` and `/library/metadata/{id}/arts`, matching
[Python PlexAPI's upload methods](https://python-plexapi.readthedocs.io/en/latest/_modules/plexapi/mixins/resources.html).
Current-image reads still use the `/thumb/{timestamp}` and `/art/{timestamp}`
URLs from metadata. The client has no
scan, refresh, analyze, generic metadata editor, or Plex-database write method.

Versions 0.15.0–0.15.1 used singular upload paths that can return HTTP 404.
Upgrade to 0.15.2, then choose **Retry artwork update** in Settings → Plex artwork.
Restarting alone preserves the suspension. Retry first verifies the saved attempt
and protected metadata before deciding whether an upload is needed. HTTP errors
now include the method and relative request path, distinguishing failed uploads
from missing metadata or image reads without exposing the token or server body.

Every change compares protected metadata before/after the request: item duration,
identity, title/summary and other descriptive fields, Media/Part/Stream attributes,
bitrate, codecs, dimensions, media paths and stream properties. Transient stream
selection/decision flags are excluded. Unexpected changes suspend further writes;
the integration never attempts to repair those properties by writing them.

Images are read back and compared, tolerating small resizing/JPEG differences.
An acknowledged upload alone does not count as verified. Plex may update artwork
bookkeeping and cache timestamps. The integration cannot guarantee unchanged
internal SQL columns or force immediate artwork refresh in every Plex client.

Before production acceptance, retain the XML from `GET /library/metadata/8`
(or your configured item) before and after a controlled event/default transition.
Confirm that technical fields are unchanged and that the intended images appear.
For exact database-column comparison, take read-only snapshots of the relevant
metadata/media/part/stream rows. No live Plex server was available during development.

## Failures and recovery

Network/image-source failures retry with backoff from 5 seconds to 5 minutes.
Invalid credentials, changed server/item identity, unsupported endpoints and
protected-metadata drift suspend delivery. Correct the issue and use **Retry
artwork update**, or save corrected settings.

A timeout after sending an image has an uncertain outcome. The attempt, source
image and original protected metadata are persisted. The worker reads Plex back
before retrying. Poster and background writes are not atomic; successful slots
are not uploaded again just because the other slot failed.

Plex does not expose an artwork generation/CAS fence. Queued obsolete work is
suppressed, but an already-transmitted old upload can temporarily take effect.
Serial writes, generation checks and read-back reconciliation converge to the
current desired images; they do not promise exactly-once remote writes.

**Activity**, the settings panel, and **Export diagnostics** show sanitized Plex
state, successful slots, retry deadlines and errors. Tokens and image-source URLs
are omitted from this diagnostic section.

## Persistence and upgrades

Version 0.15.0 adds schema 9. Back up the controller database before deployment;
older controller releases reject this newer schema. Preserve a pre-upgrade backup
if you need to roll back to the earlier controller version.

Connection/binding settings, default-image bytes, current applied images and
pending work live in the controller SQLite database. Images are content-addressed;
unreferenced versions are pruned. Credentials live separately in mode-0600 files
under `<controller-database>.plex-secrets/` in the same persistent data volume.

An ordinary configuration export still contains planner preferences only. SQLite
backups include defaults and integration configuration but not credential files.
A full volume backup includes both. After restoring on another host, re-enter the
token if its credential file is absent. Tokens are write-only in the UI, never
stored in browser localStorage, and excluded from command receipts and diagnostics.

## Implementation boundary

The controller's before-commit integration hook reconciles actual write
transactions, covering device, lifecycle and catalog changes uniformly. This
avoids missing log-free renewals or completion that changes content status without
calling `save_device()`. It does no network I/O, uses the caller's transaction, and
sends a thread-safe worker notification only after commit when desired state or a
deadline changes. A savepoint isolates projection failure from the playback
transaction. The existing player and Teamarr polling policies are unchanged.
