import { type Page, type WebSocketRoute } from "@playwright/test";
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

export async function screenFixture(
  page: Page,
  interval = 100,
  timestamp = (index: number) => index * 100000,
) {
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
          (frame.key ? 1n << 62n : 0n) | BigInt(timestamp(index)),
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
