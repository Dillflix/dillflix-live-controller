import { test, expect } from "@playwright/test";

test("tennis day coverage is selectable on a phone with an unknown end", async ({ page, request }) => {
  const loaded = await request.post("/api/v1/simulation", {
    data: { action: "scenario", scenario: "tennis" },
  });
  expect(loaded.ok()).toBe(true);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  const card = page.getByTestId("event-demo:tennis");
  await expect(card).toContainText("Beijing Open: Day 4");
  await expect(card).toContainText("Tennis");
  await card.getByRole("button", { name: "Play now" }).click();
  await expect.poll(async () => {
    const data = await (await request.get("/api/v1/overview")).json();
    return data.device.plan[0]?.content_id;
  }).toBe("demo:tennis");
  await page.reload();
  await expect(page.getByTestId("event-demo:tennis")).toBeVisible();
  await expect(page.getByRole("region", { name: "Now playing" })).toContainText("Beijing Open: Day 4");
  await page.screenshot({ path: "test-results/tennis-phone.png", fullPage: true });
  await page.getByRole("button", { name: "Priorities", exact: true }).click();
  await page.getByRole("button", { name: "Add rule", exact: true }).click();
  await expect(page.getByLabel("Coverage source").locator("option[value=dazn_tennis]")).toHaveText("Tennis coverage · DAZN");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
