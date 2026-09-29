import { chromium, firefox } from 'playwright'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import fs from 'node:fs'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '../..')
const artifactsDir = process.env.ARTIFACTS_DIR || path.join(root, 'artifacts')
const screenshotPath = path.join(artifactsDir, 'gantt-kapitanchuk-verify.png')

fs.mkdirSync(artifactsDir, { recursive: true })

const baseURL = process.env.BASE_URL || 'http://127.0.0.1:8080'

async function ensureFreshAnalytics(page) {
  await page.goto(`${baseURL}/analytics`, { waitUntil: 'networkidle' })
  const bodyText = await page.locator('body').innerText()
  const hasNewCopy = bodyText.includes('Каждый маршрут')
  const hasOldCopy = bodyText.includes('Расчётное расписание') && !hasNewCopy
  if (hasOldCopy || !hasNewCopy) {
    console.log('Old analytics copy detected or missing "Каждый маршрут"; hard-reloading with cache disabled')
    await page.goto(`${baseURL}/analytics`, { waitUntil: 'networkidle' })
    await page.reload({ waitUntil: 'networkidle' })
  }
}

async function selectYugocentr(page) {
  const button = page.getByRole('button', { name: /Югоцентр/i })
  if (await button.count()) {
    const pressed = await button.first().getAttribute('aria-pressed')
    if (pressed !== 'true') {
      await button.first().click()
      await page.waitForTimeout(500)
    }
    console.log('Selected Югоцентр')
  } else {
    // Dataset switcher may use different labels
    const any = page.locator('button', { hasText: /Югоцентр/i })
    if (await any.count()) {
      await any.first().click()
      console.log('Clicked Югоцентр (fallback locator)')
      await page.waitForTimeout(500)
    } else {
      console.log('Югоцентр button not found — continuing with current dataset')
    }
  }
}

async function launchBrowser() {
  const prefer = process.env.PW_BROWSER || 'auto'
  const tryLaunch = async (label, fn) => {
    try {
      const browser = await fn()
      console.log(`Launched browser: ${label}`)
      return browser
    } catch (err) {
      console.log(`Failed to launch ${label}: ${err.message.split('\n')[0]}`)
      return null
    }
  }
  const attempts = []
  if (prefer === 'chrome' || prefer === 'auto') {
    attempts.push(['chrome-channel', () => chromium.launch({ headless: true, channel: 'chrome' })])
  }
  if (prefer === 'chromium' || prefer === 'auto') {
    attempts.push(['chromium', () => chromium.launch({ headless: true })])
  }
  if (prefer === 'firefox' || prefer === 'auto') {
    attempts.push(['firefox', () => firefox.launch({ headless: true })])
  }
  for (const [label, fn] of attempts) {
    const browser = await tryLaunch(label, fn)
    if (browser) return browser
  }
  console.error('No browser could be launched')
  process.exit(1)
}

async function main() {
  const browser = await launchBrowser()
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
  })
  // Disable cache for hard reload path
  await context.route('**/*', async (route) => {
    const headers = {
      ...route.request().headers(),
      'Cache-Control': 'no-cache',
      Pragma: 'no-cache',
    }
    await route.continue({ headers })
  })

  const page = await context.newPage()
  page.setDefaultTimeout(60_000)

  await ensureFreshAnalytics(page)
  await selectYugocentr(page)

  // Wait for diagram / gantt
  const gantt = page.locator('[data-gantt-scroll], [data-gantt-track], [data-gantt-engineer]').first()
  await gantt.waitFor({ state: 'visible', timeout: 60_000 })
  // Give data time to settle
  await page.waitForTimeout(1500)

  const bodyText = await page.locator('body').innerText()
  if (!bodyText.includes('Каждый маршрут') && bodyText.includes('Расчётное расписание')) {
    console.log('Still old copy after first pass — second hard reload')
    await page.evaluate(() => location.reload(true))
    await page.waitForLoadState('networkidle')
    await selectYugocentr(page)
    await gantt.waitFor({ state: 'visible', timeout: 60_000 })
    await page.waitForTimeout(1500)
  }

  // Find engineer row containing Капитанчук
  const engineerRow = page.locator('[data-gantt-engineer]').filter({ hasText: 'Капитанчук' }).first()
  await engineerRow.waitFor({ state: 'visible', timeout: 30_000 })

  const segments = engineerRow.locator('[data-gantt-segment]')
  const count = await segments.count()
  const kinds = []
  const details = []

  for (let i = 0; i < count; i++) {
    const seg = segments.nth(i)
    const kind = await seg.getAttribute('data-gantt-segment')
    const style = await seg.getAttribute('style')
    // Parse left/width from inline style
    const leftMatch = style?.match(/left:\s*([^;]+)/)
    const widthMatch = style?.match(/width:\s*([^;]+)/)
    const left = leftMatch?.[1]?.trim() ?? null
    const width = widthMatch?.[1]?.trim() ?? null
    kinds.push(kind)
    details.push({ kind, left, width, style })
  }

  console.log('--- KAPITASCHUK KINDS ---')
  console.log(JSON.stringify(kinds))
  console.log('--- DETAILS ---')
  console.log(JSON.stringify(details, null, 2))

  // Assert not a single long travel then one service
  const isBadPattern =
    kinds.length <= 2 ||
    (kinds.length === 2 && kinds[0] === 'travel' && (kinds[1] === 'service' || kinds[1] === 'emergency')) ||
    (kinds.filter((k) => k === 'travel').length === 1 &&
      kinds.filter((k) => k === 'service' || k === 'emergency').length === 1 &&
      kinds.length <= 3)

  if (isBadPattern) {
    console.error('ASSERT FAIL: kinds look like single long travel + one service:', kinds)
  } else {
    console.log('ASSERT OK: multi-segment pattern present')
  }

  // Expect alternating travel/wait/service pattern ending possibly with free
  const hasTravel = kinds.includes('travel')
  const hasService = kinds.includes('service') || kinds.includes('emergency')
  if (!hasTravel || !hasService) {
    console.error('ASSERT FAIL: missing travel or service in kinds')
  }

  await page.screenshot({ path: screenshotPath, fullPage: true })
  console.log('SCREENSHOT', screenshotPath)

  await browser.close()

  // Always print kinds array as required
  console.log('KINDS_ARRAY', JSON.stringify(kinds))

  if (isBadPattern || !hasTravel || !hasService) {
    process.exitCode = 1
  }
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
