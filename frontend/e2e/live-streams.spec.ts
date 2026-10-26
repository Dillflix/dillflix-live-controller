import { test, expect, type Page, type Locator } from "@playwright/test";
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
      now_playing: null as { title: string; valid_until: string } | null,
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
      now_playing: null as { title: string; valid_until: string } | null,
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
  let playlist = media.playlist;
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
          body: mediaStatus === 200 ? playlist : "Unavailable",
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
    set playlist(value: string) {
      playlist = value;
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

async function openSettings(card: Locator) {
  const settings = card.locator("details.df-stream-settings");
  if (!(await settings.evaluate((element: HTMLDetailsElement) => element.open)))
    await settings.locator("summary").click();
}
async function closeStream(card: Locator) {
  await openSettings(card);
  await card.getByRole("button", { name: "Close stream" }).click();
}
async function quality(card: Locator, tier: string) {
  await openSettings(card);
  await card.getByRole("combobox").selectOption(tier);
  await card.locator("summary").click();
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
    await expect(
      panel.getByRole("article", { name: "Feed 1", exact: true }),
    ).toBeVisible();
    await expect(
      panel.getByRole("article", { name: "Feed 2", exact: true }),
    ).toBeVisible();
    await expect(panel).not.toContainText("Choose a feed to watch");
    await expect(panel).not.toContainText("sound off initially");
    const firstBox = await page.getByTestId("stream-sports0").boundingBox();
    const secondBox = await page.getByTestId("stream-sports1").boundingBox();
    expect(secondBox!.y).toBeGreaterThanOrEqual(firstBox!.y + firstBox!.height);
    expect(secondBox!.x).toBe(firstBox!.x);
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

test("real HLS decodes locally, independent viewers switch quality and release on close/page exit", async ({
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
  await expect(first.locator("video")).toHaveJSProperty("muted", false);
  await expect(first.locator("video")).toHaveJSProperty("controls", false);
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
  await quality(first, "1");
  await expect.poll(() => data.created.length).toBe(3);
  await expect.poll(() => data.deleted).toContain(data.created[0].id);
  expect(data.created[2]).toMatchObject({ stream_id: "sports0", tier: 1 });
  await expect(first.locator("video")).toHaveJSProperty("volume", 0.25);
  expect(data.deleted).not.toContain(data.created[1].id);
  await closeStream(first);
  await expect.poll(() => data.deleted).toContain(data.created[2].id);
  await expect(first.locator("video")).toHaveCount(0);
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: true,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(second.locator("video")).toHaveCount(1);
  expect(data.deleted).not.toContain(data.created[1].id);
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: false,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(second.locator("video")).toHaveCount(1);
  expect(data.deleted).not.toContain(data.created[1].id);
  await page.evaluate(() => window.dispatchEvent(new Event("pagehide")));
  await expect.poll(() => data.deleted).toContain(data.created[1].id);
  await expect(second.locator("video")).toHaveCount(0);
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
  await closeStream(card);
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
  await closeStream(card);
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
  await quality(card, "1");
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
  await closeStream(card);
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
  await closeStream(card);
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
  await expect(card.locator("video")).toHaveJSProperty("muted", false);
  await closeStream(card);
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

test("player sound, pause, quality, collapse and fullscreen controls retain viewing intent", async ({
  page,
}) => {
  const data = await fixture(page);
  await page.goto("/public/");
  const card = page.getByTestId("stream-sports0");
  await card.getByRole("button", { name: "Watch stream" }).click();
  const video = card.locator("video");
  await expect
    .poll(() => video.evaluate((v: HTMLVideoElement) => v.videoWidth))
    .toBe(160);
  await expect(video).toHaveJSProperty("muted", false);
  await expect(video).toHaveJSProperty("paused", false);
  await expect(card).not.toContainText("Playing live coverage");
  await expect(card.getByRole("button", { name: "Stop watching" })).toHaveCount(
    0,
  );
  await expect(
    card.getByRole("button", { name: "Go live", exact: true }),
  ).toHaveCount(0);
  await card.getByRole("button", { name: "Mute", exact: true }).click();
  await expect(video).toHaveJSProperty("muted", true);
  await card.getByRole("button", { name: "Pause", exact: true }).click();
  await expect(video).toHaveJSProperty("paused", true);
  expect(data.deleted).toHaveLength(0);
  await quality(card, "1");
  await expect.poll(() => data.created.length).toBe(2);
  await expect(
    card.getByRole("button", { name: "Play", exact: true }),
  ).toBeEnabled();
  await expect(video).toHaveJSProperty("muted", true);
  await expect(video).toHaveJSProperty("paused", true);
  await card.getByRole("button", { name: "Play", exact: true }).click();
  await expect(video).toHaveJSProperty("paused", false);
  await card.getByRole("button", { name: "Collapse Feed 1" }).click();
  await expect(video).toBeHidden();
  await expect(
    card.getByRole("button", { name: "Expand Feed 1" }),
  ).toHaveAttribute("aria-expanded", "false");
  expect(data.deleted).not.toContain(data.created[1].id);
  await expect(video).toHaveJSProperty("paused", false);
  await card.getByRole("button", { name: "Unmute Feed 1" }).click();
  await expect(video).toHaveJSProperty("muted", false);
  await card.getByRole("button", { name: "Expand Feed 1" }).click();
  await expect(video).toBeVisible();
  expect(data.created).toHaveLength(2);
  await page.setViewportSize({ width: 320, height: 900 });
  await openSettings(card);
  await card.screenshot({
    path: "test-results/live-streams-mobile-settings.png",
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await card.locator("summary").press("Escape");
  await page.setViewportSize({ width: 1280, height: 900 });
  await card.getByRole("button", { name: "Fullscreen", exact: true }).click();
  await expect
    .poll(() => page.evaluate(() => document.fullscreenElement?.className))
    .toContain("df-stream-surface");
  await openSettings(card);
  await expect(
    card.getByRole("combobox", { name: "Feed 1 quality" }),
  ).toBeVisible();
  await card.locator("summary").press("Escape");
  await closeStream(card);
  await expect
    .poll(() => page.evaluate(() => document.fullscreenElement))
    .toBeNull();
  await expect.poll(() => data.deleted).toContain(data.created[1].id);
  expect(data.schedulerWrites).toBe(0);
});

test("blocked audible playback offers Play without muting or closing the session", async ({
  page,
}) => {
  const data = await fixture(page);
  await page.addInitScript(() => {
    const play = HTMLMediaElement.prototype.play;
    let blocked = false;
    HTMLMediaElement.prototype.play = function () {
      if (!blocked) {
        blocked = true;
        return Promise.reject(
          new DOMException("User gesture required", "NotAllowedError"),
        );
      }
      return play.call(this);
    };
  });
  await page.goto("/public/");
  const card = page.getByTestId("stream-sports0");
  await card.getByRole("button", { name: "Watch stream" }).click();
  await expect(
    card.getByRole("button", { name: "Play", exact: true }),
  ).toBeEnabled();
  await expect(card.locator("video")).toHaveJSProperty("muted", false);
  expect(data.deleted).toHaveLength(0);
  await card.getByRole("button", { name: "Play", exact: true }).click();
  await expect(card.locator("video")).toHaveJSProperty("paused", false);
  await closeStream(card);
  await expect.poll(() => data.deleted).toContain(data.created[0].id);
});

test("Jump to live skips ahead from paused native playback", async ({
  page,
}) => {
  const data = await fixture(page, true);
  // A bounded fixture provides a deterministic seekable range in native HLS.
  data.playlist = media.playlist + "#EXT-X-ENDLIST\n";
  await page.goto("/public/");
  test.skip(
    !(await page.evaluate(() =>
      document
        .createElement("video")
        .canPlayType("application/vnd.apple.mpegurl"),
    )),
    "Native HLS unavailable",
  );
  const card = page.getByTestId("stream-sports0");
  await card.getByRole("button", { name: "Watch stream" }).click();
  const video = card.locator("video");
  await expect
    .poll(() =>
      video.evaluate((v: HTMLVideoElement) =>
        v.seekable.length ? v.seekable.end(v.seekable.length - 1) : 0,
      ),
    )
    .toBeGreaterThan(6);
  await card.getByRole("button", { name: "Pause", exact: true }).click();
  await video.evaluate((v: HTMLVideoElement) => {
    v.currentTime = 0;
  });
  await card.getByRole("button", { name: "Jump to live" }).click();
  await expect(video).toHaveJSProperty("paused", false);
  await expect
    .poll(() => video.evaluate((v: HTMLVideoElement) => v.currentTime))
    .toBeGreaterThan(5);
  await closeStream(card);
  await expect.poll(() => data.deleted).toContain(data.created[0].id);
});

for (const width of [1280, 320]) {
  test(`feed titles stay distinct and visible when collapsed at ${width}px`, async ({
    page,
  }) => {
    const data = await fixture(page);
    const title =
      "Vancouver Canucks at Tampa Bay Lightning - Live NHL coverage";
    data.streams[0].now_playing = {
      title,
      valid_until: new Date(Date.now() + 60000).toISOString(),
    };
    data.streams[1].status = "live";
    data.streams[1].now_playing = {
      title: "Ningbo Open: Day 3",
      valid_until: new Date(Date.now() + 60000).toISOString(),
    };
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/public/");
    const first = page.getByTestId("stream-sports0");
    const second = page.getByTestId("stream-sports1");
    await expect(first.locator(".df-stream-event-title")).toHaveText(title);
    await expect(second.locator(".df-stream-event-title")).toHaveText(
      "Ningbo Open: Day 3",
    );
    await expect(first).not.toContainText("Ningbo Open");
    await first.getByRole("button", { name: "Collapse Feed 1" }).click();
    await expect(first.locator(".df-stream-event-title")).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await first.screenshot({
      path: `test-results/live-streams-title-${width}.png`,
    });
    expect(data.created).toHaveLength(0);
  });
}

test("event title refresh does not replace an active viewing session", async ({
  page,
}) => {
  const data = await fixture(page);
  data.streams[0].now_playing = {
    title: "First event",
    valid_until: new Date(Date.now() + 60000).toISOString(),
  };
  await page.goto("/public/");
  const card = page.getByTestId("stream-sports0");
  await card.getByRole("button", { name: "Watch stream" }).click();
  await expect
    .poll(() =>
      card.locator("video").evaluate((v: HTMLVideoElement) => v.videoWidth),
    )
    .toBe(160);
  await card.getByRole("button", { name: "Pause", exact: true }).click();
  await expect(card.locator(".df-stream-event-title")).toHaveText(
    "First event",
  );
  data.streams[0].now_playing = {
    title: "Next event",
    valid_until: new Date(Date.now() + 60000).toISOString(),
  };
  await expect(card.locator(".df-stream-event-title")).toHaveText(
    "Next event",
    { timeout: 20000 },
  );
  await expect(card.locator("video")).toHaveJSProperty("paused", true);
  expect(data.created).toHaveLength(1);
  expect(data.deleted).toHaveLength(0);
  await closeStream(card);
});

test("expired and unavailable feed titles are hidden without waiting for another catalog response", async ({
  page,
}) => {
  const data = await fixture(page);
  data.streams[0].now_playing = {
    title: "Expiring event",
    valid_until: new Date(Date.now() + 3000).toISOString(),
  };
  data.streams[1].now_playing = {
    title: "Unverified feed title",
    valid_until: new Date(Date.now() + 60000).toISOString(),
  };
  await page.goto("/public/");
  const first = page.getByTestId("stream-sports0");
  await expect(first.locator(".df-stream-event-title")).toHaveText(
    "Expiring event",
  );
  await expect(
    page.getByTestId("stream-sports1").locator(".df-stream-event-title"),
  ).toHaveCount(0);
  await expect(first.locator(".df-stream-event-title")).toHaveCount(0, {
    timeout: 5000,
  });
  await expect(
    first.getByRole("button", { name: "Collapse Feed 1" }),
  ).toBeVisible();
});
