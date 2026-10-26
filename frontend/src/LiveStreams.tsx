import { useCallback, useEffect, useRef, useState } from "react";
import {
  ChevronDown,
  ChevronUp,
  Maximize,
  Minimize,
  Pause,
  Play,
  RefreshCw,
  Settings,
  Volume2,
  VolumeX,
  X,
} from "lucide-react";
import {
  startLiveStream,
  streamResponse,
  type StreamPlayerState,
} from "./liveStreamPlayer";

type Availability = "live" | "offline" | "unknown";
type Stream = {
  id: string;
  label: string;
  status: Availability;
  default_tier: number;
  tiers: { id: number; label: string; status: Availability }[];
};
type Catalog = { enabled: boolean; streams: Stream[] };
const availability = {
  live: "Live",
  offline: "Offline",
  unknown: "Status unknown",
};

function StreamCard({ stream }: { stream: Stream }) {
  const name =
    ({ sports0: "Feed 1", sports1: "Feed 2" } as Record<string, string>)[
      stream.id
    ] || stream.label;
  const [tier, setTier] = useState(stream.default_tier);
  const [watching, setWatching] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<StreamPlayerState>("connecting");
  const [detail, setDetail] = useState("");
  const [muted, setMuted] = useState(false);
  const [volume, setVolume] = useState(1);
  const [behind, setBehind] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  const video = useRef<HTMLVideoElement>(null);
  const surface = useRef<HTMLDivElement>(null);
  const paused = useRef(false);
  const player = useRef<ReturnType<typeof startLiveStream> | null>(null);
  const selected = stream.tiers.find((option) => option.id === tier);
  const bodyId = `stream-body-${stream.id}`;

  useEffect(() => {
    if (!stream.tiers.some((option) => option.id === tier))
      setTier(stream.default_tier);
  }, [stream.tiers, stream.default_tier, tier]);
  useEffect(() => {
    if (!watching || !video.current) return;
    const current = startLiveStream(
      video.current,
      stream.id,
      tier,
      (next, message) => {
        setState(next);
        setDetail(message);
      },
      !paused.current,
    );
    player.current = current;
    const timer = setInterval(() => setBehind(current.isBehindLive()), 1000);
    return () => {
      clearInterval(timer);
      current.dispose();
      player.current = null;
    };
  }, [watching, stream.id, tier, attempt]);
  useEffect(() => {
    // Leaving the page releases sessions; changing tabs or collapsing does not.
    const leave = () => {
      player.current?.dispose();
      setWatching(false);
    };
    const changed = () =>
      setFullscreen(document.fullscreenElement === surface.current);
    window.addEventListener("pagehide", leave);
    document.addEventListener("fullscreenchange", changed);
    return () => {
      window.removeEventListener("pagehide", leave);
      document.removeEventListener("fullscreenchange", changed);
    };
  }, []);
  const watch = () => {
    paused.current = false;
    if (!watching) {
      setMuted(false);
      setVolume(1);
    }
    setDetail("");
    setAttempt((value) => value + 1);
    setWatching(true);
  };
  const close = () => {
    setWatching(false);
    setDetail("");
  };
  const togglePlay = () => {
    const element = video.current;
    if (!element) return;
    if (element.paused) {
      paused.current = false;
      void element
        .play()
        .catch(() => setDetail("Press Play to resume the stream."));
    } else {
      paused.current = true;
      element.pause();
    }
  };
  const toggleSound = () => {
    if (!video.current) return;
    const silent = video.current.muted || video.current.volume === 0;
    if (video.current.volume === 0) video.current.volume = 1;
    video.current.muted = !silent;
  };
  const toggleFullscreen = async () => {
    try {
      if (document.fullscreenElement === surface.current)
        await document.exitFullscreen();
      else if (surface.current?.requestFullscreen)
        await surface.current.requestFullscreen();
      else {
        // iPhone Safari exposes fullscreen on the video instead of its wrapper.
        const native = video.current as
          (HTMLVideoElement & { webkitEnterFullscreen?: () => void }) | null;
        if (native?.webkitEnterFullscreen) native.webkitEnterFullscreen();
        else setDetail("Fullscreen is unavailable in this browser.");
      }
    } catch {
      setDetail("Fullscreen is unavailable in this browser.");
    }
  };
  const soundLabel = muted || volume === 0 ? "Unmute" : "Mute";
  const playLabel = state === "paused" ? "Play" : "Pause";
  const canControl = watching && state !== "error" && state !== "connecting";
  return (
    <article
      className="df-stream-card"
      aria-label={name}
      data-testid={`stream-${stream.id}`}
    >
      <div className="df-stream-head">
        <h3>
          <button
            className="df-stream-collapse"
            aria-expanded={!collapsed}
            aria-controls={bodyId}
            aria-label={`${collapsed ? "Expand" : "Collapse"} ${name}`}
            onClick={() => setCollapsed(!collapsed)}
          >
            {collapsed ? <ChevronDown size={18} /> : <ChevronUp size={18} />}
            {name}
          </button>
        </h3>
        <div className="df-stream-head-actions">
          {collapsed && canControl && (
            <>
              <button
                className="df-button"
                onClick={togglePlay}
                aria-label={`${playLabel} ${name}`}
              >
                {state === "paused" ? <Play size={17} /> : <Pause size={17} />}
              </button>
              <button
                className="df-button"
                onClick={toggleSound}
                aria-label={`${soundLabel} ${name}`}
              >
                {muted || volume === 0 ? (
                  <VolumeX size={17} />
                ) : (
                  <Volume2 size={17} />
                )}
              </button>
            </>
          )}
          <span
            className={`df-event-status ${stream.status === "live" ? "df-live" : ""}`}
          >
            {availability[stream.status]}
          </span>
        </div>
      </div>
      <div id={bodyId} hidden={collapsed}>
        <div
          ref={surface}
          className={`df-stream-surface ${watching ? "is-watching" : ""}`}
        >
          {watching ? (
            <video
              ref={video}
              className="df-stream-video"
              aria-label={`${name} video`}
              playsInline
              preload="none"
              onVolumeChange={() => {
                if (!video.current) return;
                setMuted(video.current.muted);
                setVolume(video.current.volume);
              }}
            />
          ) : (
            <div className="df-stream-idle">
              <button
                className="df-button df-button-primary"
                onClick={watch}
                disabled={
                  stream.status === "offline" ||
                  !selected ||
                  selected.status === "offline"
                }
              >
                <Play size={20} />
                Watch stream
              </button>
            </div>
          )}
          {
            <div
              className="df-stream-toolbar"
              role="group"
              aria-label={`${name} player controls`}
            >
              {watching && (
                <>
                  {state === "error" ? (
                    <button
                      onClick={watch}
                      aria-label="Retry stream"
                      title="Retry stream"
                    >
                      <RefreshCw size={20} />
                    </button>
                  ) : (
                    <button
                      onClick={togglePlay}
                      disabled={!canControl}
                      aria-label={playLabel}
                      title={playLabel}
                    >
                      {state === "paused" ? (
                        <Play size={20} />
                      ) : (
                        <Pause size={20} />
                      )}
                    </button>
                  )}
                  <button
                    onClick={toggleSound}
                    aria-label={soundLabel}
                    title={soundLabel}
                  >
                    {muted || volume === 0 ? (
                      <VolumeX size={20} />
                    ) : (
                      <Volume2 size={20} />
                    )}
                  </button>
                  <input
                    className="df-stream-volume"
                    type="range"
                    min="0"
                    max="1"
                    step="0.05"
                    value={muted ? 0 : volume}
                    aria-label={`${name} volume`}
                    onChange={(event) => {
                      if (!video.current) return;
                      video.current.volume = Number(event.target.value);
                      video.current.muted = false;
                    }}
                  />
                  <div className="df-stream-live-position">
                    {behind && state !== "error" ? (
                      <button
                        className="df-stream-jump"
                        onClick={() => {
                          paused.current = false;
                          player.current?.goLive();
                        }}
                        title="Skip ahead to the current broadcast"
                      >
                        Jump to live
                      </button>
                    ) : (
                      <span>
                        {state === "paused"
                          ? "Paused"
                          : state === "connecting"
                            ? "Connecting..."
                            : state === "error"
                              ? ""
                              : "Live"}
                      </span>
                    )}
                  </div>
                </>
              )}
              {!watching && <span className="df-stream-live-position" />}
              <details
                className="df-stream-settings"
                onKeyDown={(event) => {
                  if (event.key === "Escape") {
                    event.currentTarget.open = false;
                    event.currentTarget.querySelector("summary")?.focus();
                  }
                }}
              >
                <summary aria-label={`${name} settings`} title="Settings">
                  <Settings size={20} />
                </summary>
                <div className="df-stream-settings-panel">
                  <label>
                    Quality
                    <select
                      aria-label={`${name} quality`}
                      value={tier}
                      disabled={!stream.tiers.length}
                      onChange={(event) => setTier(Number(event.target.value))}
                    >
                      {stream.tiers.map((option) => (
                        <option
                          key={option.id}
                          value={option.id}
                          disabled={option.status === "offline"}
                        >
                          {option.label}
                          {option.status === "offline" ? " (offline)" : ""}
                        </option>
                      ))}
                    </select>
                  </label>
                  {watching && (
                    <button onClick={close}>
                      <X size={16} />
                      Close stream
                    </button>
                  )}
                </div>
              </details>
              {watching && (
                <button
                  onClick={() => void toggleFullscreen()}
                  aria-label={fullscreen ? "Exit fullscreen" : "Fullscreen"}
                  title={fullscreen ? "Exit fullscreen" : "Fullscreen"}
                >
                  {fullscreen ? <Minimize size={20} /> : <Maximize size={20} />}
                </button>
              )}
            </div>
          }
        </div>
        {(detail || stream.status === "offline") && (
          <p
            className={`df-stream-detail ${watching && state === "error" ? "df-stream-error" : ""}`}
            role={watching && state === "error" ? "alert" : undefined}
            aria-live="polite"
          >
            {detail || "This feed is currently offline."}
          </p>
        )}
      </div>
    </article>
  );
}

