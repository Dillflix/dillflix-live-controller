import { useState } from "react";
import { ChevronDown, ChevronUp, ChevronsUp, Plus, X } from "lucide-react";
import type { Team } from "./types";

export function TeamRanking({
  teams,
  leagues,
  ranks,
  busy,
  onSave,
}: {
  teams: Team[];
  leagues: string[];
  ranks: Record<string, string[]>;
  busy: boolean;
  onSave: (league: string, order: string[]) => void;
}) {
  const [league, setLeague] = useState("nfl");
  const [query, setQuery] = useState("");
  const ordered = ranks[league] || [];
  const name = (id: string) => teams.find((t) => t.key === id)?.full_name || id;
  const available = teams.filter(
    (t) =>
      t.league === league &&
      !ordered.includes(t.key) &&
      `${t.full_name} ${t.abbreviation}`
        .toLowerCase()
        .includes(query.toLowerCase()),
  );
  const move = (index: number, next: number) => {
    const changed = ordered.slice();
    const [id] = changed.splice(index, 1);
    changed.splice(next, 0, id);
    onSave(league, changed);
  };
  return (
    <>
      <p>
        Put your favorite teams first. Other teams tie within a priority; manual
        choices still come first.
      </p>
      <label className="df-field df-spacer">
        League
        <select
          value={league}
          onChange={(e) => {
            setLeague(e.target.value);
            setQuery("");
          }}
        >
          {leagues.map((l) => (
            <option key={l} value={l}>
              {l === "pga" ? "Golf" : l.toUpperCase()}
            </option>
          ))}
        </select>
      </label>
      <h3>Preferred teams</h3>
      {!ordered.length && (
        <p className="df-subtitle">
          No team preferences saved for this league.
        </p>
      )}
      {ordered.map((id, index) => (
        <div
          className="df-rule df-team-preference"
          key={id}
          data-testid={`ranked-${id}`}
        >
          <span className="df-order">{index + 1}</span>
          <div className="df-row-copy">
            <strong>{name(id)}</strong>
            {!teams.some((t) => t.key === id) && (
              <p>Saved preference · waiting for directory data</p>
            )}
          </div>
          <div className="df-team-actions">
            <div className="df-move">
              <button
                type="button"
                aria-label={`Move ${name(id)} up`}
                disabled={busy || index === 0}
                onClick={() => move(index, index - 1)}
              >
                <ChevronUp size={18} />
              </button>
              <button
                type="button"
                aria-label={`Move ${name(id)} down`}
                disabled={busy || index === ordered.length - 1}
                onClick={() => move(index, index + 1)}
              >
                <ChevronDown size={18} />
              </button>
            </div>
            <button
              className="df-button df-button-quiet"
              type="button"
              aria-label={`Make ${name(id)} first`}
              disabled={busy || index === 0}
              onClick={() => move(index, 0)}
            >
              <ChevronsUp size={17} />
            </button>
            <button
              className="df-button df-button-quiet"
              type="button"
              aria-label={`Remove ${name(id)} preference`}
              disabled={busy}
              onClick={() =>
                onSave(
                  league,
                  ordered.filter((key) => key !== id),
                )
              }
            >
              <X size={16} />
            </button>
          </div>
        </div>
      ))}
      <h3 className="df-spacer">Add a team</h3>
      <label className="df-field">
        Find a team
        <input
          type="search"
          placeholder="Search the team directory"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </label>
      <div className="df-team-directory">
        {available.map((team) => (
          <div className="df-setting" key={team.key}>
            <div className="df-row-copy">
              <strong>{team.full_name}</strong>
              <p>{team.abbreviation}</p>
            </div>
            <button
              className="df-button"
              type="button"
              aria-label={`Add ${team.full_name} preference`}
              disabled={busy}
              onClick={() => onSave(league, [...ordered, team.key])}
            >
              <Plus size={16} />
              Add
            </button>
          </div>
        ))}
        {!available.length && (
          <p className="df-subtitle">
            No additional teams match. Cached teams remain available even when
            they have no upcoming event.
          </p>
        )}
      </div>
    </>
  );
}
