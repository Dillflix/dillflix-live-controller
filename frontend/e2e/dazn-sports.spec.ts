import { test, expect } from "@playwright/test";

test("DAZN squash and MMA priorities are available without scheduled events", async ({ page, request }) => {
  await request.post("/api/v1/simulation", { data: { action: "scenario", scenario: "normal" } });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.getByRole("button", { name: "Priorities", exact: true }).click();
  await page.getByRole("button", { name: "Add rule", exact: true }).click();
  await expect(page.locator("option[value=nsl]")).toHaveText("National Squash League");
  await expect(page.locator("option[value=beatdown]")).toHaveText("Beatdown MMA");
  await expect(page.getByLabel("Coverage source").locator("option[value=dazn_sports]"))
    .toHaveText("Squash & MMA · DAZN");
});
