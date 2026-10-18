/** Format the event's own dates, including DST and overnight end times. */
export function eventSchedule(start: string, end: string | null, timeZone: string): string {
  const date = (value: string) => new Intl.DateTimeFormat(undefined, {
    weekday: "short", month: "short", day: "numeric", timeZone,
  }).format(new Date(value));
  const time = (value: string) => new Intl.DateTimeFormat(undefined, {
    hour: "numeric", minute: "2-digit", timeZone,
  }).format(new Date(value));
  const zone = (value: string) => new Intl.DateTimeFormat("en-US", {
    timeZone, timeZoneName: "short",
  }).formatToParts(new Date(value)).find(part => part.type === "timeZoneName")!.value;
  const startZone = zone(start);
  if (!end) return `${date(start)} · ${time(start)} ${startZone}`;
  const endZone = zone(end);
  const endDate = date(start) === date(end) ? "" : `${date(end)} · `;
  return `${date(start)} · ${time(start)}${startZone !== endZone ? ` ${startZone}` : ""} – ${endDate}${time(end)} ${endZone} est.`;
}
