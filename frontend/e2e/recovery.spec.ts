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

test("phone: coverage handoff keeps the event protected and explains the new request", async ({
  page,
  request,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.locator("summary").click();
  await page
    .getByRole("combobox", { name: "Scenario", exact: true })
    .selectOption("coverage_switch");
  await expect(page.locator(".df-playing")).toContainText("Simulated live");
  await expect(page.locator(".df-playing-title")).toContainText(
    "PGA Tour final round",
  );
  const before = await (await request.get("/api/v1/overview")).json();
  expect(before.device.observed.viewing_option_id).toBe("demo-option:golf:tsn");
  await page.getByRole("button", { name: "+15 min", exact: true }).click();
  await expect
    .poll(async () => {
      const state = await (await request.get("/api/v1/overview")).json();
      return (
        state.device.observed?.verified &&
        state.device.observed?.viewing_option_id
      );
    })
    .toBe("demo-option:golf:sportsnet");
  const after = await (await request.get("/api/v1/overview")).json();
  expect(after.device.plan).toEqual(before.device.plan);
  expect(after.device.started_at).toBe(before.device.started_at);
  expect(after.device.last_switch_at).toBe(before.device.last_switch_at);
  expect(after.playback_job.id).not.toBe(before.playback_job.id);
  await expect(page.locator(".df-playing-title")).toContainText("Protected");
  await page
    .getByRole("button", { name: "Playback details", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toContainText(
    "Updated coverage for the same event",
  );
  await expect(page.getByRole("dialog")).toContainText("sportsnet");
  await page.keyboard.press("Escape");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
});

test("a prolonged playback outage preserves the latest manual choice and resumes on reconnect", async ({
  page,
  request,
}) => {
  test.setTimeout(40000);
  await page.setViewportSize({ width: 320, height: 780 });
  await page.goto("/");
  await page.locator("summary").click();
  await page
    .getByRole("combobox", { name: "Scenario", exact: true })
    .selectOption("device_outage");
  await expect(page.locator(".df-playing")).toContainText("Simulated live");
  await expect(page.getByTestId("playback-warning")).toContainText(
    "your watch plan is saved",
  );
  const before = await (await request.get("/api/v1/overview")).json();
  await page
    .getByTestId("event-demo:lions")
    .getByRole("button", { name: "Play now", exact: true })
    .click();
  await expect
    .poll(async () => {
      const state = await (await request.get("/api/v1/overview")).json();
      return state.device.plan[0].content_id;
    })
    .toBe("demo:lions");
  await expect(page.locator(".df-playing")).toContainText("unverified", {
    timeout: 20000,
  });
  const waiting = await (await request.get("/api/v1/overview")).json();
  expect(waiting.playback_job.id).toBe(before.playback_job.id);
  expect(waiting.device.observed.verified).toBe(false);
  await page
    .getByRole("button", { name: "Reconnect simulator", exact: true })
    .click();
  await expect(page.getByTestId("playback-warning")).toBeHidden();
  await expect(page.locator(".df-playing-title")).toContainText(
    "Lions at Bills",
  );
  await expect(page.locator(".df-playing")).toContainText("Simulated live");
  const after = await (await request.get("/api/v1/overview")).json();
  expect(after.device.observed.content_id).toBe("demo:lions");
  expect(after.device.plan).toEqual(waiting.device.plan);
  expect(after.playback_job.delivery_attempts).toBe(1);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
});
