import { useCallback, useEffect, useRef, useState } from "react";
import { api, commandId } from "./api";

type Slot = "poster" | "background";
type PlexInfo = {
  revision: number;
  enabled: boolean;
  base_url: string;
  rating_key: string;
  credential_configured: boolean;
  real_playback: boolean;
  title?: string;
  library?: string;
  version?: string;
  default_images: Partial<
    Record<Slot, { digest: string; width: number; height: number }>
  >;
  status: {
    pending: boolean;
    hold: boolean;
    in_flight: boolean;
    blocked: string | null;
    error: string | null;
    retry_at: number | null;
    fallback_at: number | null;
    last_success: string | null;
    desired: { title?: string; mode?: string; plex_title?: string };
    applied_title?: { value: string; at: string } | null;
    applied: Partial<Record<Slot, { digest: string; at: string }>>;
  };
};

export function PlexSettings({
  deviceId,
  refreshKey,
}: {
  deviceId: string;
  refreshKey: string;
}) {
  const path = `/devices/${deviceId}/plex`;
  const [info, setInfo] = useState<PlexInfo | null>(null);
  const [url, setUrl] = useState("");
  const [key, setKey] = useState("8");
  const [token, setToken] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [removeToken, setRemoveToken] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [checked, setChecked] = useState("");
  const draftRevision = useRef<number | null>(null);
  const dirty = useRef(false);
  const serial = useRef(0);

  const load = useCallback(
    async (reset = false) => {
      const ticket = ++serial.current;
      const value = await api<PlexInfo>(path);
      if (ticket !== serial.current) return;
      setInfo(value);
      if (reset || !dirty.current) {
        setUrl(value.base_url);
        setKey(value.rating_key);
        setEnabled(value.enabled);
        draftRevision.current = value.revision;
        if (reset) {
          setToken("");
          setRemoveToken(false);
          dirty.current = false;
        }
      }
    },
    [path],
  );

  useEffect(() => {
    void load().catch((e) => setError(e.message));
  }, [load, refreshKey]);
  const edit = () => {
    dirty.current = true;
    setNotice("");
  };
  const run = async (
    work: () => Promise<unknown>,
    success: string,
    reset = false,
  ) => {
    if (busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await work();
      await load(reset);
      setNotice(success);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const command = () => ({
    command_id: commandId(),
    expected_revision: info!.revision,
  });
  const connection = () => ({
    base_url: url,
    rating_key: key,
    ...(token ? { token } : {}),
  });
  const act = (action: string, message: string) =>
    run(() => api(path + "/" + action, command()), message, true);
  const upload = async (slot: Slot, file: File) => {
    if (file.size > 8 * 1024 * 1024)
      throw new Error("Default images must be at most 8 MiB.");
    const query = new URLSearchParams({
      command_id: commandId(),
      expected_revision: String(info!.revision),
    });
    const response = await fetch(`/api/v1${path}/defaults/${slot}?${query}`, {
      method: "POST",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      body: file,
    });
    if (!response.ok) {
      const body = await response.json();
      throw new Error(
        typeof body.detail === "string" ? body.detail : "Image upload failed",
      );
    }
  };
  const timestamp = (value: number) =>
    new Date(value * 1000).toLocaleTimeString();

  return (
    <section
      className="df-panel df-spacer df-plex"
      aria-label="Plex item settings"
    >
      <h2>Plex title and artwork</h2>
      <p>
        Show the verified live event's title and artwork on a Plex library item.
        The title returns to “Dillflix Live” with your default images five
        minutes after the last fresh playback confirmation.
      </p>
      {error && (
        <div role="alert" className="df-plex-error">
          {error}
        </div>
      )}
      {notice && <p role="status">{notice}</p>}
      {!info ? (
        <p>Loading Plex settings…</p>
      ) : (
        <>
          <div className="df-plex-grid">
            <label>
              Plex server URL
              <input
                type="url"
                aria-label="Plex server URL"
                placeholder="http://plex-server:32400"
                value={url}
                disabled={busy}
                onChange={(e) => {
                  edit();
                  setUrl(e.target.value);
                }}
              />
            </label>
            <label>
              Plex item ID or link
              <input
                aria-label="Plex item ID or link"
                value={key}
                disabled={busy}
                onChange={(e) => {
                  edit();
                  setKey(e.target.value);
                }}
              />
            </label>
            <label>
              Plex token
              <input
                type="password"
                aria-label="Plex token"
                autoComplete="new-password"
                value={token}
                disabled={busy}
                placeholder={
                  info.credential_configured
                    ? "Saved; leave blank to keep"
                    : "Enter token"
                }
                onChange={(e) => {
                  edit();
                  setToken(e.target.value);
                  setRemoveToken(false);
                }}
              />
            </label>
            <div className="df-plex-toggle">
              <label>
                <input
                  type="checkbox"
                  aria-label="Remove saved Plex token"
                  checked={removeToken}
                  disabled={busy || !info.credential_configured}
                  onChange={(e) => {
                    edit();
                    setRemoveToken(e.target.checked);
                    setEnabled(false);
                    setToken("");
                  }}
                />{" "}
                Remove saved token
              </label>
            </div>
          </div>
          <div className="df-setting">
            <div className="df-row-copy">
              <strong>Automatic title and artwork updates</strong>
              <p>
                {info.real_playback
                  ? "Save the connection and both defaults before enabling."
                  : "Requires real Prime Player playback; demo mode cannot update Plex."}
              </p>
            </div>
            <input
              type="checkbox"
              className="df-switch"
              aria-label="Automatic Plex title and artwork updates"
              checked={enabled}
              disabled={busy || !info.real_playback || removeToken}
              onChange={(e) => {
                edit();
                setEnabled(e.target.checked);
              }}
            />
          </div>
          <div className="df-plex-actions">
            <button
              className="df-button"
              disabled={busy || !url}
              onClick={() =>
                void run(async () => {
                  const result = await api<{
                    title: string;
                    library: string;
                    version: string;
                  }>(path + "/test", connection());
                  setChecked(
                    `${result.title} · ${result.library} · Plex ${result.version}`,
                  );
                }, "Connection and item read access verified. Title and artwork writes are checked when applied.")
              }
            >
              Test Plex connection
            </button>
            <button
              className="df-button"
              disabled={busy || !url}
              onClick={() =>
                void run(
                  () =>
                    api(
                      path,
                      {
                        ...connection(),
                        command_id: commandId(),
                        expected_revision: draftRevision.current,
                        enabled,
                        remove_token: removeToken,
                      },
                      "PUT",
                    ),
                  "Plex settings saved.",
                  true,
                )
              }
            >
              Save Plex settings
            </button>
            <button
              className="df-button"
              disabled={busy}
              onClick={() =>
                void run(() => load(true), "Saved settings loaded.")
              }
            >
              Reload saved settings
            </button>
          </div>
          {checked && (
            <p>
              Connection test: <strong>{checked}</strong>
            </p>
          )}
          {info.title && (
            <p>
              Target: <strong>{info.title}</strong> · {info.library} · Item{" "}
              {info.rating_key}
            </p>
          )}
          <h3>Default artwork</h3>
          <p>
            Upload images or save this item's current Plex artwork as defaults.
            Matchup thumbnails are landscape and are uploaded without cropping.
          </p>
          <div className="df-plex-grid">
            {(["poster", "background"] as const).map((slot) => (
              <div key={slot}>
                <label>
                  Default {slot}
                  <input
                    type="file"
                    aria-label={`Default ${slot}`}
                    accept="image/png,image/jpeg,image/webp"
                    disabled={busy}
                    onChange={(e) => {
                      const file = e.target.files?.[0];
                      if (file)
                        void run(
                          () => upload(slot, file),
                          `Default ${slot} saved.`,
                        );
                      e.target.value = "";
                    }}
                  />
                </label>
                {info.default_images[slot] && (
                  <figure>
                    <img
                      alt={`Default ${slot} preview`}
                      src={`/api/v1${path}/images/default/${slot}?v=${info.default_images[slot]!.digest}`}
                    />
                    <figcaption>
                      {info.default_images[slot]!.width} ×{" "}
                      {info.default_images[slot]!.height}
                    </figcaption>
                  </figure>
                )}
              </div>
            ))}
          </div>
          <button
            className="df-button"
            disabled={busy || !info.credential_configured || !info.base_url}
            onClick={() =>
              void act(
                "defaults/capture",
                "Current Plex artwork saved as defaults.",
              )
            }
          >
            Use current Plex artwork as defaults
          </button>
          <h3>Update status</h3>
          <p>
            {info.status.blocked
              ? "Updates suspended"
              : info.status.in_flight
                ? "Updating Plex…"
                : info.status.pending
                  ? "Update pending"
                  : info.enabled
                    ? "Title and artwork synchronized"
                    : "Automatic updates disabled"}
            {info.status.desired.title ? ` · ${info.status.desired.title}` : ""}
          </p>
          {info.status.desired.plex_title && (
            <p>Requested title: {info.status.desired.plex_title}</p>
          )}
          {info.status.applied_title && (
            <p>
              Verified title: {info.status.applied_title.value} ·{" "}
              {new Date(info.status.applied_title.at).toLocaleTimeString()}
            </p>
          )}
          {(info.status.blocked || info.status.error) && (
            <p role="status" className="df-plex-error">
              {info.status.blocked || info.status.error}
            </p>
          )}
          {info.status.retry_at && (
            <p>Retry at {timestamp(info.status.retry_at)}</p>
          )}
          {info.enabled && info.status.fallback_at && (
            <p>
              Defaults due at {timestamp(info.status.fallback_at)} unless a
              newer playback confirmation arrives.
            </p>
          )}
          {info.status.last_success && (
            <p>
              Last complete update:{" "}
              {new Date(info.status.last_success).toLocaleString()}
            </p>
          )}
          <div className="df-plex-grid">
            {(["poster", "background"] as const).map(
              (slot) =>
                info.status.applied[slot] && (
                  <figure key={slot}>
                    <img
                      alt={`Applied ${slot}`}
                      src={`/api/v1${path}/images/applied/${slot}?v=${info.status.applied[slot]!.digest}`}
                    />
                    <figcaption>
                      {slot === "poster" ? "Poster" : "Background"}: verified{" "}
                      {new Date(
                        info.status.applied[slot]!.at,
                      ).toLocaleTimeString()}
                    </figcaption>
                  </figure>
                ),
            )}
          </div>
          <div className="df-plex-actions">
            <button
              className="df-button"
              disabled={busy || !info.enabled}
              onClick={() =>
                void act(
                  "resync",
                  "Title and artwork resynchronization queued.",
                )
              }
            >
              Resync Plex now
            </button>
            <button
              className="df-button"
              disabled={busy || !(info.status.error || info.status.blocked)}
              onClick={() => void act("retry", "Plex update retry queued.")}
            >
              Retry Plex update
            </button>
            <button
              className="df-button"
              disabled={
                busy ||
                !info.real_playback ||
                !info.default_images.poster ||
                !info.default_images.background ||
                !info.credential_configured
              }
              onClick={() =>
                void act(
                  "restore-defaults",
                  "Default title and artwork queued. Automatic updates are now disabled.",
                )
              }
            >
              Restore defaults and disable
            </button>
          </div>
        </>
      )}
    </section>
  );
}
