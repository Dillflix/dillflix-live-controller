import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

const media = JSON.parse(
  readFileSync(
    new URL("../../tests/fixtures/live-hls.json", import.meta.url),
    "utf8",
  ),
);
const path = "/api/public/v1/streams";
const overview = {
  revision: 1,
  now: "2026-10-25T12:00:00Z",
  viewer: { name: "Guest", guest: true },
  timezone: "America/Vancouver",
  permissions: { play_now: false, add_to_plan: false },
  actions_message: "Shared coverage requests are disabled.",
  now_playing: {
    event: null,
    verified: false,
    simulated: false,
    switching: false,
  },
  events: [],
};

async function fixture(page: Page, native = false) {
  if (!native)
    await page.addInitScript(() => {
      const canPlayType = HTMLMediaElement.prototype.canPlayType;
      HTMLMediaElement.prototype.canPlayType = function (type) {
        return type === "application/vnd.apple.mpegurl"
          ? ""
          : canPlayType.call(this, type);
      };
    });
  const streams = [
    {
      id: "sports0",
      label: "Sports 0",
      status: "live",
      default_tier: 0,
      tiers: [
        { id: 0, label: "1080p", status: "live" },
        { id: 1, label: "720p", status: "live" },
      ],
    },
    {
      id: "sports1",
      label: "Sports 1",
      status: "unknown",
      default_tier: 0,
      tiers: [{ id: 0, label: "1080p", status: "unknown" }],
    },
    {
      id: "rink-cam",
      label: "Rink camera",
      status: "offline",
      default_tier: 2,
      tiers: [{ id: 2, label: "480p", status: "offline" }],
    },
  ];
  const created: { id: string; stream_id: string; tier: number }[] = [];
  const deleted: string[] = [];
  const manifests: string[] = [];
  const segments: string[] = [];
  let schedulerWrites = 0;
  let createStatus = 200;
  let mediaStatus = 200;
  let enabled = true;
  let hold: Promise<void> | undefined;
  let resume: (() => void) | undefined;
  await page.route("**/api/public/v1/overview", (route) =>
    route.fulfill({ json: overview }),
  );
  await page.route("**/api/public/v1/watch-plan", (route) => {
    schedulerWrites++;
    return route.fulfill({ status: 500 });
  });
  await page.route(`**${path}`, (route) =>
    route.fulfill({ json: { enabled, streams } }),
  );
  await page.route(
    /\/api\/public\/v1\/streams\/sessions(?:\/.*)?$/,
    async (route) => {
      const request = route.request();
      if (request.method() === "POST") {
        const id = `browser_${String(created.length + 1).padStart(48, "0")}`;
        created.push({ id, ...request.postDataJSON() });
        if (hold) await hold;
        return route.fulfill(
          createStatus === 200
            ? {
                json: {
                  session_id: id,
                  expires_in: 120,
                  playlist_url: `${path}/sessions/${id}/hls/index.m3u8`,
                },
              }
            : { status: createStatus, json: { detail: "Not available" } },
        );
      }
      const name = new URL(request.url()).pathname.split("/").at(-1)!;
      if (request.method() === "DELETE") {
        deleted.push(name);
        return route.fulfill({ status: 204 });
      }
      if (name === "index.m3u8") {
        manifests.push(request.url());
        return route.fulfill({
          status: mediaStatus,
          contentType: "application/vnd.apple.mpegurl",
          body: mediaStatus === 200 ? media.playlist : "Unavailable",
        });
      }
      if (media.segments[name]) {
        segments.push(name);
        return route.fulfill({
          contentType: "video/mp2t",
          body: Buffer.from(media.segments[name], "base64"),
        });
      }
      return route.fulfill({ status: 404 });
    },
  );
  return {
    created,
    deleted,
    manifests,
    segments,
    streams,
    get schedulerWrites() {
      return schedulerWrites;
    },
    set createStatus(value: number) {
      createStatus = value;
    },
    set mediaStatus(value: number) {
      mediaStatus = value;
    },
    set enabled(value: boolean) {
      enabled = value;
    },
    holdCreation() {
      hold = new Promise((resolve) => {
        resume = resolve;
      });
    },
    resumeCreation() {
      resume?.();
      hold = undefined;
    },
  };
}

