import { test, expect } from "@playwright/test";

test("city VPN policies persist, reorder and undo without changing the watch plan", async ({
  page,
  request,
}) => {
  await request.post("/api/v1/simulation", {
    data: { action: "scenario", scenario: "normal" },
  });
  await page.route("**/api/v1/devices/living-room/vpn-locations", (route) =>
    route.fulfill({
      json: {
        enabled: true,
        locations: [
          {
            id: "toronto",
            city: "Toronto",
            qualified: true,
            routing_confirmed: true,
          },
          {
            id: "montreal",
            city: "Montreal",
            qualified: false,
            routing_confirmed: true,
          },
        ],
      },
    }),
  );
  const before = await (await request.get("/api/v1/overview")).json();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const panel = page.getByRole("region", { name: "VPN location policies" });
  await panel.getByRole("button", { name: "Add location policy" }).click();
  await panel
    .getByLabel("Permitted team cities 1")
    .fill("Toronto\nHamilton\nOttawa");
  await panel.getByRole("button", { name: "Add location policy" }).click();
  await expect(
    panel.getByText(
      "Player qualification not recorded; automatic switching is blocked",
    ),
  ).toBeVisible();
  await panel.getByLabel("Enable location fallback").check();
  await panel.getByRole("button", { name: "Save VPN policies" }).click();
  await expect
    .poll(
      async () =>
        (await (await request.get("/api/v1/overview")).json()).device
          .preferences.vpn_policy?.enabled,
    )
    .toBe(true);
  await page.reload();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(panel.getByLabel("Permitted team cities 1")).toHaveValue(
    "Toronto\nHamilton\nOttawa",
  );
  const exported = await (
    await request.get("/api/v1/devices/living-room/configuration")
  ).json();
  expect(
    exported.configuration.preferences.vpn_policy.locations[1].target_id,
  ).toBe("montreal");
  expect(
    (await (await request.get("/api/v1/overview")).json()).device.plan,
  ).toEqual(before.device.plan);
  await panel.getByRole("button", { name: "Move earlier" }).nth(1).click();
  await panel.getByRole("button", { name: "Save VPN policies" }).click();
  await expect
    .poll(
      async () =>
        (await (await request.get("/api/v1/overview")).json()).device
          .preferences.vpn_policy.locations[0].target_id,
    )
    .toBe("montreal");
  await page
    .getByRole("button", { name: "Undo last edit", exact: true })
    .click();
  await expect(panel.getByLabel("VPN location 1")).toHaveValue("toronto");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await expect(panel).toHaveCSS("border-radius", "12px");
  await expect(panel.getByLabel("Permitted team cities 1")).toHaveCSS(
    "min-height",
    "100px",
  );
  await panel.screenshot({ path: "test-results/vpn-phone.png" });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await panel.screenshot({ path: "test-results/vpn-desktop.png" });
});
