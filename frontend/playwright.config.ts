import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import chromium from "@sparticuz/chromium";
const local = resolve(".browser-cache/chromium");
export default defineConfig({
  testDir: "e2e",
  workers: 1,
  retries: 0,
  use: {
    baseURL: process.env.TEST_BASE_URL || "http://127.0.0.1:8790",
    headless: true,
    launchOptions: existsSync(local)
      ? {
          executablePath: local,
          args: chromium.args.filter(
            (arg) =>
              ![
                "--single-process",
                "--disable-web-security",
                "--allow-running-insecure-content",
              ].includes(arg),
          ),
        }
      : {},
  },
  reporter: "list",
});
