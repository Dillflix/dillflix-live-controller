import { test, expect } from "@playwright/test";
import { readFile } from "node:fs/promises";

test("Activity shows fresh monitoring evidence independently of old activity entries", async ({
  page,
  request,
}) => {
  const data = await (await request.get("/api/v1/overview")).json();
  data.device.preferences.timezone = "America/Vancouver";
  data.device.reason =
    "Event live status is unknown; retaining verified playback";
  data.device.playback_state = "verified";
  data.device.observed = {
    content_id: data.events[0].content_id,
    verified: true,
    simulated: false,
    observed_at: "2026-10-04T21:31:54Z",
    viewing_option_id: "prime-option",
    presentation: "live",
  };
  data.device.executor_health = {
    state: "ok",
    last_contact_at: "2026-10-04T21:32:09Z",
  };
  data.playback_job = {
    id: "current-request",
    content_id: data.device.observed.content_id,
    state: "verified",
    progress: "playing_verified",
    deadline_at: Date.parse("2026-10-04T20:44:00Z") / 1000,
    delivery_attempts: 1,
    purpose: "selection",
    error: null,
  };
  data.activity = [
    {
      sequence: 1,
      at: "2026-10-04T20:39:00Z",
      kind: "verified",
      message: "Live playback verified",
      detail: "Prime accepted live startup",
    },
  ];
  await page.route("**/api/v1/overview", (route) =>
    route.fulfill({ json: data }),
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  const monitoring = page.locator(".df-panel").filter({
    has: page.getByRole("heading", { name: "Playback monitoring" }),
  });
  await expect(monitoring).toContainText(
    /Last playback evidence: .*2:31:54 .* · verified/,
  );
  await expect(monitoring).toContainText(
    /Last executor contact: .*2:32:09 /,
  );
  await expect(monitoring).not.toContainText("deadline");
  await expect(page.locator(".df-activity")).toHaveCount(1);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();

  // A failed check retains the previous evidence time without claiming verification.
  data.device.observed.verified = false;
  data.device.playback_state = "unverified";
  data.device.executor_health.last_contact_at = "2026-10-04T21:33:09Z";
  data.device.reason =
    "Playback evidence is missing; allowing time for recovery";
  await page.reload();
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  await expect(monitoring).toContainText(
    /Last playback evidence: .*2:31:54 .* · unverified/,
  );
  await expect(monitoring).toContainText(
    /Last executor contact: .*2:33:09 /,
  );

  data.playback_job.state = "pending";
  data.playback_job.progress = "navigating";
  await page.reload();
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  await expect(monitoring).toContainText(/Launch deadline 1:44/);
});

test("Activity exports device-scoped diagnostics when the player is not configured", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Playback monitoring" }),
  ).toBeVisible();
  const waiting = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export diagnostics" }).click();
  const download = await waiting;
  const path = await download.path();
  const bundle = JSON.parse(await readFile(path!, "utf8"));
  expect(bundle.schema_version).toBe(1);
  expect(bundle.device.id).toBe("living-room");
  expect(bundle.prime_player.state).toBe("not_configured");
  expect(Array.isArray(bundle.activity)).toBe(true);
});
