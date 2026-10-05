import React, {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  ArrowDown,
  ArrowUp,
  CalendarCheck,
  CalendarDays,
  CheckCheck,
  ChevronDown,
  ChevronUp,
  Clock3,
  FlaskConical,
  Info,
  LayoutGrid,
  ListFilter,
  LoaderCircle,
  Pause,
  Pencil,
  Play,
  Plus,
  Radio,
  Settings2,
  ShieldCheck,
  Trash2,
  Tv,
  Users,
  Undo2,
  X,
} from "lucide-react";
import { api, ApiError, commandId } from "./api";
import { TeamRanking } from "./TeamRanking";
import { ConfigurationTools } from "./ConfigurationTools";
import { DiagnosticsTools } from "./DiagnosticsTools";
import { ScreenPanel } from "./ScreenPanel";
import type {
  Action,
  ConfigurationDocument,
  Content,
  ImportPreview,
  Overview,
  Preferences,
  Preview,
  Rule,
  SimulationResult,
  Team,
} from "./types";
import "./styles.css";

const navs = [
  ["events", "Events", LayoutGrid],
  ["plan", "Watch plan", CalendarDays],
  ["rules", "Priorities", ListFilter],
  ["activity", "Activity", Activity],
  ["settings", "Settings", Settings2],
] as const;
type View = (typeof navs)[number][0];
type Modal =
  | { type: "details"; id: string }
  | {
      type: "conflict";
      id: string;
      priority: "first" | "last";
      preview: Preview;
    }
  | { type: "rule"; rule: Rule; revision: number }
  | { type: "import"; document: ConfigurationDocument; preview: ImportPreview }
  | { type: "teams" }
  | { type: "simulate"; result: SimulationResult }
  | { type: "playback" }
  | null;
const leagueLabels: Record<string, string> = {
  "college-football": "College Football",
  pga: "Golf",
  tennis: "Tennis",
  "uefa.champions": "UEFA Champions League",
  f1: "Formula 1",
};
const leagueName = (code: string) => leagueLabels[code] || code.toUpperCase();
const initialRule = (): Rule => ({
  id: commandId(),
  name: "",
  enabled: true,
  league: "all",
  phase: "any",
  team_id: null,
  source: null,
  kind: null,
});