for (const width of [1280, 320]) {
  test(`stream catalog ${width}px shows configured feeds without autoplay or planner writes`, async ({
    page,
  }) => {
    const data = await fixture(page);
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/public/");
    const panel = page.getByRole("region", { name: "Watch streams" });
    await expect(panel.getByRole("article")).toHaveCount(3);
    await expect(page.getByTestId("stream-sports1")).toContainText(
      "Status unknown",
    );
    await expect(
      page
        .getByTestId("stream-rink-cam")
        .getByRole("button", { name: "Watch stream" }),
    ).toBeDisabled();
    await expect(panel.locator("video")).toHaveCount(0);
    expect(data.created).toHaveLength(0);
    expect(data.schedulerWrites).toBe(0);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: `test-results/live-streams-${width}.png`,
      fullPage: true,
    });
  });
}

test("real HLS decodes locally, independent viewers switch quality and release on stop/hidden", async ({
  page,
}) => {
  const data = await fixture(page);
  await page.goto("/public/");
  const first = page.getByTestId("stream-sports0");
  const second = page.getByTestId("stream-sports1");
  await first.getByRole("button", { name: "Watch stream" }).click();
  await expect
    .poll(() =>
      first
        .locator("video")
        .evaluate((video: HTMLVideoElement) => video.videoWidth),
    )
    .toBe(160);
  await expect
    .poll(() =>
      first
        .locator("video")
        .evaluate(
          (video: HTMLVideoElement) =>
            video.getVideoPlaybackQuality().totalVideoFrames,
        ),
    )
    .toBeGreaterThan(0);
  await expect(first.locator("video")).toHaveJSProperty("muted", true);
  await expect(first.locator("video")).toHaveJSProperty("controls", true);
  await expect(first.locator("video")).toHaveJSProperty("playsInline", true);
  expect(data.manifests.length).toBeGreaterThan(0);
  expect(data.segments.length).toBeGreaterThan(0);
  await first.locator("video").evaluate((video: HTMLVideoElement) => {
    video.muted = false;
    video.volume = 0.25;
  });
  await expect(first.locator("video")).toHaveJSProperty("muted", false);
  await second.getByRole("button", { name: "Watch stream" }).click();
  await expect.poll(() => data.created.length).toBe(2);
  await expect
    .poll(() =>
      second
        .locator("video")
        .evaluate((video: HTMLVideoElement) => video.videoWidth),
    )
    .toBe(160);
  await page
    .getByRole("region", { name: "Watch streams" })
    .screenshot({ path: "test-results/live-streams-playing.png" });
  await first.getByRole("combobox").selectOption("1");
  await expect.poll(() => data.created.length).toBe(3);
  await expect.poll(() => data.deleted).toContain(data.created[0].id);
  expect(data.created[2]).toMatchObject({ stream_id: "sports0", tier: 1 });
  expect(data.deleted).not.toContain(data.created[1].id);
  await first.getByRole("button", { name: "Stop watching" }).click();
  await expect.poll(() => data.deleted).toContain(data.created[2].id);
  await expect(first.locator("video")).toHaveCount(0);
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: true,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(() => data.deleted).toContain(data.created[1].id);
  await expect(second.locator("video")).toHaveCount(0);
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: false,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(
    second.getByRole("button", { name: "Watch stream" }),
  ).toBeVisible();
  expect(data.created).toHaveLength(3);
  expect(data.schedulerWrites).toBe(0);
});

test("a stopped pending request releases its late session without attaching media", async ({
  page,
}) => {
  const data = await fixture(page);
  data.holdCreation();
  await page.goto("/public/");
  const card = page.getByTestId("stream-sports0");
  await card.getByRole("button", { name: "Watch stream" }).click();
  await expect.poll(() => data.created.length).toBe(1);
  await card.getByRole("button", { name: "Stop watching" }).click();
  data.resumeCreation();
  await expect.poll(() => data.deleted).toContain(data.created[0].id);
  await expect(card.locator("video")).toHaveCount(0);
  expect(data.manifests).toHaveLength(0);
  expect(data.schedulerWrites).toBe(0);
});

