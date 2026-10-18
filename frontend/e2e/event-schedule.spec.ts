import { test, expect } from "@playwright/test";
import { eventSchedule } from "../src/eventSchedule";

test("event timezone follows its date rather than today's offset", () => {
  expect(eventSchedule("2026-07-18T02:30:00Z", null, "America/Vancouver")).toContain("PDT");
  expect(eventSchedule("2026-12-18T02:30:00Z", null, "America/Vancouver")).toContain("PST");
  expect(eventSchedule("2026-07-18T02:30:00Z", null, "America/Toronto")).toContain("EDT");
});

test("a DST transition identifies the zone at both ends", () => {
  expect(eventSchedule("2026-11-01T08:30:00Z", "2026-11-01T10:30:00Z", "America/Vancouver"))
    .toMatch(/1:30 AM PDT.*2:30 AM PST est\./);
});

test("overnight events include the end date in the schedule", () => {
  const text = eventSchedule("2026-07-18T06:30:00Z", "2026-07-18T08:30:00Z", "America/Vancouver");
  expect(text).toMatch(/Jul 17.*11:30 PM.*Jul 18.*1:30 AM PDT est\./);
});
