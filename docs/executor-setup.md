# Playback setup

Controller 0.14.0 uses the external Prime Player service for real execution. Follow [Prime Player setup, migration and target acceptance](prime-player.md) and [manual ownership](prime-player-ownership.md).

Supported playback modes are `simulator` and `prime-player`. The old `prime-video` screenshot/ADB executor, accessibility collector, MediaSession probe, APK installer and vision model settings have been removed. Screen mirroring remains separately configured through [screen setup](screen-mirroring.md).

Read-only diagnostics:

```bash
python -m controller.prime_player.check
```

This checks API version, device serial, runtime capabilities and acknowledged ownership without searching, playing or sending input. A configured text model is optional for exact unambiguous label matches; ambiguous results require it or result in abstention.
