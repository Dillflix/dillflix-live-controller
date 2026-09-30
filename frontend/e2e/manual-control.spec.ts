import { test, expect, type Page, type WebSocketRoute } from "@playwright/test";
import { screenFixture } from "./screen-fixture";

const controlPath = "/api/v1/devices/living-room/control";
function manualFixture() {
  let session: {
    session_id: string;
    started_at: string;
    expires_at: string;
    return_mode: string;
  } | null = null;
  let revision = 1000;
  let automation = "active";
  let latest: WebSocketRoute | undefined;
  const changes: any[] = [],
    inputs: any[] = [];
  let connections = 0,
    closed = 0;
  let acknowledge = true;
  return {
    changes,
    inputs,
    get session() {
      return session;
    },
    get connections() {
      return connections;
    },
    get closed() {
      return closed;
    },
    set acknowledge(value: boolean) {
      acknowledge = value;
    },
    async install(page: Page) {
      await screenFixture(page);
      await page.route("**/api/v1/overview", async (route) => {
        const response = await route.fetch();
        const data = await response.json();
        data.device = {
          ...data.device,
          revision,
          automation,
          manual_control: session,
          ...(session
            ? { observed: null, desired: null, playback_state: "waiting" }
            : {}),
        };
        data.meta.server_time = new Date().toISOString();
        await route.fulfill({ json: data });
      });
      await page.route(`**${controlPath}`, async (route) => {
        const body = route.request().postDataJSON();
        if (body.expected_revision !== revision) {
          await route.fulfill({
            status: 409,
            json: { detail: "Configuration changed. Refresh and try again." },
          });
          return;
        }
        changes.push(body);
        if (body.action === "release") {
          session = null;
          automation = body.release_mode;
          latest?.close();
        } else {
          if (body.action === "take") latest?.close();
          const previous = session?.return_mode || automation;
          session = {
            session_id: body.session_id,
            started_at: new Date().toISOString(),
            expires_at: new Date(
              Date.now() + body.minutes * 60000,
            ).toISOString(),
            return_mode: previous,
          };
          automation = "paused";
        }
        revision++;
        await route.fulfill({
          json: { accepted: true, revision, command_id: body.command_id },
        });
      });
      await page.routeWebSocket(`**${controlPath}/input`, (ws) => {
        latest = ws;
        connections++;
        let didClose = false;
        ws.onClose(() => {
          if (!didClose) {
            didClose = true;
            closed++;
            ws.close();
          }
        });
        ws.onMessage((message) => {
          const body = JSON.parse(String(message));
          if (body.owner_token) {
            ws.send(JSON.stringify({ type: "ready" }));
            return;
          }
          inputs.push(body);
          if (acknowledge)
            ws.send(JSON.stringify({ type: "sent", seq: body.seq }));
        });
      });
    },
  };
}

