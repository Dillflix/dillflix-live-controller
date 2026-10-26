import { test, expect, type Page, type Route } from "@playwright/test";
import { screenFixture } from "./screen-fixture";

const streams = [
  { id: "living-room", name: "Living room", playback_state: "verified", automation: "active", verified: true,
    current_event: { content_id: "demo:lions", title: "Detroit Lions at Buffalo Bills" }, target_event: null },
  { id: "den", name: "Den", playback_state: "navigating", automation: "active", verified: false,
    current_event: null, target_event: { content_id: "demo:golf", title: "Live golf coverage" } },
];

async function setup(page: Page, base: any) {
  const requests: { path: string; body: any }[] = [];
  let delayed: Route | null = null;
  let delayNext = false;
  await page.addInitScript(() => {
    let update: (() => void) | undefined;
    (window as any).EventSource = class {
      addEventListener(_event: string, callback: () => void) { update = callback; }
      close() { update = undefined; }
    };
    (window as any).streamUpdate = () => update?.();
  });
  const overview = (id: string) => ({
    ...structuredClone(base), streams, shared_plan: true,
    playback_job: null,
    plan_preview: { ...structuredClone(base.plan_preview), segments: base.plan_preview.segments.map((segment: any) => ({
      ...segment, device_id: "living-room", device_name: "Living room",
    })) },
    device: { ...structuredClone(base.device), id, name: id === "den" ? "Den" : "Living room",
      observed: id === "den" ? null : { content_id: "demo:lions", verified: true, simulated: true,
        observed_at: base.meta.now, viewing_option_id: "demo:lions:view", presentation: "live" },
      desired: id === "den" ? "demo:golf" : "demo:lions", playback_state: id === "den" ? "navigating" : "verified",
      reason: id === "den" ? "Starting live coverage" : "Live playback verified" },
  });
  await page.route("**/api/v1/overview*", async (route) => {
    const id = new URL(route.request().url()).searchParams.get("device_id") || "living-room";
    if (id === "living-room" && delayNext) { delayed = route; delayNext = false; return; }
    await route.fulfill({ json: overview(id) });
  });
  await page.route("**/api/v1/devices/*/screen", (route) => route.fulfill({ json: { enabled: false, stream_path: null } }));
  await page.route("**/api/v1/devices/*/automation", async (route) => {
    requests.push({ path: new URL(route.request().url()).pathname, body: route.request().postDataJSON() });
    await route.fulfill({ json: { accepted: true } });
  });
  return { requests, overview, delay() { delayNext = true; }, get pending() { return delayed; } };
}

test("both streams remain visible while shared planning and selected-device commands stay distinct", async ({ page, request }) => {
  const base = await (await request.get("/api/v1/overview")).json();
  const fixture = await setup(page, base);
  await page.goto("/");
  const cards = page.getByRole("region", { name: "Live streams" });
  await expect(cards.getByRole("button", { name: "Select Living room" })).toContainText("Detroit Lions");
  await expect(cards.getByRole("button", { name: "Select Den" })).toContainText("Live golf");
  await page.screenshot({ path: "test-results/streams-desktop.png", fullPage: true });
  await page.getByRole("button", { name: "Select Den" }).click();
  await expect(page.getByRole("button", { name: "Select Den" })).toHaveAttribute("aria-pressed", "true");
  await expect(page).toHaveURL(/device=den/);
  await page.getByRole("button", { name: "Pause", exact: true }).click();
  expect(fixture.requests[0].path).toBe("/api/v1/devices/den/automation");
  await page.getByRole("button", { name: /Watch plan/ }).first().click();
  await expect(page.getByRole("heading", { name: "Shared watch plan" })).toBeVisible();
  await page.getByRole("button", { name: "Select Living room" }).click();
  await expect(page.getByRole("heading", { name: "Shared watch plan" })).toBeVisible();
  await page.setViewportSize({ width: 320, height: 780 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: "test-results/streams-phone.png", fullPage: true });
});

