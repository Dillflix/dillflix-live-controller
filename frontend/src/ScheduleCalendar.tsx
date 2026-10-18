import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  CalendarDays,
  ChevronLeft,
  ChevronRight,
  LayoutGrid,
  X,
} from "lucide-react";
import { eventSchedule } from "./eventSchedule";
import { leagueName } from "./leagues";

export function useScheduleView() {
  const [view, setView] = useState<"cards" | "calendar">(() => {
    try {
      return localStorage.getItem("df-schedule-view") === "calendar"
        ? "calendar"
        : "cards";
    } catch {
      return "cards";
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem("df-schedule-view", view);
    } catch {
      /* Storage is optional. */
    }
  }, [view]);
  return [view, setView] as const;
}

export function ScheduleViewToggle({
  value,
  onChange,
}: {
  value: "cards" | "calendar";
  onChange: (value: "cards" | "calendar") => void;
}) {
  return (
    <div className="df-view-toggle" role="group" aria-label="Schedule view">
      <button
        type="button"
        aria-pressed={value === "cards"}
        onClick={() => onChange("cards")}
      >
        <LayoutGrid size={15} /> Cards
      </button>
      <button
        type="button"
        aria-pressed={value === "calendar"}
        onClick={() => onChange("calendar")}
      >
        <CalendarDays size={15} /> Calendar
      </button>
    </div>
  );
}

/** Date keys are civil dates in the display zone, never the browser's local zone. */
export function calendarDate(value: string, timeZone: string): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    timeZone,
  }).formatToParts(new Date(value));
  const part = (name: string) => parts.find((p) => p.type === name)!.value;
  return `${part("year")}-${part("month")}-${part("day")}`;
}

export function calendarDayOffset(day: string, offset: number): string {
  const date = new Date(`${day}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + offset);
  return date.toISOString().slice(0, 10);
}

interface CalendarEvent {
  content_id: string;
  title: string;
  league: string;
  start_time: string;
  expected_end_time: string | null;
}

export function ScheduleCalendar<T extends CalendarEvent>({
  events,
  now,
  timeZone,
  renderEvent,
}: {
  events: T[];
  now: string;
  timeZone: string;
  renderEvent: (event: T) => ReactNode;
}) {
  const today = calendarDate(now, timeZone);
  const [page, setPage] = useState(0);
  const [pickedDay, setPickedDay] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const detail = useRef<HTMLElement>(null);
  // Nonterminal events that started before today stay visible; an estimated end
  // never removes them or changes their playback eligibility.
  const dayOf = (event: T) => {
    const start = calendarDate(event.start_time, timeZone);
    return start < today ? today : start;
  };
  const lastDay = events.reduce(
    (last, event) => (dayOf(event) > last ? dayOf(event) : last),
    today,
  );
  const maxPage = Math.floor(
    (Date.parse(lastDay) - Date.parse(today)) / (7 * 86400000),
  );
  const currentPage = Math.min(page, maxPage);
  const days = Array.from({ length: 7 }, (_, i) =>
    calendarDayOffset(today, currentPage * 7 + i),
  );
  const sorted = [...events].sort(
    (a, b) =>
      a.start_time.localeCompare(b.start_time) ||
      a.content_id.localeCompare(b.content_id),
  );
  const groups = new Map(
    days.map((day) => [day, sorted.filter((e) => dayOf(e) === day)]),
  );
  const activeDay =
    pickedDay && days.includes(pickedDay)
      ? pickedDay
      : days.find((day) => groups.get(day)!.length) || days[0];
  const selectedEvent = events.find((e) => e.content_id === selected);
  const dateLabel = (day: string, weekday = false) =>
    new Intl.DateTimeFormat(undefined, {
      ...(weekday ? { weekday: "short" as const } : {}),
      month: "short",
      day: "numeric",
      timeZone: "UTC",
    }).format(new Date(`${day}T12:00:00Z`));
  const startLabel = (event: T) =>
    new Intl.DateTimeFormat(undefined, {
      hour: "numeric",
      minute: "2-digit",
      timeZone,
      timeZoneName: "short",
    }).format(new Date(event.start_time));
  useEffect(() => {
    if (selected) detail.current?.focus({ preventScroll: false });
  }, [selected]);
  return (
    <section className="df-calendar" aria-label="Event calendar">
      <div className="df-calendar-toolbar">
        <h3>
          {dateLabel(days[0])} – {dateLabel(days[6])}
        </h3>
        <div className="df-row">
          <button
            type="button"
            className="df-button"
            onClick={() => {
              setPage(0);
              setPickedDay(today);
            }}
          >
            Today
          </button>
          <button
            type="button"
            className="df-button"
            aria-label="Previous seven days"
            disabled={currentPage === 0}
            onClick={() => setPage(currentPage - 1)}
          >
            <ChevronLeft size={16} />
          </button>
          <button
            type="button"
            className="df-button"
            aria-label="Next seven days"
            disabled={currentPage >= maxPage}
            onClick={() => setPage(currentPage + 1)}
          >
            <ChevronRight size={16} />
          </button>
        </div>
      </div>
      <div
        className="df-calendar-day-picker"
        role="group"
        aria-label="Calendar day"
      >
        {days.map((day) => (
          <button
            type="button"
            key={day}
            aria-label={dateLabel(day, true)}
            aria-pressed={activeDay === day}
            onClick={() => setPickedDay(day)}
          >
            <span>
              {new Intl.DateTimeFormat(undefined, {
                weekday: "short",
                timeZone: "UTC",
              }).format(new Date(`${day}T12:00:00Z`))}
            </span>
            <strong>{Number(day.slice(-2))}</strong>
            <small>{groups.get(day)!.length}</small>
          </button>
        ))}
      </div>
      <div className="df-calendar-week">
        {days.map((day) => (
          <section
            className="df-calendar-day"
            key={day}
            data-active={day === activeDay}
            aria-label={dateLabel(day, true)}
          >
            <header>
              <h4>{day === today ? "Today" : dateLabel(day, true)}</h4>
              <span>{groups.get(day)!.length} events</span>
            </header>
            <div className="df-calendar-events">
              {groups.get(day)!.length ? (
                groups.get(day)!.map((event) => (
                  <button
                    type="button"
                    className="df-calendar-event"
                    key={event.content_id}
                    aria-pressed={selected === event.content_id}
                    data-testid={`calendar-${event.content_id}`}
                    onClick={() => setSelected(event.content_id)}
                    aria-label={`${event.title}, ${eventSchedule(event.start_time, event.expected_end_time, timeZone)}`}
                  >
                    <time dateTime={event.start_time}>
                      {calendarDate(event.start_time, timeZone) < today
                        ? "Started earlier"
                        : startLabel(event)}
                    </time>
                    <strong>{event.title}</strong>
                    <span>{leagueName(event.league)}</span>
                  </button>
                ))
              ) : (
                <p className="df-calendar-empty">No matching events</p>
              )}
            </div>
          </section>
        ))}
      </div>
      {selectedEvent && (
        <section
          className="df-calendar-selection"
          ref={detail}
          tabIndex={-1}
          aria-label="Selected event"
        >
          <div className="df-section-head">
            <h3>Selected event</h3>
            <button
              type="button"
              className="df-button"
              aria-label="Close selected event"
              onClick={() => setSelected(null)}
            >
              <X size={16} />
            </button>
          </div>
          {renderEvent(selectedEvent)}
        </section>
      )}
    </section>
  );
}
