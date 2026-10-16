import { test, expect } from "@playwright/test";

const denied = {
  status: "unentitled",
  state: "complete",
  blocked: true,
  can_check: true,
  checked_at: "2026-10-15T23:00:00Z",
  next_check_at: null,
  reason: "Subscription required for the matching feeds",
};

test("admin entitlement gates cards and details, supports recheck, and keeps unknown usable", async ({
  page,
  request,
}) => {
  await request.post("/api/v1/simulation", {
    data: { action: "scenario", scenario: "normal" },
  });
  let pending = false;
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
        e.entitlement = pending
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
      pending = true;
      await route.fulfill({ json: { accepted: true } });
    },
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  const card = page.getByTestId("event-demo:lions");
  await expect(card).toContainText("Subscription required");
  await expect(
    card.getByRole("button", { name: "Play now", exact: true }),
  ).toBeDisabled();
  await card.getByRole("button", { name: /Details for/ }).click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByRole("button", { name: "Add to plan", exact: true }),
  ).toBeDisabled();
  await expect(
    dialog.getByRole("button", { name: "Play now", exact: true }),
  ).toBeDisabled();
  await dialog
    .getByRole("button", { name: "Recheck entitlement", exact: true })
    .click();
  expect(requests).toHaveLength(1);
  expect(requests[0].content_id).toBe("demo:lions");
  await page.keyboard.press("Escape");
  await expect(card).toContainText("Entitlement unconfirmed");
  await expect(card).toContainText("Retry scheduled");
  await expect(
    card.getByRole("button", { name: "Play now", exact: true }),
  ).toBeEnabled();
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
  const card = page.getByTestId("event-test-live");
  await expect(
    card.getByRole("button", { name: "Add to plan" }),
  ).toBeDisabled();
  await expect(card.getByRole("button", { name: "Play now" })).toBeDisabled();
  await expect(
    page.getByRole("button", { name: /check entitlement/i }),
  ).toHaveCount(0);
});
