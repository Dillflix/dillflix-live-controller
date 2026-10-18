import { test, expect } from "@playwright/test";
import { calendarDate, calendarDayOffset } from "../src/ScheduleCalendar";

test("calendar days follow the display timezone across midnight and DST", () => {
  expect(calendarDate("2025-03-09T07:30:00Z", "America/Vancouver")).toBe(
    "2025-03-08",
  );
  expect(calendarDate("2025-03-09T07:30:00Z", "America/Toronto")).toBe(
    "2025-03-09",
  );
  expect(calendarDayOffset("2025-03-08", 2)).toBe("2025-03-10");
  expect(calendarDayOffset("2025-12-31", 1)).toBe("2026-01-01");
});

for (const publicView of [false, true])
  for (const width of [1280, 390, 320]) {
    test(`${publicView ? "public" : "admin"} calendar ${width}px: dates, filters and shared event actions`, async ({
      page,
    }) => {
      const now = "2026-10-18T06:00:00Z"; // Oct 17, 11 PM in the display timezone.
      const prefix = publicView ? "/api/public/v1" : "/api/v1";
      const sent: any[] = [];
      await page.route(`**${prefix}/overview`, async (route) => {
        const response = publicView ? undefined : await route.fetch();
        const body = publicView
          ? {
              revision: 1,
              now,
              viewer: { name: "Viewer", guest: false },
              timezone: "America/Vancouver",
              permissions: { play_now: true, add_to_plan: true },
              actions_message: "Shared watch plan",
              now_playing: {
                event: null,
                verified: false,
                simulated: true,
                switching: false,
              },
              events: [
                {
                  kind: "broadcast",
                  phase: "regular",
                  scores: [],
                  planned: false,
                  viewing_option_count: 1,
                  teams: [],
                  expected_end_time: null,
                },
              ],
            }
          : await response!.json();
        if (!publicView) {
          body.meta.now = now;
          body.device.preferences.timezone = "America/Vancouver";
          body.device.manual_control = null;
        }
        const template = body.events[0];
        body.events = [
          [
            "night",
            "Overnight tennis",
            "tennis",
            "2026-10-18T06:30:00Z",
            "2026-10-18T08:30:00Z",
            "entitled",
          ],
          [
            "redzone",
            "NFL RedZone",
            "nfl",
            "2026-10-18T17:00:00Z",
            "2026-10-19T00:00:00Z",
            "entitled",
          ],
          [
            "edge",
            "Next Saturday football",
            "nfl",
            "2026-10-24T17:00:00Z",
            null,
            "entitled",
          ],
          [
            "denied",
            "Unentitled football",
            "nfl",
            "2026-10-18T18:00:00Z",
            null,
            "unentitled",
          ],
        ].map(([id, title, league, start_time, expected_end_time, status]) => ({
          ...template,
          content_id: `cal-${id}`,
          title,
          league,
          start_time,
          expected_end_time,
          teams: [],
          active: true,
          kind: "broadcast",
          watch_entry_id: null,
          planned: false,
          state: "unknown",
          lifecycle: { ...template.lifecycle, state: "unknown" },
          playable: false,
          entitlement: {
            status,
            state: "complete",
            blocked: status !== "entitled",
            can_check: true,
          },
        }));
        await route.fulfill({ response, json: body });
      });
      await page.route(`**${prefix}/**watch-plan**`, async (route) => {
        const request = route.request().postDataJSON();
        if (route.request().url().endsWith("/preview")) {
          await route.fulfill({
            json: {
              revision: request.expected_revision,
              conflicts: [],
              segments: [],
              unknown_timing: [],
            },
          });
        } else {
          sent.push(request);
          await route.fulfill({ json: { accepted: true } });
        }
      });
      await page.setViewportSize({ width, height: 1000 });
      await page.goto(publicView ? "/public/" : "/");
      await page
        .getByRole("button", { name: "Upcoming 3", exact: true })
        .click();
      await page.getByRole("button", { name: "Calendar", exact: true }).click();
      const calendar = page.getByRole("region", {
        name: "Event calendar",
        exact: true,
      });
      await expect(calendar).toBeVisible();
      await expect(calendar.locator(".df-calendar-day:visible")).toHaveCount(
        width > 900 ? 7 : 1,
      );
      await expect(page.getByTestId("calendar-cal-denied")).toHaveCount(0);
      await expect(page.getByTestId("calendar-cal-night")).toBeVisible();
      await page.getByTestId("calendar-cal-night").click();
      const selection = page.getByRole("region", {
        name: "Selected event",
        exact: true,
      });
      await expect(selection.locator(".df-event-meta")).toContainText(
        /Oct 17.*Oct 18.*PDT est\./,
      );
      await selection
        .getByRole("button", { name: "Add to plan", exact: true })
        .click();
      await expect.poll(() => sent.length).toBe(1);
      expect(publicView ? sent[0].content_id : sent[0].action.content_id).toBe(
        "cal-night",
      );
      await page.getByRole("button", { name: "Cards", exact: true }).click();
      await expect(page.locator('[data-testid^="event-cal-"]')).toHaveCount(3);
      await page.getByRole("button", { name: "Calendar", exact: true }).click();
      await page
        .locator(".df-filters")
        .getByRole("button", { name: "NFL", exact: true })
        .click();
      await expect(page.getByTestId("calendar-cal-night")).toHaveCount(0);
      await expect(page.getByTestId("calendar-cal-redzone")).toBeVisible();
      await page.getByRole("searchbox").fill("RedZone");
      await expect(page.locator('[data-testid^="calendar-cal-"]')).toHaveCount(
        1,
      );
      await expect(
        page.getByRole("button", { name: "Next seven days", exact: true }),
      ).toBeDisabled();
      await page.getByRole("searchbox").fill("");
      await page
        .getByRole("button", { name: "Next seven days", exact: true })
        .click();
      await expect(page.getByTestId("calendar-cal-edge")).toBeVisible();
      await page
        .getByRole("button", { name: "Previous seven days", exact: true })
        .click();
      await expect(page.getByTestId("calendar-cal-redzone")).toBeVisible();
      await page
        .locator(".df-filters")
        .getByRole("button", { name: "All sports", exact: true })
        .click();
      if (width <= 900) {
        await page
          .getByRole("group", { name: "Calendar day" })
          .getByRole("button", { name: "Sun, Oct 18", exact: true })
          .click();
      }
      await expect(page.getByTestId("calendar-cal-redzone")).toBeVisible();
      if (publicView)
        await expect(
          page.getByLabel("Entitlement", { exact: true }),
        ).toHaveCount(0);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await calendar.scrollIntoViewIfNeeded();
      await page.screenshot({
        path: `test-results/entitlement-calendar-${publicView ? "public" : "admin"}-${width}.png`,
      });
      await page.reload();
      await expect(
        page.getByRole("button", { name: "Calendar", exact: true }),
      ).toHaveAttribute("aria-pressed", "true");
    });
  }
