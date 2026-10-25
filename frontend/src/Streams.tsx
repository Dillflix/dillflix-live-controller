import { useState } from "react";
import { Tv } from "lucide-react";
import type { StreamSummary } from "./types";

// Each workspace owns its drafts, remote credentials and asynchronous reads.
// Changing this key unmounts the previous workspace and closes its sockets.
export function useStreamSelection() {
  const [selected, setSelected] = useState<string | null>(() =>
    new URLSearchParams(window.location.search).get("device"),
  );
  function select(id: string) {
    const url = new URL(window.location.href);
    url.searchParams.set("device", id);
    window.history.replaceState(null, "", url);
    setSelected(id);
  }
  return [selected, select] as const;
}

export function streamAssignment(streams: StreamSummary[], contentId: string) {
  const current = (item: StreamSummary) => item.current_event?.content_id || item.observed?.content_id;
  const stream = streams.find((item) => (item.target_event?.content_id || item.desired) === contentId ||
    ((item.verified ?? item.playback_state === "verified") && current(item) === contentId));
  if (!stream) return null;
  return (stream.verified ?? stream.playback_state === "verified") && current(stream) === contentId
    ? `Playing on ${stream.name}` : `Assigned to ${stream.name}`;
}

export function Streams({ streams, selected, select, events, publicView = false }: {
  streams: StreamSummary[];
  selected: string;
  select: (id: string) => void;
  events: { content_id: string; title: string }[];
  publicView?: boolean;
}) {
  if (streams.length < 2) return null;
  return <section className="df-streams" aria-label="Live streams">
    <div className="df-section-head">
      <div><h2>Live streams</h2><p className="df-subtitle">One shared watch plan · Different events across available streams</p></div>
    </div>
    <div className="df-stream-grid">
      {streams.map((stream) => {
        const navigating = stream.playback_state === "navigating";
        const verified = stream.verified ?? stream.playback_state === "verified";
        const current = stream.current_event ?? events.find((item) => item.content_id === stream.observed?.content_id);
        const target = stream.target_event ?? events.find((item) => item.content_id === stream.desired);
        const event = navigating ? target : current;
        const statusLabels = { manual: "Manual control", paused: "Automation paused", starting: "Starting",
          playing: "Playing", unavailable: "Unavailable", unverified: "Unverified", waiting: "Waiting" };
        const state = stream.status ? statusLabels[stream.status] : stream.manual_control ? "Manual control" : stream.automation === "paused"
          ? "Automation paused" : navigating ? "Starting" : verified ? "Playing"
            : stream.playback_state === "failed" ? "Unavailable" : event ? "Last observed" : "Waiting";
        return <button type="button" key={stream.id} className="df-stream-card"
          aria-pressed={stream.id === selected} aria-label={`Select ${stream.name}`}
          onClick={() => select(stream.id)}>
          <span className="df-stream-name"><Tv size={16} />{stream.name}<span className="df-stream-status">{state}</span></span>
          <strong>{event?.title || "Waiting for live sports"}</strong>
          <span className="df-stream-action">{stream.id === selected
            ? publicView ? "Selected stream" : "Viewing controls" : publicView ? "View stream" : "View controls"}</span>
        </button>;
      })}
    </div>
  </section>;
}
