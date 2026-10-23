import { defineConfig } from "@playwright/test";

// Uses a preinstalled Chromium when PW_CHROMIUM_PATH is set (e.g. /opt/pw-browsers/chromium-1194/chrome-linux/chrome);
// otherwise run `npx playwright install chromium` first.
export default defineConfig({
  testDir: "./tests",
  timeout: 240_000,
  expect: { timeout: 60_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:5173",
    headless: true,
    viewport: { width: 1440, height: 900 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    launchOptions: process.env.PW_CHROMIUM_PATH ? { executablePath: process.env.PW_CHROMIUM_PATH } : {},
  },
});
