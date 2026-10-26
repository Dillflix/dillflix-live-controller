import { test, expect } from "@playwright/test";
import { disableBrowserStreams } from "./public-stream-fixture";
test.beforeEach(async ({ page }) => disableBrowserStreams(page));

for (const publicView of [false, true]) {
  test(`${publicView ? "public" : "admin"} Events defaults to entitled and filters unresolved results`, async ({ page }) => {
    const path = publicView ? "/api/public/v1/overview" : "/api/v1/overview";
    await page.route(`**${path}`, async route => {
      const response = publicView ? undefined : await route.fetch();
      const body = publicView ? {
        revision: 1, viewer: { name: "Viewer", guest: false }, timezone: "America/Vancouver",
        permissions: { play_now: true, add_to_plan: true }, actions_message: "",
        now_playing: { event: null, verified: false, simulated: true, switching: false },
        events: [{ content_id: "template", title: "Example", league: "nfl", phase: "regular",
          kind: "event", start_time: "2026-10-16T19:00:00Z", expected_end_time: null,
          state: "live", active: true, scores: [], playable: true, planned: false,
          viewing_option_count: 1, teams: [] }],
      } : await response!.json();
      const template = body.events[0];
      body.events = ["entitled", "unentitled", "queued", "checking", "no_listing"].map((kind, i) => ({
        ...template, content_id: `filter-${kind}`, title: `Filter event ${i}`, active: true,
        state: "live", lifecycle: { ...template.lifecycle, state: "live" }, playable: true,
        entitlement: { status: ["entitled", "unentitled"].includes(kind) ? kind : "unknown",
          state: ["queued", "checking"].includes(kind) ? kind : kind === "no_listing" ? "retry" : "complete",
          result: kind === "no_listing" ? "no_listing" : null, blocked: kind === "unentitled",
          can_check: true, checked_at: null, next_check_at: null, reason: null }
      }));
      if (publicView) body.events.push({ ...template, content_id: "filter-unchecked", entitlement: null });
      await route.fulfill({ response, json: body });
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(publicView ? "/public/" : "/");
    const filter = page.getByLabel("Entitlement", { exact: true });
    if (publicView) {
      await expect(filter).toHaveCount(0);
      await expect(page.locator('[data-testid^="event-filter-"]')).toHaveCount(1);
      await expect(page.getByTestId("event-filter-entitled")).toBeVisible();
      await expect(page.locator(".df-event-footer .df-entitlement-indicator")).toHaveCount(0);
      await page.getByRole("searchbox").fill("no such event");
      await page.getByRole("button", { name: "Clear filters" }).click();
      await expect(page.locator('[data-testid^="event-filter-"]')).toHaveCount(1);
      await page.screenshot({ path: "test-results/public-entitled-only.png" });
      return;
    }
    await expect(filter).toHaveValue("entitled");
    await expect(page.getByTestId("event-filter-entitled")).toBeVisible();
    await expect(page.getByTestId("event-filter-queued")).toHaveCount(0);
    await filter.selectOption("all");
    await expect(page.locator('[data-testid^="event-filter-"]')).toHaveCount(5);
    for (const kind of ["unentitled", "queued", "checking", "no_listing"]) {
      await filter.selectOption(kind);
      await expect(page.locator('[data-testid^="event-filter-"]')).toHaveCount(1);
      await expect(page.getByTestId(`event-filter-${kind}`)).toBeVisible();
    }
    await filter.selectOption("unknown");
    await expect(page.locator('[data-testid^="event-filter-"]')).toHaveCount(3);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `test-results/entitlement-filter-${publicView ? "public" : "admin"}.png` });
  });
}
