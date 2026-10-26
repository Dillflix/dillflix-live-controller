import type { Page } from "@playwright/test";

// Schedule fixtures mock public identity/overview. Keep the optional viewer
// disabled too, so these tests do not depend on the demo server's proxy auth.
export async function disableBrowserStreams(page: Page) {
  await page.route("**/api/public/v1/streams", (route) =>
    route.fulfill({ json: { enabled: false, streams: [] } }),
  );
}
