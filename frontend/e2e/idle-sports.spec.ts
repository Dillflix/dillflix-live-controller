import { test, expect } from "@playwright/test";
import { disableBrowserStreams } from "./public-stream-fixture";

test.beforeEach(async ({ page }) => disableBrowserStreams(page));

test("idle sports defaults off, saves on a phone, exports and undoes", async ({ page, request }) => {
  await request.post("/api/v1/simulation", { data: { action: "scenario", scenario: "normal" } });
  await page.setViewportSize({ width: 320, height: 780 });
  await page.goto("/");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const toggle = page.getByLabel("Play sports when schedule is idle", { exact: true });
  await expect(toggle).not.toBeChecked();
  await expect(page.getByTestId("idle-sports-setting-status")).toHaveText("Idle sports is off.");
  await toggle.click();
  await expect(toggle).toBeChecked();
  await page.reload();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(toggle).toBeChecked();
  const exported = await (await request.get("/api/v1/devices/living-room/configuration")).json();
  expect(exported.configuration.preferences.idle_sports_enabled).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: "test-results/idle-sports-settings-phone.png", fullPage: true });
  await page.getByRole("button", { name: "Undo last edit", exact: true }).click();
  await expect(toggle).not.toBeChecked();
});

test("admin keeps idle playback visible without adding it to cards, calendar or manual actions", async ({ page }) => {
  let state = "ready";
  let legacy = false;
  let errorCode: string | undefined;
  await page.route("**/api/v1/overview", async route => {
    const response = await route.fetch();
    const body = await response.json();
    const template = body.events[0];
    const event = {
      ...template, content_id: "prime_idle:fixture", title: "Evening Sports Analysis", source: "prime_idle",
      league: "sports", active: true, playable: true, teams: [], scores: [], watch_entry_id: null,
      lifecycle: { ...template.lifecycle, state: "live" },
      entitlement: { status: "entitled", state: "complete", blocked: false },
    };
    body.events = [event];
    body.device.plan = [];
    body.device.manual_control = null;
    body.device.automation = "active";
    body.device.playback_state = "verified";
    body.device.desired = event.content_id;
    body.device.observed = { content_id: event.content_id, verified: true, simulated: true };
    body.device.idle_sports = { state, error_code: errorCode };
    if (legacy) delete body.device.preferences.idle_sports_enabled;
    else body.device.preferences.idle_sports_enabled = true;
    body.playback_job = null;
    await route.fulfill({ response, json: body });
  });
  await page.goto("/");
  await expect(page.locator(".df-playing-title")).toContainText("Evening Sports Analysis");
  await expect(page.getByTestId("idle-sports-status")).toContainText("Sports selected from Prime Video");
  await expect(page.getByTestId("event-prime_idle:fixture")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Mark event finished", exact: true })).toHaveCount(0);
  await page.getByLabel("Entitlement", { exact: true }).selectOption("all");
  await expect(page.locator(".df-event")).toHaveCount(0);
  await page.getByRole("button", { name: "Calendar", exact: true }).click();
  await expect(page.locator(".df-calendar").getByText("Evening Sports Analysis")).toHaveCount(0);
  await page.getByRole("button", { name: "Playback details", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText("Evening Sports Analysis");
  await expect(page.getByRole("dialog").getByRole("button", { name: "Play now", exact: true })).toHaveCount(0);
  await expect(page.getByRole("dialog").getByRole("button", { name: "Add to plan", exact: true })).toHaveCount(0);
  for (const [next, message] of [
    ["checking", "Checking Prime Video for sports on now."],
    ["unavailable", "No confirmed sports selection is available. Checking again automatically."],
  ]) {
    state = next;
    await page.reload();
    await expect(page.getByTestId("idle-sports-status")).toHaveText(message);
  }
  errorCode = "prime_linear_catalog_timeout";
  await page.reload();
  await expect(page.getByTestId("idle-sports-status")).toHaveText("Prime Video's sports catalogue timed out. Retrying automatically.");
  legacy = true;
  await page.reload();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByLabel("Play sports when schedule is idle", { exact: true })).not.toBeChecked();
});

test("public idle playback stays visible but cannot be picked or planned", async ({ page }) => {
  const event = {
    content_id: "prime_idle:fixture", title: "Evening Sports Analysis", league: "sports", phase: "unknown",
    kind: "broadcast", start_time: "2026-11-23T19:00:00Z", expected_end_time: "2026-11-23T20:00:00Z",
    state: "live", active: true, scores: [], playable: true, planned: false, teams: [],
    viewing_option_count: 1, entitlement: { status: "entitled", state: "complete", blocked: false },
  };
  await page.route("**/api/public/v1/overview", route => route.fulfill({ json: {
    revision: 1, viewer: { name: "Viewer", guest: false }, timezone: "America/Vancouver",
    now: "2026-11-23T19:15:00Z", permissions: { play_now: true, add_to_plan: true }, actions_message: "",
    now_playing: { event, verified: true, simulated: true, switching: false }, events: [event],
  } }));
  await page.goto("/public/");
  await expect(page.getByRole("region", { name: "Now playing" })).toContainText("Evening Sports Analysis");
  await expect(page.getByRole("region", { name: "Now playing" })).toContainText("while the schedule is idle");
  await expect(page.getByTestId("event-prime_idle:fixture")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Play now", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Add to plan", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Calendar", exact: true }).click();
  await expect(page.locator(".df-calendar").getByText("Evening Sports Analysis")).toHaveCount(0);
});
