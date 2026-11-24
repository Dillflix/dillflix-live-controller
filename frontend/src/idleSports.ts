import type { Device } from "./types";

export function isIdleSports(event: { source?: string; content_id: string }) {
  return event.source === "prime_idle" || event.content_id.startsWith("prime_idle:");
}

export function idleSportsStatus(device: Device): string {
  if (!device.preferences.idle_sports_enabled) return "Idle sports is off.";
  if (device.manual_control) return "Idle sports waits while you use the remote.";
  if (device.automation === "paused") return "Idle sports is paused with automatic playback.";
  switch (device.idle_sports?.state) {
    case "checking": return "Checking Prime Video for sports on now.";
    case "ready": return "Sports selected from Prime Video for gaps in your schedule.";
    case "unavailable":
      if (device.idle_sports.error_code === "prime_linear_catalog_timeout") {
        return "Prime Video's sports catalogue timed out. Retrying automatically.";
      }
      return "No confirmed sports selection is available. Checking again automatically.";
    case "disabled": return "Idle sports is waiting to become available.";
    default: return "Ready to look for sports when your schedule is idle.";
  }
}
