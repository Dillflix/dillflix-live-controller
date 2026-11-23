import { test, expect } from "@playwright/test";

test("WPT priorities can be configured before an airing is discovered", async ({ page, request }) => {
  await request.post("/api/v1/simulation", { data: { action: "scenario", scenario: "normal" } });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.getByRole("button", { name: "Priorities", exact: true }).click();
  await page.getByRole("button", { name: "Add rule", exact: true }).click();
  await expect(page.locator("option[value=wpt]")).toHaveText("World Poker Tour");
  await expect(page.getByLabel("Coverage source").locator("option[value=sportsnet_wpt]"))
    .toHaveText("World Poker Tour · Sportsnet");
});
