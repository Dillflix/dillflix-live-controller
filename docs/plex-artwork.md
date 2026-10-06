# Plex title and artwork

The optional **Settings → Plex title and artwork** integration updates an existing Plex
video library item's title, poster and background from accepted controller playback.
It is disabled by default. It adds no separate service, now-playing HTTP polling,
player status checks, scans, analysis, or media-property editing operations.

## Configure

1. Deploy the controller with your normal Compose files. Keep the
   existing `.env`, Prime Player socket configuration and persistent volume.
2. Open **Settings → Plex title and artwork**. Enter the Plex server URL reachable from
   the controller container, a Plex token with metadata-edit permission, and the
   numeric item ID (for example `8`) or the item's Plex link.
3. Use **Test Plex connection** to verify read access and the target title, then
   **Save Plex settings**. This test performs no writes and does not
   prove edit permission.
4. Use **Use current Plex artwork as defaults**, or upload both default images.
   PNG, JPEG and static WebP images are limited to 8 MiB and 32 megapixels each.
   Captured defaults are saved image bytes, independent of subsequent Plex edits.
5. With real Prime Player playback configured, enable **Automatic title and artwork
   updates** and save. Observe the first controlled change, verified title, image
   previews and status before leaving the integration unattended.

Do not use `localhost` for a Plex server outside the controller container.
A LAN address such as `http://PLEX_HOST:32400` works when reachable from that
container. Existing nginx authentication protects the settings and previews.

## Behavior

- The title follows the currently verified event, for example **Beijing Open:
  Day 7**, including events without an image. The default title is **Dillflix Live**.
  Requested and last verified titles are shown separately in settings and diagnostics.
- Upgrading an enabled 0.15.x installation schedules the current title on startup;
  no new playback request or settings change is required. Version 0.16.1 also
  clears suspensions caused solely by the retired media-property checks. Other
  suspensions require **Retry Plex update**. The database remains schema 9.
- The poster prefers the current event's game-thumbs matchup thumbnail,
  retaining its URL/style and landscape proportions. Without a matchup, it uses
  Teamarr's supplied `artwork.cover_url` unchanged. DAZN tennis day/court coverage
  supplies a landscape provider image here; it does not define two competitors.
  Provider URLs are never rewritten into game-thumbs paths. Images are not cropped.
- The background uses its saved default until a distinct upstream background
  source is implemented. The two slots are tracked independently.
- A desired/queued event does not replace the currently verified event's title or art.
- A new accepted positive observation renews the fallback deadline. Re-reading
  an old sample does not. Defaults are due **five minutes after the last fresh
  accepted confirmation**, without an extra grace period after evidence expiry.
- Brief lost verification, pause, manual handoff, or completion holds the current
  title and artwork until that deadline. Automation pause alone does not invalidate
  continuing verified playback.
- A new event without an image uses the default poster. A failed image download
  for a new event also uses its default and retries; a failed refresh for the
  same event retains that event's last good image.
- Content changes, changed source URLs, changed defaults and explicit resync
  trigger work. A source silently changing bytes behind an unchanged URL needs
  **Resync Plex now**; there is no image polling loop.
- Demo/simulator mode cannot write Plex titles or artwork, including defaults.
- Disabling prevents new writes and leaves the current title and images in place. A request
  already transmitted can still complete. **Restore defaults and disable**
  explicitly disables automation and queues **Dillflix Live** and both default images.

The worker uses committed desired state and deadline/retry wake-ups. Ordinary
controller restart retains the existing deadline and resolves uncertain writes
before proceeding. A database **restore** suspends the integration and discards
historical pending jobs; review the restored settings before enabling it again.

For inspection, `GET /api/v1/devices/living-room/now-playing` returns the accepted
playback and selected `event.thumbnail_url`. This is a read-only diagnostic view
of the same internal projection; the artwork worker never polls it. See the
[endpoint contract](now-playing.md). A 0.15.0 deployment returned 404 because that
route was removed; it is restored in 0.15.1.

## API scope and verification

Artwork writes are raw image POSTs to the configured single item's
`/library/metadata/{id}/posters` and `/library/metadata/{id}/arts`, matching
[Python PlexAPI's upload methods](https://python-plexapi.readthedocs.io/en/latest/_modules/plexapi/mixins/resources.html).
Current-image reads still use the `/thumb/{timestamp}` and `/art/{timestamp}`
URLs from metadata.

The title uses `PUT /library/sections/{section}/all` with exactly `id` (the configured
single item), `type=1`, `title.value` and `title.locked=1`, following
[Python PlexAPI's title-edit contract](https://python-plexapi.readthedocs.io/en/latest/_modules/plexapi/mixins/edit.html).
The lock prevents metadata agents from replacing the managed title. The client
has no scan, refresh, analyze, arbitrary-field editor or Plex-database write method.

Versions 0.15.0–0.15.1 used singular upload paths that can return HTTP 404.
Upgrade to 0.15.2 or later, then choose **Retry Plex update** in Plex settings
(called **Retry artwork update** in 0.15.x).
Restarting alone preserves the suspension. Retry first reads back the saved attempt
before deciding whether an upload is needed. HTTP errors
now include the method and relative request path, distinguishing failed uploads
from missing metadata or image reads without exposing the token or server body.

Version 0.16.1 removes before/after comparisons of media and descriptive
properties, including duration, bitrate, streams and sort title. No property
fingerprint is recorded or checked during normal delivery or retry recovery.
The configured server/item identity is still checked. Title writes verify the
requested title and its lock; no sort-title or media-property edit is sent.

Images are read back and compared, tolerating small resizing/JPEG differences.
An acknowledged upload/edit alone does not count as verified. Plex may update artwork
bookkeeping and cache timestamps. The integration cannot guarantee unchanged
internal SQL columns or force immediate artwork refresh in every Plex client.

Artwork uploads were confirmed working by the operator on 0.15.2. Verify the
intended title and artwork on the installed Plex server after deployment.

## Failures and recovery

Network/image-source failures retry with backoff from 5 seconds to 5 minutes.
Invalid credentials, changed server/item identity and unsupported endpoints
suspend delivery. Correct the issue and use **Retry Plex update**, or save
corrected settings. Old media-property suspensions resume automatically on upgrade;
other suspensions are retained.

A timeout after sending a title or image has an uncertain outcome. The attempt
and intended title or source image are persisted. The worker reads Plex back
before retrying, without comparing media properties. Obsolete fingerprints in
older attempts are discarded during reconciliation. Poster and background writes are not atomic; successful slots
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
