import { test, expect } from "@playwright/test";

test("refresh page tabs, select discovery pages, persist and undo", async ({
  page,
  request,
}) => {
  await request.post("/api/v1/simulation", {
    data: { action: "scenario", scenario: "empty" },
  });
  const initial = await (await request.get("/api/v1/overview")).json();
  if (initial.device.automation !== "active")
    await request.post("/api/v1/devices/living-room/automation", {
      data: {
        command_id: crypto.randomUUID(),
        expected_revision: initial.device.revision,
        mode: "active",
      },
    });
  await page.setViewportSize({ width: 320, height: 780 });
  await page.goto("/");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Refresh available pages" }).click();
  await expect(
    page.getByLabel("Discover on DAZN", { exact: true }),
  ).toBeVisible({ timeout: 15000 });
  await expect(
    page.getByLabel("Discover on DAZN", { exact: true }),
  ).not.toBeChecked();
  await page.getByLabel("Discover on DAZN", { exact: true }).click();
  await expect(
    page.getByLabel("Discover on DAZN", { exact: true }),
  ).toBeChecked();
  await page.reload();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(
    page.getByLabel("Discover on DAZN", { exact: true }),
  ).toBeChecked();
  const exported = await (
    await request.get("/api/v1/devices/living-room/configuration")
  ).json();
  expect(
    exported.configuration.preferences.prime_discovery.enabled_pages,
  ).toEqual(["dazn"]);
  await expect(page.locator(".df-playing-title")).toContainText(
    "Simulated live sports",
    { timeout: 15000 },
  );
  await page.getByRole("button", { name: "Refresh available pages" }).click();
  await expect(
    page.getByText(
      "Refresh queued. It will run when automation owns an idle device.",
    ),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Undo last edit", exact: true })
    .click();
  await expect(
    page.getByLabel("Discover on DAZN", { exact: true }),
  ).not.toBeChecked();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/prime-discovery-phone.png",
    fullPage: true,
  });
});
