# Public live coverage and PlexSSO

The existing `/` application remains the admin controller. `/public/` is a separate
frontend entry point with Now playing, Live/Upcoming events, sport filters, search,
Play now, and Add to plan. It has no finish-event, pause, remote, screen, settings,
diagnostics, or administrative watch-plan controls. Both interfaces control the
same living-room device and share one durable watch plan.

## Playback policy

Selection is ordered **eligible admin watch-plan entries → eligible user requests
→ automatic priorities**. Play now goes to the front of the actor's tier. Order
within each tier is stable. An admin Add to plan also outranks user requests as
soon as that event is eligible. Upcoming admin entries do not block a live user
request until they become eligible. Existing commitments without actor metadata
are treated as admin entries. Schema 9 prevents older controllers from opening a
database containing this policy and silently ignoring it.

Public Play now saves a request; it does not promise an immediate switch while an
admin selection is protected. It cannot resume paused automation, take over manual
control, remove entries, reorder admin commitments, mark events finished, or
change configuration. User requests remain queued and become eligible again when
higher entries finish. Missing feed entries, estimated ends, retry delays and
temporary status errors still do not complete commitments.

Admin Play now or Add to plan on a user-requested event promotes that commitment
to the admin tier. A public request for an admin-owned event leaves its ownership
and position intact. Adding an already-planned event is idempotent. Public Play
now on another user's event records the latest requesting actor and moves that
event to the front of the user tier. Public entries are limited to 100 retained
commitments; admins can remove old entries in Watch plan.

Settings → Public app has independent **Allow public Play now** and **Allow public
Add to plan** switches. Both default off and persist across restarts. Disabling a
switch prevents new actions; previously accepted requests stay in the plan. The
API enforces these switches transactionally, along with configuration revisions.
The read model refreshes every five seconds. Stale submissions return 409 and the
page refreshes; it never silently retries against a newer plan.

The admin Watch plan, Now playing, and activity records identify admin/user source
and the authenticated username, or Guest for anonymous requests. Actions made through the public app always have
user priority, even if the visitor also has admin-site access.

## Guest mode (no public authentication)

Set this in `.env`, then recreate the controller with `docker compose up -d --build`:

```dotenv
CONTROLLER_PUBLIC_AUTH_MODE=guest
```

Visitors can open `/public/`, browse the schedule, and use whichever actions the
admin enables under Settings → Public app, without logging in or using PlexSSO.
The page displays **Browsing as Guest**. Play now and Add to plan still default
off; enabling guest mode does not enable either action automatically. Existing
action switches and watch-plan entries survive mode changes and restarts.

All anonymous requests are attributed to **Guest** (`guest:anonymous`) and use
the user priority tier. This mode does not identify individual visitors or create
accounts. Browser-supplied identity headers are ignored on public routes, even
when that browser also has admin credentials. Admin priority, pause/manual
control, revision checks, idempotency, and same-origin writes still apply.

Public access and admin authentication are configured independently:

| Public mode | Proxy secret | Public app | Admin app |
| --- | --- | --- | --- |
| `proxy` (default) | Set | Requires authenticated proxy identity | Requires admin proxy identity |
| `proxy` | Blank | API disabled (503) | Existing trusted-LAN behavior |
| `guest` | Set | No login required | Requires admin proxy identity |
| `guest` | Blank | No login required | Blocked (403), including APIs and sockets |

Keep `CONTROLLER_PROXY_SECRET` configured to administer the device in guest mode.
That secret authenticates the admin reverse proxy; it does **not** require
PlexSSO or a public login. The admin proxy can continue using PlexSSO, or use your
other private/admin authentication. A complete
[nginx guest example](nginx.guest.conf.example) uses an anonymous public hostname
and HTTP Basic authentication on a separate admin hostname, with no PlexSSO.
Replace the certificate paths, password-file path, hostnames and proxy secret.
The nginx password file is managed by your existing admin authentication setup;
do not put its credentials in this repository.

If adapting the PlexSSO example instead, turn off `auth_request` on the **public
server only** and remove its login redirects. Leaving nginx authentication on
would still prompt public visitors to sign in even though the application is in
guest mode. Keep the admin server's authentication and header overrides intact.

With no proxy secret, guest browsing works directly at `/public/`, but admin
endpoints are deliberately unavailable. Previously enabled public actions still
work; a fresh install remains browse-only until an admin configures its private
proxy and enables actions. Guest mode never turns a public connection into a
legacy administrator. Set `CONTROLLER_PUBLIC_AUTH_MODE=proxy` and restart to
require public login again; existing Guest entries retain their attribution.
The container uses anonymous `GET /healthz`, which returns only `{"ok": true}`;
the detailed `/api/health` report remains restricted to admins in guest mode.

## Reuse PlexSSO through nginx

