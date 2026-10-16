import { test, expect } from "@playwright/test";

test("Activity shows entitlement queue blockers, failures and manual retry on phone", async ({
  page,
}) => {
  const requests: Record<string, unknown>[] = [];
  const checked = "2026-10-15T23:00:00Z";
  await page.route("**/api/v1/overview", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.entitlement_queue = {
      phase: "waiting",
      reason: "Playback monitoring has priority",
      updated_at: checked,
      dispatch_after: null,
      failed_jobs: 1,
      counts: { queued: 1, checking: 0, retry: 1, complete: 0, stopped: 0 },
      rows: [
        {
          content_id: "queued-event",
          title: "Wuhan Open: Day 5",
          failures: 0,
          error: null,
          eligible: true,
          started_at: checked,
          last_deferral: {
            at: checked,
            reason: "A playback request has priority",
          },
          entitlement: {
            status: "unknown",
            state: "queued",
            checked_at: null,
            next_check_at: null,
            blocked: false,
            reason: null,
            can_check: true,
          },
        },
        {
          content_id: "retry-event",
          title: "Toronto at Montreal",
          failures: 2,
          error: {
            code: "prime_timeout",
            message: "Catalogue request timed out",
          },
          eligible: true,
          started_at: checked,
          last_deferral: null,
          entitlement: {
            status: "unknown",
            state: "retry",
            checked_at: checked,
            next_check_at: "2026-10-16T01:00:00Z",
            blocked: false,
            reason: "Entitlement check failed; retry scheduled",
            can_check: true,
          },
        },
      ],
    };
    await route.fulfill({ response, json: body });
  });
  await page.route(
    "**/api/v1/devices/living-room/entitlement-checks",
    async (route) => {
      requests.push(route.request().postDataJSON());
      await route.fulfill({ json: { accepted: true } });
    },
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.getByLabel("Entitlement", { exact: true }).selectOption("all");
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  const queue = page.getByRole("region", { name: "Entitlement checks" });
  await expect(queue).toContainText("Playback monitoring has priority");
  await expect(queue).toContainText("1 with errors");
  await expect(queue).toContainText("Entitlement not checked yet");
  await expect(queue).toContainText("Last deferred");
  await expect(queue).toContainText("prime_timeout");
  await expect(queue).toContainText("Retry scheduled");
  await queue.getByLabel("Show checks").selectOption("failed");
  await expect(queue).not.toContainText("Wuhan Open");
  await queue.getByRole("button", { name: "Recheck entitlement" }).click();
  expect(requests[0].content_id).toBe("retry-event");
  await queue.getByLabel("Show checks").selectOption("all");
  await queue.getByLabel("Find an event").fill("Wuhan");
  await expect(queue).not.toContainText("Toronto at Montreal");
  await queue
    .getByRole("button", { name: "Check entitlement", exact: true })
    .click();
  expect(requests[1].content_id).toBe("queued-event");
  await queue.getByLabel("Find an event").clear();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/entitlement-queue-phone.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({
    path: "test-results/entitlement-queue-desktop.png",
    fullPage: true,
  });
});

const denied = {
  status: "unentitled",
  state: "complete",
  blocked: true,
  can_check: true,
  checked_at: "2026-10-15T23:00:00Z",
  next_check_at: null,
  reason: "Subscription required for the matching feeds",
};

