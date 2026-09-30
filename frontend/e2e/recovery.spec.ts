import { test, expect } from "@playwright/test";

test.beforeEach(async ({ request }) => {
  await request.post("/api/v1/simulation", {
    data: { action: "scenario", scenario: "normal" },
  });
  const { device } = await (await request.get("/api/v1/overview")).json();
  if (device.automation === "paused")
    await request.post("/api/v1/devices/living-room/automation", {
      data: {
        command_id: crypto.randomUUID(),
        expected_revision: device.revision,
        mode: "active",
      },
    });
});

test("navigation timeout shows progress, records the reason and falls back live", async ({
  page,
  request,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.locator("summary").click();
  await page
    .getByRole("combobox", { name: "Scenario", exact: true })
    .selectOption("timeout");
  await expect(page.locator(".df-playing")).toContainText(
    "waiting for live verification",
  );
  await page
    .getByRole("button", { name: "Playback details", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toContainText("Navigation deadline");
  await page.keyboard.press("Escape");
  await expect
    .poll(
      async () => {
        const body = await (
          await request.get("/api/v1/devices/living-room/jobs")
        ).json();
        return body.items.some(
          (j: { state: string }) => j.state === "timed_out",
        );
      },
      { timeout: 12000 },
    )
    .toBeTruthy();
  await expect(page.locator(".df-playing-title")).toContainText(
    "Lions at Bills",
  );
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  await expect(
    page
      .locator(".df-activity")
      .filter({ hasText: "Navigation exceeded its deadline" })
      .first(),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
});

test("a replay result is rejected and the reason is visible in activity", async ({
  page,
  request,
}) => {
  await page.goto("/");
  await page.locator("summary").click();
  await page
    .getByRole("combobox", { name: "Scenario", exact: true })
    .selectOption("replay");
  await expect
    .poll(async () => {
      const body = await (
        await request.get("/api/v1/devices/living-room/jobs")
      ).json();
      return body.items.some(
        (j: { state: string; error: string }) =>
          j.state === "rejected" &&
          j.error === "Requested live playback was not verified",
      );
    })
    .toBeTruthy();
  await expect(page.locator(".df-playing-title")).toContainText(
    "Lions at Bills",
  );
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  await expect(
    page
      .locator(".df-activity")
      .filter({ hasText: "Requested live playback was not verified" })
      .first(),
  ).toBeVisible();
});
