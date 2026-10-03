# Take control

Use **Device screen → Take control** to navigate the real Fire TV yourself while the controller suspends event selection. The remote requires the same `SCREEN_ADB_SERIAL`, local ADB installation/authorization and pinned scrcpy server as [screen mirroring](screen-mirroring.md). No extra service or environment setting is needed.

When the remote connects, it sends a wake-only command before enabling the controls. This also happens when reconnecting an active session. It does not toggle power or navigate away from the current app. Wake waits for playback cancellation and valid session ownership; a failed send leaves the remote disconnected so you can retry. Opening the screen preview alone does not wake the device.

Choose **Control for** before starting: 5, 15 or 30 minutes; or 1, 2, **4**, 8, 12 or 24 hours. For an unsupported event, choose four hours, take control, then open its app using the remote. Automation stays suspended even if you close the browser. This is a wall-clock deadline; the controller does not know when that manually opened event finishes. Choose enough time for overruns.

The countdown states what happens at expiry. If automation was active before takeover, it resumes selection then. If it was paused, it stays paused. During control:

- Use the D-pad, OK, Back, Home, Menu, Play/pause or Delete buttons.
- Focus the remote to use arrows, Enter, Escape, Home, the menu key, Space or Backspace. Shortcuts do not intercept normal form fields. Enter/Space on a focused button activates that button normally.
- Focus a search field on the TV, type in **Send text to the TV**, then send. Supported text is 1–200 printable ASCII characters. Text is not saved; actual text-field behavior depends on the Fire TV app.
- Choose **Reset timer to** and **Extend control** to set a new duration from now.
- **Resume automation** ends the session and reevaluates the current live watch plan. **End control and stay paused** releases the remote without resuming selection.

Your watch plan and priorities remain editable and saved. **Play now** is unavailable until manual control ends, preventing an accidental navigation interruption. Manual navigation clears the controller's prior playback identity; it never claims that the previously selected event is still playing.

The remote waits for playback cancellation to be acknowledged before accepting input. If cancellation is unavailable, automation remains paused and the session is retained; **Reconnect remote** retries the handoff. Release and expiry finish manual input cleanup before automation can resume.

Other browsers can watch the screen but cannot send inputs. **Take over → Confirm takeover** transfers ownership and disconnects the previous remote. A reload in the owning tab retains ownership when session storage is available. Closing the tab may lose its credential; reopen the controller and explicitly take over if necessary. One tab can attach to a session at a time.

Hiding the screen only releases video; the remote remains usable. Backgrounding the tab disconnects both active transports while leaving the manual session running. Returning reconnects with fresh input state. A failed input connection offers **Reconnect remote**. If delivery was uncertain, inspect the TV before sending again: the controller never automatically replays inputs. Key taps always include release events; long press, pointer/touch injection, clipboard sync, power and arbitrary ADB commands are not offered.

Controller restarts retain the session and original deadline. An already-expired session is ended when the service starts. Offline database restore clears ownership and leaves automation paused. These timers require the controller service to be running to act at expiry.

## Update nginx

Use the updated WebSocket location in [nginx.conf.example](nginx.conf.example). Its pattern now covers both streams:

```nginx
location ~ ^/api/v1/devices/[^/]+/(screen/stream|control/input)$ {
    # Copy the full proxy/Upgrade directives from nginx.conf.example.
}
```

Preserve `$http_host` for both HTTP and WebSocket requests, including nonstandard public ports. Keep existing proxy authentication on both paths. Vite already proxies both WebSockets during development.

## Current limits

Manual remote inputs affect the real device. With `PLAYBACK_ADAPTER=simulator`, Resume restores simulated automation. With `prime-player`, it enables service-backed playback after manual transport drains and the service acknowledges automatic ownership. Take control waits for input quiescence; native stop confirmation is reported separately. Physical remotes and unrelated ADB/ws-scrcpy clients remain outside this gate.

Local tests exercise scrcpy framing through a real subprocess/TCP test double and remote UI flows in Chromium. Real Fire TV input handling, simultaneous capture/control compatibility, Docker/nginx and physical Safari still need testing on your host. If video works but input does not, confirm the input WebSocket proxy route, ADB authorization and Fire OS input-injection support. A `sent` response only acknowledges transport delivery; look at the screen to confirm the app's response.
