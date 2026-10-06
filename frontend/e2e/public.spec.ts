import { test, expect } from "@playwright/test";

function fixture() {
  const event = {
    content_id: "public:live",
    title: "Detroit Lions at Carolina Panthers",
    league: "nfl",
    phase: "regular",
    kind: "event",
    start_time: "2026-10-04T20:00:00Z",
    expected_end_time: "2026-10-05T00:00:00Z",
    state: "live",
    active: true,
    scores: [19, 22],
    playable: true,
    planned: false,
    viewing_option_count: 1,
    teams: [
      {
        key: "away",
        name: "Lions",
        city: "Detroit",
        full_name: "Detroit Lions",
        abbreviation: "DET",
        logo_url: null,
      },
      {
        key: "home",
        name: "Panthers",
        city: "Carolina",
        full_name: "Carolina Panthers",
        abbreviation: "CAR",
        logo_url: null,
      },
    ],
  };
  return {
    revision: 5,
    viewer: { name: "sportsfan", guest: false },
    timezone: "America/Vancouver",
    permissions: { play_now: true, add_to_plan: true },
    actions_message:
      "Admin selections take priority. Your requests stay in the shared watch plan.",
    now_playing: {
      event: { ...event, content_id: "current", title: "Live golf coverage" },
      verified: true,
      simulated: true,
      switching: false,
    },
    events: [
      event,
      {
        ...event,
        content_id: "public:upcoming",
        title: "Tomorrow's game",
        state: "scheduled",
        playable: false,
        start_time: "2026-10-05T20:00:00Z",
        expected_end_time: "2026-10-06T00:00:00Z",
      },
    ],
  };
}

