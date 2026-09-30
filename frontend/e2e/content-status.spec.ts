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
  ).toContainText("ended");
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
