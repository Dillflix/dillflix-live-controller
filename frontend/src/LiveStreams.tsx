import { useCallback, useEffect, useRef, useState } from "react";
import { Play, Radio, RefreshCw, Square } from "lucide-react";
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
  const [tier, setTier] = useState(stream.default_tier);
  const [watching, setWatching] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<StreamPlayerState>("connecting");
  const [detail, setDetail] = useState("");
  const video = useRef<HTMLVideoElement>(null);
  const player = useRef<ReturnType<typeof startLiveStream> | null>(null);
  const selected = stream.tiers.find((option) => option.id === tier);

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
    );
    player.current = current;
    return () => {
      current.dispose();
      player.current = null;
    };
  }, [watching, stream.id, tier, attempt]);
  useEffect(() => {
    const hide = () => {
      if (!player.current) return;
      player.current?.dispose();
      setWatching(false);
      setDetail(
        "Stopped while this page was hidden. Choose Watch stream to resume.",
      );
    };
    const visibility = () => {
      if (document.hidden) hide();
    };
    document.addEventListener("visibilitychange", visibility);
    window.addEventListener("pagehide", hide);
    return () => {
      document.removeEventListener("visibilitychange", visibility);
      window.removeEventListener("pagehide", hide);
    };
  }, []);
  const watch = () => {
    setDetail("");
    setAttempt((value) => value + 1);
    setWatching(true);
  };
  const stop = () => {
    setWatching(false);
    setDetail("Stopped in this browser.");
  };
  return (
    <article
      className="df-stream-card"
      aria-label={stream.label}
      data-testid={`stream-${stream.id}`}
    >
      <div className="df-stream-head">
        <h3>{stream.label}</h3>
        <span
          className={`df-event-status ${stream.status === "live" ? "df-live" : ""}`}
        >
          {availability[stream.status]}
        </span>
      </div>
      {watching && (
        <video
          ref={video}
          className="df-stream-video"
          aria-label={`${stream.label} video`}
          controls
          playsInline
          muted
          preload="none"
        />
      )}
      <div className="df-stream-controls">
        <label className="df-stream-quality">
          <span>Quality</span>
          <select
            aria-label={`${stream.label} quality`}
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
        {!watching ? (
          <button
            className="df-button df-button-primary"
            onClick={watch}
            disabled={
              stream.status === "offline" ||
              !selected ||
              selected.status === "offline"
            }
          >
            <Play size={15} />
            Watch stream
          </button>
        ) : (
          <>
            {state === "error" ? (
              <button className="df-button df-button-primary" onClick={watch}>
                <RefreshCw size={15} />
                Retry stream
              </button>
            ) : (
              <button
                className="df-button"
                disabled={state === "connecting"}
                onClick={() => player.current?.goLive()}
              >
                <Radio size={15} />
                Go live
              </button>
            )}
            <button className="df-button" onClick={stop}>
              <Square size={15} />
              Stop watching
            </button>
          </>
        )}
      </div>
      <p
        className={`df-stream-detail ${watching && state === "error" ? "df-stream-error" : ""}`}
        role={watching && state === "error" ? "alert" : undefined}
        aria-live="polite"
      >
        {detail ||
          (stream.status === "offline"
            ? "This feed is currently offline."
            : "Watch here with sound off initially. Enable sound in the video controls.")}
      </p>
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
      <p className="df-subtitle df-streams-intro">
        Choose a feed to watch. Play now below changes the shared coverage.
      </p>
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
