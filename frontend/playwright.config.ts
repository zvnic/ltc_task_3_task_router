import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests',
  timeout: 60_000,
  workers: 1,
  outputDir: '/artifacts/test-results',
  use: {
    baseURL: process.env.BASE_URL ?? 'http://localhost:8080',
    // Спека вводит APP_PASSWORD в форму входа, а trace пишет параметры действий и тела
    // запросов в /artifacts, примонтированный с хоста. Пока пароль идёт через UI, трассы не пишем.
    trace: 'off',
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH }
      : undefined,
  },
  reporter: [['list'], ['html', { outputFolder: '/artifacts/playwright-report', open: 'never' }]],
  projects: [
    { name: 'desktop-1440', use: { viewport: { width: 1440, height: 900 } } },
    { name: 'desktop-1280', use: { viewport: { width: 1280, height: 800 } } },
    { name: 'tablet-768', use: { viewport: { width: 768, height: 900 } } },
  ],
})
