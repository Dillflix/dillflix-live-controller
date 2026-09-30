export interface Team {
  key: string;
  id: string;
  provider: string;
  full_name: string;
  city: string | null;
  name: string | null;
  abbreviation: string;
  logo_url?: string | null;
}
export interface ViewingOption {
  id: string;
  app?: string | null;
  decision: string;
  reasons: string[];
  [key: string]: unknown;
}
export interface Content {
  content_id: string;
  kind: string;
  title: string;
  league: string;
  source: string;
  phase: string;
  teams: Team[];
  start_time: string;
  expected_end_time: string | null;
  end_time_estimated: boolean;
  active: boolean;
  artwork: Record<string, string | null>;
  lifecycle: {
    state: string;
    stale: boolean;
    observed_at: string;
    source: string;
    simulated: boolean;
  };
  viewing_options: ViewingOption[];
  playable: boolean;
  availability_reason: string | null;
  watch_entry_id: string | null;
  priority: number;
  scores: (number | null)[];
  status_detail: string | null;
  failure: { attempts: number; retry_after: string; reason: string } | null;
}
export interface Rule {
  id: string;
  name: string;
  enabled: boolean;
  league: string;
  phase: string;
  team_id: string | null;
  source: string | null;
  kind: "event" | "session" | "broadcast" | null;
}
export interface Preferences {
  timezone: string;
  minimum_viewing_seconds: number;
  switch_cooldown_seconds: number;
  same_tier_switching: boolean;
}
export interface Entry {
  id: string;
  content_id: string;
  created_at: string;
}
export interface Device {
  id: string;
  name: string;
  revision: number;
  rules: Rule[];
  team_ranks: Record<string, string[]>;
  plan: Entry[];
  automation: "active" | "paused";
  intent_version: number;
  desired: string | null;
  observed: {
    content_id: string;
    verified: boolean;
    simulated: boolean;
    observed_at: string;
    viewing_option_id: string;
    presentation: string;
  } | null;
  playback_state: string;
  reason: string;
  next_candidate?: { content_id: string; reason: string } | null;
  preferences: Preferences;
}
export interface Preview {
  revision?: number;
  entries?: Entry[];
  conflicts: string[][];
  unknown_timing: string[];
  segments: {
    start: string;
    end: string;
    content_id: string | null;
    estimated: boolean;
  }[];
  note: string;
}
export interface Overview {
  device: Device;
  events: Content[];
  plan_preview: Preview;
  activity: {
    sequence: number;
    at: string;
    kind: string;
    message: string;
    detail: string;
  }[];
  health: {
    state: string;
    error?: string | null;
    last_success?: string;
    count?: number;
  };
  meta: {
    mode: "demo" | "teamarr";
    playback_adapter: string;
    status_simulated: boolean;
    now: string;
    server_time: string;
    scenario: string | null;
  };
}
export interface Action {
  type: "add" | "play_now" | "remove" | "reorder";
  content_id?: string;
  entry_id?: string;
  ordered_entry_ids?: string[];
  priority?: "first" | "last";
}
export interface SimulationResult {
  decision: { content_id: string | null; reason: string; manual: boolean };
  alternatives: {
    content_id: string;
    title: string;
    priority: number;
    eligible: boolean;
    reason: string | null;
  }[];
}
