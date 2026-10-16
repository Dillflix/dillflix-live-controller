import { useState } from "react";
import { EntitlementStatus, type Entitlement } from "./EntitlementStatus";

export interface EntitlementQueueView {
  phase: string;
  reason: string | null;
  updated_at: string | null;
  dispatch_after: string | null;
  counts: Record<Entitlement["state"], number>;
  failed_jobs: number;
  rows: {
    content_id: string;
    title: string;
    entitlement: Entitlement;
    failures: number;
    error: { code?: string; message?: string } | null;
    last_deferral: { reason: string; at: string } | null;
    started_at: string | null;
    eligible: boolean;
  }[];
}

export function EntitlementQueue({
  value,
  timezone,
  busy,
  onCheck,
}: {
  value?: EntitlementQueueView | null;
  timezone: string;
  busy: boolean;
  onCheck: (event: { content_id: string }) => void;
}) {
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(20);
  if (!value) return null;
  const date = (s: string) =>
    new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      timeZone: timezone,
    }).format(new Date(s));
  const rows = value.rows.filter(
    (r) =>
      (filter === "all" ||
        (filter === "failed"
          ? r.entitlement.state === "retry" && r.error
          : r.entitlement.state === filter)) &&
      r.title.toLocaleLowerCase().includes(query.toLocaleLowerCase()),
  );
  return (
    <section
      className="df-panel df-entitlement-queue"
      aria-labelledby="entitlement-queue-title"
    >
      <h3 id="entitlement-queue-title">Entitlement checks</h3>
      <p className="df-subtitle">
        Checks run during stable playback. Launches, recovery and manual control
        take priority; routine monitoring runs between catalogue searches.
      </p>
      <div
        className={`df-entitlement-worker${["error", "unavailable"].includes(value.phase) ? " is-error" : ""}`}
        role="status"
      >
        <strong>
          {value.phase === "matching"
            ? "Matching results"
            : value.phase === "searching"
              ? "Checking an event"
              : value.phase === "waiting"
                ? "Queue waiting"
                : value.phase === "unavailable"
                  ? "Worker unavailable"
                  : value.phase === "error"
                    ? "Worker error"
                    : value.phase === "ready"
                      ? "Ready for next check"
                      : "Queue idle"}
        </strong>
        <span>{value.reason}</span>
        {value.updated_at && (
          <small>Last worker update {date(value.updated_at)}</small>
        )}
        {value.dispatch_after && (
          <small>
            Next automatic dispatch no earlier than {date(value.dispatch_after)}
          </small>
        )}
      </div>
      <p>
        {value.counts.queued} queued · {value.counts.checking} checking ·{" "}
        {value.counts.retry} awaiting retry · {value.counts.complete} completed
        · {value.failed_jobs} with errors
      </p>
      <div className="df-entitlement-filters">
        <label className="df-field">
          Show checks
          <select
            value={filter}
            onChange={(e) => {
              setFilter(e.target.value);
              setLimit(20);
            }}
          >
            <option value="all">All checks</option>
            <option value="queued">Queued</option>
            <option value="checking">Checking</option>
            <option value="retry">Awaiting retry</option>
            <option value="failed">Errors</option>
            <option value="complete">Completed</option>
            <option value="stopped">Stopped</option>
          </select>
        </label>
        <label className="df-field">
          Find an event
          <input
            type="search"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setLimit(20);
            }}
            placeholder="Event name"
          />
        </label>
      </div>
      <ul className="df-entitlement-jobs">
        {rows.slice(0, limit).map((row) => (
          <li key={row.content_id}>
            <h4>{row.title}</h4>
            <EntitlementStatus
              value={row.entitlement}
              timezone={timezone}
              details
              busy={busy}
              onCheck={() => onCheck(row)}
            />
            {row.started_at && <p>Last started {date(row.started_at)}</p>}
            {row.failures > 0 && <p>Unconfirmed checks: {row.failures}</p>}
            {row.error && (
              <p className="df-entitlement-error">
                Last error: {row.error.code}
                {row.error.message ? ` — ${row.error.message}` : ""}
              </p>
            )}
            {row.last_deferral && (
              <p>
                Last deferred {date(row.last_deferral.at)}:{" "}
                {row.last_deferral.reason}
              </p>
            )}
            {!row.eligible && (
              <p>
                Outside the imported schedule window; automatic checking is
                paused.
              </p>
            )}
          </li>
        ))}
      </ul>
      {!rows.length && <p>No checks match this filter.</p>}
      {rows.length > limit && (
        <button
          className="df-button df-button-quiet"
          onClick={() => setLimit(limit + 20)}
        >
          Show more checks ({rows.length - limit} remaining)
        </button>
      )}
    </section>
  );
}
