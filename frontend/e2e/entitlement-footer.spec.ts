import { test, expect } from "@playwright/test";

test("upcoming footer moves from Queued to Checking to Entitled without an extra row", async ({
  page,
  request,
}) => {
  await request.post("/api/v1/simulation", {
    data: { action: "scenario", scenario: "normal" },
  });
  let state = "queued";
  await page.route("**/api/v1/overview", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const event = body.events.find(
      (e: { content_id: string }) => e.content_id === "demo:lions",
    );
    body.device.observed = null;
    body.device.desired = null;
    event.lifecycle.state = "scheduled";
    event.start_time = new Date(
      Date.parse(body.meta.now) + 3600000,
    ).toISOString();
    event.playable = false;
    event.watch_entry_id = null;
    event.entitlement = {
      status: state === "complete" ? "entitled" : "unknown",
      state,
      checked_at: state === "complete" ? body.meta.now : null,
      next_check_at: null,
      blocked: false,
      reason: null,
      can_check: true,
    };
    await route.fulfill({ response, json: body });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  for (const value of ["queued", "checking", "complete"]) {
    state = value;
    await page.goto("/");
  await page.getByLabel("Entitlement", { exact: true }).selectOption("all");
    await page
      .locator(".df-segmented")
      .getByRole("button", { name: /^Upcoming \d+$/ })
      .click();
    const card = page.getByTestId("event-demo:lions");
    await expect(card.locator(".df-entitlement")).toHaveCount(0);
    const footer = card.locator(".df-event-footer");
    await expect(footer.locator(".df-entitlement-indicator")).toContainText(
      value === "complete"
        ? "Entitled"
        : value === "queued"
          ? "Queued"
          : "Checking",
    );
    await expect(footer.locator(".df-entitlement-indicator svg")).toHaveCount(
      1,
    );
    await expect(
      footer.getByRole("button", { name: "Play now", exact: true }),
    ).toHaveCount(0);
    if (value === "complete") {
      await expect(
        footer.getByRole("button", { name: "Add to plan", exact: true }),
      ).toBeEnabled();
      await expect(
        footer.getByRole("button", { name: "Check", exact: true }),
      ).toHaveCount(0);
    } else {
      await expect(
        footer.getByRole("button", { name: "Add to plan", exact: true }),
      ).toHaveCount(0);
      if (value === "queued")
        await expect(
          footer.getByRole("button", { name: "Check", exact: true }),
        ).toBeEnabled();
      else
        await expect(
          footer.getByRole("button", { name: "Check", exact: true }),
        ).toBeDisabled();
    }
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await card.screenshot({
      path: `test-results/entitlement-footer-${value}.png`,
    });
  }
});
