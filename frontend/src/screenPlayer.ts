import type JMuxer from "jmuxer";

export type ScreenState =
  "connecting" | "live" | "reconnecting" | "unsupported" | "play_required";
type Frame = { data: Uint8Array; pts: number };

// Device timestamps determine frame durations, including when the TV menu is static.
// Neither this player nor its API sends input or claims that an event is playing.
export function startScreenPlayer(
  video: HTMLVideoElement,
  path: string,
  onState: (state: ScreenState, detail: string) => void,
) {
  let stopped = false;
  let generation = 0;
  let attempt = 0;
  let socket: WebSocket | undefined;
  let muxer: JMuxer | undefined;
  let retryTimer: ReturnType<typeof setTimeout> | undefined;
  let watchdog: ReturnType<typeof setInterval> | undefined;
  let frameCallback: number | undefined;

  function clear() {
    generation++;
    clearTimeout(retryTimer);
    clearInterval(watchdog);
    if (frameCallback !== undefined)
      video.cancelVideoFrameCallback?.(frameCallback);
    if (socket) {
      socket.onopen = socket.onmessage = socket.onclose = socket.onerror = null;
      socket.close();
      socket = undefined;
    }
    video.onerror = null;
    if (muxer) {
      const url = muxer.url;
      muxer.destroy();
      if (url) URL.revokeObjectURL(url);
      muxer = undefined;
    }
    video.pause();
    video.removeAttribute("src");
    video.replaceChildren();
    video.load();
  }

  function reconnect(detail: string) {
    if (stopped) return;
    clear();
    const delay = Math.min(15000, 1000 * 2 ** Math.min(attempt++, 4));
    onState("reconnecting", detail);
    retryTimer = setTimeout(connect, delay);
  }

  function unsupported() {
    clear();
    onState(
      "unsupported",
      "This browser cannot decode the screen stream. Use a browser with H.264 Media Source support.",
    );
  }

  async function connect() {
    if (stopped) return;
    const current = ++generation;
    const active = () => !stopped && current === generation;
    onState(
      attempt ? "reconnecting" : "connecting",
      "Connecting to the device screen…",
    );
    const platform = window as typeof window & {
      ManagedMediaSource?: typeof MediaSource;
      WebKitMediaSource?: typeof MediaSource;
    };
    if (
      !platform.MediaSource &&
      !platform.ManagedMediaSource &&
      !platform.WebKitMediaSource
    ) {
      unsupported();
      return;
    }
    try {
      const { default: Muxer } = await import("jmuxer");
      if (!active()) return;
      let config: Uint8Array | undefined;
      let pending: Frame | undefined;
      let metadataSeen = false;
      let hasPicture = false;
      let lastPicture = performance.now();
      let previousTime = -1;
      let durationRemainder = 0;
      let healthySince: number | undefined;
      let mediaTime = 0;
      let detail = "The screen connection was interrupted. Reconnecting…";

      const picture = () => {
        if (!active()) return;
        lastPicture = performance.now();
        // Brief successful playback must not reset an ongoing failure's backoff.
        healthySince ??= lastPicture;
        if (lastPicture - healthySince > 10000) attempt = 0;
        if (!hasPicture) {
          hasPicture = true;
          onState("live", "Live device screen");
        }
      };
      const nextPicture = () => {
        picture();
        if (active())
          frameCallback = video.requestVideoFrameCallback(nextPicture);
      };
      if (video.requestVideoFrameCallback)
        frameCallback = video.requestVideoFrameCallback(nextPicture);

      muxer = new Muxer({
        node: video,
        mode: "video",
        fps: 30,
        flushingTime: 0,
        maxDelay: 500,
        clearBuffer: true,
        onKeyframePosition: () => {
          // JMuxer 2.1.4 uses frameCount * latestDuration for this index,
          // which is incorrect for scrcpy's variable-rate frames. Correct the
          // just-added entry using our accumulated MP4 sample durations.
          if (muxer?.kfPosition.length)
            muxer.kfPosition[muxer.kfPosition.length - 1] = mediaTime / 1000;
        },
        onReady: () => {
          if (!active() || socket) return;
          const url = new URL(path, window.location.href);
          url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
          socket = new WebSocket(url);
          socket.binaryType = "arraybuffer";
          socket.onmessage = (event) => {
            if (!active()) return;
            try {
              if (typeof event.data === "string") {
                const message = JSON.parse(event.data);
                if (message.type === "error") {
                  reconnect(String(message.message));
                } else if (
                  message.type === "stream" &&
                  message.protocol === 1 &&
                  message.codec === "h264"
                ) {
                  if (metadataSeen)
                    reconnect("The device display changed. Reconnecting…");
                  metadataSeen = true;
                } else throw new Error("Invalid stream metadata");
                return;
              }
              const bytes = new Uint8Array(event.data);
              const header = new DataView(event.data);
              if (
                !metadataSeen ||
                bytes.length < 13 ||
                header.getUint32(8) !== bytes.length - 12 ||
                bytes.length > 4 * 1024 * 1024 + 12
              )
                throw new Error("Invalid video packet");
              const flags = header.getBigUint64(0);
              const data = bytes.subarray(12);
              if (flags & (1n << 63n)) {
                config = data;
                return;
              }
              const pts = Number(flags & ((1n << 62n) - 1n));
              if (pending) {
                const elapsed = (pts - pending.pts) / 1000;
                if (elapsed < 0)
                  throw new Error(
                    `Encoder timestamp moved backwards (${elapsed} ms)`,
                  );
                // Equal timestamps need a positive MP4 sample duration. Long
                // static-screen gaps are valid; compress them for a live view
                // instead of treating them as corrupt H.264 or replaying a gap.
                const duration = Math.max(1, Math.min(1000, elapsed));
                const rounded = Math.max(
                  1,
                  Math.round(duration + durationRemainder),
                );
                durationRemainder += duration - rounded;
                muxer?.feed({
                  video: pending.data,
                  duration: rounded,
                  isLastVideoFrameComplete: true,
                });
                if (!active()) return;
                mediaTime += rounded;
              }
              if (config) {
                const combined = new Uint8Array(config.length + data.length);
                combined.set(config);
                combined.set(data, config.length);
                pending = { data: combined, pts };
                config = undefined;
              } else pending = { data, pts };
            } catch (error) {
              const reason =
                error instanceof Error ? error.message : "Invalid video packet";
              console.warn("Device screen packet error:", reason);
              reconnect(`Screen stream error: ${reason}. Reconnecting…`);
            }
          };
          socket.onclose = () => active() && reconnect(detail);
          socket.onerror = () => {
            detail =
              "Cannot connect to the screen feed. Check the controller connection.";
          };
          video.play().catch(() => {
            if (active())
              onState(
                "play_required",
                "Tap Resume screen to allow video in this browser.",
              );
          });
        },
        onError: (error) => {
          // Leave JMuxer's callback stack before destroying its buffers.
          console.warn("Device screen media buffer error:", error);
          queueMicrotask(() => {
            if (active())
              reconnect(
                `Screen media buffer error (${error.name || "unknown"}). Reconnecting…`,
              );
          });
        },
        onUnsupportedCodec: () => {
          if (active()) unsupported();
        },
      });
      video.onerror = () => {
        const error = video.error;
        console.warn(
          "Device screen decoder error:",
          error?.code,
          error?.message,
        );
        if (active())
          reconnect(
            `Video decoder error ${error?.code ?? "unknown"}: ${error?.message || "The browser rejected a video frame"}. Reconnecting…`,
          );
      };
      watchdog = setInterval(() => {
        if (!active()) return;
        if (
          video.buffered.length &&
          video.buffered.end(video.buffered.length - 1) -
            video.buffered.start(0) >
            60
        ) {
          // JMuxer retains 30 seconds and cleans every 10 seconds. The safety
          // ceiling must leave room for that normal retention/cleanup cycle.
          reconnect("Refreshing the screen buffer to stay live…");
          return;
        }
        if (
          !video.requestVideoFrameCallback &&
          video.readyState >= 2 &&
          video.currentTime !== previousTime
        )
          picture();
        previousTime = video.currentTime;
        if (
          !video.paused &&
          performance.now() - lastPicture > (hasPicture ? 8000 : 60000)
        )
          reconnect("No fresh screen frames. Reconnecting…");
        // A paused video may need a user gesture; absence of a socket/frame still needs recovery.
        else if (!hasPicture && performance.now() - lastPicture > 60000)
          reconnect("No screen image received. Reconnecting…");
      }, 1000);
    } catch {
      if (active()) unsupported();
    }
  }

  void connect();
  return () => {
    stopped = true;
    clear();
  };
}
