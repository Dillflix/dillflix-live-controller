import { test, expect } from "@playwright/test";

test("unconfirmed future broadcasts appear in Upcoming without a Play now action", async ({
  page,
}) => {
  await page.route("**/api/v1/overview", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const golf = body.events.find(
      (e: { content_id: string }) => e.content_id === "demo:golf",
    );
    golf.start_time = new Date(
      new Date(body.meta.now).getTime() + 7200000,
    ).toISOString();
    golf.lifecycle.state = "unknown";
    golf.playable = false;
    await route.fulfill({ response, json: body });
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "What's on" })).toBeVisible();
  await expect(page.getByTestId("event-demo:golf")).toHaveCount(0);
  await page
    .locator(".df-segmented")
    .getByRole("button", { name: "Upcoming", exact: true })
    .click();
  const card = page.getByTestId("event-demo:golf");
  await expect(card).toBeVisible();
  await expect(card.getByRole("button", { name: "Add to plan" })).toBeVisible();
  await expect(card.getByRole("button", { name: "Play now" })).toHaveCount(0);
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

for (const [name, width, height] of [
  ["desktop", 1280, 1000],
  ["phone", 390, 844],
  ["narrow-phone", 320, 780],
] as const) {
  test(`${name}: manual overlap, persisted order, live playback and no horizontal overflow`, async ({
    page,
    request,
  }) => {
    await page.setViewportSize({ width, height });
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto("/");
    await expect(
      page.getByRole("heading", { name: "What's on" }),
    ).toBeVisible();
    await page
      .locator(".df-segmented")
      .getByRole("button", { name: "Upcoming", exact: true })
      .click();
    await page
      .getByTestId("event-demo:jays")
      .getByRole("button", { name: "Add to plan" })
      .click();
    const dialog = page.getByRole("dialog");
    await expect(
      dialog.getByRole("heading", { name: "Two good games. One TV." }),
    ).toBeVisible();
    await dialog.getByRole("radio").nth(1).click();
    await expect(dialog.getByRole("radio").nth(1)).toBeChecked();
    await dialog.getByRole("button", { name: "Save watch plan" }).click();
    await expect(dialog).toBeHidden();
    await page
      .getByRole("button", { name: "Watch plan", exact: false })
      .first()
      .click();
    await expect(
      page.getByRole("heading", { name: "Watch plan", exact: true }),
    ).toBeVisible();
    await expect(page.locator(".df-plan-row").first()).toContainText(
      "Blue Jays",
    );
    await page.reload();
    await page
      .getByRole("button", { name: "Watch plan", exact: false })
      .first()
      .click();
    await expect(page.locator(".df-plan-row").first()).toContainText(
      "Blue Jays",
    );
    await page.getByRole("button", { name: "Events", exact: true }).click();
    await page
      .getByTestId("event-demo:lions")
      .getByRole("button", { name: "Play now", exact: true })
      .click();
    await expect(page.locator(".df-playing-title")).toContainText(
      "Lions at Bills",
    );
    await expect(page.locator(".df-playing")).toContainText("Simulated live");
    await page.getByRole("button", { name: "Pause", exact: true }).click();
    await expect(page.locator(".df-playing")).toContainText(
      "Automation paused",
    );
    await page.reload();
    await expect(page.locator(".df-playing")).toContainText(
      "Automation paused",
    );
    await page.getByRole("button", { name: "Resume", exact: true }).click();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBeTruthy();
    expect(errors).toEqual([]);
    await page.screenshot({ path: `test-results/${name}.png`, fullPage: true });
    const state = await (await request.get("/api/v1/overview")).json();
    expect(state.device.plan[0].content_id).toBe("demo:lions");
  });
}

test("rules, team ranking, keyboard dialog and scenario views", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1100, height: 900 });
  await page.goto("/");
  await page.getByRole("button", { name: "Priorities", exact: true }).click();
  await page.getByRole("button", { name: "Add rule", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("Name", { exact: true })
    .fill("Golf before all remaining sports");
  await dialog
    .getByRole("combobox", { name: "League", exact: true })
    .selectOption("pga");
  await dialog.getByRole("button", { name: "Save priority" }).click();
  await expect(dialog).toBeHidden();
  await expect(
    page
      .locator(".df-rule")
      .filter({ hasText: "Golf before all remaining sports" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Rank teams", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText("Detroit Lions");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Add Detroit Lions preference" })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Add Buffalo Bills preference" })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Move Detroit Lions down" })
    .click();
  await expect(
    page.getByRole("dialog").locator(".df-team-preference").first(),
  ).toContainText("Buffalo Bills");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();
  await page
    .getByRole("button", { name: "Test priorities", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toContainText("NFL RedZone");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();
  await page.locator("summary").click();
  await page
    .getByRole("combobox", { name: "Scenario", exact: true })
    .selectOption("overtime");
  await expect(page.locator(".df-playing-title")).toContainText(
    "Canadiens at Maple Leafs",
  );
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page
    .getByLabel("Minimum time on an event", { exact: true })
    .selectOption("600");
  await page.reload();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(
    page.getByLabel("Minimum time on an event", { exact: true }),
  ).toHaveValue("600");
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Activity", exact: true }),
  ).toBeVisible();
});
