import { useEffect, useState } from "react";
import { api } from "./api";
import type { VPNPolicy } from "./types";

type Location = {
  id: string;
  city: string;
  qualified: boolean;
  routing_confirmed: boolean;
};
const defaults: VPNPolicy = {
  enabled: false,
  locations: [],
  acquisition_timeout: 1800,
  max_transitions: 4,
  access_ttl: 300,
};

export function VPNSettings({
  deviceId,
  value,
  save,
}: {
  deviceId: string;
  value?: VPNPolicy | null;
  save: (value: VPNPolicy) => void;
}) {
  const [draft, setDraft] = useState<VPNPolicy>(value || defaults);
  const [locations, setLocations] = useState<Location[]>([]);
  const [error, setError] = useState("");
  const saved = JSON.stringify(value || defaults);
  useEffect(() => {
    setDraft(JSON.parse(saved));
  }, [saved]);
  const refresh = () => {
    setError("");
    api<{ locations: Location[] }>(`/devices/${deviceId}/vpn-locations`)
      .then((v) => setLocations(v.locations))
      .catch((e) => setError(e.message));
  };
  useEffect(refresh, [deviceId]);
  const update = (
    index: number,
    patch: Partial<VPNPolicy["locations"][number]>,
  ) =>
    setDraft({
      ...draft,
      locations: draft.locations.map((row, i) =>
        i === index ? { ...row, ...patch } : row,
      ),
    });
  return (
    <section className="panel" aria-label="VPN location policies">
      <h2>VPN location policies</h2>
      <p>
        Always try the current connection first. If no suitable feed is found,
        try matching VPN locations in the order below, then direct. Direct is
        always permitted. Each switch interrupts playback and may take three
        minutes or longer.
      </p>
      <label>
        <input
          type="checkbox"
          checked={draft.enabled}
          onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })}
        />{" "}
        Enable location fallback
      </label>
      <p>
        Match either team's city, not the event venue. Events without a matching
        city can fall back only to direct. Providers and entitlement are checked
        again after each switch.
      </p>
      {error && <p role="alert">{error}</p>}
      <button type="button" onClick={refresh}>
        Refresh VPN locations
      </button>
      {draft.locations.map((row, index) => {
        const location = locations.find((v) => v.id === row.target_id);
        return (
          <fieldset key={index}>
            <legend>Location policy {index + 1}</legend>
            <label>
              VPN location{" "}
              <select
                aria-label={`VPN location ${index + 1}`}
                value={row.target_id}
                onChange={(e) => update(index, { target_id: e.target.value })}
              >
                {!location && (
                  <option value={row.target_id}>
                    {row.target_id || "Choose location"}
                  </option>
                )}
                {locations
                  .filter(
                    (v) =>
                      v.id === row.target_id ||
                      !draft.locations.some((r) => r.target_id === v.id),
                  )
                  .map((v) => (
                    <option key={v.id} value={v.id}>
                      {v.city} ({v.id})
                    </option>
                  ))}
              </select>
            </label>
            <p>
              {location
                ? location.qualified
                  ? "Player qualification recorded"
                  : "Player qualification not recorded; automatic switching is blocked"
                : "Location is not currently available from the player"}
            </p>
            <label>
              Permitted team cities (one per line)
              <textarea
                aria-label={`Permitted team cities ${index + 1}`}
                value={row.team_cities.join("\n")}
                onChange={(e) =>
                  update(index, { team_cities: e.target.value.split("\n") })
                }
              />
            </label>
            <button
              type="button"
              disabled={index === 0}
              onClick={() => {
                const rows = [...draft.locations];
                [rows[index - 1], rows[index]] = [rows[index], rows[index - 1]];
                setDraft({ ...draft, locations: rows });
              }}
            >
              Move earlier
            </button>
            <button
              type="button"
              onClick={() =>
                setDraft({
                  ...draft,
                  locations: draft.locations.filter((_, i) => i !== index),
                })
              }
            >
              Remove location policy
            </button>
          </fieldset>
        );
      })}
      <button
        type="button"
        disabled={
          draft.locations.length >= 4 ||
          locations.every((v) =>
            draft.locations.some((r) => r.target_id === v.id),
          )
        }
        onClick={() => {
          const next = locations.find(
            (v) => !draft.locations.some((r) => r.target_id === v.id),
          );
          if (next)
            setDraft({
              ...draft,
              locations: [
                ...draft.locations,
                { target_id: next.id, team_cities: [next.city] },
              ],
            });
        }}
      >
        Add location policy
      </button>
      <p>
        Acquisition has a shared {Math.round(draft.acquisition_timeout / 60)}
        -minute deadline and at most {draft.max_transitions} switches. Upcoming
        feeds and technical errors do not trigger location fallback.
      </p>
      <button
        type="button"
        onClick={() =>
          save({
            ...draft,
            locations: draft.locations.map((row) => ({
              ...row,
              team_cities: row.team_cities.map((c) => c.trim()).filter(Boolean),
            })),
          })
        }
      >
        Save VPN policies
      </button>
    </section>
  );
}
