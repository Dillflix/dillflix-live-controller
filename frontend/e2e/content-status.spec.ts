import { test, expect } from "@playwright/test";

test("inactive unknown RedZone stays in the plan without being advertised as live or upcoming", async ({ page }) => {
  await page.route("**/api/v1/overview", async route => {
    const response = await route.fetch();
    const data = await response.json();
    const original = data.events.find((e: { content_id: string }) => e.content_id === "demo:redzone");
    const redzone = {
      ...original, active: false, playable: false, watch_entry_id: "retained-redzone",
      start_time: new Date(Date.parse(data.meta.now) - 86400000).toISOString(),
      lifecycle: { ...original.lifecycle, state: "unknown", stale: false, source: "teamarr_history" },
    };
    data.events = [redzone, {
      ...original, content_id: "test:live", title: "Confirmed live game", active: false,
      playable: true, lifecycle: { ...original.lifecycle, state: "live" },
    }];
    data.device.plan = [{ id: "retained-redzone", content_id: redzone.content_id,
      created_at: redzone.start_time, actor: { type: "admin", id: "admin", name: "Admin" } }];
    data.device.desired = null;
    data.device.observed = null;
    data.device.playback_state = "waiting";
    data.device.recovery = null;
    await route.fulfill({ response, json: data });
  });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Live 1", exact: true })).toBeVisible();
  await expect(page.getByTestId("event-demo:redzone")).toHaveCount(0);
  await expect(page.getByTestId("event-test:live")).toBeVisible();
  await page.getByRole("button", { name: "Upcoming 0", exact: true }).click();
  await expect(page.getByTestId("event-demo:redzone")).toHaveCount(0);
  await page.getByRole("button", { name: "Watch plan", exact: false }).first().click();
  await expect(page.locator(".df-plan-row").filter({ hasText: "NFL RedZone" })).toContainText("unknown");
});

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

test("phone: a reserved event outside discovery stays live until its independent status ends", async ({
  page,
  request,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.locator("summary").click();
  await page
    .getByRole("combobox", { name: "Scenario", exact: true })
    .selectOption("outside_feed");
  await expect(page.locator(".df-playing-title")).toContainText(
    "Canadiens at Maple Leafs",
  );
  await page
    .getByRole("button", {
      name: "Details for Canadiens at Maple Leafs",
      exact: true,
    })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("Outside the current feed");
  await expect(dialog).toContainText("Simulated event lifecycle");
  await expect(dialog).toContainText("Last checked");
  await page.keyboard.press("Escape");
  await request.post("/api/v1/simulation", {
    data: { action: "advance", minutes: 120 },
  });
  await expect(page.locator(".df-playing-title")).toContainText("NFL RedZone");
  await page
    .getByRole("button", { name: "Watch plan", exact: false })
    .first()
    .click();
  await expect(
    page.locator(".df-plan-row").filter({ hasText: "Canadiens" }),
  ).toHaveCount(0);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
});

test("status lookup failure is separate from schedule health and preserves the reservation", async ({
  page,
}) => {
  await page.goto("/");
  await page.locator("summary").click();
  await page
    .getByRole("combobox", { name: "Scenario", exact: true })
    .selectOption("status_outage");
  await expect(page.getByTestId("status-warning")).toContainText(
    "Your watch plan is retained",
  );
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const connections = page
    .locator(".df-panel")
    .filter({
      has: page.getByRole("heading", { name: "Connections", exact: true }),
    });
  await expect(
    connections
      .locator(".df-setting")
      .filter({ has: page.getByText("Schedule", { exact: true }) }),
  ).toContainText("ok");
  const status = connections
    .locator(".df-setting")
    .filter({ has: page.getByText("Content status", { exact: true }) });
  await expect(status).toContainText("degraded");
  await expect(status).toContainText("lookup failures");
  await page
    .getByRole("button", { name: "Watch plan", exact: false })
    .first()
    .click();
  await expect(
    page.locator(".df-plan-row").filter({ hasText: "Canadiens" }),
  ).toContainText("unknown");
});
