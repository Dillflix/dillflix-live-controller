import { test, expect } from "@playwright/test";

test.beforeEach(async ({ request }) => {
  await request.post("/api/v1/simulation", {
    data: { action: "scenario", scenario: "outside_feed" },
  });
  const { device } = await (await request.get("/api/v1/overview")).json();
  await request.post("/api/v1/devices/living-room/automation", {
    data: {
      command_id: crypto.randomUUID(),
      expected_revision: device.revision,
      mode: "active",
    },
  });
});

for (const width of [1280, 390]) {
  test(`manual completion clears paused playback and can be undone at ${width}px`, async ({
    page,
    request,
  }) => {
    await page.setViewportSize({ width, height: 844 });
    await page.goto("/");
    await expect(page.locator(".df-playing-title")).toContainText(
      "Canadiens at Maple Leafs",
    );
    await expect(page.locator(".df-playing")).toContainText("Simulated live");
    await page.getByRole("button", { name: "Pause", exact: true }).click();
    const { device: before } = await (
      await request.get("/api/v1/overview")
    ).json();
    await page
      .getByRole("button", { name: "Mark event finished", exact: true })
      .click();
    await expect(page.locator(".df-playing-title")).toContainText(
      "Waiting for live sports",
    );
    await expect(
      page.getByRole("button", { name: "Resume", exact: true }),
    ).toBeVisible();
    await expect(page.getByTestId("status-warning")).toHaveCount(0);
    await page.reload();
    await expect(page.locator(".df-playing-title")).toContainText(
      "Waiting for live sports",
    );
    const { device: after, events } = await (
      await request.get("/api/v1/overview")
    ).json();
    expect(after.plan).toEqual(before.plan);
    expect(after.preferences).toEqual(before.preferences);
    expect(after.automation).toBe("paused");
    expect(
      events.find(
        (e: { content_id: string }) => e.content_id === "demo:canadiens",
      ).lifecycle.source,
    ).toBe("manual_completion");
    await page
      .getByRole("button", { name: "Watch plan", exact: false })
      .first()
      .click();
    await expect(
      page.locator(".df-plan-row").filter({ hasText: "Canadiens" }),
    ).toContainText("ended");
    await page
      .getByRole("button", { name: "Undo last edit", exact: true })
      .click();
    await expect(
      page.locator(".df-plan-row").filter({ hasText: "Canadiens" }),
    ).toContainText("live");
    await expect(
      page.getByRole("button", { name: "Resume", exact: true }),
    ).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBeTruthy();
  });
}

test("event details can complete active playback and automation selects another live event", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.locator(".df-playing-title")).toContainText(
    "Canadiens at Maple Leafs",
  );
  await page
    .getByRole("button", {
      name: "Details for Canadiens at Maple Leafs",
      exact: true,
    })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Mark event finished", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.locator(".df-playing-title")).toContainText("NFL RedZone");
  await page
    .getByRole("button", { name: "Watch plan", exact: false })
    .first()
    .click();
  await expect(
    page.locator(".df-plan-row").filter({ hasText: "Canadiens" }),
  ).toContainText("ended");
});
