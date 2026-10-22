import { useEffect, useState } from "react";
import type { Overview } from "./types";

export function LaunchProgress({ job, serverTime }: {
  job: NonNullable<Overview["playback_job"]>;
  serverTime: string;
}) {
  const [ticks, setTicks] = useState(0);
  useEffect(() => {
    setTicks(0);
    const timer = setInterval(() => setTicks((n) => n + 1), 1000);
    return () => clearInterval(timer);
  }, [serverTime]);
  const progress = job.launch_progress;
  const elapsed = progress?.started_at
    ? Math.max(0, Math.floor((Date.parse(serverTime) - Date.parse(progress.started_at)) / 1000) + ticks)
    : null;
  const duration = elapsed !== null && Number.isFinite(elapsed)
    ? `${Math.floor(elapsed / 60)}m ${elapsed % 60}s` : null;
  return <div className="df-playing-detail">
    <span aria-live="polite">{progress?.title || "Preparing playback"}
      {progress?.detail ? ` · ${progress.detail}` : ""}</span>
    {duration && <span> · {duration} in this stage</span>}
  </div>;
}