test("authentication errors are actionable and Retry stream creates a fresh independent session", async ({
  page,
}) => {
  const data = await fixture(page);
  data.createStatus = 403;
  await page.goto("/public/");
  const card = page.getByTestId("stream-sports0");
  await card.getByRole("button", { name: "Watch stream" }).click();
  await expect(card.getByRole("alert")).toContainText(
    "Sign in through the public page",
  );
  data.createStatus = 200;
  await card.getByRole("button", { name: "Retry stream" }).click();
  await expect
    .poll(() =>
      card
        .locator("video")
        .evaluate((video: HTMLVideoElement) => video.videoWidth),
    )
    .toBe(160);
  await card.getByRole("button", { name: "Stop watching" }).click();
  await expect.poll(() => data.deleted).toContain(data.created[1].id);
  expect(data.schedulerWrites).toBe(0);
});

test("a quality change fences late allocation results and releases only the older session", async ({
  page,
}) => {
  const data = await fixture(page);
  data.holdCreation();
  await page.goto("/public/");
  const card = page.getByTestId("stream-sports0");
  await card.getByRole("button", { name: "Watch stream" }).click();
  await expect.poll(() => data.created.length).toBe(1);
  await card.getByRole("combobox").selectOption("1");
  await expect.poll(() => data.created.length).toBe(2);
  data.resumeCreation();
  await expect.poll(() => data.deleted).toContain(data.created[0].id);
  await expect
    .poll(() =>
      card
        .locator("video")
        .evaluate((video: HTMLVideoElement) => video.videoWidth),
    )
    .toBe(160);
  expect(data.manifests.every((url) => url.includes(data.created[1].id))).toBe(
    true,
  );
  expect(data.deleted).not.toContain(data.created[1].id);
  await card.getByRole("button", { name: "Stop watching" }).click();
  await expect.poll(() => data.deleted).toContain(data.created[1].id);
});

test("media authentication failure releases the session and offers manual retry", async ({
  page,
}) => {
  const data = await fixture(page);
  data.mediaStatus = 401;
  await page.goto("/public/");
  const card = page.getByTestId("stream-sports0");
  await card.getByRole("button", { name: "Watch stream" }).click();
  await expect(card.getByRole("alert")).toContainText(
    "Sign in through the public page",
  );
  await expect.poll(() => data.deleted).toContain(data.created[0].id);
  await expect(
    card.getByRole("button", { name: "Retry stream" }),
  ).toBeVisible();
  expect(data.created).toHaveLength(1);
});

test("transient media failures have bounded retries and release the failed session", async ({
  page,
}) => {
  const data = await fixture(page);
  data.mediaStatus = 503;
  await page.goto("/public/");
  const card = page.getByTestId("stream-sports0");
  await card.getByRole("button", { name: "Watch stream" }).click();
  await expect(card.getByRole("alert")).toContainText("Retry to reconnect", {
    timeout: 10000,
  });
  await expect.poll(() => data.deleted).toContain(data.created[0].id);
  expect(data.manifests.length).toBeLessThanOrEqual(3);
  expect(data.created).toHaveLength(1);
  data.mediaStatus = 200;
  await card.getByRole("button", { name: "Retry stream" }).click();
  await expect
    .poll(() =>
      card
        .locator("video")
        .evaluate((video: HTMLVideoElement) => video.videoWidth),
    )
    .toBe(160);
  await card.getByRole("button", { name: "Stop watching" }).click();
  await expect.poll(() => data.deleted).toContain(data.created[1].id);
});

test("native HLS decodes when the browser supports it and releases on stop", async ({
  page,
}) => {
  const data = await fixture(page, true);
  await page.goto("/public/");
  test.skip(
    !(await page.evaluate(() =>
      document
        .createElement("video")
        .canPlayType("application/vnd.apple.mpegurl"),
    )),
    "Browser has no native HLS; the MSE tests cover playback here.",
  );
  const card = page.getByTestId("stream-sports0");
  await card.getByRole("button", { name: "Watch stream" }).click();
  await expect
    .poll(() =>
      card
        .locator("video")
        .evaluate((video: HTMLVideoElement) => video.videoWidth),
    )
    .toBe(160);
  await expect(card.locator("video")).toHaveJSProperty("muted", true);
  await card.getByRole("button", { name: "Stop watching" }).click();
  await expect.poll(() => data.deleted).toContain(data.created[0].id);
});

test("disabled browser streams leave the existing public schedule unchanged", async ({
  page,
}) => {
  const data = await fixture(page);
  data.enabled = false;
  await page.goto("/public/");
  await expect(page.getByRole("heading", { name: "What's on" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Watch streams" })).toHaveCount(
    0,
  );
  expect(data.created).toHaveLength(0);
});
