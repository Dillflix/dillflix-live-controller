export interface Team {
  key: string;
  id: string;
  provider: string;
  league: string;
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
    last_known_state: string;
    tracked: boolean;
    observed_at: string | null;
    received_at: string | null;
    valid_until: string | null;
    effective_valid_until: string | null;
    source: string | null;
    timestamp_basis: string | null;
    simulated: boolean;
    refresh: {
      state: string;
      last_attempt: string | null;
      last_success: string | null;
      next_check_at: string | null;
      error: string | null;
    };
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
  manual_control?: {
    session_id: string;
    started_at: string;
    expires_at: string;
    return_mode: "active" | "paused";
  } | null;
  intent_version: number;
  desired: string | null;
  observed: {
    content_id: string;
    verified: boolean;
    simulated: boolean;
    observed_at: string;
    valid_until?: string;
    health?: string;
    viewing_option_id: string;
    presentation: string;
  } | null;
  playback_state: string;
  executor_health?: {
    state: "starting" | "ok" | "offline";
    last_contact_at?: string | null;
    next_probe_at?: string | null;
    since?: string | null;
    failures?: number;
  };
  recovery?: {
    content_id: string;
    attempts: number;
    since?: string | null;
    retry_after?: string | null;
    stable_since?: string | null;
  } | null;
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
  status_health: {
    state: string;
    pinned_count: number;
    checked_count: number;
    error_count: number;
    stale_count: number;
    unknown_count: number;
    last_attempt: string | null;
    adapter: string;
    simulated: boolean;
  };
  playback_job: {
    id: string;
    purpose: "selection" | "route_handoff" | "recovery";
    content_id: string;
    state: string;
    progress: string | null;
    deadline_at: number | null;
    delivery_attempts: number;
    error: string | null;
  } | null;
  teams: Team[];
  undo: { id: number; description: string; created_at: string } | null;
  team_directory_health: {
    state: string;
    count?: number;
    last_attempt?: string;
  };
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

export interface ConfigurationDocument {
  format: "dillflix-controller-config";
  schema_version: 1;
  source_mode: "demo" | "teamarr";
  exported_at: string;
  configuration: {
    rules: Rule[];
    team_ranks: Record<string, string[]>;
    preferences: Preferences;
  };
}
export interface ImportPreview {
  revision: number;
  configuration: ConfigurationDocument["configuration"];
  warnings: string[];
  summary: {
    current_rules: number;
    imported_rules: number;
    ranked_teams: number;
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