test("admin uses one footer status and Check until entitlement is confirmed", async ({
  page,
  request,
}) => {
  await request.post("/api/v1/simulation", {
    data: { action: "scenario", scenario: "normal" },
  });
  let checks = 0;
  const requests: Record<string, unknown>[] = [];
  await page.route("**/api/v1/overview", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.device.observed = null;
    body.device.desired = null;
    body.device.playback_state = "waiting";
    body.device.plan = [];
    for (const e of body.events) {
      e.watch_entry_id = null;
      if (e.content_id === "demo:lions")
        e.entitlement =
          checks > 1
            ? {
                ...denied,
                status: "entitled",
                state: "complete",
                blocked: false,
              }
            : checks === 1
              ? {
                  ...denied,
                  status: "unknown",
                  state: "retry",
                  blocked: false,
                  next_check_at: "2026-10-16T04:00:00Z",
                }
              : denied;
    }
    await route.fulfill({ response, json: body });
  });
  await page.route(
    "**/api/v1/devices/living-room/entitlement-checks",
    async (route) => {
      requests.push(route.request().postDataJSON());
      checks += 1;
      await route.fulfill({ json: { accepted: true } });
    },
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.getByLabel("Entitlement", { exact: true }).selectOption("all");
  const card = page.getByTestId("event-demo:lions");
  await expect(card.locator(".df-event-footer")).toContainText("Unentitled");
  await expect(card.locator(".df-entitlement")).toHaveCount(0);
  await expect(
    card.getByRole("button", { name: "Play now", exact: true }),
  ).toHaveCount(0);
  await card.getByRole("button", { name: /Details for/ }).click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByRole("button", { name: "Add to plan", exact: true }),
  ).toHaveCount(0);
  await expect(
    dialog.getByRole("button", { name: "Play now", exact: true }),
  ).toHaveCount(0);
  await dialog.getByRole("button", { name: "Check", exact: true }).click();
  expect(requests).toHaveLength(1);
  expect(requests[0].content_id).toBe("demo:lions");
  await page.keyboard.press("Escape");
  await expect(card.locator(".df-event-footer")).toContainText("Unconfirmed");
  await expect(card.locator(".df-entitlement-indicator")).toHaveAttribute(
    "title",
    /Retry scheduled/,
  );
  await expect(
    card.getByRole("button", { name: "Play now", exact: true }),
  ).toHaveCount(0);
  await card.getByRole("button", { name: "Check", exact: true }).click();
  await expect(card.locator(".df-event-footer")).toContainText("Entitled");
  await expect(
    card.getByRole("button", { name: "Play now", exact: true }),
  ).toBeEnabled();
  await expect(
    card.getByRole("button", { name: "Check", exact: true }),
  ).toHaveCount(0);
  await card.getByRole("button", { name: /Details for/ }).click();
  await expect(
    dialog.getByRole("button", { name: "Add to plan", exact: true }),
  ).toBeEnabled();
  await page.keyboard.press("Escape");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/entitlement-phone.png",
    fullPage: true,
  });
});

test("public entitlement gates both actions and exposes no admin check control", async ({
  page,
}) => {
  const event = {
    content_id: "test-live",
    title: "Jets vs. Lions",
    league: "nfl",
    phase: "regular",
    kind: "event",
    start_time: "2026-10-15T23:00:00Z",
    expected_end_time: null,
    state: "live",
    active: true,
    scores: [],
    playable: true,
    planned: false,
    viewing_option_count: 1,
    teams: [],
    entitlement: denied,
  };
  await page.route("**/api/public/v1/overview", (route) =>
    route.fulfill({
      json: {
        revision: 1,
        viewer: { name: "Viewer", guest: false },
        timezone: "America/Vancouver",
        permissions: { play_now: true, add_to_plan: true },
        actions_message: "",
        now_playing: {
          event: null,
          verified: false,
          simulated: true,
          switching: false,
        },
        events: [event],
      },
    }),
  );
  await page.goto("/public/");
  await page.getByLabel("Entitlement", { exact: true }).selectOption("all");
  const card = page.getByTestId("event-test-live");
  await expect(card.getByRole("button", { name: "Add to plan" })).toHaveCount(
    0,
  );
  await expect(card.getByRole("button", { name: "Play now" })).toHaveCount(0);
  await expect(card.locator(".df-event-footer")).toContainText("Unentitled");
  await expect(
    page.getByRole("button", { name: /check entitlement/i }),
  ).toHaveCount(0);
});