test("four-hour control, text and focused keyboard input, extend and resume", async ({
  page,
}) => {
  const fixture = manualFixture();
  await fixture.install(page);
  await page.goto("/");
  await page.getByLabel("Manual control duration").selectOption("240");
  await page.getByRole("button", { name: "Take control", exact: true }).click();
  await expect(
    page.getByText("You have control", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Remote connected", { exact: true }),
  ).toBeVisible();
  expect(fixture.changes[0].minutes).toBe(240);
  await page.getByRole("group", { name: "TV remote", exact: true }).focus();
  await page.keyboard.press("ArrowUp");
  await expect.poll(() => fixture.inputs.length).toBe(1);
  expect(fixture.inputs[0].key).toBe("up");
  const text = page.getByLabel("Send text to the TV");
  await text.fill("UFC main card");
  await text.press("ArrowLeft");
  expect(fixture.inputs.length).toBe(1);
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => fixture.inputs.length).toBe(2);
  expect(fixture.inputs[1].text).toBe("UFC main card");
  await expect(text).toHaveValue("");
  await page.getByLabel("Manual control duration").selectOption("480");
  await page
    .getByRole("button", { name: "Extend control", exact: true })
    .click();
  await expect.poll(() => fixture.changes.length).toBe(2);
  expect(fixture.changes[1].minutes).toBe(480);
  expect(fixture.connections).toBe(1);
  await page.screenshot({
    path: "test-results/manual-control-desktop.png",
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "Resume automation", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Take control", exact: true }),
  ).toBeVisible();
  expect(fixture.changes.at(-1).release_mode).toBe("active");
  await expect.poll(() => fixture.closed).toBe(1);
});

test("phone remote is usable at 320px and can end while keeping automation paused", async ({
  page,
}) => {
  const fixture = manualFixture();
  await fixture.install(page);
  await page.setViewportSize({ width: 320, height: 780 });
  await page.goto("/");
  await page.getByRole("button", { name: "Take control", exact: true }).click();
  await expect(
    page.getByText("Remote connected", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Right", exact: true }).click();
  await expect.poll(() => fixture.inputs.length).toBe(1);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  const size = await page
    .getByRole("button", { name: "Select", exact: true })
    .boundingBox();
  expect(size!.width).toBeGreaterThanOrEqual(44);
  expect(size!.height).toBeGreaterThanOrEqual(44);
  await page.screenshot({
    path: "test-results/manual-control-phone.png",
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "End control and stay paused", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Take control", exact: true }),
  ).toBeVisible();
  expect(fixture.changes.at(-1).release_mode).toBe("paused");
});

test("disconnect never replays input; background tab releases remote but retains session", async ({
  page,
}) => {
  const fixture = manualFixture();
  await fixture.install(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Take control", exact: true }).click();
  await expect(
    page.getByText("Remote connected", { exact: true }),
  ).toBeVisible();
  fixture.acknowledge = false;
  await page.getByRole("button", { name: "Home", exact: true }).click();
  await expect(
    page.getByText(
      "Input delivery is uncertain. Check the TV before sending it again.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Reconnect remote", exact: true }),
  ).toBeVisible();
  fixture.acknowledge = true;
  await page
    .getByRole("button", { name: "Reconnect remote", exact: true })
    .click();
  await expect(
    page.getByText("Remote connected", { exact: true }),
  ).toBeVisible();
  expect(fixture.inputs.length).toBe(1);
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      value: true,
      configurable: true,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(() => fixture.closed).toBe(2);
  expect(fixture.session).not.toBeNull();
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      value: false,
      configurable: true,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(
    page.getByText("Remote connected", { exact: true }),
  ).toBeVisible();
  expect(fixture.inputs.length).toBe(1);
  await page.reload();
  await expect(
    page.getByText("You have control", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Remote connected", { exact: true }),
  ).toBeVisible();
  expect(fixture.inputs.length).toBe(1);
});

test("another browser must explicitly confirm a takeover", async ({
  page,
  context,
}) => {
  const fixture = manualFixture();
  await fixture.install(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Take control", exact: true }).click();
  await expect(
    page.getByText("Remote connected", { exact: true }),
  ).toBeVisible();
  const other = await context.newPage();
  await fixture.install(other);
  await other.goto("/");
  await expect(
    other.getByText("Manual control in another browser", { exact: true }),
  ).toBeVisible();
  await expect(
    other.getByRole("group", { name: "TV remote", exact: true }),
  ).toHaveCount(0);
  await other.getByRole("button", { name: "Take over", exact: true }).click();
  expect(fixture.changes.length).toBe(1);
  await other
    .getByRole("button", { name: "Confirm takeover", exact: true })
    .click();
  await expect(
    other.getByText("You have control", { exact: true }),
  ).toBeVisible();
  expect(fixture.changes[1].takeover).toBeTruthy();
  await expect(
    page.getByText("Manual control in another browser", { exact: true }),
  ).toBeVisible();
  await other.close();
});
