export interface Entitlement {
  status: "entitled" | "unentitled" | "unknown";
  state: "queued" | "checking" | "retry" | "complete" | "stopped";
  checked_at: string | null;
  next_check_at: string | null;
  blocked: boolean;
  reason: string | null;
  can_check: boolean;
}

export function EntitlementStatus({
  value,
  timezone,
  onCheck,
  busy = false,
  details = false,
}: {
  value?: Entitlement | null;
  timezone: string;
  onCheck?: () => void;
  busy?: boolean;
  details?: boolean;
}) {
  if (!value) return null;
  const date = (s: string) =>
    new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      timeZone: timezone,
    }).format(new Date(s));
  const label =
    value.status === "entitled"
      ? "Included with your subscription"
      : value.status === "unentitled"
        ? "Subscription required"
        : value.checked_at
          ? "Entitlement unconfirmed"
          : "Entitlement not checked yet";
  return (
    <div className={`df-entitlement${value.blocked ? " is-blocked" : ""}`}>
      <div>
        <p>
          {label}
          {value.state === "checking"
            ? " · Checking…"
            : value.state === "queued"
              ? " · Queued"
              : ""}
        </p>
        {value.state === "retry" && value.next_check_at && (
          <small>Retry scheduled {date(value.next_check_at)}</small>
        )}
        {details && value.checked_at && (
          <small>Last checked {date(value.checked_at)}</small>
        )}
        {details && value.reason && <small>{value.reason}</small>}
      </div>
      {onCheck && value.can_check && (
        <button
          className="df-button df-button-quiet"
          type="button"
          disabled={busy || value.state === "checking"}
          onClick={onCheck}
        >
          {value.state === "checking"
            ? "Checking…"
            : value.checked_at
              ? "Recheck entitlement"
              : "Check entitlement"}
        </button>
      )}
    </div>
  );
}
