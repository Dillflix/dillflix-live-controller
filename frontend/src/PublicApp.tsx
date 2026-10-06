import { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { Clock3, Play, Plus, Radio, ShieldCheck, Tv } from "lucide-react";
import { commandId } from "./api";
import "./styles.css";

interface PublicEvent {
  content_id: string;
  title: string;
  league: string;
  phase: string;
  kind: string;
  start_time: string;
  expected_end_time: string | null;
  state: string;
  active: boolean;
  scores: (number | null)[];
  playable: boolean;
  planned: boolean;
  viewing_option_count: number;
  teams: {
    key: string;
    name: string;
    city: string;
    full_name: string;
    abbreviation: string;
    logo_url: string | null;
  }[];
}
interface PublicOverview {
  revision: number;
  viewer: { name: string; guest: boolean };
  timezone: string;
  permissions: { play_now: boolean; add_to_plan: boolean };
  actions_message: string;
  now_playing: {
    event: PublicEvent | null;
    verified: boolean;
    simulated: boolean;
    switching: boolean;
  };
  events: PublicEvent[];
}
const names: Record<string, string> = {
  "college-football": "College Football",
  pga: "Golf",
  tennis: "Tennis",
  "uefa.champions": "UEFA Champions League",
  f1: "Formula 1",
};
const leagueName = (code: string) => names[code] || code.toUpperCase();

function EventDetails({
  event,
  when,
  onClose,
}: {
  event: PublicEvent;
  when: string;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="df-dialog df-public-details"
      aria-labelledby="public-event-title"
      onClose={onClose}
    >
      <div className="df-dialog-head">
        <h2 id="public-event-title">{event.title}</h2>
        <button className="df-button" onClick={() => dialog.current?.close()}>
          Close
        </button>
      </div>
      <p>
        {leagueName(event.league)} ·{" "}
        {event.phase === "unknown" ? event.kind : event.phase}
      </p>
      <p>{when}</p>
      <p>
        {event.viewing_option_count} viewing{" "}
        {event.viewing_option_count === 1 ? "option" : "options"}
      </p>
      {event.planned && <p>In watch plan</p>}
    </dialog>
  );
}

async function publicApi<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch("/api/public/v1" + path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  if (!response.ok) {
    const value = await response.json().catch(() => null);
    throw new Error(
      typeof value?.detail === "string"
        ? value.detail
        : value?.detail?.message ||
            "Unable to load live coverage. Please try again.",
    );
  }
  return response.json();
}

function PublicApp() {
  const [data, setData] = useState<PublicOverview | null>(null);
  const [tab, setTab] = useState("live");
  const [sport, setSport] = useState("all");
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [connected, setConnected] = useState(false);
  const [busy, setBusy] = useState(false);
  const [detailsId, setDetailsId] = useState<string | null>(null);
  const loading = useRef(0);
  const submitting = useRef(false);
  const load = useCallback(async () => {
    const serial = ++loading.current;
    try {
      const next = await publicApi<PublicOverview>("/overview");
      if (serial === loading.current) {
        setData(next);
        setConnected(true);
      }
    } catch (e) {
      if (serial === loading.current) {
        setConnected(false);
        setError((e as Error).message);
      }
    }
  }, []);
  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 5000);
    return () => {
      clearInterval(timer);
      ++loading.current;
    };
  }, [load]);
  const request = async (event: PublicEvent, action: "add" | "play_now") => {
    if (!data || submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await publicApi("/watch-plan", {
        command_id: commandId(),
        expected_revision: data.revision,
        action,
        content_id: event.content_id,
      });
      setNotice(
        action === "add"
          ? `${event.title} added to the shared watch plan.`
          : `Play request saved for ${event.title}. Admin selections take priority.`,
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      await load();
      setBusy(false);
      submitting.current = false;
    }
  };
  if (!data)
    return (
      <div id="df-app">
        <main className="df-public-main">
          <h1>Dillflix Live</h1>
          <p className="df-subtitle" role={error ? "alert" : "status"}>
            {error || "Loading live coverage…"}
          </p>
          {error && (
            <button className="df-button" onClick={() => void load()}>
              Try again
            </button>
          )}
        </main>
      </div>
    );

  const time = (value: string) =>
    new Intl.DateTimeFormat(undefined, {
      hour: "numeric",
      minute: "2-digit",
      timeZone: data.timezone,
    }).format(new Date(value));
  const date = (value: string) =>
    new Intl.DateTimeFormat(undefined, {
      weekday: "short",
      month: "short",
      day: "numeric",
      timeZone: data.timezone,
    }).format(new Date(value));
  const visible = data.events.filter(
    (e) =>
      e.active &&
      !["ended", "cancelled"].includes(e.state) &&
      (tab === "live"
        ? e.state === "live" || e.playable
        : e.state !== "live" && !e.playable) &&
      (sport === "all" || e.league === sport) &&
      `${e.title} ${e.teams.map((t) => t.full_name).join(" ")}`
        .toLowerCase()
        .includes(query.toLowerCase()),
  );
  const now = data.now_playing;
  const details = data.events.find((event) => event.content_id === detailsId);
  const disabled = busy || !connected;
  return (
    <div id="df-app">
      <header className="df-header df-public-header">
        <div className="df-brand">
          dillflix<span> live</span>
        </div>
        <span className="df-footnote">
          {data.viewer.guest
            ? "Browsing as Guest"
            : `Signed in as ${data.viewer.name}`}
        </span>
      </header>
      <main className="df-public-main">
        <section className="df-playing" aria-label="Now playing">
          <div className="df-playing-copy">
            <div className="df-playing-top">
              <Radio size={15} />
              <span>
                {now.event && !now.verified ? "Last observed" : "Now playing"}
              </span>
              {now.event && (
                <span className="df-pill">
                  {now.verified
                    ? now.simulated
                      ? "Simulated live"
                      : "Live"
                    : "Unverified"}
                </span>
              )}
            </div>
            <h1 className="df-playing-title">
              {now.event?.title || "Waiting for live sports"}
            </h1>
            <p className="df-playing-detail">
              {now.switching
                ? "Switching coverage · waiting for playback confirmation"
                : "Shared live coverage on Dillflix"}
            </p>
          </div>
        </section>
        <div className="df-title-row">
          <div>
            <div className="df-eyebrow">Watch together</div>
            <h1>What's on</h1>
            <p className="df-subtitle">{data.actions_message}</p>
          </div>
        </div>
        {!connected && (
          <div className="df-warning" role="status">
            Connection interrupted. Showing the last received schedule; actions
            are unavailable until reconnected.
          </div>
        )}
        {error && (
          <div className="df-toast error" role="alert">
            {error}
            <button className="df-button" onClick={() => setError("")}>
              Dismiss
            </button>
          </div>
        )}
        {notice && (
          <div className="df-toast" role="status">
            {notice}
          </div>
        )}
        {(!data.permissions.play_now || !data.permissions.add_to_plan) && (
          <p className="df-subtitle df-public-permissions">
            {!data.permissions.play_now &&
              "Play now is disabled by the admin. "}
            {!data.permissions.add_to_plan &&
              "Add to plan is disabled by the admin."}
          </p>
        )}
        <div className="df-toolbar">
          <div className="df-segmented">
            <button
              aria-pressed={tab === "live"}
              onClick={() => setTab("live")}
            >
              Live{" "}
              {data.events.filter((e) => e.active && e.state === "live").length}
            </button>
            <button
              aria-pressed={tab === "upcoming"}
              onClick={() => setTab("upcoming")}
            >
              Upcoming
            </button>
          </div>
          <label className="df-search">
            <span className="df-screenreader">Find a team or event</span>
            <input
              type="search"
              placeholder="Find a team or event"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
        </div>
        <div className="df-filters">
          {[
            "all",
            ...Array.from(
              new Set(data.events.filter((e) => e.active).map((e) => e.league)),
            ).sort(),
          ].map((league) => (
            <button
              className="df-filter"
              key={league}
              aria-pressed={sport === league}
              onClick={() => setSport(league)}
            >
              {league === "all" ? "All sports" : leagueName(league)}
            </button>
          ))}
        </div>
        <div className="df-section-head">
          <h2>{tab === "live" ? "Live coverage" : "Coming up"}</h2>
          <span>{visible.length} events</span>
        </div>
        {visible.length ? (
          <div className="df-grid">
            {visible.map((e) => {
              const current =
                now.verified && now.event?.content_id === e.content_id;
              return (
                <article
                  className={`df-event ${current ? "is-current" : ""}`}
                  key={e.content_id}
                  data-testid={`event-${e.content_id}`}
                >
                  <div className="df-event-body">
                    <div className="df-event-top">
                      <span>{leagueName(e.league)}</span>
                      <span>·</span>
                      <span>{e.phase === "unknown" ? e.kind : e.phase}</span>
                      <span
                        className={`df-event-status ${e.state === "live" ? "df-live" : ""}`}
                      >
                        {e.state === "live"
                          ? "Live"
                          : e.state === "scheduled"
                            ? time(e.start_time)
                            : e.state}
                      </span>
                    </div>
                    {e.teams.length ? (
                      <div className="df-match">
                        {e.teams.map((team, i) => (
                          <div className="df-team" key={team.key}>
                            {team.logo_url ? (
                              <img
                                className="df-monogram"
                                src={team.logo_url}
                                alt=""
                                loading="lazy"
                                onError={(ev) => {
                                  ev.currentTarget.style.display = "none";
                                }}
                              />
                            ) : (
                              <span className="df-monogram">
                                {team.abbreviation}
                              </span>
                            )}
                            <div className="df-team-text">
                              <small>{team.city}</small>
                              <strong>{team.name || team.full_name}</strong>
                            </div>
                            {e.state === "live" && e.scores[i] != null && (
                              <span className="df-score">{e.scores[i]}</span>
                            )}
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="df-broadcast">
                        <div className="df-broadcast-mark">
                          <Tv />
                        </div>
                        <strong>{e.title}</strong>
                      </div>
                    )}
                    <div className="df-event-meta">
                      <Clock3 size={14} />
                      <span>
                        {date(e.start_time)} · {time(e.start_time)}
                        {e.expected_end_time &&
                          ` – ${time(e.expected_end_time)} est.`}
                      </span>
                    </div>
                  </div>
                  <div className="df-event-footer df-public-event-footer">
                    <span className="df-footnote">
                      {e.planned ? <ShieldCheck size={15} /> : <Tv size={15} />}
                      {current
                        ? "Now playing"
                        : e.planned
                          ? "In watch plan"
                          : `${e.viewing_option_count} viewing ${e.viewing_option_count === 1 ? "option" : "options"}`}
                    </span>
                    <div className="df-row df-public-actions">
                      {current && (
                        <button
                          className="df-button"
                          onClick={() => setDetailsId(e.content_id)}
                        >
                          Details
                        </button>
                      )}
                      {!e.planned && !current && (
                        <button
                          className="df-button"
                          disabled={disabled || !data.permissions.add_to_plan}
                          onClick={() => void request(e, "add")}
                        >
                          <Plus size={14} />
                          Add to plan
                        </button>
                      )}
                      {e.playable && !current && (
                        <button
                          className="df-button df-button-primary"
                          disabled={disabled || !data.permissions.play_now}
                          onClick={() => void request(e, "play_now")}
                        >
                          <Play size={14} />
                          Play now
                        </button>
                      )}
                    </div>
                  </div>
                </article>
              );
            })}
          </div>
        ) : (
          <div className="df-empty">
            <h3>No events match</h3>
            <p>Try another sport, or check the upcoming schedule.</p>
            <button
              className="df-button"
              onClick={() => {
                setQuery("");
                setSport("all");
              }}
            >
              Clear filters
            </button>
          </div>
        )}
        <p className="df-subtitle df-public-footer">
          Times shown in {data.timezone}. End times are estimates.
        </p>
      </main>
      {details && (
        <EventDetails
          event={details}
          when={`${date(details.start_time)} · ${time(details.start_time)}${details.expected_end_time ? ` – ${time(details.expected_end_time)} est.` : ""}`}
          onClose={() => setDetailsId(null)}
        />
      )}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<PublicApp />);