PlexSSO already supports nginx `auth_request`, service-specific access tiers, and
verified identity response headers. This integration uses that existing flow;
the controller never receives a Plex password or token. See upstream
[configuration](https://github.com/drkno/PlexSSO#access-control-service-rules),
[SSO response implementation](https://github.com/drkno/PlexSSO/blob/main/backend/PlexSSO/Controllers/SsoController.cs),
and [header names](https://github.com/drkno/PlexSSO/blob/main/backend/PlexSSO/Model/Constants.cs).

1. Generate a long random proxy secret (for example `openssl rand -hex 32`). Set
   `CONTROLLER_PROXY_SECRET` in `.env` and the same value in nginx's
   `X-Dillflix-Proxy-Key` header. Keep it on the servers; never in frontend code.
2. Bind the controller privately (`CONTROLLER_BIND_ADDRESS=127.0.0.1` for nginx
   on the same host), or use a private Docker network without publishing the
   controller port. Enable HTTPS on both public and admin hostnames.
3. Add separate PlexSSO services named `dillflix-admin` and `dillflix-public`.
   Require Owner access for the admin service and NormalUser for public. For
   example, merge this into the existing PlexSSO `accessControls` configuration:

   ```json
   {
     "dillflix-admin": [{"path": "/", "minimumAccessTier": "Owner", "controlType": "Block"}],
     "dillflix-public": [{"path": "/", "minimumAccessTier": "NormalUser", "controlType": "Block"}]
   }
   ```

   Review exemptions deliberately if other people should administer the device.
   Plex server membership alone must never grant access to the admin service.
4. Adapt [the nginx example](nginx.public.conf.example) for the existing TLS
   certificates, login hostname, controller address, and PlexSSO address. nginx
   reads `X-PlexSSO-Username` from the **auth subrequest response**, overwrites the
   controller's identity headers, and assigns a fixed role for each authorized
   service. Do not copy user identity or role from incoming browser headers.
5. Recreate the controller and reload nginx after `nginx -t`. Test an owner and
   an ordinary authorized Plex account, then enable the desired public actions
   in admin Settings. Anonymous requests and revoked Plex accounts should fail
   authentication at nginx. Users must be unable to reach admin APIs or sockets.

In the default `CONTROLLER_PUBLIC_AUTH_MODE=proxy`, the controller requires the proxy credential,
verified username, and `admin`/`user` role on all HTTP and WebSocket routes. Public
users can reach only public APIs, the public page, and built assets. Browser
mutations also require a matching Origin, including scheme and hostname; nginx
must overwrite `Host` and `X-Forwarded-Proto`. Sibling subdomains are rejected.
Programmatic admin writes must supply that same Origin in addition to their
existing proxy authentication. Existing executor API token checks still apply;
machine clients must also pass the authenticated admin proxy boundary.

In proxy mode without `CONTROLLER_PROXY_SECRET`, the existing trusted-LAN/admin deployment
continues to work; **public APIs fail closed with 503**. Do not expose that legacy
deployment as a public service. Public API responses omit actor lists, internal
playback locators, full source snapshots, private configuration, and diagnostics.

## Identity and future integrations

The identity used here is `plexsso:<verified username>`. This supports attribution
and actor-bound command receipts today. PlexSSO's auth-request response exposes
username and email, not an immutable Plex account ID. A renamed username therefore
appears as a new actor; historical attribution remains unchanged. No account
profile database, Plex Watchlist sync, personal plans, or Plex playback integration
is created by this change.

Guest requests use the separate aggregate identity described above; enabling
guest mode does not claim a verified Plex identity for anonymous visitors.

Current PlexSSO also documents an
[OIDC plugin](https://github.com/drkno/PlexSSO/blob/main/docs/oidc-plugin.md).
If immutable account linking or more app-specific claims are needed later, a
separate OIDC integration can use its verified subject. The installed PlexSSO
version and plugin configuration need checking before adopting it; OIDC is not
required for this proxy-based implementation.

## Validation scope

Automated tests cover role isolation (including WebSockets), forbidden public
commands, header spoofing, same-origin writes, independent switches, actor-bound
idempotency, stale revisions, pause/manual ownership, admin preemption, public
resumption, read-model redaction, persistence, and responsive interface behavior.
Real PlexSSO login, nginx configuration, Docker deployment and target playback
hardware require deployment acceptance; local simulator tests do not verify them.

Local validation on Windows: frontend build and lint passed; all six new browser
tests passed, and the other 35 existing browser tests passed in the regression
run. The focused public/controller/now-playing backend run passed 86 tests. The
broader backend run passed 534 tests with 19 failures in existing Linux-specific
permissions, backup/fsync, and executable-fixture paths, plus two skips. The
Linux deployment test module cannot collect on Windows (`pwd` is unavailable).
Backend runs used a temporary Windows `flock` compatibility adapter outside the
repository because production imports `fcntl`; this is not a substitute for
running `python -m pytest -q` on the deployment's Linux environment. Public browser
tests mock the viewer API, while backend tests exercise the actual proxy boundary
and command path. No real PlexSSO server or playback device was contacted.

Guest-mode follow-up: the combined public/guest backend tests passed, including
anonymous access with and without an admin proxy secret, identity spoofing,
admin-route/socket isolation, mode changes, and disabled actions. The 42-test
browser run passed 41 tests; one existing remote-control test encountered a
response-disposal error during teardown and passed on its focused rerun without
code changes. The full Python suite still cannot collect the Linux deployment
module on Windows (`pwd` is unavailable).
