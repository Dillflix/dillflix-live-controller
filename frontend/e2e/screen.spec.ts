import { test, expect, type Page, type WebSocketRoute } from "@playwright/test";
import { readFileSync } from "node:fs";

const fixture = JSON.parse(
  readFileSync(
    new URL("../../tests/fixtures/screen-h264.json", import.meta.url),
    "utf8",
  ),
);
const path = "/api/v1/devices/living-room/screen";
const metadata = {
  type: "stream",
  protocol: 1,
  codec: "h264",
  device_name: "Test TV",
  width: 320,
  height: 180,
  max_fps: 10,
};

function packet(flags: bigint, data: Buffer) {
  const header = Buffer.alloc(12);
  header.writeBigUInt64BE(flags);
  header.writeUInt32BE(data.length, 8);
  return Buffer.concat([header, data]);
}

async function screenFixture(page: Page, interval = 100) {
  let opened = 0;
  let closed = 0;
  let latest: WebSocketRoute | undefined;
  await page.route(`**${path}`, (route) =>
    route.fulfill({ json: { enabled: true, stream_path: path + "/stream" } }),
  );
  await page.routeWebSocket(`**${path}/stream`, (ws) => {
    opened++;
    latest = ws;
    ws.send(JSON.stringify(metadata));
    ws.send(packet(1n << 63n, Buffer.from(fixture.config, "base64")));
    let index = 0;
    const timer = setInterval(() => {
      const frame = fixture.frames[index % fixture.frames.length];
      ws.send(
        packet(
          (frame.key ? 1n << 62n : 0n) | BigInt(index * 100000),
          Buffer.from(frame.data, "base64"),
        ),
      );
      index++;
    }, interval);
    ws.onClose(() => {
      closed++;
      clearInterval(timer);
    });
  });
  return {
    get opened() {
      return opened;
    },
    get closed() {
      return closed;
    },
    disconnect() {
      latest?.close({ code: 1011, reason: "Fixture interruption" });
    },
  };
}

test("screen decodes real H.264, reconnects and releases video when hidden", async ({
  page,
  request,
}) => {
  const fixture = await screenFixture(page);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(page.getByTestId("screen-state")).toHaveText("Live screen", {
    timeout: 15000,
  });
  const video = page.getByLabel("Living room live screen");
  await expect
    .poll(() => video.evaluate((node: HTMLVideoElement) => node.videoWidth))
    .toBe(320);
  await expect
    .poll(() => video.evaluate((node: HTMLVideoElement) => node.currentTime))
    .toBeGreaterThan(0.3);
  await page.screenshot({
    path: "test-results/screen-desktop.png",
    fullPage: true,
  });
  const before = (await (await request.get("/api/v1/overview")).json()).device;
  fixture.disconnect();
  await expect.poll(() => fixture.opened).toBeGreaterThan(1);
  await expect(page.getByTestId("screen-state")).toHaveText("Live screen");
  await page.getByRole("button", { name: "Hide screen", exact: true }).click();
  await expect(page.locator("video")).toHaveCount(0);
  await expect.poll(() => fixture.closed).toBeGreaterThan(0);
  const opened = fixture.opened;
  await page.reload();
  await expect(
    page.getByRole("button", { name: "Show screen", exact: true }),
  ).toBeVisible();
  expect(fixture.opened).toBe(opened);
  await page.getByRole("button", { name: "Show screen", exact: true }).click();
  await expect(page.getByTestId("screen-state")).toHaveText("Live screen");
  const after = (await (await request.get("/api/v1/overview")).json()).device;
  expect(after.plan).toEqual(before.plan);
  expect(after.revision).toBe(before.revision);
  expect(errors).toEqual([]);
});

test("background tabs release capture and return to a fresh live stream", async ({
  page,
}) => {
  const fixture = await screenFixture(page);
  await page.goto("/");
  await expect(page.getByTestId("screen-state")).toHaveText("Live screen");
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      value: true,
      configurable: true,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(page.getByTestId("screen-state")).toHaveText("Suspended");
  await expect.poll(() => fixture.closed).toBeGreaterThan(0);
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      value: false,
      configurable: true,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(page.getByTestId("screen-state")).toHaveText("Live screen");
  expect(fixture.opened).toBeGreaterThan(1);
});

test("an excessive retained video buffer is replaced with a fresh stream", async ({
  page,
}) => {
  const fixture = await screenFixture(page, 5);
  await page.goto("/");
  await expect(page.getByTestId("screen-state")).toHaveText("Live screen");
  await expect
    .poll(() => fixture.opened, { timeout: 10000 })
    .toBeGreaterThan(1);
  await expect(page.getByTestId("screen-state")).toHaveText("Live screen");
});

test("phone: inline live screen, useful offline state and no horizontal overflow", async ({
  page,
}) => {
  const fixture = await screenFixture(page);
  await page.setViewportSize({ width: 320, height: 780 });
  await page.goto("/");
  await expect(page.getByTestId("screen-state")).toHaveText("Live screen", {
    timeout: 15000,
  });
  await expect(page.locator("video")).toHaveAttribute("playsinline", "");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: "test-results/screen-phone.png",
    fullPage: true,
  });
  fixture.disconnect();
  await expect(page.getByTestId("screen-state")).toHaveText("Reconnecting");
  await expect(page.getByTestId("screen-state")).toHaveText("Live screen");
});

test("unsupported browser offers an explanation without opening a stream", async ({
  page,
}) => {
  const fixture = await screenFixture(page);
  await page.addInitScript(() => {
    Object.defineProperty(window, "MediaSource", {
      value: undefined,
      configurable: true,
    });
    Object.defineProperty(window, "ManagedMediaSource", {
      value: undefined,
      configurable: true,
    });
    Object.defineProperty(window, "WebKitMediaSource", {
      value: undefined,
      configurable: true,
    });
  });
  await page.goto("/");
  await expect(page.getByTestId("screen-state")).toHaveText("Unsupported");
  await expect(
    page.getByText("This browser cannot decode the screen stream.", {
      exact: false,
    }),
  ).toBeVisible();
  expect(fixture.opened).toBe(0);
});
