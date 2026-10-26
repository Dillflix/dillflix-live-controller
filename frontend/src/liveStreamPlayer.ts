import type Hls from "hls.js";

export type StreamPlayerState =
  "connecting" | "live" | "buffering" | "paused" | "error";
export type StreamSession = {
  session_id: string;
  expires_in: number;
  playlist_url: string;
};
const api = "/api/public/v1/streams/sessions";

export async function streamResponse<T>(response: Response): Promise<T> {
  if (response.status === 401 || response.status === 403)
    throw new Error(
      "Your access to live streams has expired or is unavailable. Sign in through the public page and try again.",
    );
  const value = await response.json().catch(() => null);
  if (!response.ok)
    throw new Error(
      typeof value?.detail === "string"
        ? value.detail
        : value?.detail?.message ||
            "The stream is unavailable. Please try again.",
    );
  if (!value)
    throw new Error("The stream service returned an invalid response.");
  return value as T;
}

function release(id: string) {
  // keepalive also releases on pagehide. A failed request is covered by the
  // server's idle expiry; do not keep playing or retain a session locally.
  void fetch(`${api}/${encodeURIComponent(id)}`, {
    method: "DELETE",
    credentials: "same-origin",
    keepalive: true,
  }).catch(() => {});
}

/** One independent, view-only session. No scheduler or playback-device APIs. */
export function startLiveStream(
  video: HTMLVideoElement,
  streamId: string,
  tier: number,
  report: (state: StreamPlayerState, detail: string) => void,
  autoplay = true,
) {
  let disposed = false;
  let session: string | undefined;
  let hls: Hls | undefined;
  let lastProgress = Date.now();
  let lastTime = -1;
  let edgeRecovery = false;
  let mediaRecovery = false;
  let requestTimeout: ReturnType<typeof setTimeout> | undefined;
  const creation = new AbortController();
  const listeners: [keyof HTMLMediaElementEventMap, EventListener][] = [];
  const on = (event: keyof HTMLMediaElementEventMap, callback: () => void) => {
    listeners.push([event, callback]);
    video.addEventListener(event, callback);
  };
  const state = (value: StreamPlayerState, detail: string) => {
    if (!disposed) report(value, detail);
  };
  const dispose = () => {
    if (disposed) return;
    disposed = true;
    clearInterval(watchdog);
    // Let an already submitted POST finish so its late response can be
    // released below. Aborting it here could orphan a successful allocation.
    for (const [event, callback] of listeners)
      video.removeEventListener(event, callback);
    hls?.destroy();
    video.pause();
    video.removeAttribute("src");
    video.load();
    if (session) release(session);
    session = undefined;
  };
  const fail = (message: string) => {
    if (disposed) return;
    state("error", message);
    dispose();
  };
  const goLive = () => {
    const target =
      hls?.liveSyncPosition ??
      (video.seekable.length
        ? video.seekable.end(video.seekable.length - 1) - 1
        : null);
    if (target !== null && Number.isFinite(target) && target >= 0) {
      video.currentTime = target;
      lastProgress = Date.now();
      return true;
    }
    return false;
  };
  const play = () => {
    if (disposed) return;
    void video.play().catch((error: DOMException) => {
      if (disposed || error.name === "AbortError") return;
      if (error.name === "NotAllowedError")
        state("paused", "Use the video's play control to begin watching.");
      else
        fail(
          "This browser could not play the stream. Try again or use another browser.",
        );
    });
  };
  on("playing", () => {
    lastProgress = Date.now();
    state("live", "");
  });
  on("waiting", () => state("buffering", "Buffering live coverage…"));
  on("pause", () => state("paused", ""));
  on("timeupdate", () => {
    if (Math.abs(video.currentTime - lastTime) > 0.05) {
      lastProgress = Date.now();
      lastTime = video.currentTime;
    }
  });
  on("error", () => {
    // hls.js handles MSE errors and gets one bounded media recovery attempt.
    if (!hls)
      fail(
        "The video could not be loaded. The source may be offline or your access may have expired. Sign in through the public page and retry.",
      );
  });
  on("ended", () =>
    fail("The live stream ended. Retry to reconnect to current coverage."),
  );
  const watchdog = setInterval(() => {
    if (
      disposed ||
      document.hidden ||
      (video.paused && video.readyState > 0) ||
      Date.now() - lastProgress < 25000
    )
      return;
    if (!edgeRecovery && goLive()) {
      edgeRecovery = true;
      state("buffering", "Reconnecting to the live edge…");
      play();
    } else fail("Live video stopped arriving. Retry to reconnect.");
  }, 1000);

  state("connecting", "Connecting to live coverage…");
  const begin = () => {
    if (autoplay) play();
    else state("paused", "");
  };
  void (async () => {
    try {
      requestTimeout = setTimeout(() => creation.abort(), 15000);
      const result = await streamResponse<StreamSession>(
        await fetch(api, {
          method: "POST",
          credentials: "same-origin",
          signal: creation.signal,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ stream_id: streamId, tier }),
        }),
      );
      clearTimeout(requestTimeout);
      if (!result.session_id || typeof result.session_id !== "string")
        throw new Error("The stream service returned an invalid session.");
      if (disposed) {
        release(result.session_id);
        return;
      }
      session = result.session_id;
      const playlist = new URL(result.playlist_url, location.origin);
      const expected = `${api}/${encodeURIComponent(session)}/hls/index.m3u8`;
      if (
        playlist.origin !== location.origin ||
        playlist.pathname !== expected ||
        playlist.search ||
        playlist.hash
      )
        throw new Error(
          "The stream service returned an invalid playback address.",
        );
      if (video.canPlayType("application/vnd.apple.mpegurl")) {
        video.src = playlist.href;
        on("loadedmetadata", begin);
        video.load();
      } else {
        const { default: Hls } = await import("hls.js");
        if (disposed) return;
        if (!Hls.isSupported())
          throw new Error(
            "This browser does not support live HLS playback. Try a current browser.",
          );
        const loadPolicy = {
          default: {
            maxTimeToFirstByteMs: 10000,
            maxLoadTimeMs: 15000,
            timeoutRetry: {
              maxNumRetry: 2,
              retryDelayMs: 1000,
              maxRetryDelayMs: 3000,
            },
            errorRetry: {
              maxNumRetry: 2,
              retryDelayMs: 1000,
              maxRetryDelayMs: 3000,
            },
          },
        };
        hls = new Hls({
          enableWorker: true,
          lowLatencyMode: false,
          backBufferLength: 10,
          maxBufferLength: 20,
          liveSyncDurationCount: 3,
          liveMaxLatencyDurationCount: 6,
          manifestLoadPolicy: loadPolicy,
          playlistLoadPolicy: loadPolicy,
          fragLoadPolicy: loadPolicy,
        });
        hls.on(Hls.Events.MANIFEST_PARSED, begin);
        hls.on(Hls.Events.ERROR, (_event, data) => {
          if (disposed) return;
          if (data.response?.code === 401 || data.response?.code === 403) {
            fail(
              "Your access to live streams has expired or is unavailable. Sign in through the public page and try again.",
            );
          } else if (data.fatal) {
            if (data.type === Hls.ErrorTypes.MEDIA_ERROR && !mediaRecovery) {
              mediaRecovery = true;
              state("buffering", "Recovering the live video…");
              hls?.recoverMediaError();
            } else
              fail(
                "The stream could not continue. Retry to reconnect to live coverage.",
              );
          }
        });
        hls.loadSource(playlist.href);
        hls.attachMedia(video);
      }
    } catch (error) {
      clearTimeout(requestTimeout);
      fail(
        (error as Error).name === "AbortError"
          ? "Connecting timed out. Please try again."
          : (error as Error).message,
      );
    }
  })();
  return {
    dispose,
    isBehindLive: () => {
      const edge =
        hls?.liveSyncPosition ??
        (video.seekable.length
          ? video.seekable.end(video.seekable.length - 1) - 1
          : null);
      return (
        edge !== null && Number.isFinite(edge) && edge - video.currentTime > 5
      );
    },
    goLive: () => {
      if (goLive()) play();
    },
  };
}
