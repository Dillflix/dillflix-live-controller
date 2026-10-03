import { test, expect } from "@playwright/test";
import { readFile } from "node:fs/promises";

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