export function LiveStreams() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const retry = useCallback(() => setRefresh((value) => value + 1), []);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      const request = new AbortController();
      const abort = () => request.abort();
      controller.signal.addEventListener("abort", abort, { once: true });
      const timeout = setTimeout(abort, 15000);
      try {
        const response = await fetch("/api/public/v1/streams", {
          cache: "no-store",
          credentials: "same-origin",
          signal: request.signal,
        });
        const data = await streamResponse<Catalog>(response);
        if (!controller.signal.aborted) {
          setCatalog(data);
          setError("");
        }
      } catch (e) {
        if (!controller.signal.aborted)
          setError(
            (e as Error).name === "AbortError"
              ? "Loading streams timed out. Refresh streams to try again."
              : (e as Error).message,
          );
      } finally {
        clearTimeout(timeout);
        controller.signal.removeEventListener("abort", abort);
        if (!controller.signal.aborted) timer = setTimeout(load, 15000);
      }
    };
    void load();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [refresh]);
  if ((!catalog && !error) || (catalog && !catalog.enabled)) return null;
  return (
    <section className="df-streams" aria-label="Watch streams">
      <div className="df-section-head">
        <h2>Watch streams</h2>
        <span>In your browser</span>
      </div>
      {error && (
        <div className="df-warning df-streams-warning">
          <p role="alert">{error}</p>
          <button className="df-button" onClick={retry}>
            Refresh streams
          </button>
        </div>
      )}
      {catalog?.streams.length ? (
        <div className="df-stream-grid">
          {catalog.streams.map((stream) => (
            <StreamCard key={stream.id} stream={stream} />
          ))}
        </div>
      ) : (
        catalog && (
          <p className="df-subtitle">No public streams are configured.</p>
        )
      )}
    </section>
  );
}
