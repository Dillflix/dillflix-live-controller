import { test, expect } from "@playwright/test";

for (const publicView of [false, true]) {
  test(`${publicView ? "public" : "admin"} Events defaults to entitled and filters unresolved results`, async ({ page }) => {
    const path = publicView ? "/api/public/v1/overview" : "/api/v1/overview";
    await page.route(`**${path}`, async route => {
      const response = await route.fetch();
      const body = await response.json();
      const template = body.events[0];
      body.events = ["entitled", "unentitled", "queued", "checking", "no_listing"].map((kind, i) => ({
        ...template, content_id: `filter-${kind}`, title: `Filter event ${i}`, active: true,
        state: "live", lifecycle: { ...template.lifecycle, state: "live" }, playable: true,
        entitlement: { status: ["entitled", "unentitled"].includes(kind) ? kind : "unknown",
          state: ["queued", "checking"].includes(kind) ? kind : kind === "no_listing" ? "retry" : "complete",
          result: kind === "no_listing" ? "no_listing" : null, blocked: kind === "unentitled",
          can_check: true, checked_at: null, next_check_at: null, reason: null }
      }));
      await route.fulfill({ response, json: body });
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(publicView ? "/public/" : "/");
    const filter = page.getByLabel("Entitlement", { exact: true });
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