function Button({
  children,
  primary = false,
  quiet = false,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  primary?: boolean;
  quiet?: boolean;
}) {
  return (
    <button
      type="button"
      {...props}
      className={`df-button ${primary ? "df-button-primary" : ""} ${quiet ? "df-button-quiet" : ""} ${props.className || ""}`}
    >
      {children}
    </button>
  );
}
function Pill({
  children,
  protected: protectedEvent = false,
}: React.PropsWithChildren<{ protected?: boolean }>) {
  return (
    <span className={`df-pill ${protectedEvent ? "protected" : ""}`}>
      {protectedEvent && <ShieldCheck size={14} />} {children}
    </span>
  );
}
function Dialog({
  title,
  onClose,
  children,
}: React.PropsWithChildren<{ title: string; onClose: () => void }>) {
  const ref = useRef<HTMLElement>(null);
  // Install keyboard handling and focus before the dialog becomes interactive.
  useLayoutEffect(() => {
    const previous = document.activeElement as HTMLElement;
    ref.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "Tab") {
        const els = Array.from(
          ref.current?.querySelectorAll<HTMLElement>(
            "button:not(:disabled),input,select,a[href]",
          ) || [],
        );
        const first = els[0],
          last = els[els.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last?.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first?.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previous?.focus();
    };
  }, [onClose]);
  return (
    <div
      className="df-overlay"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <section
        className="df-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="dialog-title"
        ref={ref}
      >
        <div className="df-dialog-head">
          <h2 id="dialog-title">{title}</h2>
          <Button quiet aria-label="Close dialog" onClick={onClose}>
            <X size={18} />
          </Button>
        </div>
        {children}
      </section>
    </div>
  );
}
function Timeline({
  preview,
  find,
  time,
}: {
  preview: Preview;
  find: (id: string | null) => Content | undefined;
  time: (s: string | null) => string;
}) {
  return (
    <>
      <div className="df-timeline">
        {preview.segments.slice(0, 6).map((s, i) => (
          <div className="df-time-entry" key={i}>
            <time>{time(s.start)}</time>
            <div className={`df-time-label ${s.content_id ? "manual" : ""}`}>
              <strong>
                {find(s.content_id)?.title || "Automatic live selection"}
              </strong>
              <small>Until {time(s.end)} · estimated</small>
            </div>
          </div>
        ))}
      </div>
      <p className="df-subtitle">{preview.note}</p>
      {preview.unknown_timing.length > 0 && (
        <p className="df-subtitle">
          Some events have unknown end times; their overlaps cannot yet be fully
          previewed.
        </p>
      )}
    </>
  );
}
function RuleForm({
  rule,
  teams,
  leagues,
  onSave,
  onDelete,
  busy,
}: {
  rule: Rule;
  teams: Team[];
  leagues: string[];
  onSave: (r: Rule) => void;
  onDelete?: () => void;
  busy: boolean;
}) {
  const [draft, setDraft] = useState(rule);
  const patch = (part: Partial<Rule>) => setDraft((d) => ({ ...d, ...part }));
  const eligibleTeams = teams.filter(
    (t) => draft.league === "all" || t.league === draft.league,
  );
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSave({ ...draft, name: draft.name.trim() });
      }}
    >
      <label className="df-field">
        Name
        <input
          required
          maxLength={100}
          value={draft.name}
          onChange={(e) => patch({ name: e.target.value })}
        />
      </label>
      <label className="df-field">
        League
        <select
          value={draft.league}
          onChange={(e) =>
            patch({
              league: e.target.value,
              team_id:
                e.target.value === "all" ||
                teams.some(
                  (t) => t.key === draft.team_id && t.league === e.target.value,
                )
                  ? draft.team_id
                  : null,
            })
          }
        >
          <option value="all">All sports</option>
          {leagues.map((l) => (
            <option key={l} value={l}>
              {leagueName(l)}
            </option>
          ))}
        </select>
      </label>
      <div className="df-row">
        <label className="df-field" style={{ flex: 1 }}>
          Stage
          <select
            value={draft.phase}
            onChange={(e) => patch({ phase: e.target.value })}
          >
            {["any", "regular", "playoffs", "final_round"].map((s) => (
              <option key={s} value={s}>
                {s.replaceAll("_", " ")}
              </option>
            ))}
          </select>
        </label>
        <label className="df-field" style={{ flex: 1 }}>
          Team
          <select
            value={draft.team_id || ""}
            onChange={(e) => patch({ team_id: e.target.value || null })}
          >
            <option value="">Any team</option>
            {draft.team_id &&
              !eligibleTeams.some((t) => t.key === draft.team_id) && (
                <option value={draft.team_id}>
                  {draft.team_id} (saved team)
                </option>
              )}
            {eligibleTeams.map((t) => (
              <option value={t.key} key={t.key}>
                {t.full_name}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label className="df-field">
        Coverage source
        <select
          value={draft.source || ""}
          onChange={(e) => patch({ source: e.target.value || null })}
        >
          <option value="">Any source</option>
          <option value="nfl_redzone">NFL RedZone</option>
          <option value="golf">Golf coverage</option>
          <option value="dazn_tennis">Tennis coverage · DAZN</option>
          <option value="games">Games</option>
          <option value="special_events">Special events</option>
        </select>
      </label>
      <label className="df-field">
        Content type
        <select
          value={draft.kind || ""}
          onChange={(e) =>
            patch({ kind: (e.target.value || null) as Rule["kind"] })
          }
        >
          <option value="">Any content type</option>
          <option value="event">Event</option>
          <option value="session">Session</option>
          <option value="broadcast">Broadcast</option>
        </select>
      </label>
      <label className="df-row">
        <input
          className="df-switch"
          type="checkbox"
          checked={draft.enabled}
          onChange={(e) => patch({ enabled: e.target.checked })}
        />
        Enabled
      </label>
      <p className="df-subtitle">
        Every selected condition must match. Team identity is provided by the
        schedule source.
      </p>
      <div className="df-dialog-actions">
        {onDelete && (
          <Button onClick={onDelete} disabled={busy}>
            Delete rule
          </Button>
        )}
        <button
          className="df-button df-button-primary"
          type="submit"
          disabled={busy}
        >
          Save priority
        </button>
      </div>
    </form>
  );
}

function App() {
  const [data, setData] = useState<Overview | null>(null),
    [view, setView] = useState<View>("events"),
    [tab, setTab] = useState("live"),
    [sport, setSport] = useState("all"),
    [query, setQuery] = useState("");
  const [modal, setModal] = useState<Modal>(null),
    [notice, setNotice] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [connected, setConnected] = useState(true);
  const fetchVersion = useRef(0);
  const load = useCallback(async () => {
    const serial = ++fetchVersion.current;
    try {
      const next = await api<Overview>("/overview");
      if (serial === fetchVersion.current) {
        setData(next);
        setConnected(true);
      }
    } catch (e) {
      if (serial === fetchVersion.current) {
        setConnected(false);
        setError((e as Error).message);
      }
    }
  }, []);
  useEffect(() => {
    void load();
    const source = new EventSource("/api/v1/updates");
    source.addEventListener("update", () => void load());
    source.onerror = () => setConnected(false);
    const poll = setInterval(() => void load(), 10000);
    return () => {
      source.close();
      clearInterval(poll);
    };
  }, [load]);
  const close = useCallback(() => setModal(null), []);
  const mutate = async (
    fn: () => Promise<unknown>,
    message: string,
    closeAfter = false,
  ) => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await fn();
      setNotice(message);
      if (closeAfter) setModal(null);
      await load();
    } catch (e) {
      setError((e as Error).message);
      if (e instanceof ApiError && e.status === 409) await load();
    } finally {
      setBusy(false);
    }
  };
  if (!data)
    return (
      <div id="df-app">
        <main className="df-main">
          <h1>Dillflix</h1>
          <p className="df-subtitle">
            {error || "Loading your sports controller…"}
          </p>
          {error && <Button onClick={() => void load()}>Try again</Button>}
        </main>
      </div>
    );
  const d = data.device,
    devicePath = `/devices/${d.id}`,
    find = (id: string | null) => data.events.find((e) => e.content_id === id);
  const time = (s: string | null) =>
    s
      ? new Intl.DateTimeFormat(undefined, {
          hour: "numeric",
          minute: "2-digit",
          timeZone: d.preferences.timezone,
        }).format(new Date(s))
      : "Time TBD";
  const date = (s: string) =>
    new Intl.DateTimeFormat(undefined, {
      weekday: "short",
      month: "short",
      day: "numeric",
      timeZone: d.preferences.timezone,
    }).format(new Date(s));
  const timelineTime = (s: string | null) =>
    s && date(s) !== date(data.meta.now) ? `${date(s)}, ${time(s)}` : time(s);
  const monitoringTime = (s: string) =>
    new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
      timeZone: d.preferences.timezone,
    }).format(new Date(s));
  const allTeams = data.teams;
  const leagues = [
    ...new Set([
      ...data.events.map((e) => e.league),
      ...allTeams.map((t) => t.league),
      ...Object.keys(d.team_ranks),
      ...d.rules.map((r) => r.league).filter((l) => l !== "all"),
      ...Object.keys(data.meta.league_choices),
      ...d.preferences.discovery_leagues,
      "pga",
      "tennis",
    ]),
  ].sort();
  const observed = find(d.observed?.content_id || null),
    desired = find(d.desired),
    protectedEvent = d.plan.some((p) => p.content_id === observed?.content_id);
  const currentEvent = d.playback_state === "navigating" ? desired : observed;
  const currentEntry = d.plan.find(
    (p) => p.content_id === currentEvent?.content_id,
  );
  const completeEvent = (event: Content) =>
    void mutate(
      () =>
        api(devicePath + "/completions", {
          command_id: commandId(),
          expected_revision: d.revision,
          content_id: event.content_id,
        }),
      `${event.title} marked finished. Use Undo last edit to reverse this.`,
      true,
    );
  const playbackOffline = d.executor_health?.state === "offline";
  const playbackSimulator = data.meta.playback_adapter === "simulator";
  const observedSimulated = d.observed?.simulated ?? playbackSimulator;
  const recoveryWaiting =
    d.playback_state === "unverified" &&
    d.recovery?.content_id === d.observed?.content_id &&
    !!d.recovery?.retry_after;
  const requestPurpose = data.playback_job
    ? {
        selection: "Event selection",
        route_handoff: "Updated coverage for the same event",
        recovery: "Live playback recovery",
      }[data.playback_job.purpose]
    : null;
  const observedOption = observed?.viewing_options.find(
    (option) => option.id === d.observed?.viewing_option_id,
  );
  const planCommand = (action: Action, expectedRevision = d.revision) =>
    api(devicePath + "/watch-plan", {
      command_id: commandId(),
      expected_revision: expectedRevision,
      action,
    });
  const saveRules = (
    rules: Rule[],
    teamRanks = d.team_ranks,
    preferences = d.preferences,
    expectedRevision = d.revision,
  ) =>
    api(
      devicePath + "/rules",
      {
        command_id: commandId(),
        expected_revision: expectedRevision,
        rules,
        team_ranks: teamRanks,
        preferences,
      },
      "PUT",
    );
  const togglePause = () =>
    void mutate(
      () =>
        api(devicePath + "/automation", {
          command_id: commandId(),
          expected_revision: d.revision,
          mode: d.automation === "active" ? "paused" : "active",
        }),
      d.automation === "active"
        ? "Automation paused. Your watch plan is saved."
        : "Automation resumed.",
    );
  const play = (e: Content) =>
    void mutate(
      () => planCommand({ type: "play_now", content_id: e.content_id }),
      "Play now requested. This event is protected.",
      true,
    );
  const add = async (e: Content) => {
    if (busy) return;
    if (e.watch_entry_id) {
      setModal({ type: "details", id: e.content_id });
      return;
    }
    setBusy(true);
    setError("");
    try {
      const preview = await api<Preview>(devicePath + "/watch-plan/preview", {
        command_id: commandId(),
        expected_revision: d.revision,
        action: { type: "add", content_id: e.content_id },
      });
      if (preview.conflicts.some((pair) => pair.includes(e.content_id))) {
        setModal({
          type: "conflict",
          id: e.content_id,
          priority: "last",
          preview,
        });
      } else {
        await planCommand({ type: "add", content_id: e.content_id });
        setNotice("Added to your watch plan.");
        setModal(null);
        await load();
      }
    } catch (e) {
      setError((e as Error).message);
      await load();
    } finally {
      setBusy(false);
    }
  };
  const remove = (entryId: string) =>
    void mutate(
      () => planCommand({ type: "remove", entry_id: entryId }),
      "Removed from your watch plan.",
      true,
    );
  const changeConflict = (priority: "first" | "last") => {
    if (modal?.type !== "conflict") return;
    const current = modal;
    void mutate(async () => {
      const preview = await api<Preview>(devicePath + "/watch-plan/preview", {
        command_id: commandId(),
        expected_revision: d.revision,
        action: { type: "add", content_id: current.id, priority },
      });
      setModal({ ...current, priority, preview });
    }, "Overlap preview updated.");
  };
  const movePlan = (index: number, delta: number) => {
    const order = d.plan.map((p) => p.id);
    [order[index], order[index + delta]] = [order[index + delta], order[index]];
    void mutate(
      () => planCommand({ type: "reorder", ordered_entry_ids: order }),
      "Watch plan order saved.",
    );
  };
  const moveRule = (index: number, delta: number) => {
    const rules = d.rules.slice();
    [rules[index], rules[index + delta]] = [rules[index + delta], rules[index]];
    void mutate(() => saveRules(rules), "Priority order saved.");
  };
  const menu = (mobile = false) =>
    navs.map(([key, label, Icon]) => (
      <button
        type="button"
        key={key}
        className="df-nav"
        aria-current={view === key ? "page" : undefined}
        onClick={() => setView(key)}
      >
        <Icon size={18} />
        <span>{label}</span>
        {key === "plan" && !mobile && (
          <span className="df-count">{d.plan.length}</span>
        )}
      </button>
    ));
  const card = (e: Content) => {
    const live = e.lifecycle.state === "live",
      current =
        d.observed?.content_id === e.content_id &&
        d.playback_state === "verified";
    const fail =
      e.failure &&
      new Date(e.failure.retry_after) > new Date(data.meta.server_time);
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
            <span>
              {e.phase === "unknown" ? e.kind : e.phase.replaceAll("_", " ")}
            </span>
            <span className={`df-event-status ${live ? "df-live" : ""}`}>
              {fail
                ? "Retry pending"
                : live
                  ? "Live"
                  : e.lifecycle.state === "scheduled"
                    ? time(e.start_time)
                    : e.lifecycle.state}
            </span>
          </div>
          {e.teams.length ? (
            <div className="df-match">
              {e.teams.map((t, i) => (
                <div className="df-team" key={t.key}>
                  {t.logo_url ? (
                    <img
                      className="df-monogram"
                      src={t.logo_url}
                      alt=""
                      loading="lazy"
                      onError={(ev) => {
                        ev.currentTarget.style.display = "none";
                      }}
                    />
                  ) : (
                    <span className="df-monogram">{t.abbreviation}</span>
                  )}
                  <div className="df-team-text">
                    <small>{t.city}</small>
                    <strong>{t.name || t.full_name}</strong>
                  </div>
                  {live && e.scores[i] != null && (
                    <span className="df-score">{e.scores[i]}</span>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <div className="df-broadcast">
              <div className="df-broadcast-mark">
                {e.source === "nfl_redzone"
                  ? "RZ"
                  : leagueName(e.league).slice(0, 3)}
              </div>
              <div>
                <strong>{e.title}</strong>
                <p>Live broadcast coverage</p>
              </div>
            </div>
          )}
          <div className="df-event-meta">
            <Clock3 size={14} />
            <span>
              {date(e.start_time)} · {time(e.start_time)}
              {e.expected_end_time && ` – ${time(e.expected_end_time)} est.`}
            </span>
          </div>
        </div>
        <div className="df-event-footer">
          <span className="df-footnote">
            {e.watch_entry_id ? <ShieldCheck size={15} /> : <Tv size={15} />}{" "}
            {current
              ? observedSimulated
                ? "Simulated playback"
                : "Live playback"
              : e.watch_entry_id
                ? "In watch plan"
                : e.viewing_options.length
                  ? `${e.viewing_options.length} viewing ${e.viewing_options.length === 1 ? "option" : "options"}`
                  : "Route needed"}
          </span>
          <div className="df-row" style={{ gap: 6 }}>
            <Button
              quiet
              aria-label={`Details for ${e.title}`}
              onClick={() => setModal({ type: "details", id: e.content_id })}
            >
              <Info size={16} />
            </Button>
            <Button
              primary={e.playable && !current}
              disabled={
                busy ||
                (!!d.manual_control && e.playable && !current) ||
                ["ended", "cancelled"].includes(e.lifecycle.state)
              }
              onClick={() =>
                current
                  ? setModal({ type: "details", id: e.content_id })
                  : e.playable
                    ? play(e)
                    : void add(e)
              }
            >
              {current ? (
                "Details"
              ) : e.playable ? (
                <>
                  <Play size={14} />
                  Play now
                </>
              ) : e.watch_entry_id ? (
                "Planned"
              ) : (
                <>
                  <Plus size={14} />
                  Add to plan
                </>
              )}
            </Button>
          </div>
        </div>
      </article>
    );
  };
  const heading = (
    eyebrow: string,
    title: string,
    subtitle: string,
    action?: React.ReactNode,
  ) => (
    <div className="df-title-row">
      <div>
        <div className="df-eyebrow">{eyebrow}</div>
        <h1>{title}</h1>
        <p className="df-subtitle">{subtitle}</p>
      </div>
      {action}
    </div>
  );
  const onNow = (e: Content) =>
    e.lifecycle.state === "live" ||
    (["unknown", "delayed", "suspended", "scheduled"].includes(
      e.lifecycle.state,
    ) &&
      new Date(e.start_time) <= new Date(data.meta.now));
  const visible = data.events.filter(
    (e) =>
      (sport === "all" || e.league === sport) &&
      (!query ||
        (e.title + " " + e.teams.map((t) => t.full_name).join(" "))
          .toLowerCase()
          .includes(query.toLowerCase())) &&
      !["ended", "cancelled"].includes(e.lifecycle.state) &&
      (tab === "live" ? onNow(e) : !onNow(e)),
  );
  const browse = () => {
    setView("events");
    setTab("upcoming");
    setSport("all");
    setQuery("");
  };
  const preference = (p: Partial<Preferences>) =>
    void mutate(
      () => saveRules(d.rules, d.team_ranks, { ...d.preferences, ...p }),
      "Settings saved.",
    );
  const modalEvent = modal && "id" in modal ? find(modal.id) : undefined;
  return (
    <div id="df-app">
      <div inert={!!modal}>
        <header className="df-header">
          <div className="df-brand">
            <span className="df-brand-mark" />
            dillflix
          </div>
          <span className="df-header-note">Live sports</span>
          <div className="df-header-right">
            <span className="df-header-device">{d.name}</span>
            <span className="df-preview">
              {data.meta.mode === "demo"
                ? "Demo · simulated"
                : data.meta.playback_adapter === "simulator"
                  ? "Teamarr · simulated playback"
                  : "Teamarr · Prime Video"}
            </span>
          </div>
        </header>
        <div className="df-app">
          <aside className="df-sidebar" aria-label="Main navigation">
            {menu()}
            <div className="df-side-footer">
              <div className="df-device">
                <Tv size={17} />
                {d.name}
              </div>
              <span>
                {data.meta.playback_adapter === "simulator"
                  ? "Playback simulator"
                  : "Prime Video playback"}
              </span>
            </div>
          </aside>
          <main className="df-main">
            <section className="df-playing" aria-label="Now playing">
              <div>
                <div className="df-playing-top">
                  {d.playback_state === "navigating" ? (
                    <LoaderCircle size={15} />
                  ) : (
                    <Radio size={15} />
                  )}
                  <span>
                    {d.manual_control
                      ? "Manual device control"
                      : d.automation === "paused"
                        ? "Automation paused"
                        : d.playback_state === "navigating"
                          ? "Switching live coverage"
                          : d.playback_state === "unverified"
                            ? "Last observed"
                            : "Now playing"}
                  </span>
                  <Pill>
                    {d.playback_state === "verified"
                      ? observedSimulated
                        ? "Simulated live"
                        : "Verified live"
                      : d.playback_state}
                  </Pill>
                </div>
                <div className="df-playing-title">
                  {(d.playback_state === "navigating" ? desired : observed)
                    ?.title ||
                    (d.manual_control
                      ? "You choose what’s on TV"
                      : "Waiting for live sports")}
                  {protectedEvent && <Pill protected>Protected</Pill>}
                  {currentEntry && (
                    <Pill>
                      {currentEntry.actor?.type === "user"
                        ? `User: ${currentEntry.actor.name}`
                        : `Admin: ${currentEntry.actor?.name || "Admin"}`}
                    </Pill>
                  )}
                </div>
                <div className="df-playing-detail">
                  {d.manual_control
                    ? "Use the device remote below. Your watch plan is saved."
                    : d.automation === "paused"
                      ? "Your watch plan is saved. Resume when ready."
                      : d.reason}
                </div>
                {data.playback_job?.state === "pending" && !playbackOffline && (
                  <div className="df-playing-detail" aria-live="polite">
                    {data.playback_job.purpose === "route_handoff"
                      ? "Updating coverage for the same event"
                      : `Request ${data.playback_job.progress || "queued"}`}{" "}
                    · waiting for live verification
                  </div>
                )}
                {d.playback_state === "unverified" && !playbackOffline && (
                  <div className="df-playing-detail" aria-live="polite">
                    {recoveryWaiting && d.automation === "active"
                      ? `Allowing time for playback to recover. Reconsidering live coverage at ${time(d.recovery!.retry_after!)}.`
                      : "Playback is unverified. Your watch plan is retained."}
                  </div>
                )}
              </div>
              <div className="df-playing-controls">
                {currentEvent &&
                  !["ended", "cancelled"].includes(
                    currentEvent.lifecycle.state,
                  ) && (
                    <Button
                      disabled={busy}
                      onClick={() => completeEvent(currentEvent)}
                    >
                      Mark event finished
                    </Button>
                  )}
                <Button
                  onClick={togglePause}
                  disabled={busy || !!d.manual_control}
                >
                  {d.automation === "paused" ? (
                    <Play size={15} />
                  ) : (
                    <Pause size={15} />
                  )}{" "}
                  {d.automation === "paused" ? "Resume" : "Pause"}
                </Button>
                <Button
                  quiet
                  aria-label="Playback details"
                  onClick={() => setModal({ type: "playback" })}
                >
                  <Info size={17} />
                </Button>
              </div>
            </section>
            <ScreenPanel
              deviceId={d.id}
              deviceName={d.name}
              device={d}
              serverTime={data.meta.server_time}
              simulated={data.meta.playback_adapter === "simulator"}
              onChange={load}
            />
            {!connected && (
              <div className="df-warning" role="status">
                Connection interrupted. Showing the last received state;
                reconnecting automatically.
              </div>
            )}
            {playbackOffline && (
              <div
                className="df-warning"
                role="status"
                data-testid="playback-warning"
              >
                Playback service unavailable. Navigation is waiting; your watch
                plan is saved. Reconnecting automatically
                {d.executor_health?.next_probe_at
                  ? `; next check at ${time(d.executor_health.next_probe_at)}.`
                  : "."}
              </div>
            )}
            {data.health.state === "degraded" && (
              <div className="df-warning">
                Schedule refresh failed. The previous catalog and manual choices
                are retained.
              </div>
            )}
            {data.status_health.state === "degraded" && (
              <div className="df-warning" data-testid="status-warning">
                Some event statuses are unavailable or out of date. Your watch
                plan is retained; missing status does not mean an event has
                ended.
              </div>
            )}
            {error && (
              <div className="df-toast error" role="alert">
                <span>{error}</span>
                <Button
                  quiet
                  onClick={() => setError("")}
                  aria-label="Dismiss error"
                >
                  <X size={16} />
                </Button>
              </div>
            )}
            {notice && (
              <div className="df-toast" role="status">
                <span>{notice}</span>
                <Button
                  quiet
                  onClick={() => setNotice("")}
                  aria-label="Dismiss message"
                >
                  <X size={16} />
                </Button>
              </div>
            )}
            {data.undo && (
              <div className="df-edit-bar">
                <span>Last edit: {data.undo.description}</span>
                <Button
                  quiet
                  disabled={busy}
                  onClick={() =>
                    void mutate(
                      () =>
                        api(devicePath + "/undo", {
                          command_id: commandId(),
                          expected_revision: d.revision,
                          history_id: data.undo!.id,
                        }),
                      "Last edit undone. Live playback is evaluated from the current schedule.",
                    )
                  }
                >
                  <Undo2 size={16} />
                  Undo last edit
                </Button>
              </div>
            )}
            {view === "events" && (
              <>
                {heading(
                  "Your sports. Your priorities.",
                  "What's on",
                  `${data.meta.mode === "demo" ? "Sample schedule" : "Teamarr schedule"} · ${time(data.meta.now)} · ${d.preferences.timezone}`,
                  <Button quiet onClick={browse}>
                    <CalendarDays size={16} />
                    Upcoming
                  </Button>,
                )}
                <div className="df-toolbar">
                  <div className="df-segmented">
                    <button
                      type="button"
                      aria-pressed={tab === "live"}
                      onClick={() => setTab("live")}
                    >
                      Live{" "}
                      {
                        data.events.filter((e) => e.lifecycle.state === "live")
                          .length
                      }
                    </button>
                    <button
                      type="button"
                      aria-pressed={tab === "upcoming"}
                      onClick={() => setTab("upcoming")}
                    >
                      Upcoming
                    </button>
                  </div>
                  <label className="df-search">
                    <span className="df-screenreader">
                      Find a team or event
                    </span>
                    <input
                      type="search"
                      value={query}
                      onChange={(e) => setQuery(e.target.value)}
                      placeholder="Find a team or event"
                    />
                  </label>
                </div>
                <div className="df-filters">
                  {["all", ...leagues].map((l) => (
                    <button
                      type="button"
                      className="df-filter"
                      aria-pressed={sport === l}
                      key={l}
                      onClick={() => setSport(l)}
                    >
                      {l === "all" ? "All sports" : leagueName(l)}
                    </button>
                  ))}
                </div>
                <div className="df-section-head">
                  <h2>{tab === "live" ? "Live coverage" : "Coming up"}</h2>
                  <span>{visible.length} events</span>
                </div>
                {visible.length ? (
                  <div className="df-grid">{visible.map(card)}</div>
                ) : (
                  <div className="df-empty">
                    <h3>No events match</h3>
                    <p>Try another sport, or check the upcoming schedule.</p>
                    <Button
                      onClick={() => {
                        setSport("all");
                        setQuery("");
                      }}
                    >
                      Clear filters
                    </Button>
                  </div>
                )}
              </>
            )}
            {view === "plan" && (
              <>
                {heading(
                  "Reserved for you",
                  "Watch plan",
                  "Manual choices take priority over automatic selection.",
                  <Button onClick={browse}>
                    <Plus size={16} />
                    Add event
                  </Button>,
                )}
                {d.plan.length ? (
                  <>
                    <div className="df-section-head">
                      <h2>When events overlap</h2>
                      <span>Admin entries first, then user requests</span>
                    </div>
                    {d.plan.map((entry, i) => {
                      const event = find(entry.content_id);
                      return (
                        <div className="df-plan-row" key={entry.id}>
                          <span className="df-order">{i + 1}</span>
                          <div className="df-row-copy">
                            <strong>{event?.title || entry.content_id}</strong>
                            <p>
                              {entry.actor?.type === "user" ? "User" : "Admin"}:{" "}
                              {entry.actor?.name || "Admin"}
                            </p>
                            <p>
                              {event
                                ? `${time(event.start_time)} – ${time(event.expected_end_time)} est.`
                                : "Waiting for updated schedule data"}
                            </p>
                            <p>
                              {event?.lifecycle.state} ·{" "}
                              {event?.failure
                                ? "Retry pending; commitment retained"
                                : "Protected until completion"}
                            </p>
                          </div>
                          <div className="df-move">
                            <button
                              type="button"
                              aria-label={`Move ${event?.title} up`}
                              disabled={
                                busy ||
                                i === 0 ||
                                (entry.actor?.type || "admin") !==
                                  (d.plan[i - 1]?.actor?.type || "admin")
                              }
                              onClick={() => movePlan(i, -1)}
                            >
                              <ChevronUp size={18} />
                            </button>
                            <button
                              type="button"
                              aria-label={`Move ${event?.title} down`}
                              disabled={
                                busy ||
                                i === d.plan.length - 1 ||
                                (entry.actor?.type || "admin") !==
                                  (d.plan[i + 1]?.actor?.type || "admin")
                              }
                              onClick={() => movePlan(i, 1)}
                            >
                              <ChevronDown size={18} />
                            </button>
                          </div>
                          <Button
                            quiet
                            aria-label={`Remove ${event?.title}`}
                            disabled={busy}
                            onClick={() => remove(entry.id)}
                          >
                            <X size={17} />
                          </Button>
                        </div>
                      );
                    })}
                    <section className="df-panel df-spacer">
                      <div className="df-section-head">
                        <h2>Expected viewing</h2>
                        <Pill>Estimated</Pill>
                      </div>
                      <Timeline
                        preview={data.plan_preview}
                        find={find}
                        time={timelineTime}
                      />
                    </section>
                  </>
                ) : (
                  <div className="df-empty">
                    <h3>Your afternoon is open</h3>
                    <p>Add events to protect them from automatic switching.</p>
                    <Button primary onClick={browse}>
                      <Plus size={15} />
                      Choose an event
                    </Button>
                  </div>
                )}
              </>
            )}
            {view === "rules" && (
              <>
                {heading(
                  "Make it your broadcast",
                  "Priorities",
                  "The first matching rule wins. Manual choices always come first.",
                  <Button
                    onClick={() =>
                      setModal({
                        type: "rule",
                        rule: initialRule(),
                        revision: d.revision,
                      })
                    }
                  >
                    <Plus size={16} />
                    Add rule
                  </Button>,
                )}
                <section className="df-panel">
                  {d.rules.map((rule, i) => (
                    <div className="df-rule" key={rule.id}>
                      <span className="df-order">{i + 1}</span>
                      <div className="df-row-copy">
                        <strong>
                          {rule.name}
                          {!rule.enabled ? " · Disabled" : ""}
                        </strong>
                        <p>
                          {leagueName(rule.league)} ·{" "}
                          {rule.phase.replaceAll("_", " ")}
                          {rule.team_id
                            ? " · " +
                              (allTeams.find((t) => t.key === rule.team_id)
                                ?.full_name || rule.team_id)
                            : ""}
                        </p>
                      </div>
                      <div className="df-move">
                        <button
                          type="button"
                          aria-label={`Move ${rule.name} up`}
                          disabled={busy || i === 0}
                          onClick={() => moveRule(i, -1)}
                        >
                          <ChevronUp size={17} />
                        </button>
                        <button
                          type="button"
                          aria-label={`Move ${rule.name} down`}
                          disabled={busy || i === d.rules.length - 1}
                          onClick={() => moveRule(i, 1)}
                        >
                          <ChevronDown size={17} />
                        </button>
                      </div>
                      <Button
                        quiet
                        aria-label={`Edit ${rule.name}`}
                        onClick={() =>
                          setModal({ type: "rule", rule, revision: d.revision })
                        }
                      >
                        <Pencil size={16} />
                      </Button>
                    </div>
                  ))}
                </section>
                <div
                  className="df-row df-spacer"
                  style={{ justifyContent: "space-between" }}
                >
                  <div className="df-row-copy">
                    <strong>Favorite teams within each league</strong>
                    <p>Break ties inside a priority.</p>
                  </div>
                  <Button onClick={() => setModal({ type: "teams" })}>
                    <Users size={16} />
                    Rank teams
                  </Button>
                </div>
                <div
                  className="df-row df-spacer"
                  style={{ justifyContent: "space-between" }}
                >
                  <div className="df-row-copy">
                    <strong>See what your priorities choose</strong>
                    <p>No playback changes are made.</p>
                  </div>
                  <Button
                    disabled={busy}
                    onClick={() =>
                      void mutate(
                        async () =>
                          setModal({
                            type: "simulate",
                            result: await api<SimulationResult>(
                              devicePath + "/simulate",
                              {},
                            ),
                          }),
                        "Selection preview ready.",
                      )
                    }
                  >
                    <FlaskConical size={16} />
                    Test priorities
                  </Button>
                </div>
              </>
            )}
            {view === "activity" && (
              <>
                {heading(
                  "Every decision explained",
                  "Activity",
                  "Selections, switches, manual choices, and recovery.",
                )}
                <section className="df-panel">
                  <h3>Playback monitoring</h3>
                  <p>{d.reason}</p>
                  <p>
                    Controller: {d.playback_state} · Executor:{" "}
                    {d.executor_health?.state ?? "not reported"}
                  </p>
                  <p>
                    Last playback evidence:{" "}
                    {d.observed
                      ? `${monitoringTime(d.observed.observed_at)} · ${d.observed.verified ? "verified" : "unverified"}`
                      : "None received"}
                  </p>
                  <p>
                    Last executor contact:{" "}
                    {d.executor_health?.last_contact_at
                      ? monitoringTime(d.executor_health.last_contact_at)
                      : "None reported"}
                  </p>
                  <p>
                    Routine successful checks update these timestamps without
                    adding activity entries.
                  </p>
                  {data.playback_job && (
                    <p>
                      Request: {data.playback_job.state} ·{" "}
                      {data.playback_job.progress ?? "No progress reported"}
                      {data.playback_job.state === "pending" &&
                      data.playback_job.deadline_at
                        ? ` · Launch deadline ${time(new Date(data.playback_job.deadline_at * 1000).toISOString())}`
                        : ""}
                    </p>
                  )}
                  {data.playback_job?.error && (
                    <p role="alert">{data.playback_job.error}</p>
                  )}
                  <DiagnosticsTools
                    devicePath={devicePath}
                    onError={setError}
                  />
                  <p>
                    Includes recent decisions, jobs, cancellation, playback
                    evidence, and player health. Player RPC history requires the
                    updated player service. Content titles and device
                    identifiers may be included.
                  </p>
                </section>
                <section className="df-panel">
                  {data.activity.map((item) => (
                    <div className="df-activity" key={item.sequence}>
                      <span className="df-activity-icon">
                        <Activity size={16} />
                      </span>
                      <div className="df-row-copy">
                        <strong>{item.message}</strong>
                        <p>{item.detail}</p>
                      </div>
                      <time>{time(item.at)}</time>
                    </div>
                  ))}
                </section>
              </>
            )}
            {view === "settings" && (
              <>
                {heading(
                  "Keep the coverage moving",
                  "Settings",
                  "Behavior for your living room.",
                )}
                <section className="df-panel df-spacer">
                  <h2>Public app</h2>
                  <p className="df-subtitle">
                    {data.meta.public_auth_mode === "guest"
                      ? "Guest mode: no sign-in required. "
                      : "Proxy authentication is required. "}
                    Allow viewers to request coverage. Admin watch-plan entries
                    always take priority. Disabling an action keeps existing
                    requests.
                  </p>
                  <p>
                    <a href="/public/" target="_blank" rel="noreferrer">
                      Open public app
                    </a>
                  </p>
                  {(
                    [
                      ["play_now", "Allow public Play now"],
                      ["add_to_plan", "Allow public Add to plan"],
                    ] as const
                  ).map(([key, label]) => (
                    <div className="df-setting" key={key}>
                      <strong>{label}</strong>
                      <input
                        type="checkbox"
                        className="df-switch"
                        aria-label={label}
                        checked={d.public_access?.[key] || false}
                        disabled={busy}
                        onChange={(e) =>
                          void mutate(
                            () =>
                              api(
                                devicePath + "/public-access",
                                {
                                  command_id: commandId(),
                                  expected_revision: d.revision,
                                  ...d.public_access,
                                  [key]: e.target.checked,
                                },
                                "PUT",
                              ),
                            "Public permissions saved.",
                          )
                        }
                      />
                    </div>
                  ))}
                </section>
                <section className="df-panel">
                  <h2>Leagues in your schedule</h2>
                  <p className="df-subtitle">
                    Choose the leagues to discover. Saved watch-plan entries and
                    current playback stay protected when a league is turned off.
                    New leagues appear after the next schedule refresh. RedZone,
                    golf and tennis coverage, and special broadcasts are managed
                    separately.
                  </p>
                  {Object.entries({
                    ...data.meta.league_choices,
                    ...Object.fromEntries(
                      d.preferences.discovery_leagues.map((code) => [
                        code,
                        leagueName(code),
                      ]),
                    ),
                  }).map(([code, name]) => (
                    <div className="df-setting" key={code}>
                      <strong>{name}</strong>
                      <input
                        type="checkbox"
                        className="df-switch"
                        aria-label={`Discover ${name}`}
                        checked={d.preferences.discovery_leagues.includes(code)}
                        disabled={busy}
                        onChange={(e) =>
                          preference({
                            discovery_leagues: e.target.checked
                              ? [...d.preferences.discovery_leagues, code]
                              : d.preferences.discovery_leagues.filter(
                                  (league) => league !== code,
                                ),
                          })
                        }
                      />
                    </div>
                  ))}
                </section>
                <section className="df-panel df-spacer">
                  <h2>Automatic switching</h2>
                  <div className="df-setting">
                    <div className="df-row-copy">
                      <strong>Minimum time on an event</strong>
                      <p>Manual choices take effect immediately.</p>
                    </div>
                    <select
                      aria-label="Minimum time on an event"
                      disabled={busy}
                      value={d.preferences.minimum_viewing_seconds}
                      onChange={(e) =>
                        preference({
                          minimum_viewing_seconds: Number(e.target.value),
                        })
                      }
                    >
                      {[
                        ...new Set([
                          0,
                          300,
                          600,
                          900,
                          d.preferences.minimum_viewing_seconds,
                        ]),
                      ]
                        .sort((a, b) => a - b)
                        .map((n) => (
                          <option value={n} key={n}>
                            {n
                              ? n % 60 === 0
                                ? `${n / 60} minutes`
                                : `${n} seconds`
                              : "No minimum"}
                          </option>
                        ))}
                    </select>
                  </div>
                  <div className="df-setting">
                    <div className="df-row-copy">
                      <strong>Switch cooldown</strong>
                      <p>Minimum interval between automatic switches.</p>
                    </div>
                    <select
                      aria-label="Switch cooldown"
                      disabled={busy}
                      value={d.preferences.switch_cooldown_seconds}
                      onChange={(e) =>
                        preference({
                          switch_cooldown_seconds: Number(e.target.value),
                        })
                      }
                    >
                      {[
                        ...new Set([
                          0,
                          30,
                          60,
                          120,
                          d.preferences.switch_cooldown_seconds,
                        ]),
                      ]
                        .sort((a, b) => a - b)
                        .map((n) => (
                          <option value={n} key={n}>
                            {n} seconds
                          </option>
                        ))}
                    </select>
                  </div>
                  <div className="df-setting">
                    <div className="df-row-copy">
                      <strong>Switch within the same priority</strong>
                      <p>
                        Allow a preferred team to interrupt the current
                        automatic event.
                      </p>
                    </div>
                    <input
                      type="checkbox"
                      className="df-switch"
                      aria-label="Switch within the same priority"
                      checked={d.preferences.same_tier_switching}
                      disabled={busy}
                      onChange={(e) =>
                        preference({ same_tier_switching: e.target.checked })
                      }
                    />
                  </div>
                  <div className="df-setting">
                    <div className="df-row-copy">
                      <strong>Display timezone</strong>
                      <p>Local display with daylight-saving support.</p>
                    </div>
                    <select
                      aria-label="Display timezone"
                      value={d.preferences.timezone}
                      disabled={busy}
                      onChange={(e) => preference({ timezone: e.target.value })}
                    >
                      {[
                        ...new Set([
                          d.preferences.timezone,
                          "America/Vancouver",
                          "America/Edmonton",
                          "America/Winnipeg",
                          "America/Toronto",
                          "America/Halifax",
                          "America/St_Johns",
                          "UTC",
                        ]),
                      ].map((t) => (
                        <option key={t}>{t}</option>
                      ))}
                    </select>
                  </div>
                </section>
                <ConfigurationTools
                  devicePath={devicePath}
                  busy={busy}
                  onError={setError}
                  onPreview={async (document) => {
                    setError("");
                    try {
                      const preview = await api<ImportPreview>(
                        devicePath + "/configuration/import/preview",
                        {
                          command_id: commandId(),
                          expected_revision: d.revision,
                          document,
                        },
                      );
                      setModal({
                        type: "import",
                        document: document as ConfigurationDocument,
                        preview,
                      });
                    } catch (e) {
                      if (e instanceof ApiError && e.status === 409)
                        await load();
                      throw e;
                    }
                  }}
                />
                <section className="df-panel">
                  <h2>Connections</h2>
                  <div className="df-setting">
                    <div className="df-row-copy">
                      <strong>Schedule</strong>
                      <p>
                        {data.meta.mode === "demo"
                          ? "Illustrative matchups and timings"
                          : "Teamarr feed · " +
                            (data.health.last_success
                              ? "last read " + time(data.health.last_success)
                              : "waiting for first snapshot")}
                      </p>
                    </div>
                    <Pill>{data.health.state}</Pill>
                  </div>
                  <div className="df-setting">
                    <div className="df-row-copy">
                      <strong>Team directory</strong>
                      <p>
                        {data.teams.length} cached teams · refreshed
                        independently of the schedule. Saved preferences are
                        retained.
                      </p>
                    </div>
                    <Pill>{data.team_directory_health.state}</Pill>
                  </div>
                  <div className="df-setting">
                    <div className="df-row-copy">
                      <strong>Content status</strong>
                      <p>
                        {data.status_health.checked_count} of{" "}
                        {data.status_health.pinned_count} watched or reserved
                        events checked.
                        {data.meta.mode === "demo"
                          ? " Independent simulated lookups."
                          : data.meta.playback_adapter === "simulator"
                            ? " Cached Teamarr status. An independent live status source is not connected."
                            : " Teamarr lifecycle and verified playback completion observations."}
                      </p>
                      {data.status_health.error_count > 0 && (
                        <p>
                          {data.status_health.error_count} lookup failures;
                          retrying automatically.
                        </p>
                      )}
                    </div>
                    <Pill>{data.status_health.state}</Pill>
                  </div>
                  <div className="df-setting">
                    <div className="df-row-copy">
                      <strong>Playback</strong>
                      <p>
                        {playbackSimulator
                          ? "Simulated. No Fire TV commands are sent."
                          : "Prime Video playback on your device."}
                      </p>
                    </div>
                    <Pill>
                      {playbackOffline
                        ? "Unavailable"
                        : playbackSimulator
                          ? "Simulator"
                          : "Prime Video"}
                    </Pill>
                  </div>
                  <div className="df-setting">
                    <div className="df-row-copy">
                      <strong>Watch plan and priorities</strong>
                      <p>
                        Saved in the controller database. Available across
                        browsers and restarts.
                      </p>
                    </div>
                    <CheckCheck size={19} />
                  </div>
                </section>
              </>
            )}
            {data.meta.mode === "demo" && (
              <div className="df-lab">
                <details>
                  <summary>Demo scenarios · {time(data.meta.now)}</summary>
                  <div className="df-lab-controls">
                    <label className="df-field">
                      Scenario
                      <select
                        disabled={busy}
                        value={data.meta.scenario || "normal"}
                        onChange={(e) =>
                          void mutate(
                            () =>
                              api("/simulation", {
                                action: "scenario",
                                scenario: e.target.value,
                              }),
                            "Demo scenario loaded. Sample watch plan reset.",
                          )
                        }
                      >
                        <option value="normal">Normal afternoon</option>
                        <option value="overlap">Manual overlap</option>
                        <option value="overtime">Canadiens overtime</option>
                        <option value="delayed">Delayed start</option>
                        <option value="failure">Playback failure</option>
                        <option value="timeout">Navigation timeout</option>
                        <option value="replay">Replay result rejected</option>
                        <option value="coverage_switch">
                          Same-event coverage change
                        </option>
                        <option value="tennis">Tennis coverage · DAZN</option>
                        <option value="device_outage">
                          Playback service outage
                        </option>
                        <option value="status_outage">
                          Status lookup unavailable
                        </option>
                        <option value="outside_feed">
                          Reserved event outside feed
                        </option>
                        <option value="stale">Stale status</option>
                        <option value="empty">No live events</option>
                      </select>
                    </label>
                    <Button
                      disabled={busy}
                      onClick={() =>
                        void mutate(
                          () =>
                            api("/simulation", {
                              action: "advance",
                              minutes: 15,
                            }),
                          "Sample clock advanced.",
                        )
                      }
                    >
                      <Clock3 size={16} />
                      +15 min
                    </Button>
                    <Button
                      disabled={busy}
                      onClick={() =>
                        void mutate(
                          () =>
                            api("/simulation", {
                              action: playbackOffline
                                ? "reconnect"
                                : "disconnect",
                            }),
                          playbackOffline
                            ? "Simulator reconnected. Rechecking playback."
                            : "Simulator disconnected. Your watch plan is saved.",
                        )
                      }
                    >
                      {playbackOffline
                        ? "Reconnect simulator"
                        : "Disconnect simulator"}
                    </Button>
                  </div>
                  <p className="df-lab-note">
                    Illustrative events and viewing routes. Loading a scenario
                    resets the sample watch plan; priorities remain saved.
                  </p>
                </details>
              </div>
            )}
          </main>
        </div>
        <nav className="df-mobile-nav" aria-label="Main navigation">
          {menu(true)}
        </nav>
      </div>
      {modal && (
        <Dialog
          title={
            modal.type === "import"
              ? "Review configuration import"
              : modal.type === "conflict"
                ? "Two good games. One TV."
                : modal.type === "rule"
                  ? "Edit priority"
                  : modal.type === "teams"
                    ? "Rank your teams"
                    : modal.type === "simulate"
                      ? "What your priorities choose"
                      : modal.type === "playback"
                        ? "Playback details"
                        : modalEvent?.title || "Event details"
          }
          onClose={close}
        >
          {error && (
            <div className="df-warning" role="alert">
              {error}
            </div>
          )}
          {modal.type === "conflict" && (
            <>
              <p>
                {modalEvent?.title} overlaps with another manual choice. Both
                retain their non-overlapping live time.
              </p>
              {(["last", "first"] as const).map((p) => (
                <label className="df-choice" key={p}>
                  <input
                    type="radio"
                    name="priority"
                    checked={modal.priority === p}
                    disabled={busy}
                    onChange={() => changeConflict(p)}
                  />
                  <span>
                    <strong>
                      {p === "last"
                        ? "Keep my current watch plan first"
                        : `Give ${modalEvent?.title} priority`}
                    </strong>
                    <small>Displaced events resume while still live.</small>
                  </span>
                </label>
              ))}
              <Timeline
                preview={modal.preview}
                find={find}
                time={timelineTime}
              />
              <div className="df-dialog-actions">
                <Button onClick={close}>Cancel</Button>
                <Button
                  disabled={busy}
                  onClick={() => changeConflict(modal.priority)}
                >
                  Refresh preview
                </Button>
                <Button
                  primary
                  disabled={busy}
                  onClick={() =>
                    void mutate(
                      () =>
                        planCommand(
                          {
                            type: "add",
                            content_id: modal.id,
                            priority: modal.priority,
                          },
                          modal.preview.revision,
                        ),
                      "Watch plan saved.",
                      true,
                    )
                  }
                >
                  Save watch plan
                </Button>
              </div>
            </>
          )}
          {modal.type === "details" && modalEvent && (
            <>
              <p>
                {leagueName(modalEvent.league)} · {date(modalEvent.start_time)}{" "}
                · {time(modalEvent.start_time)}
              </p>
              <dl className="df-kv">
                <dt>Event status</dt>
                <dd>
                  {modalEvent.lifecycle.state}
                  {modalEvent.lifecycle.stale ? " · stale" : ""}
                </dd>
                <dt>Expected end</dt>
                <dd>{time(modalEvent.expected_end_time)} · estimate only</dd>
                <dt>Status source</dt>
                <dd>
                  {modalEvent.lifecycle.timestamp_basis === "manual"
                    ? "Marked finished by you"
                    : modalEvent.lifecycle.timestamp_basis === "feed_received"
                      ? "Cached Teamarr status; provider freshness is unknown"
                      : modalEvent.lifecycle.timestamp_basis === "fixture"
                        ? "Simulated event lifecycle"
                        : modalEvent.lifecycle.source ||
                          "Awaiting status evidence"}
                </dd>
                <dt>Observation time</dt>
                <dd>
                  {modalEvent.lifecycle.observed_at
                    ? time(modalEvent.lifecycle.observed_at)
                    : "Not supplied"}
                </dd>
                <dt>Status valid until</dt>
                <dd>
                  {modalEvent.lifecycle.timestamp_basis === "manual"
                    ? "Until you undo the manual completion"
                    : time(modalEvent.lifecycle.effective_valid_until)}
                </dd>
                {modalEvent.lifecycle.tracked && (
                  <>
                    <dt>Status lookup</dt>
                    <dd>
                      {modalEvent.lifecycle.timestamp_basis === "manual"
                        ? "Manual completion takes precedence"
                        : modalEvent.lifecycle.refresh.error ||
                          (modalEvent.lifecycle.refresh.last_success
                            ? "Last checked " +
                              time(modalEvent.lifecycle.refresh.last_success)
                            : "Waiting for first check")}
                    </dd>
                  </>
                )}
                {!modalEvent.active && (
                  <>
                    <dt>Schedule visibility</dt>
                    <dd>
                      Outside the current feed; retained for your watch plan or
                      playback.
                    </dd>
                  </>
                )}
                <dt>Viewing options</dt>
                <dd>
                  {modalEvent.viewing_options.length} valid candidates; chosen
                  by the playback service
                </dd>
                <dt>Availability</dt>
                <dd>
                  {modalEvent.availability_reason || "Live-only playback"}
                </dd>
              </dl>
              <div className="df-info df-spacer">
                Manual protection survives delays and overtime. Other manual
                entries may take precedence during overlap.
              </div>
              <div className="df-dialog-actions">
                {!["ended", "cancelled"].includes(
                  modalEvent.lifecycle.state,
                ) && (
                  <Button
                    disabled={busy}
                    onClick={() => completeEvent(modalEvent)}
                  >
                    Mark event finished
                  </Button>
                )}
                {modalEvent.watch_entry_id ? (
                  <Button
                    onClick={() => remove(modalEvent.watch_entry_id!)}
                    disabled={busy}
                  >
                    Remove from plan
                  </Button>
                ) : (
                  <Button
                    onClick={() => void add(modalEvent)}
                    disabled={
                      busy ||
                      ["ended", "cancelled"].includes(
                        modalEvent.lifecycle.state,
                      )
                    }
                  >
                    Add to plan
                  </Button>
                )}
                {modalEvent.playable && (
                  <Button
                    primary
                    disabled={busy || !!d.manual_control}
                    onClick={() => play(modalEvent)}
                  >
                    <Play size={15} />
                    Play now
                  </Button>
                )}
              </div>
            </>
          )}
          {modal.type === "playback" && (
            <>
              <p>The desired target and observed playback are separate.</p>
              <dl className="df-kv">
                <dt>Requested</dt>
                <dd>{desired?.title || "Waiting"}</dd>
                <dt>Observed</dt>
                <dd>{observed?.title || "None"}</dd>
                <dt>State</dt>
                <dd>{d.playback_state}</dd>
                <dt>Playback service</dt>
                <dd>
                  {playbackOffline
                    ? "Unavailable; reconnecting automatically"
                    : data.meta.playback_adapter === "simulator"
                      ? "Connected · simulator"
                      : "Connected · Prime Video"}
                </dd>
                {d.executor_health?.last_contact_at && (
                  <>
                    <dt>Last contact</dt>
                    <dd>{time(d.executor_health.last_contact_at)}</dd>
                  </>
                )}
                {observedOption && (
                  <>
                    <dt>Observed coverage</dt>
                    <dd>
                      {[observedOption.app, observedOption.channel]
                        .filter((value) => typeof value === "string" && value)
                        .join(" · ") || "Valid viewing option"}
                    </dd>
                  </>
                )}
                <dt>Verified at</dt>
                <dd>
                  {time(d.observed?.observed_at || null)}
                  {d.observed?.simulated ? " · simulated" : ""}
                </dd>
                <dt>Reason</dt>
                <dd>{d.reason}</dd>
                {data.playback_job && (
                  <>
                    <dt>Last request purpose</dt>
                    <dd>{requestPurpose}</dd>
                    <dt>Last request progress</dt>
                    <dd>
                      {(
                        data.playback_job.progress || data.playback_job.state
                      ).replaceAll("_", " ")}
                    </dd>
                    <dt>Delivery attempts</dt>
                    <dd>{data.playback_job.delivery_attempts}</dd>
                    {data.playback_job.state === "pending" &&
                      data.playback_job.deadline_at && (
                        <>
                          <dt>Navigation deadline</dt>
                          <dd>
                            {time(
                              new Date(
                                data.playback_job.deadline_at * 1000,
                              ).toISOString(),
                            )}
                          </dd>
                        </>
                      )}
                    {data.playback_job.error && (
                      <>
                        <dt>Last request error</dt>
                        <dd>{data.playback_job.error}</dd>
                      </>
                    )}
                  </>
                )}
                {recoveryWaiting && !playbackOffline && (
                  <>
                    <dt>Recovery</dt>
                    <dd>
                      Observing before another playback attempt. Reconsidering
                      at {time(d.recovery!.retry_after!)}; a live event must
                      still be confirmed.
                    </dd>
                  </>
                )}
              </dl>
            </>
          )}
          {modal.type === "rule" && (
            <RuleForm
              key={modal.rule.id}
              rule={modal.rule}
              teams={allTeams}
              leagues={leagues}
              busy={busy}
              onDelete={
                d.rules.some((r) => r.id === modal.rule.id)
                  ? () =>
                      void mutate(
                        () =>
                          saveRules(
                            d.rules.filter((r) => r.id !== modal.rule.id),
                            d.team_ranks,
                            d.preferences,
                            modal.revision,
                          ),
                        "Rule removed.",
                        true,
                      )
                  : undefined
              }
              onSave={(r) => {
                const existing = d.rules.findIndex((x) => x.id === r.id);
                const rules = d.rules.slice();
                if (existing >= 0) rules[existing] = r;
                else {
                  const tail = rules.findIndex(
                    (x) => x.league === "all" && !x.team_id && !x.source,
                  );
                  rules.splice(tail < 0 ? rules.length : tail, 0, r);
                }
                void mutate(
                  () =>
                    saveRules(
                      rules,
                      d.team_ranks,
                      d.preferences,
                      modal.revision,
                    ),
                  "Priority saved.",
                  true,
                );
              }}
            />
          )}
          {modal.type === "teams" && (
            <TeamRanking
              teams={allTeams}
              leagues={leagues}
              ranks={d.team_ranks}
              busy={busy}
              onSave={(league, order) =>
                void mutate(
                  () =>
                    saveRules(d.rules, { ...d.team_ranks, [league]: order }),
                  "Team preferences saved.",
                )
              }
            />
          )}
          {modal.type === "import" && (
            <>
              <p>
                This replaces your priorities, team preferences, and settings.
                Your watch plan and automation mode stay in place.
              </p>
              <dl className="df-kv">
                <dt>Priority rules</dt>
                <dd>
                  {modal.preview.summary.current_rules} current →{" "}
                  {modal.preview.summary.imported_rules} imported
                </dd>
                <dt>Preferred teams</dt>
                <dd>{modal.preview.summary.ranked_teams}</dd>
                <dt>Display timezone</dt>
                <dd>{modal.preview.configuration.preferences.timezone}</dd>
                <dt>Exported from</dt>
                <dd>
                  {modal.document.source_mode} ·{" "}
                  {date(modal.document.exported_at)}
                </dd>
              </dl>
              {modal.preview.warnings.map((warning, i) => (
                <p className="df-warning" key={i}>
                  {warning}
                </p>
              ))}
              <details className="df-import-details df-spacer">
                <summary>Review imported priorities and settings</summary>
                <ol>
                  {modal.preview.configuration.rules.map((rule) => (
                    <li key={rule.id}>
                      <strong>{rule.name}</strong>
                      {!rule.enabled && " (disabled)"}
                      <p>
                        {[
                          leagueName(rule.league),
                          rule.phase.replaceAll("_", " "),
                          rule.kind,
                          rule.source,
                          rule.team_id &&
                            (allTeams.find((t) => t.key === rule.team_id)
                              ?.full_name ||
                              rule.team_id),
                        ]
                          .filter(Boolean)
                          .join(" · ")}
                      </p>
                    </li>
                  ))}
                </ol>
                {Object.entries(modal.preview.configuration.team_ranks)
                  .filter(([, keys]) => keys.length)
                  .map(([league, keys]) => (
                    <p key={league}>
                      <strong>{leagueName(league)} team order:</strong>{" "}
                      {keys
                        .map(
                          (key) =>
                            allTeams.find((t) => t.key === key)?.full_name ||
                            key,
                        )
                        .join(" → ")}
                    </p>
                  ))}
                <p>
                  Leagues to discover:{" "}
                  {modal.preview.configuration.preferences.discovery_leagues
                    .map(leagueName)
                    .join(", ") || "None"}
                  <br />
                  Minimum viewing:{" "}
                  {
                    modal.preview.configuration.preferences
                      .minimum_viewing_seconds
                  }{" "}
                  seconds.
                  <br />
                  Switch cooldown:{" "}
                  {
                    modal.preview.configuration.preferences
                      .switch_cooldown_seconds
                  }{" "}
                  seconds.
                  <br />
                  Same-priority switching:{" "}
                  {modal.preview.configuration.preferences.same_tier_switching
                    ? "on"
                    : "off"}
                  .
                </p>
              </details>
              <div className="df-info df-spacer">
                You can undo this import. Undo restores saved configuration; it
                does not rewind live playback.
              </div>
              <div className="df-dialog-actions">
                <Button onClick={close}>Cancel</Button>
                <Button
                  primary
                  disabled={busy}
                  onClick={() =>
                    void mutate(
                      () =>
                        api(devicePath + "/configuration/import", {
                          command_id: commandId(),
                          expected_revision: modal.preview.revision,
                          document: modal.document,
                        }),
                      "Configuration imported.",
                      true,
                    )
                  }
                >
                  Apply configuration
                </Button>
              </div>
            </>
          )}
          {modal.type === "simulate" && (
            <>
              <p>This preview does not change playback.</p>
              <div className="df-info df-spacer">
                <h3>
                  {find(modal.result.decision.content_id)?.title ||
                    "Waiting for live sports"}
                </h3>
                <p>{modal.result.decision.reason}</p>
              </div>
              {modal.result.alternatives.map((e) => (
                <div className="df-setting" key={e.content_id}>
                  <div className="df-row-copy">
                    <strong>{e.title}</strong>
                    <p>
                      {e.eligible
                        ? "Eligible live event"
                        : e.reason || "Not live"}
                    </p>
                  </div>
                  <Pill>
                    {e.priority < 1000000 ? "#" + (e.priority + 1) : "Unranked"}
                  </Pill>
                </div>
              ))}
            </>
          )}
        </Dialog>
      )}
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
