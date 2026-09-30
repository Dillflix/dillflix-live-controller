import { test, expect } from "@playwright/test";
import { readFile } from "node:fs/promises";

const devicePath = "/api/v1/devices/living-room";

test.beforeEach(async ({ request }) => {
  await request.post("/api/v1/simulation", {
    data: { action: "scenario", scenario: "normal" },
  });
  const { device } = await (await request.get("/api/v1/overview")).json();
  await request.put(devicePath + "/rules", {
    data: {
      command_id: crypto.randomUUID(),
      expected_revision: device.revision,
      rules: device.rules,
      team_ranks: {},
      preferences: { ...device.preferences, minimum_viewing_seconds: 300 },
    },
  });
});

test("phone: an off-schedule team can be preferred, reloaded and undone", async ({
  page,
  request,
}) => {
  await page.setViewportSize({ width: 320, height: 780 });
  await page.goto("/");
  await page.getByRole("button", { name: "Priorities", exact: true }).click();
  await page.getByRole("button", { name: "Rank teams", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("combobox", { name: "League", exact: true })
    .selectOption("nhl");
  await dialog.getByLabel("Find a team").fill("Vancouver");
  await dialog
    .getByRole("button", { name: "Add Vancouver Canucks preference" })
    .click();
  await expect(dialog.getByTestId("ranked-demo:nhl:VAN")).toBeVisible();
  expect(
    await dialog.evaluate((el) => el.scrollWidth <= el.clientWidth),
  ).toBeTruthy();
  await page.screenshot({
    path: "test-results/team-directory-phone.png",
    fullPage: true,
  });
  await page.keyboard.press("Escape");
  await page.reload();
  await page
    .getByRole("button", { name: "Undo last edit", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText("Last edit undone");
  const { device } = await (await request.get("/api/v1/overview")).json();
  expect(device.team_ranks).toEqual({});
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
});

test("configuration export, reviewed import and undo preserve the watch plan", async ({
  page,
  request,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const before = await (await request.get("/api/v1/overview")).json();
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export configuration" }).click();
  const download = await downloadPromise;
  const document = JSON.parse(await readFile((await download.path())!, "utf8"));
  expect(document.configuration).not.toHaveProperty("plan");
  expect(document.configuration).not.toHaveProperty("automation");
  document.configuration.preferences.minimum_viewing_seconds = 75;
  document.configuration.preferences.switch_cooldown_seconds = 45;
  document.configuration.team_ranks = { nhl: ["demo:nhl:VAN"] };
  await page.getByLabel("Configuration file").setInputFiles({
    name: "configuration.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify(document)),
  });
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByRole("heading", { name: "Review configuration import" }),
  ).toBeVisible();
  expect(
    (await (await request.get("/api/v1/overview")).json()).device.revision,
  ).toBe(before.device.revision);
  await dialog.getByRole("button", { name: "Apply configuration" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByLabel("Minimum time on an event")).toHaveValue("75");
  await expect(page.getByLabel("Switch cooldown")).toHaveValue("45");
  const after = await (await request.get("/api/v1/overview")).json();
  expect(after.device.plan).toEqual(before.device.plan);
  await page.getByRole("button", { name: "Undo last edit" }).click();
  await expect(page.getByLabel("Minimum time on an event")).toHaveValue("300");
  await page.getByLabel("Configuration file").setInputFiles({
    name: "invalid.json",
    mimeType: "application/json",
    buffer: Buffer.from("not-json"),
  });
  await expect(page.getByRole("alert")).toContainText("not valid JSON");
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("an import preview cannot overwrite a later edit, even after the UI refreshes", async ({
  page,
  request,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const document = await (
    await request.get(devicePath + "/configuration")
  ).json();
  await page.getByLabel("Configuration file").setInputFiles({
    name: "configuration.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify(document)),
  });
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByRole("button", { name: "Apply configuration" }),
  ).toBeVisible();
  const { device } = await (await request.get("/api/v1/overview")).json();
  const changed = await request.put(devicePath + "/rules", {
    data: {
      command_id: crypto.randomUUID(),
      expected_revision: device.revision,
      ...document.configuration,
      preferences: {
        ...document.configuration.preferences,
        minimum_viewing_seconds: 900,
      },
    },
  });
  expect(changed.ok()).toBeTruthy();
  // Wait for SSE/poll to bring the new revision into App while the old preview remains open.
  await expect(page.getByLabel("Minimum time on an event")).toHaveValue("900");
  await dialog.getByRole("button", { name: "Apply configuration" }).click();
  await expect(dialog.getByRole("alert")).toContainText(
    "Configuration changed",
  );
  expect(
    (await (await request.get("/api/v1/overview")).json()).device.preferences
      .minimum_viewing_seconds,
  ).toBe(900);
});

test("stale overlap review requires a fresh preview before saving", async ({
  page,
  request,
}) => {
  await page.goto("/");
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
    dialog.getByRole("button", { name: "Save watch plan" }),
  ).toBeVisible();
  const { device } = await (await request.get("/api/v1/overview")).json();
  await request.post(devicePath + "/watch-plan", {
    data: {
      command_id: crypto.randomUUID(),
      expected_revision: device.revision,
      action: { type: "add", content_id: "demo:lions" },
    },
  });
  // Last edit is rendered outside the modal as refreshed server state.
  await expect(page.locator(".df-edit-bar")).toContainText(
    "Add event to watch plan",
  );
  await dialog.getByRole("button", { name: "Save watch plan" }).click();
  await expect(dialog.getByRole("alert")).toContainText(
    "Configuration changed",
  );
  await dialog.getByRole("button", { name: "Refresh preview" }).click();
  await expect(dialog.getByRole("alert")).toHaveCount(0);
  await dialog.getByRole("button", { name: "Save watch plan" }).click();
  await expect(dialog).toBeHidden();
  const after = await (await request.get("/api/v1/overview")).json();
  expect(
    after.device.plan.map((p: { content_id: string }) => p.content_id),
  ).toEqual(["demo:canadiens", "demo:lions", "demo:jays"]);
});
