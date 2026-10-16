import type { Entitlement } from "./EntitlementStatus";

export function matchesEntitlement(value: Entitlement | null | undefined, filter: string) {
  if (filter === "all") return true;
  if (filter === "entitled") return !value || value.status === "entitled";
  if (filter === "no_listing") return value?.status === "unknown" && value.result === "no_listing";
  if (filter === "queued" || filter === "checking") return value?.state === filter;
  if (filter === "unknown") return value?.status === "unknown";
  return value?.status === filter;
}

export function EntitlementFilter({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return <label className="df-entitlement-filter">Entitlement
    <select value={value} onChange={e => onChange(e.target.value)}>
      <option value="entitled">Entitled</option>
      <option value="all">All events</option>
      <option value="unentitled">Unentitled</option>
      <option value="unknown">Unconfirmed (all)</option>
      <option value="no_listing">No Prime listing found</option>
      <option value="queued">Queued</option>
      <option value="checking">Checking</option>
    </select>
  </label>;
}
