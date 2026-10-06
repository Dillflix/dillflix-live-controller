import { test, expect } from "@playwright/test";

const endpoint = "/api/v1/devices/living-room/plex";
const empty = () => ({
  revision: 0,
  enabled: false,
  base_url: "",
  rating_key: "8",
  credential_configured: false,
  real_playback: false,
  default_images: {},
  defaults: {},
  status: {
    pending: false,
    hold: false,
    in_flight: false,
    blocked: null,
    error: null,
    retry_at: null,
    fallback_at: null,
    last_success: null,
    desired: {},
    applied: {},
  },
});

test("Plex settings save a write-only credential and test without enabling", async ({
  page,
}) => {
  let saved = empty();
  let submitted = "";
  await page.route("**" + endpoint, async (route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON();
      submitted = body.token;
      expect(body.expected_revision).toBe(saved.revision);
      saved = {
        ...saved,
        revision: saved.revision + 1,
        base_url: body.base_url,
        rating_key: body.rating_key,
        credential_configured: true,
      };
      await route.fulfill({
        json: { accepted: true, revision: saved.revision },
      });
    } else await route.fulfill({ json: saved });
  });
  await page.route("**" + endpoint + "/test", (route) =>
    route.fulfill({
      json: {
        title: "Dillflix Live - Stream 1",
        library: "Streams",
        version: "1.2.3",
      },
    }),
  );
  await page.goto("/");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const panel = page.getByRole("region", { name: "Plex item settings" });
  await expect(
    panel.getByLabel("Automatic Plex title and artwork updates"),
  ).toBeDisabled();
  await panel.getByLabel("Plex server URL").fill("http://plex:32400");
  await panel
    .getByLabel("Plex token", { exact: true })
    .fill("test-private-token");
  await panel.getByRole("button", { name: "Test Plex connection" }).click();
  await expect(panel.getByRole("status")).toContainText("read access verified");
  await panel.getByRole("button", { name: "Save Plex settings" }).click();
  await expect(panel.getByRole("status")).toContainText("settings saved");
  expect(submitted).toBe("test-private-token");
  await expect(panel.getByLabel("Plex token", { exact: true })).toHaveValue("");
  await expect(panel.getByLabel("Plex token", { exact: true })).toHaveAttribute(
    "placeholder",
    "Saved; leave blank to keep",
  );
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain(
    "test-private-token",
  );
});

test("Plex settings preserve a conflicted draft and fit a 320 pixel phone", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await page.route("**" + endpoint, (route) =>
    route.request().method() === "PUT"
      ? route.fulfill({
          status: 409,
          json: {
            detail: "Plex settings changed. Reload and review your changes.",
          },
        })
      : route.fulfill({ json: empty() }),
  );
  await page.goto("/");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const panel = page.getByRole("region", { name: "Plex item settings" });
  await panel.getByLabel("Plex server URL").fill("http://draft-plex:32400");
  await panel.getByRole("button", { name: "Save Plex settings" }).click();
  await expect(panel.getByRole("alert")).toContainText("Reload and review");
  await expect(panel.getByLabel("Plex server URL")).toHaveValue(
    "http://draft-plex:32400",
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: "test-results/plex-settings-phone.png",
    fullPage: true,
  });
});

test("Plex default upload validation reports a recoverable error", async ({
  page,
}) => {
  await page.route("**" + endpoint, (route) =>
    route.fulfill({ json: empty() }),
  );
  await page.route("**" + endpoint + "/defaults/poster?**", (route) =>
    route.fulfill({
      status: 502,
      json: { detail: "Use a valid static PNG, JPEG or WebP image" },
    }),
  );
  await page.goto("/");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const panel = page.getByRole("region", { name: "Plex item settings" });
  await panel.getByLabel("Default poster", { exact: true }).setInputFiles({
    name: "bad.png",
    mimeType: "image/png",
    buffer: Buffer.from("invalid"),
  });
  await expect(panel.getByRole("alert")).toContainText("valid static PNG");
  await expect(
    panel.getByLabel("Default poster", { exact: true }),
  ).toBeEnabled();
});

test("Plex title status distinguishes the requested default from the verified event", async ({
  page,
}) => {
  const info = empty();
  await page.route("**" + endpoint, (route) =>
    route.fulfill({
      json: {
        ...info,
        enabled: true,
        real_playback: true,
        status: {
          ...info.status,
          pending: true,
          desired: {
            mode: "defaults",
            title: "Default artwork",
            plex_title: "Dillflix Live",
          },
          applied_title: {
            value: "Beijing Open: Day 7",
            at: "2026-10-06T05:00:00Z",
          },
        },
      },
    }),
  );
  await page.goto("/");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const panel = page.getByRole("region", { name: "Plex item settings" });
  await expect(
    panel.getByText("Requested title: Dillflix Live", { exact: true }),
  ).toBeVisible();
  await expect(
    panel.getByText(/^Verified title: Beijing Open: Day 7/),
  ).toBeVisible();
  await expect(panel.getByText(/Update pending/)).toBeVisible();
  await expect(
    panel.getByLabel("Automatic Plex title and artwork updates"),
  ).toBeVisible();
});
