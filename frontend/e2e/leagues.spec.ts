import { test, expect } from "@playwright/test";

for (const [league, code] of [
  ["NHL", "nhl"],
  ["College Football", "college-football"],
  ["EFL Championship", "eng.2"],
  ["Bundesliga", "ger.1"],
  ["2. Bundesliga", "ger.2"],
  ["MLS", "usa.1"],
  ["NCAA Women's Basketball", "womens-college-basketball"],
  ["PWHL", "pwhl"],
  ["United Rugby Championship", "urc"],
  ["Australian AFL", "afl"],
]) {
  test(`${league} discovery can be changed on a phone, reloaded, exported and undone`, async ({
    page,
    request,
  }) => {
    await request.post("/api/v1/simulation", {
      data: { action: "scenario", scenario: "normal" },
    });
    const before = await (await request.get("/api/v1/overview")).json();
    await page.setViewportSize({ width: 320, height: 780 });
    await page.goto("/");
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    for (const name of Object.values(before.meta.league_choices))
      await expect(
        page.getByLabel(`Discover ${name}`, { exact: true }),
      ).toBeChecked();
    await page.getByLabel(`Discover ${league}`, { exact: true }).click();
    await expect(
      page.getByLabel(`Discover ${league}`, { exact: true }),
    ).not.toBeChecked();
    await page.reload();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await expect(
      page.getByLabel(`Discover ${league}`, { exact: true }),
    ).not.toBeChecked();
    const saved = await (await request.get("/api/v1/overview")).json();
    expect(saved.device.plan).toEqual(before.device.plan);
    expect(
      saved.events.some(
        (e: { content_id: string }) => e.content_id === "demo:canadiens",
      ),
    ).toBe(true);
    const exported = await (
      await request.get("/api/v1/devices/living-room/configuration")
    ).json();
    expect(exported.configuration.preferences.discovery_leagues).not.toContain(
      code,
    );
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: `test-results/leagues-${code}-phone.png`,
      fullPage: true,
    });
    await page
      .getByRole("button", { name: "Undo last edit", exact: true })
      .click();
    await expect(
      page.getByLabel(`Discover ${league}`, { exact: true }),
    ).toBeChecked();
  });
}
