import { useEffect, useRef, useState } from "react";
import {
  ChevronDown,
  ChevronUp,
  Maximize2,
  Monitor,
  RefreshCw,
} from "lucide-react";
import { api } from "./api";
import { startScreenPlayer, type ScreenState } from "./screenPlayer";

type ScreenConfig = { enabled: boolean; stream_path: string | null };

export function ScreenPanel({
  deviceId,
  deviceName,
}: {
  deviceId: string;
  deviceName: string;
}) {
  const [config, setConfig] = useState<ScreenConfig | null>(null);
  const [configError, setConfigError] = useState(false);
  const [reload, setReload] = useState(0);
  const [open, setOpen] = useState(() => {
    try {
      return localStorage.getItem("dillflix-screen-open") !== "false";
    } catch {
      return true;
    }
  });
  const [visible, setVisible] = useState(!document.hidden);
  const [state, setState] = useState<ScreenState>("connecting");
  const [detail, setDetail] = useState("Connecting to the device screen…");
  const video = useRef<HTMLVideoElement>(null);
  const stage = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let active = true;
    api<ScreenConfig>(`/devices/${encodeURIComponent(deviceId)}/screen`)
      .then((data) => {
        if (active) {
          setConfig(data);
          setConfigError(false);
        }
      })
      .catch(() => {
        if (active) setConfigError(true);
      });
    return () => {
      active = false;
    };
  }, [deviceId, reload]);
  useEffect(() => {
    const change = () => setVisible(!document.hidden);
    document.addEventListener("visibilitychange", change);
    return () => document.removeEventListener("visibilitychange", change);
  }, []);
  useEffect(() => {
    if (
      !open ||
      !visible ||
      !config?.enabled ||
      !config.stream_path ||
      !video.current
    )
      return;
    return startScreenPlayer(
      video.current,
      config.stream_path,
      (next, message) => {
        setState(next);
        setDetail(message);
      },
    );
  }, [open, visible, config, reload]);

  const expanded = open && config?.enabled;
  function toggle() {
    setOpen(!open);
    try {
      localStorage.setItem("dillflix-screen-open", String(!open));
    } catch {
      /* Storage can be disabled. */
    }
  }
  async function fullscreen() {
    const element = video.current as
      (HTMLVideoElement & { webkitEnterFullscreen?: () => void }) | null;
    try {
      if (stage.current?.requestFullscreen)
        await stage.current.requestFullscreen();
      else if (element?.webkitEnterFullscreen) element.webkitEnterFullscreen();
      else setDetail("Fullscreen is unavailable in this browser.");
    } catch {
      setDetail("Fullscreen is unavailable in this browser.");
    }
  }
  return (
    <section
      className={`df-screen${expanded ? " df-screen-open" : ""}`}
      aria-label="Device screen"
    >
      <div className="df-screen-header">
        <div className="df-screen-title">
          <Monitor size={19} />
          <div>
            <h2>Device screen</h2>
            <p>
              {configError
                ? "Screen connection unavailable"
                : !config
                  ? "Checking connection…"
                  : config.enabled
                    ? deviceName
                    : "Connect a device to enable live mirroring"}
            </p>
          </div>
        </div>
        <div className="df-screen-actions">
          {expanded && (
            <span
              className={`df-screen-state ${state === "live" && visible ? "is-live" : ""}`}
              data-testid="screen-state"
            >
              {!visible
                ? "Suspended"
                : state === "live"
                  ? "Live screen"
                  : state === "reconnecting"
                    ? "Reconnecting"
                    : state === "unsupported"
                      ? "Unsupported"
                      : state === "play_required"
                        ? "Ready"
                        : "Connecting"}
            </span>
          )}
          {configError && (
            <button
              className="df-button df-button-quiet"
              onClick={() => setReload((value) => value + 1)}
            >
              Retry
            </button>
          )}
          {config?.enabled && (
            <button
              className="df-button df-button-quiet"
              onClick={toggle}
              aria-expanded={!!expanded}
              aria-controls="device-screen-player"
            >
              {expanded ? <ChevronUp size={17} /> : <ChevronDown size={17} />}
              {expanded ? "Hide screen" : "Show screen"}
            </button>
          )}
        </div>
      </div>
      {expanded && (
        <div id="device-screen-player">
          <div className="df-screen-stage" ref={stage}>
            <video
              ref={video}
              autoPlay
              muted
              playsInline
              disablePictureInPicture
              disableRemotePlayback
              aria-label={`${deviceName} live screen`}
            />
            {(state !== "live" || !visible) && (
              <div className="df-screen-overlay">
                <Monitor size={32} />
                <p>
                  {!visible
                    ? "Screen viewing resumes when you return."
                    : detail}
                </p>
                {state === "play_required" && (
                  <button
                    className="df-button"
                    onClick={() =>
                      video.current
                        ?.play()
                        .catch(() =>
                          setDetail(
                            "This browser is still blocking video playback.",
                          ),
                        )
                    }
                  >
                    Resume screen
                  </button>
                )}
              </div>
            )}
          </div>
          <div className="df-screen-footer">
            <p>
              {state === "live"
                ? "Video only · Viewing stops when you hide this screen"
                : "Your watch plan continues independently of this screen view"}
            </p>
            <div className="df-screen-actions">
              <button
                className="df-button df-button-quiet"
                onClick={() => setReload((value) => value + 1)}
                aria-label="Reconnect screen"
              >
                <RefreshCw size={17} />
              </button>
              <button
                className="df-button df-button-quiet"
                onClick={fullscreen}
                aria-label="Fullscreen screen"
                disabled={state !== "live"}
              >
                <Maximize2 size={17} />
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
