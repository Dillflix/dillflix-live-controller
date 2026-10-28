import { test, expect } from "@playwright/test";

test("attempt limit defaults to two, persists and supports undo on a phone", async ({ page, request }) => {
  await request.post("/api/v1/simulation", { data: { action: "scenario", scenario: "normal" } });
  const before = await (await request.get("/api/v1/overview")).json();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const limit = page.getByLabel("Unsuccessful playback attempt limit");
  await expect(limit).toHaveValue("2");
  await limit.selectOption("3");
  await expect.poll(async () => (await (await request.get("/api/v1/overview")).json()).device.preferences.event_attempt_limit).toBe(3);
  await page.reload();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(limit).toHaveValue("3");
  const after = await (await request.get("/api/v1/overview")).json();
  expect(after.device.plan).toEqual(before.device.plan);
  await page.getByRole("button", { name: "Undo last edit", exact: true }).click();
  await expect(limit).toHaveValue("2");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
});

test("exhausted events explain the limit and keep Play now available", async ({ page, request }) => {
  await request.post("/api/v1/simulation", { data: { action: "scenario", scenario: "normal" } });
  await page.route("**/api/v1/overview", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const event = body.events.find((e: { content_id: string }) => e.content_id === "demo:lions");
    event.playback_retry = { attempts: 2, limit: 2, exhausted: true,
      message: "Automatic attempts stopped after 2 unsuccessful cycles. Watch plan retained; use Play now to try again." };
    await route.fulfill({ response, json: body });
  });
  await page.goto("/");
  const card = page.getByTestId("event-demo:lions");
  await expect(card.getByText("Attempt limit reached", { exact: true })).toBeVisible();
  await expect(card.getByText(/use Play now to try again/)).toBeVisible();
  await expect(card.getByRole("button", { name: "Play now", exact: true })).toBeEnabled();
});