for (const width of [1280, 390, 320]) {
  test(`public ${width}px: safe controls, filtering and user requests`, async ({
    page,
  }) => {
    const data = fixture();
    const sent: Record<string, unknown>[] = [];
    await page.setViewportSize({ width, height: 900 });
    await page.route("**/api/public/v1/overview", (route) =>
      route.fulfill({ json: data }),
    );
    await page.route("**/api/public/v1/watch-plan", async (route) => {
      const body = route.request().postDataJSON();
      sent.push(body);
      data.revision++;
      data.events.find((e) => e.content_id === body.content_id)!.planned = true;
      await route.fulfill({
        json: { accepted: true, revision: data.revision },
      });
    });
    await page.goto("/public/");
    await expect(
      page.getByRole("heading", { name: "What's on" }),
    ).toBeVisible();
    await expect(
      page.getByRole("region", { name: "Now playing" }),
    ).toContainText("Live golf coverage");
    for (const name of [
      "Pause",
      "Mark event finished",
      "Take control",
      "Settings",
      "Priorities",
    ]) {
      await expect(page.getByRole("button", { name, exact: true })).toHaveCount(
        0,
      );
    }
    await page
      .getByTestId("event-public:live")
      .getByRole("button", { name: "Play now" })
      .click();
    await expect(page.getByRole("status")).toContainText(
      "Admin selections take priority",
    );
    expect(sent[0]).toMatchObject({
      action: "play_now",
      content_id: "public:live",
      expected_revision: 5,
    });
    expect(sent[0]).not.toHaveProperty("actor");
    await page.getByRole("button", { name: "Upcoming", exact: true }).click();
    await page
      .getByTestId("event-public:upcoming")
      .getByRole("button", { name: "Add to plan" })
      .click();
    await expect(page.getByTestId("event-public:upcoming")).toContainText(
      "In watch plan",
    );
    expect(sent[1]).toMatchObject({ action: "add", expected_revision: 6 });
    await page.getByRole("searchbox").fill("no such team");
    await expect(
      page.getByRole("heading", { name: "No events match" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Clear filters" }).click();
    await expect(page.getByTestId("event-public:upcoming")).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: `test-results/public-${width}.png`,
      fullPage: true,
    });
  });
}

for (const planned of [false, true]) {
  for (const width of [1280, 320]) {
    test(`public current event shows read-only details (${planned ? "planned" : "automatic"}, ${width}px)`, async ({
      page,
    }) => {
      const data = fixture();
      data.events[0].planned = planned;
      data.now_playing.event = data.events[0];
      data.permissions = { play_now: false, add_to_plan: false };
      let writes = 0;
      await page.setViewportSize({ width, height: 900 });
      await page.route("**/api/public/v1/overview", (route) =>
        route.fulfill({ json: data }),
      );
      await page.route("**/api/public/v1/watch-plan", (route) => {
        writes++;
        return route.fulfill({ status: 500 });
      });
      await page.goto("/public/");
      const card = page.getByTestId("event-public:live");
      await expect(card).toContainText("Now playing");
      await expect(
        card.getByRole("button", { name: "Add to plan" }),
      ).toHaveCount(0);
      await expect(card.getByRole("button", { name: "Play now" })).toHaveCount(
        0,
      );
      const details = card.getByRole("button", {
        name: "Details",
        exact: true,
      });
      await details.click();
      const dialog = page.getByRole("dialog", { name: data.events[0].title });
      await expect(dialog).toBeVisible();
      await expect(dialog).toContainText("1 viewing option");
      await expect(dialog.getByRole("button")).toHaveCount(1);
      expect(
        await dialog.evaluate((element) => {
          const rect = element.getBoundingClientRect();
          return rect.left >= 0 && rect.right <= innerWidth;
        }),
      ).toBe(true);
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
      await expect(details).toBeFocused();
      await details.click();
      await dialog.getByRole("button", { name: "Close" }).click();
      await expect(dialog).toHaveCount(0);
      expect(writes).toBe(0);
      // Last-observed playback is not a verified current event.
      data.now_playing.verified = false;
      await page.reload();
      await expect(
        card.getByRole("button", { name: "Details", exact: true }),
      ).toHaveCount(0);
      await expect(
        card.getByRole("button", { name: "Play now" }),
      ).toBeVisible();
      await expect(
        card.getByRole("button", { name: "Add to plan" }),
      ).toHaveCount(planned ? 0 : 1);
    });
  }
}

test("public controls follow permissions and stale command errors refresh the page", async ({
  page,
}) => {
  const data = fixture();
  data.permissions.play_now = false;
  await page.route("**/api/public/v1/overview", (route) =>
    route.fulfill({ json: data }),
  );
  await page.route("**/api/public/v1/watch-plan", async (route) => {
    data.revision++;
    data.permissions.add_to_plan = false;
    await route.fulfill({
      status: 409,
      json: {
        detail: { message: "Configuration changed. Refresh and try again." },
      },
    });
  });
  await page.goto("/public/");
  await expect(page.getByRole("button", { name: "Play now" })).toBeDisabled();
  await page.getByRole("button", { name: "Add to plan" }).click();
  await expect(page.getByRole("alert")).toContainText("Configuration changed");
  await expect(
    page.getByRole("button", { name: "Add to plan" }),
  ).toBeDisabled();
});

test("public authentication failure has a useful empty state", async ({
  page,
}) => {
  await page.route("**/api/public/v1/overview", (route) =>
    route.fulfill({
      status: 401,
      json: { detail: "Sign in through PlexSSO to continue." },
    }),
  );
  await page.goto("/public/");
  await expect(page.getByRole("alert")).toContainText(
    "Sign in through PlexSSO",
  );
  await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Play now" })).toHaveCount(0);
});

test("guest mode shows anonymous identity and permits public requests", async ({
  page,
}) => {
  const data = fixture();
  data.viewer = { name: "Guest", guest: true };
  await page.route("**/api/public/v1/overview", (route) =>
    route.fulfill({ json: data }),
  );
  await page.route("**/api/public/v1/watch-plan", async (route) => {
    expect(route.request().postDataJSON()).toMatchObject({
      action: "play_now",
      content_id: "public:live",
    });
    expect(route.request().postDataJSON()).not.toHaveProperty("actor");
    await route.fulfill({ json: { accepted: true, revision: 6 } });
  });
  await page.goto("/public/");
  await expect(
    page.getByText("Browsing as Guest", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Signed in as Guest")).toHaveCount(0);
  await page.getByRole("button", { name: "Play now" }).click();
  await expect(page.getByRole("status")).toContainText("Play request saved");
});

test("admin can persist separate public permission switches", async ({
  page,
  request,
}) => {
  await page.goto("/");
  await page
    .getByRole("button", { name: "Settings", exact: true })
    .first()
    .click();
  const play = page.getByRole("checkbox", { name: "Allow public Play now" });
  const add = page.getByRole("checkbox", { name: "Allow public Add to plan" });
  await play.click();
  await expect(play).toBeChecked();
  await expect(page.getByRole("status")).toContainText(
    "Public permissions saved",
  );
  await add.click();
  await expect(add).toBeChecked();
  await page.reload();
  await page
    .getByRole("button", { name: "Settings", exact: true })
    .first()
    .click();
  await expect(play).toBeChecked();
  await expect(add).toBeChecked();
  await play.click();
  await expect(play).not.toBeChecked();
  const { device } = await (await request.get("/api/v1/overview")).json();
  expect(device.public_access).toEqual({ play_now: false, add_to_plan: true });
});
