# Production recovery acceptance

Run `tools/production_recovery_acceptance.py` on the host that already runs the
controller and player. Keep both production services running. This is not the
isolated device-validation harness: it leaves service configuration, adaptation
cache, network policy and watch plan in place.

Preflight requires fresh verified live playback, automatic ownership (no manual
control or handoff), and a ready Prime network context on the specified device.
If preflight fails, no fault is injected. The test records both Git revisions,
baseline health and playback, then force-stops `com.amazon.firebat` exactly once.
This deliberately interrupts playback. It never issues Play, switches network,
dismisses an overlay, restarts services, clears adaptation state or repeats the
fault. Existing production recovery must do the work.

```bash
cd /appdata/dillflix-live-controller &&
sudo -v &&
python3 tools/production_recovery_acceptance.py \
  --serial 192.168.0.178:5555 \
  --interrupt-playback \
  --output "$HOME/prime-production-recovery-$(date -u +%Y%m%dT%H%M%SZ)"
```

Success requires a new Prime session, ready network context and fresh controller
observations of the same live event, bound to the new session, sustained for 60
seconds. The recovery deadline defaults to 15 minutes. Manual takeover or pause
invalidates the test. An event ending or changing during the test can prevent a
pass; inspect evidence rather than interpreting that as a startup defect.

The adjacent archive contains chronological health/controller snapshots, both
commits, service logs and persisted adaptation artifacts. Capture errors are
retained. The production services remain running after success or failure; the
test never attempts its own recovery. Do not send remote input while it runs.

This validates automatic recovery from a dead application using the actual
deployed supervisor and current persisted adaptation profile. It does not emulate
a new unknown Prime bundle, prove relocation to future contracts, or prove that
all possible startup overlays can be dismissed. It also does not claim to test
the transition of the network connection itself: that remains governed by the
deployed recovery policy.
