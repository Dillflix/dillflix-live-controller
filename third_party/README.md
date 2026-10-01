# Screen capture dependencies

- **Genymobile scrcpy 3.3.4**, Apache-2.0. Source: https://github.com/Genymobile/scrcpy/tree/v3.3.4, commit `fb6381f5b9bb96f3fa823d899f4c32de2ec84ab3`. The unmodified server is downloaded by `controller.screen_install` and verified against SHA-256 `8588238c9a5a00aa542906b6ec7e6d5541d9ffb9b5d0f6e1bc0e365e2303079e`. License: [scrcpy-LICENSE](scrcpy-LICENSE).
- **JMuxer 2.1.4**, MIT, copyright Samir Das. Source: https://github.com/webstream-labs/jmuxer. Installed through the pinned npm dependency/lockfile and bundled for the browser. License: [jmuxer-LICENSE](jmuxer-LICENSE).
- **NetrisTV/ws-scrcpy** was evaluated as an architectural reference, at commit `cd6cea6ebde17af6685c2b325183fc24bef9d52b`. No source files or modified server binaries from it are copied into this project. Reference: https://github.com/NetrisTV/ws-scrcpy.

These license files are included in the Docker image. Browser dependency notices are also available at `/screen-licenses.txt`.

The native focus collector in `controller/executor/accessibility.py` is a Python port of the user's supplied Prime Video exploration (`agent-control/tvtheseus/accessibility.js`, `prime-focus-burst.js`, `observations.js`, and `focus-metadata.js`). The reference fixture records SHA-256 hashes of the original collector modules; `tools/generate_accessibility_conformance.cjs` regenerates its expected snapshots from that unchanged source. Node is used only for fixture regeneration. This provenance is separate from the upstream screen-capture dependencies above.

Version 0.10 additionally ports complete-record framing and listener/capture safeguards from the supplied `prime-path-capture-fixed (1).zip` recorder (`event-records.js` and `capture.js`). Selected supplied capture-05 accessibility and media records are replay fixtures with source hashes; they are development evidence, not generated model answers. The MediaSession adapter consumes the observed `dev.tvprobe.mediasession` JSON contract; no APK/source for that service was included or redistributed.

`tests/fixtures/screen-h264.json` is an original, synthetic test pattern generated with ffmpeg/libx264, not captured television content. Its config and ten access units are base64 encoded. Generation parameters: `testsrc2=size=320x180:rate=10`, one second, `libx264`, `ultrafast`, `zerolatency`, baseline profile, `yuv420p`, and `aud=1:keyint=10:min-keyint=10:scenecut=0`. Test packets add the scrcpy timestamps and config/keyframe flags at runtime.
