import { test, expect } from "@playwright/test";

for (const publicView of [false, true]) {
  test(`${publicView ? "public" : "admin"} tab counts match filtered cards in both tabs`, async ({ page }) => {
    const path = publicView ? "/api/public/v1/overview" : "/api/v1/overview";
    await page.route(`**${path}`, async route => {
      const response = publicView ? undefined : await route.fetch();
      const body = publicView ? {
        revision: 1, viewer: { name: "Viewer", guest: false }, timezone: "America/Vancouver",
        permissions: { play_now: true, add_to_plan: true }, actions_message: "",
        now_playing: { event: null, verified: false, simulated: true, switching: false },
        events: [{ kind: "event", phase: "regular", scores: [], planned: false,
          viewing_option_count: 1, teams: [], expected_end_time: null }],
      } : await response!.json();
      const template = body.events[0];
      const now = Date.parse(body.meta?.now || "2026-10-16T19:00:00Z");
      body.events = [
        ["live-nfl", "nfl", "live", true, "entitled"],
        ["live-nhl", "nhl", "live", true, "entitled"],
        // The list treats this as live even without a literal live state.
        ["started-nfl", "nfl", "scheduled", true, "entitled"],
        // Rule-based broadcasts such as RedZone have a known start but unknown live status.
        ["future-nfl", "nfl", "unknown", false, "entitled"],
        ["future-nhl", "nhl", "scheduled", false, "entitled"],
        ["denied-live", "nfl", "live", true, "unentitled"],
        ["denied-future", "nfl", "scheduled", false, "unentitled"],
        ["ended", "nfl", "ended", true, "entitled"],
        ["cancelled", "nfl", "cancelled", false, "entitled"],
      ].map(([id, league, state, playable, status]) => ({
        ...template, content_id: `counts-${id}`, title: `Count event ${id}`, league,
        active: true, state, lifecycle: { ...template.lifecycle, state }, playable,
        start_time: new Date(now + (playable ? -3600000 : 3600000)).toISOString(),
        teams: [{ key: "team", name: "Team", city: "Test", full_name: "Searchable Team",
          abbreviation: "TST", logo_url: null }],
        entitlement: { status, state: "complete", blocked: status === "unentitled" },
      }));
      await route.fulfill({ response, json: body });
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(publicView ? "/public/" : "/");
    const tabs = page.locator(".df-segmented");
    const cards = page.locator('[data-testid^="event-counts-"]');
    const checkCounts = async (live: number, upcoming: number) => {
      const liveTab = tabs.getByRole("button", { name: `Live ${live}`, exact: true });
      const upcomingTab = tabs.getByRole("button", { name: `Upcoming ${upcoming}`, exact: true });
      await expect(liveTab).toBeVisible();
      await expect(upcomingTab).toBeVisible();
      await liveTab.click();
      await expect(cards).toHaveCount(live);
      await expect(page.locator(".df-section-head")).toContainText(`${live} events`);
      await upcomingTab.click();
      await expect(cards).toHaveCount(upcoming);
      await expect(page.locator(".df-section-head")).toContainText(`${upcoming} events`);
    };
    await checkCounts(3, 2);
    const unknownStatus = page.getByTestId("event-counts-future-nfl")
      .locator(".df-event-top .df-event-status");
    await expect(unknownStatus).toContainText(/\d+:\d{2} [AP]M/);
    await expect(unknownStatus).not.toContainText(/unknown|Live/);
    await expect(cards.first().locator(".df-event-meta")).toContainText(/P[DS]T/);
    await expect(page.getByText(/\d+ viewing options?/)).toHaveCount(0);
    await expect(page.getByText(/Times shown in/)).toHaveCount(0);
    await page.screenshot({ path: `test-results/entitlement-schedule-${publicView ? "public" : "admin"}.png` });
    await page.locator(".df-filters").getByRole("button", { name: "NFL", exact: true }).click();
    await checkCounts(2, 1);
    await page.getByRole("searchbox").fill("Searchable Team");
    await checkCounts(2, 1);
    await page.getByRole("searchbox").fill("future");
    await checkCounts(0, 1);
    await page.getByRole("searchbox").fill("no such event");
    await checkCounts(0, 0);
    await page.getByRole("searchbox").fill("");
    await page.locator(".df-filters").getByRole("button", { name: "All sports", exact: true }).click();
    await checkCounts(3, 2);
    if (!publicView) {
      const entitlement = page.getByLabel("Entitlement", { exact: true });
      await entitlement.selectOption("all");
      await checkCounts(4, 3);
      await entitlement.selectOption("unentitled");
      await checkCounts(1, 1);
      await entitlement.selectOption("queued");
      await checkCounts(0, 0);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `test-results/entitlement-counts-${publicView ? "public" : "admin"}.png` });
  });
}