test("a late overview for the previous stream cannot replace the selected stream", async ({ page, request }) => {
  const fixture = await setup(page, await (await request.get("/api/v1/overview")).json());
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Select Den" })).toBeVisible();
  fixture.delay();
  await page.evaluate(() => (window as any).streamUpdate());
  await expect.poll(() => !!fixture.pending).toBe(true);
  await page.getByRole("button", { name: "Select Den" }).click();
  await expect(page.getByRole("button", { name: "Select Den" })).toHaveAttribute("aria-pressed", "true");
  await fixture.pending!.fulfill({ json: fixture.overview("living-room") });
  await page.getByRole("button", { name: "Pause", exact: true }).click();
  expect(fixture.requests[0].path).toBe("/api/v1/devices/den/automation");
});

test("switching streams clears credentials and dirty Plex drafts and releases the old screen", async ({ page, request }) => {
  await setup(page, await (await request.get("/api/v1/overview")).json());
  const video = await screenFixture(page);
  await page.route("**/api/v1/devices/*/vpn-locations", (route) => route.fulfill({ json: { locations: [] } }));
  await page.route("**/api/v1/devices/*/plex", (route) => route.fulfill({ json: {
    revision: 0, enabled: false, base_url: "", rating_key: "8", credential_configured: false,
    real_playback: false, default_images: {}, status: { pending: false, hold: false, in_flight: false,
      blocked: null, error: null, retry_at: null, fallback_at: null, last_success: null, desired: {}, applied: {} },
  } }));
  await page.goto("/");
  await expect(page.getByTestId("screen-state")).toHaveText("Live screen");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByLabel("Plex server URL").fill("http://first-stream.invalid");
  await page.getByLabel("Plex token", { exact: true }).fill("secret-for-first-stream");
  await page.getByRole("button", { name: "Select Den" }).click();
  await expect(page.getByLabel("Plex server URL")).toHaveValue("");
  await expect(page.getByLabel("Plex token", { exact: true })).toHaveValue("");
  await expect.poll(() => video.closed).toBeGreaterThan(0);
  await expect(page.locator("video")).toHaveCount(0);
});

test("public stream selection keeps shared requests public and targets the selected stream", async ({ page }) => {
  const requests: string[] = [];
  const event = { content_id: "demo:lions", title: "Detroit Lions at Carolina Panthers", league: "nfl", phase: "regular",
    kind: "event", start_time: "2026-10-25T17:00:00Z", expected_end_time: "2026-10-25T21:00:00Z", state: "live",
    active: true, scores: [], playable: true, planned: false, viewing_option_count: 1, teams: [],
    entitlement: { status: "entitled", state: "complete", blocked: false } };
  await page.route("**/api/public/v1/overview*", (route) => {
    const id = new URL(route.request().url()).searchParams.get("device_id") || "living-room";
    return route.fulfill({ json: { device: { id, name: id === "den" ? "Den" : "Living room" }, streams, shared_plan: true,
      now: "2026-10-25T18:00:00Z", revision: 1, viewer: { name: "Guest", guest: true }, timezone: "America/Vancouver",
      permissions: { play_now: true, add_to_plan: true }, actions_message: "Requests join the shared watch plan.",
      now_playing: { event: null, verified: false, simulated: false, switching: false }, events: [event] } });
  });
  await page.route("**/api/public/v1/watch-plan*", async (route) => {
    requests.push(route.request().url());
    await route.fulfill({ json: { accepted: true } });
  });
  await page.goto("/public/");
  await page.getByRole("button", { name: "Select Den" }).click();
  await expect(page.getByRole("button", { name: "Select Den" })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "Play now", exact: true }).click();
  expect(requests[0]).toContain("/api/public/v1/watch-plan?device_id=den");
  await expect(page.getByRole("button", { name: "Pause", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Take control", exact: true })).toHaveCount(0);
});
