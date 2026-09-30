# Working on Dillflix Controller

Read README.md and docs/architecture.md before changing behavior. This is a standalone project; Teamarr is an external catalog dependency.

- Keep playback explicitly simulated until a task authorizes an executor integration. Do not add Fire TV or ADB side effects to frontend or planner code.
- Live content only. Estimated end times, missing feed entries, and temporary errors do not prove completion. Preserve manual commitments through those conditions.
- Keep manual intent, desired playback, and observed playback separate. Check request intent before accepting asynchronous results.
- Use the opaque Teamarr feed entry ID as content_id. Preserve the complete source snapshot and pass every permitted viewing option. Do not add controller app preferences or parse team identities from display names.
- Maintain API command idempotency and configuration revision checks. The web interface consumes the server's state and previews.
- Preserve device-scoped records and contracts while the first implementation supports one device. Authentication belongs to the existing nginx proxy.
- Run `python -m pytest -q` for controller changes. For interface changes, run `npm run test:browser` in frontend with the project Python environment active. The browser runner builds the frontend and starts an isolated demo database.
- Keep credentials, local databases, node_modules, browser caches, and test-run artifacts out of Git. Follow README.md for deployment and explicitly report untested external integrations.
