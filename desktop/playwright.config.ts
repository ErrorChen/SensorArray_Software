import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.e2e.ts",
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 12_000 },
  retries: 0,
  outputDir: "../validation_artifacts/playwright-results",
  reporter: [
    ["line"],
    ["html", { outputFolder: "../validation_artifacts/playwright-report", open: "never" }]
  ],
  use: {
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure"
  }
});
