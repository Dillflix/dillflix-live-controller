import { test, expect } from "@playwright/test";

test("launch stages show network startup and elapsed time before playback verification", async ({ page, request }) => {
  const data = await (await request.get("/api/v1/overview")).json();
  data.device.playback_state = "navigating";
  data.device.desired = data.events[0]?.content_id;
  data.device.automation = "active";
  data.device.manual_control = null;
  data.device.reason = "Starting Prime on the connected network";
  data.meta.server_time = "2026-10-22T23:04:40Z";
  data.playback_job = { state: "pending", progress: "network_starting_prime", launch_progress: {
    phase: "network_starting_prime", title: "Starting Prime on the connected network · toronto",
    detail: "Waiting for Prime initialization", started_at: "2026-10-22T23:03:40Z",
  }};
  await page.route("**/api/v1/overview", (route) => route.fulfill({ json: data }));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  const banner = page.locator(".df-playing");
  await expect(banner).toContainText("Waiting for Prime initialization");
  await expect(banner).toContainText(/1m \d+s in this stage/);
  await expect(banner).not.toContainText("waiting for live verification");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await page.screenshot({ path: "test-results/launch-progress-mobile.png", fullPage: true });
  data.playback_job.launch_progress = { phase: "verifying", title: "Verifying live playback",
    detail: null, started_at: data.meta.server_time };
  await page.reload();
  await expect(banner).toContainText("Verifying live playback");
  await expect(banner).not.toContainText("Waiting for Prime initialization");
});
