import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  CornerUpLeft,
  Home,
  Menu,
  Play,
  Radio,
  Delete,
} from "lucide-react";
import { api, ApiError, commandId } from "./api";
import type { Device } from "./types";

type Credentials = { session_id: string; owner_token: string };
type Change = Credentials & {
  command_id: string;
  expected_revision: number;
  action: "take" | "extend" | "release";
  minutes: number;
  takeover: boolean;
  release_mode: "active" | "paused";
};
const shortcuts: Record<string, string> = {
  ArrowUp: "up",
  ArrowDown: "down",
  ArrowLeft: "left",
  ArrowRight: "right",
  Enter: "select",
  Escape: "back",
  Home: "home",
  ContextMenu: "menu",
  " ": "play_pause",
  Backspace: "backspace",
};

export function RemoteControls({
  device,
  serverTime,
  enabled,
  simulated,
  onChange,
}: {
  device: Device;
  serverTime: string;
  enabled: boolean;
  simulated: boolean;
  onChange: () => Promise<void>;
}) {
  const storage = `dillflix-remote-${device.id}`;
  const [credentials, setCredentials] = useState<Credentials | null>(() => {
    try {
      return JSON.parse(sessionStorage.getItem(storage) || "null");
    } catch {
      return null;
    }
  });
  const [minutes, setMinutes] = useState(15);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState<Change | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [connection, setConnection] = useState("disconnected");
  const [connectionDetail, setConnectionDetail] = useState("");
  const [reload, setReload] = useState(0);
  const [visible, setVisible] = useState(!document.hidden);
  const [clock, setClock] = useState(Date.now());
  const [sending, setSending] = useState(false);
  const [text, setText] = useState("");
  const socket = useRef<WebSocket | null>(null);
  const pending = useRef<{
    seq: number;
    text: boolean;
    timeout: ReturnType<typeof setTimeout>;
  } | null>(null);
  const seq = useRef(0),
    lastSent = useRef(0);
  const remote = useRef<HTMLDivElement>(null);
  const session = device.manual_control;
  const offset = useMemo(
    () => Date.parse(serverTime) - Date.now(),
    [serverTime],
  );
  const seconds = session
    ? Math.max(
        0,
        Math.ceil((Date.parse(session.expires_at) - clock - offset) / 1000),
      )
    : 0;
  const owned = !!session && session.session_id === credentials?.session_id;
  const active = owned && seconds > 0;
  const ready = active && connection === "ready" && visible && !busy;
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useEffect(() => {
    const tick = setInterval(() => setClock(Date.now()), 1000);
    const visibility = () => setVisible(!document.hidden);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      clearInterval(tick);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, []);
  useEffect(() => {
    setConfirm(false);
  }, [session?.session_id]);
  useEffect(() => {
    if (!active || !credentials || !visible) return;
    let disposed = false;
    const ws = new WebSocket(
      `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/v1/devices/${encodeURIComponent(device.id)}/control/input`,
    );
    socket.current = ws;
    seq.current = 0;
    setConnection("connecting");
    setConnectionDetail("Connecting remote…");
    const timer = setTimeout(() => {
      setConnectionDetail(
        "Remote connection timed out. Reconnect to try again.",
      );
      ws.close();
    }, 50000);
    ws.onopen = () => ws.send(JSON.stringify(credentials));
    ws.onmessage = (event) => {
      if (disposed) return;
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        ws.close();
        return;
      }
      if (message.type === "ready") {
        clearTimeout(timer);
        setConnection("ready");
        setConnectionDetail("Remote connected");
        remote.current?.focus({ preventScroll: true });
      } else if (
        message.type === "sent" &&
        pending.current &&
        message.seq === pending.current.seq
      ) {
        clearTimeout(pending.current.timeout);
        if (pending.current.text) setText("");
        pending.current = null;
        setSending(false);
      } else if (message.type === "error") {
        setConnectionDetail(message.message);
        ws.close();
      }
    };
    ws.onclose = () => {
      clearTimeout(timer);
      if (disposed) return;
      if (pending.current) {
        clearTimeout(pending.current.timeout);
        pending.current = null;
        setConnectionDetail(
          "Input delivery is uncertain. Check the TV before sending it again.",
        );
      } else {
        setConnectionDetail((previous) =>
          previous === "Remote connected" || previous === "Connecting remote…"
            ? "Remote disconnected. Automation stays suspended until the deadline."
            : previous,
        );
      }
      setSending(false);
      setConnection("disconnected");
      void onChangeRef.current();
    };
    return () => {
      disposed = true;
      clearTimeout(timer);
      if (pending.current) clearTimeout(pending.current.timeout);
      pending.current = null;
      setSending(false);
      socket.current = null;
      ws.close();
      setConnection("disconnected");
    };
  }, [active, credentials, device.id, visible, reload]);

  async function submit(change: Change) {
    setBusy(true);
    setError("");
    setRetry(null);
    try {
      await api(`/devices/${encodeURIComponent(device.id)}/control`, change);
      setConfirm(false);
    } catch (e) {
      setError((e as Error).message);
      if (!(e instanceof ApiError)) setRetry(change);
    } finally {
      await onChange();
      setBusy(false);
    }
  }
  function change(
    action: Change["action"],
    release_mode: Change["release_mode"] = "active",
  ) {
    const identity =
      action === "take"
        ? { session_id: commandId(), owner_token: commandId() + commandId() }
        : credentials;
    if (!identity) return;
    if (action === "take") {
      setCredentials(identity);
      try {
        sessionStorage.setItem(storage, JSON.stringify(identity));
      } catch {
        /* This tab still owns the session until closed. */
      }
    }
    void submit({
      ...identity,
      command_id: commandId(),
      expected_revision: device.revision,
      action,
      minutes,
      takeover: !!session,
      release_mode,
    });
  }
  function send(value: { key: string } | { text: string }) {
    const ws = socket.current;
    if (!ready || !ws || ws.readyState !== WebSocket.OPEN || pending.current)
      return;
    const id = ++seq.current;
    lastSent.current = performance.now();
    setSending(true);
    pending.current = {
      seq: id,
      text: "text" in value,
      timeout: setTimeout(() => {
        setConnectionDetail(
          "Input delivery is uncertain. Check the TV before sending it again.",
        );
        setConnection("disconnected");
        ws.close();
      }, 3000),
    };
    ws.send(JSON.stringify({ seq: id, ...value }));
  }
  function keyButton(
    key: string,
    label: string,
    icon: React.ReactNode,
    extra = "",
  ) {
    return (
      <button
        type="button"
        className={`df-remote-key ${extra}`}
        aria-label={label}
        disabled={!ready}
        onClick={() => send({ key })}
      >
        {icon}
      </button>
    );
  }
  if (!enabled && !session) return null;
  return (
    <div className="df-remote">
      <div className="df-remote-heading">
        <div>
          <h3>
            <Radio size={17} />{" "}
            {session
              ? owned
                ? "You have control"
                : "Manual control in another browser"
              : "Device remote"}
          </h3>
          <p>
            {session
              ? `${seconds >= 3600 ? `${Math.floor(seconds / 3600)}:` : ""}${seconds >= 3600 ? String(Math.floor((seconds % 3600) / 60)).padStart(2, "0") : Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")} remaining · ${session.return_mode === "active" ? "Automation resumes at expiry" : "Automation stays paused at expiry"}`
              : "Temporarily suspend automation and control the TV."}
          </p>
        </div>
        <label className="df-remote-duration">
          {owned ? "Reset timer to" : "Control for"}
          <select
            aria-label="Manual control duration"
            value={minutes}
            disabled={busy}
            onChange={(e) => setMinutes(Number(e.target.value))}
          >
            {[5, 15, 30, 60, 120, 240, 480, 720, 1440].map((n) => (
              <option key={n} value={n}>
                {n < 60
                  ? `${n} minutes`
                  : `${n / 60} ${n === 60 ? "hour" : "hours"}`}
              </option>
            ))}
          </select>
        </label>
      </div>
      {error && (
        <p className="df-remote-error" role="alert">
          {error}{" "}
          {retry && (
            <button
              className="df-button"
              disabled={busy}
              onClick={() => void submit(retry)}
            >
              Retry change
            </button>
          )}
        </p>
      )}
      {!owned && (
        <div className="df-remote-start">
          {confirm ? (
            <>
              <p>
                Taking over disconnects the other remote. The watch plan stays
                saved.
              </p>
              <button
                className="df-button df-button-primary"
                disabled={busy}
                onClick={() => change("take")}
              >
                Confirm takeover
              </button>
              <button className="df-button" onClick={() => setConfirm(false)}>
                Cancel
              </button>
            </>
          ) : (
            <button
              className="df-button df-button-primary"
              disabled={busy || !enabled}
              onClick={() => (session ? setConfirm(true) : change("take"))}
            >
              {session ? "Take over" : "Take control"}
            </button>
          )}
        </div>
      )}
      {owned && (
        <>
          <div className="df-remote-status" role="status">
            <span>
              {!visible
                ? "Remote suspended while this tab is hidden"
                : !seconds
                  ? "Control expired; updating…"
                  : connectionDetail || "Connecting remote…"}
            </span>
            {connection === "disconnected" && visible && seconds > 0 && (
              <button
                className="df-button df-button-quiet"
                onClick={() => setReload((n) => n + 1)}
              >
                Reconnect remote
              </button>
            )}
          </div>
          <div
            className="df-remote-body"
            ref={remote}
            tabIndex={0}
            role="group"
            aria-label="TV remote"
            onKeyDown={(e) => {
              if (
                e.target instanceof HTMLElement &&
                e.target.closest("input,textarea,select,[contenteditable=true]")
              )
                return;
              // Native button Enter/Space activates that button once; it must not also send Select.
              if (
                e.target instanceof HTMLElement &&
                e.target.closest("button") &&
                ["Enter", " "].includes(e.key)
              )
                return;
              if (e.ctrlKey || e.metaKey || e.altKey || !shortcuts[e.key])
                return;
              e.preventDefault();
              if (e.repeat && performance.now() - lastSent.current < 125)
                return;
              send({ key: shortcuts[e.key] });
            }}
          >
            <div className="df-remote-pad">
              {keyButton("up", "Up", <ArrowUp />, "df-key-up")}
              {keyButton("left", "Left", <ArrowLeft />, "df-key-left")}
              {keyButton("select", "Select", "OK", "df-key-select")}
              {keyButton("right", "Right", <ArrowRight />, "df-key-right")}
              {keyButton("down", "Down", <ArrowDown />, "df-key-down")}
            </div>
            <div className="df-remote-extras">
              <div className="df-remote-shortcuts">
                {keyButton(
                  "back",
                  "Back",
                  <>
                    <CornerUpLeft size={19} />
                    <span>Back</span>
                  </>,
                )}
                {keyButton(
                  "home",
                  "Home",
                  <>
                    <Home size={19} />
                    <span>Home</span>
                  </>,
                )}
                {keyButton(
                  "menu",
                  "Menu",
                  <>
                    <Menu size={19} />
                    <span>Menu</span>
                  </>,
                )}
                {keyButton(
                  "play_pause",
                  "Play or pause device",
                  <>
                    <Play size={19} />
                    <span>Play / pause</span>
                  </>,
                )}
                {keyButton(
                  "backspace",
                  "Delete character",
                  <>
                    <Delete size={19} />
                    <span>Delete</span>
                  </>,
                )}
              </div>
              <form
                className="df-remote-text"
                onSubmit={(e) => {
                  e.preventDefault();
                  send({ text });
                }}
              >
                <label htmlFor="remote-text">Send text to the TV</label>
                <div>
                  <input
                    id="remote-text"
                    value={text}
                    maxLength={200}
                    disabled={sending}
                    pattern="[ -~]+"
                    onChange={(e) => setText(e.target.value)}
                    placeholder="Focus a search field on the TV first"
                    autoComplete="off"
                  />
                  <button
                    className="df-button"
                    disabled={
                      !ready || sending || !text || !/^[ -~]+$/.test(text)
                    }
                  >
                    Send
                  </button>
                </div>
                <small>
                  English letters, numbers and punctuation. Text is not saved.
                </small>
              </form>
              <p className="df-remote-help">
                With the remote focused: arrows to move, Enter to select, Esc to
                go back. Inputs are sent directly to the device.
              </p>
            </div>
          </div>
          <div className="df-remote-end">
            <button
              className="df-button"
              disabled={busy || !seconds}
              onClick={() => change("extend")}
            >
              Extend control
            </button>
            <button
              className="df-button df-button-primary"
              disabled={busy || !seconds}
              onClick={() => change("release", "active")}
            >
              Resume automation
            </button>
            <button
              className="df-button df-button-quiet"
              disabled={busy || !seconds}
              onClick={() => change("release", "paused")}
            >
              End control and stay paused
            </button>
          </div>
          <p className="df-remote-help">
            Your watch plan is saved. These remote inputs control the TV.
            {simulated ? " Automated event playback remains simulated." : ""}
          </p>
          {device.manual_control?.input_ready === false && (
            <p role="status">Waiting for playback to stop before enabling the remote.</p>
          )}
        </>
      )}
    </div>
  );
}
