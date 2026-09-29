import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

// Password gate is optional (AUTH_ENABLED=false by default → public app).
async function signIn(page: Page): Promise<void> {
  const status = await page.request.get('/api/v1/auth/status')
  const body = status.ok() ? await status.json() : { authenticated: true, auth_enabled: false }
  if (!body.auth_enabled) {
    await page.goto('/planning')
    await page.waitForURL(/\/planning/, { timeout: 15_000 })
    return
  }
  const password = process.env.APP_PASSWORD
  if (!password) {
    throw new Error('Не задан APP_PASSWORD: e2e не может пройти вход при AUTH_ENABLED=true.')
  }
  await page.goto('/login')
  const passwordInput = page.getByLabel('Пароль')
  if (await passwordInput.isVisible()) {
    await passwordInput.fill(password)
    await page.getByRole('button', { name: 'Войти' }).click()
  }
  await page.waitForURL(/\/planning/, { timeout: 15_000 })
}

test.beforeEach(async ({ page }) => {
  const status = await page.request.get('/api/v1/auth/status')
  const body = status.ok() ? await status.json() : { authenticated: true, auth_enabled: false }
  if (!body.auth_enabled) {
    return
  }
  const password = process.env.APP_PASSWORD
  if (!password) throw new Error('APP_PASSWORD is required when AUTH_ENABLED=true')
  const response = await page.request.post('/api/v1/auth/login', { data: { password } })
  expect(response.ok()).toBe(true)
})

test('dispatcher can inspect a calculated route', async ({ page }, testInfo) => {
  test.setTimeout(120_000)
  const useLiveMapTiles = process.env.LIVE_MAP_TILES === '1'
  if (!useLiveMapTiles) await page.route('https://tiles.openfreemap.org/**', (route) => route.abort())
  await signIn(page)
  await page.goto('/planning')
  await expect(page.getByRole('heading', { name: 'Task Router Диспетчерская' })).toBeVisible()
  await expect(page.locator('link[rel="icon"]')).toHaveAttribute('href', '/favicon.png')
  const faviconResponse = await page.request.get('/favicon.png')
  expect(faviconResponse.status()).toBe(200)
  expect(faviconResponse.headers()['content-type']).toContain('image/png')
  if ((page.viewportSize()?.width ?? 0) >= 1024) {
    await expect(page.getByLabel('Разработано командой Штатный интеллект')).toBeVisible()
  }
  await expect(page.getByLabel('Метод расчёта').locator('option')).toHaveCount(3)
  await expect(page.getByLabel('Описание выбранного метода')).toContainText('OR-Tools Guided Local Search')
  await expect(page.getByLabel('Формула метода')).toContainText('min lex')
  await expect(page.getByRole('checkbox', { name: 'Сравнить все' })).toBeChecked()
  const calculate = page.getByRole('button', { name: 'Рассчитать план', exact: true })
  const schedule = page.getByRole('button', { name: 'Расписание', exact: true })
  await expect(schedule.or(calculate)).toBeVisible({ timeout: 15_000 })
  if (await calculate.isVisible()) await calculate.click()
  await expect(schedule).toBeVisible({ timeout: 45_000 })
  const routeMap = page.getByLabel('Карта расчётных маршрутов')
  await expect(routeMap).toHaveAttribute('data-map-ready', 'true', { timeout: 15_000 })
  await expect(routeMap).toHaveAttribute('data-routing-quality', 'exact', { timeout: 30_000 })
  await expect(routeMap).toHaveAttribute('data-direction-arrows', 'true')
  expect(Number(await routeMap.getAttribute('data-route-coordinate-count'))).toBeGreaterThan(1)
  const routeOverview = page.getByRole('button', { name: 'Весь маршрут', exact: true })
  await expect(routeOverview).toHaveAttribute('aria-pressed', 'true')
  const selectedTransport = await routeMap.getAttribute('data-selected-transport')
  expect(['car', 'public_transport', 'walking', 'bicycle']).toContain(selectedTransport)
  await expect(page.getByLabel('Легенда типов заявок')).toContainText('Авария')
  expect(Number(await page.locator('[data-emergency-count]').getAttribute('data-emergency-count'))).toBeGreaterThan(0)
  const mapLibreWorkerUrl = page.workers().map((worker) => worker.url()).find((url) => url.includes('maplibre-gl-worker'))
  expect(mapLibreWorkerUrl).toBeTruthy()
  const mapLibreWorkerResponse = await page.request.get(mapLibreWorkerUrl!)
  expect(mapLibreWorkerResponse.status()).toBe(200)
  if (useLiveMapTiles) {
    await expect(page.getByLabel('Расчётные маршруты на нейтральной подложке')).toBeHidden()
    await expect(page.getByLabel('Карта расчётных маршрутов')).toHaveAttribute('data-map-language', 'ru')
    await expect(page.getByLabel('Карта расчётных маршрутов')).toHaveAttribute('data-house-numbers', 'true')
    await expect(page.getByText(/Русские подписи · номера домов при приближении/).first()).toBeVisible()
    if (testInfo.project.name === 'desktop-1280') {
      const zoomIn = page.getByRole('button', { name: 'Приблизить' })
      await expect(zoomIn).toBeVisible()
      for (let step = 0; step < 6; step += 1) await zoomIn.click()
      await page.waitForTimeout(1_500)
      await page.screenshot({ path: '/artifacts/map-ru-house-numbers-1280.png', fullPage: true })
    }
  } else {
    const fallbackMap = page.getByLabel('Расчётные маршруты на нейтральной подложке')
    await expect(fallbackMap).toBeVisible()
    expect(await fallbackMap.locator('[data-route-sequence-marker]').count()).toBeGreaterThan(2)
    expect(await fallbackMap.locator(`[data-route-transport="${selectedTransport}"]`).count()).toBeGreaterThan(0)
  }
  const routePointSequence = page.getByLabel('Порядок точек маршрута')
  await expect(routePointSequence).toContainText('Старт маршрута')
  await expect(routePointSequence).toContainText('Финиш маршрута')
  expect(Number(await routePointSequence.getAttribute('data-route-point-count'))).toBeGreaterThan(2)
  const routeTimeline = page.getByLabel('Линейное выполнение маршрута')
  await expect(routeTimeline).toBeVisible()
  await expect(routeTimeline).toContainText('Офис зоны')
  await expect(routeTimeline).toContainText('г. Москва, ул Юных Ленинцев, д 83с 4')
  await expect(routeTimeline).toContainText('Финиш маршрута')
  await expect(routeTimeline).toContainText('Без возврата в офис')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1)).toBe(true)
  expect(await routeTimeline.evaluate((element) => element.getBoundingClientRect().right <= document.documentElement.clientWidth + 1)).toBe(true)
  expect(await page.getByLabel('Карта расчётных маршрутов').evaluate((element) => element.getBoundingClientRect().right <= document.documentElement.clientWidth + 1)).toBe(true)
  const timelineBox = await routeTimeline.boundingBox()
  const routeListBox = await page.getByLabel('Список маршрутов инженеров').boundingBox()
  const routeMapBox = await page.locator('[data-route-map]').boundingBox()
  const scheduleBox = await page.getByLabel('Расписание и сравнение маршрутов').boundingBox()
  expect(timelineBox).not.toBeNull()
  expect(routeListBox).not.toBeNull()
  expect(routeMapBox).not.toBeNull()
  expect(scheduleBox).not.toBeNull()
  expect(timelineBox!.x).toBeLessThanOrEqual(routeListBox!.x + 1)
  expect(timelineBox!.x + timelineBox!.width).toBeGreaterThanOrEqual(routeMapBox!.x + routeMapBox!.width - 1)
  // Карта и список маршрутов — сразу под сводкой плана, лента маршрута — под ними.
  expect(routeListBox!.y + routeListBox!.height).toBeLessThanOrEqual(timelineBox!.y + 1)
  expect(routeMapBox!.y + routeMapBox!.height).toBeLessThanOrEqual(timelineBox!.y + 1)
  expect(timelineBox!.y + timelineBox!.height).toBeLessThanOrEqual(scheduleBox!.y + 1)
  // Прозрачность расчёта: шаги построения плана и объяснение каждого визита.
  const howBuilt = page.getByLabel('Как построен план')
  await expect(howBuilt).toContainText('Проверка')
  // шаг выбора есть у любого плана: расчёт методами, вставка заявки или ручное назначение
  await expect(howBuilt).toContainText('Выбор')
  await page.getByRole('button', { name: 'Почему эта бригада' }).first().click()
  await expect(page.getByText(/Каждое из этих условий заново проверил валидатор/)).toBeVisible()
  await expect(page.getByLabel('Методы и модель расчёта')).toContainText('Как считается дорога')
  // модель v6 со справочником дорог: API отдаёт road_network, панель называет источник
  await expect(page.getByLabel('Методы и модель расчёта')).toContainText('по дорогам OpenStreetMap')
  await expect(page.getByLabel('Методы и модель расчёта')).toContainText('road_matrix_by_transport_v6')
  // модель v6 со справочником дорог: API отдаёт road_network, панель называет источник
  await expect(page.getByLabel('Методы и модель расчёта')).toContainText('по дорогам OpenStreetMap')
  await expect(page.getByLabel('Методы и модель расчёта')).toContainText('road_matrix_by_transport_v6')
  if (testInfo.project.name === 'desktop-1440') {
    expect(routeMapBox!.height).toBeGreaterThanOrEqual(670)
  }
  await expect(page.getByText(/\d+ визит/).first()).toBeVisible()
  if (testInfo.project.name === 'desktop-1440') {
    const routeList = page.getByLabel('Список маршрутов инженеров')
    let inspectedAlternativeRoadRoute = false
    for (const transport of ['walking', 'bicycle']) {
      const routeButton = routeList.locator(`[data-route-transport="${transport}"]`).first().locator('xpath=ancestor::button[1]')
      if ((await routeButton.count()) === 0) {
        continue
      }
      await routeButton.click()
      await expect(routeMap).toHaveAttribute('data-selected-transport', transport)
      await expect(routeMap).toHaveAttribute('data-routing-quality', 'exact', { timeout: 30_000 })
      await expect(routeMap).toHaveAttribute('data-direction-arrows', 'true')
      expect(Number(await routeMap.getAttribute('data-route-coordinate-count'))).toBeGreaterThan(1)
      await expect(page.getByRole('button', { name: 'Весь маршрут', exact: true })).toHaveAttribute('aria-pressed', 'true')
      await page.screenshot({ path: `/artifacts/route-${transport}-1440.png`, fullPage: true })
      inspectedAlternativeRoadRoute = true
    }
    expect(inspectedAlternativeRoadRoute).toBe(true)
    const transitButton = routeList.locator('[data-route-transport="public_transport"]').first().locator('xpath=ancestor::button[1]')
    await expect(transitButton).toBeVisible()
    await transitButton.click()
    await expect(routeMap).toHaveAttribute('data-routing-quality', 'external', { timeout: 30_000 })
    const transitWidget = page.getByLabel('Маршрут общественного транспорта')
    await expect(transitWidget).toBeVisible()
    await expect(transitWidget).toHaveAttribute('data-route-view', 'overview')
    await expect(page.getByRole('button', { name: 'Весь маршрут', exact: true })).toHaveAttribute('aria-pressed', 'true')
    const transitPointSequence = page.getByLabel('Порядок точек маршрута')
    await expect(transitPointSequence).toBeVisible()
    const transitPointCount = Number(await transitPointSequence.getAttribute('data-route-point-count'))
    expect(transitPointCount).toBeGreaterThan(2)
    await expect(transitPointSequence.locator('[data-route-point-kind="start"]')).toContainText('Старт маршрута')
    await expect(transitPointSequence.locator('[data-route-point-kind="finish"]')).toContainText('Финиш маршрута')
    await expect(page.getByLabel('Старт и финиш маршрута')).toContainText('0 · Старт')
    await expect(page.getByLabel('Старт и финиш маршрута')).toContainText('Финиш')
    expect(await transitPointSequence.locator('[data-route-leg]').count()).toBe(
      transitPointCount - 1,
    )
    const transitFrame = transitWidget.locator('iframe')
    await expect(transitFrame).toHaveAttribute('src', /rtt=mt/)
    await expect(transitFrame).toHaveAttribute('src', /mode=routes/)
    const overviewSource = await transitFrame.getAttribute('src')
    expect(new URL(overviewSource!).searchParams.get('rtext')?.split('~').length).toBeGreaterThan(2)
    expect((await transitFrame.boundingBox())?.width ?? 0).toBeGreaterThan(700)
    await expect(transitFrame).toHaveAttribute('data-widget-loaded', 'true', { timeout: 15_000 })
    await page.screenshot({ path: '/artifacts/route-public-transport-overview-1440.png', fullPage: true })
    const secondTransitLeg = page.getByRole('button', { name: '1 → 2', exact: true })
    await expect(secondTransitLeg).toBeVisible()
    await transitPointSequence.locator('[data-route-leg="1-2"]').click()
    await expect(secondTransitLeg).toHaveAttribute('aria-pressed', 'true')
    await expect(transitWidget).toHaveAttribute('data-route-view', 'leg')
    expect((await transitFrame.boundingBox())?.width ?? 0).toBeGreaterThan(700)
    const transitDetails = page.getByLabel('Детали выбранного переезда')
    await expect(transitDetails).toBeVisible()
    const transitDetailsBox = await transitDetails.boundingBox()
    expect(transitDetailsBox).not.toBeNull()
    expect(transitDetailsBox!.width).toBeLessThanOrEqual(380)
    await expect(transitDetails.locator('[data-selected-transit-point="origin"]')).toContainText('Промежуточная точка 1')
    await expect(transitDetails.locator('[data-selected-transit-point="destination"]')).toContainText(
      transitPointCount > 3 ? 'Промежуточная точка 2' : 'Финиш маршрута',
    )
    await expect(page.getByLabel('Соседние переезды маршрута ОТ')).toContainText('0 → 1')
    if (transitPointCount > 3) {
      await expect(page.getByLabel('Соседние переезды маршрута ОТ')).toContainText('2 → 3')
    }
    await expect(transitFrame).toHaveAttribute('data-widget-loaded', 'true', { timeout: 15_000 })
    await page.waitForTimeout(2_000)
    await page.screenshot({ path: '/artifacts/route-public-transport-1440.png', fullPage: true })
    const carButton = routeList.locator('[data-route-transport="car"]').first().locator('xpath=ancestor::button[1]')
    await carButton.click()
    await expect(routeMap).toHaveAttribute('data-routing-quality', 'exact', { timeout: 30_000 })
    await page.getByRole('button', { name: 'Пересчитать план' }).click()
    await expect(page.getByRole('button', { name: 'Пересчитать план' })).toBeEnabled({ timeout: 45_000 })
    await page.getByRole('button', { name: 'Сравнение' }).click()
    const comparisonTable = page.getByRole('table')
    await expect(comparisonTable.getByText('Последовательный baseline', { exact: true })).toBeVisible()
    await expect(comparisonTable.getByText('OR-Tools Guided Local Search', { exact: true })).toBeVisible()
    await expect(comparisonTable.getByText('Вставка и локальные переносы', { exact: true })).toBeVisible()
    await expect(page.getByText('Методы в этом расчёте: 3 из 3.')).toBeVisible()
    await expect(page.getByText('Охват заявок')).toBeVisible()
    await expect(page.getByText('Структура времени маршрутов')).toBeVisible()
    await expect(page.getByLabel('Сравнение охвата и расчётного пробега по методам')).toBeVisible()
    await expect(page.getByText(/Воспроизводимость:/)).toBeVisible()
    await page.getByRole('button', { name: 'Добавить заявку в течение дня' }).click()
    const incomingDialog = page.getByRole('dialog', { name: 'Новая заявка в течение дня' })
    await expect(incomingDialog).toBeVisible()
    await expect(incomingDialog.getByText(/Авария имеет максимальный приоритет/)).toBeVisible()
    await page.getByRole('button', { name: 'Добавить и пересчитать' }).click()
    await expect(incomingDialog).toBeHidden({ timeout: 45_000 })
    await page.getByRole('button', { name: 'Сравнение' }).click()
    await expect(page.getByText(/Инженер завершает текущий выезд/)).toBeVisible()
    await page.getByRole('button', { name: 'Добавить заявку в течение дня' }).click()
    await page.getByRole('button', { name: 'Обычная' }).click()
    await expect(incomingDialog.getByText(/только в свободный интервал/)).toBeVisible()
    await page.getByRole('button', { name: 'Добавить и пересчитать' }).click()
    await expect(incomingDialog).toBeHidden({ timeout: 45_000 })
    // Правило обычной вставки объясняет баннер корректировки: он виден всегда, в отличие
    // от вкладки «Сравнение», на которой проверка держалась раньше.
    await expect(
      page.locator('[data-replan-banner]').getByText(/минимальным дополнительным временем в пути/),
    ).toBeVisible()
    // Авария по адресу вне справочника зоны: координата — из поиска по адресу. Ответ
    // поиска подменён, чтобы e2e не зависел от публичного Nominatim. Метка прогона делает
    // адрес новым: заявка прошлого прогона остаётся в базе и попадает в справочник зоны.
    const runMark = `e2e-${Date.now().toString(36)}`
    const freeAddress = `Москва, Волгоградский проспект, 97к1 (${runMark})`
    await page.route(
      (url) => url.pathname === '/api/v1/geocode',
      (route) => route.fulfill({
        json: {
          query: freeAddress,
          provider: 'OpenStreetMap Nominatim',
          attribution: '© участники OpenStreetMap',
          items: [{
            address: '97 к1, Волгоградский проспект, район Кузьминки, Москва, Россия',
            district: 'Кузьминки',
            coordinates: { latitude: 55.707821, longitude: 37.751864 },
          }],
        },
      }),
    )
    await page.getByRole('button', { name: 'Добавить заявку в течение дня' }).click()
    await incomingDialog.getByLabel('Адрес', { exact: true }).fill(freeAddress)
    await expect(incomingDialog.getByText(/Координаты не заданы/)).toBeVisible()
    await expect(page.getByRole('button', { name: 'Добавить и пересчитать' })).toBeDisabled()
    await incomingDialog.getByRole('button', { name: 'Найти', exact: true }).click()
    await expect(incomingDialog.getByText(/Координата найдена по адресу в OpenStreetMap/)).toBeVisible()
    await expect(incomingDialog.getByLabel('Координаты (широта, долгота)')).toHaveValue('55.707821, 37.751864')
    await expect(incomingDialog.getByLabel('Район', { exact: true })).toHaveValue('Кузьминки')
    await incomingDialog.screenshot({ path: `/artifacts/incoming-free-address-${testInfo.project.name}.png` })
    await page.getByRole('button', { name: 'Добавить и пересчитать' }).click()
    await expect(incomingDialog).toBeHidden({ timeout: 45_000 })
    const datasets = await (await page.request.get('/api/v1/datasets')).json() as Array<{ id: string }>
    type CreatedRequest = { address: string; coordinate_source: string; coordinates: { latitude: number } }
    const created: CreatedRequest[] = []
    for (const item of datasets) {
      const response = await page.request.get(`/api/v1/datasets/${item.id}/requests?search=${encodeURIComponent(runMark)}`)
      const body = await response.json() as { items: CreatedRequest[] }
      created.push(...body.items.filter((request) => request.address === freeAddress))
    }
    expect(created.length).toBeGreaterThan(0)
    expect(created.at(-1)?.coordinate_source).toBe('geocoded')
    expect(created.at(-1)?.coordinates.latitude).toBeCloseTo(55.7078, 3)
  }
  await page.screenshot({ path: `/artifacts/${testInfo.project.name}.png`, fullPage: true })
})

test('dispatcher can switch independent service zones', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop-1440')
  await signIn(page)
  await page.goto('/planning')
  const zones = page.getByRole('group', { name: 'Зона обслуживания' })
  await expect(zones.getByRole('button')).toHaveCount(3)
  await zones.getByRole('button', { name: 'Юго-восток' }).click()
  await expect(zones.getByRole('button', { name: 'Юго-восток' })).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByRole('heading', { name: /^Юго-восток/ })).toBeVisible()
  await expect(page.getByText(/Дата 17\.08\.2026/)).toBeVisible()
  await zones.getByRole('button', { name: 'Югоцентр' }).click()
  await expect(zones.getByRole('button', { name: 'Югоцентр' })).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByRole('heading', { name: /^Югоцентр/ })).toBeVisible()
  await expect(page.getByText(/Дата 17\.08\.2026/)).toBeVisible()
})

test('engineer can open a dedicated route page', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop-1440')
  await signIn(page)
  await page.goto('/planning')
  const routeLink = page.getByRole('link', { name: /Открыть маршрут инженера/ }).first()
  await expect(routeLink).toBeVisible({ timeout: 15_000 })
  await routeLink.click()
  await expect(page.getByLabel('Маршрут конкретного инженера')).toBeVisible()
  await expect(page.getByRole('heading', { name: /Маршрут:/ })).toBeVisible()
  await expect(page.getByText('Загрузка смены')).toBeVisible()
  await expect(page.getByLabel('Линейное выполнение маршрута')).toBeVisible()
  await expect(page.getByLabel('Карта расчётных маршрутов')).toHaveAttribute('data-map-ready', 'true', { timeout: 15_000 })
  await expect(page.getByLabel('Карта расчётных маршрутов')).toHaveAttribute('data-routing-quality', 'exact', { timeout: 30_000 })
  expect(
    Number(await page.getByLabel('Карта расчётных маршрутов').getAttribute('data-route-coordinate-count')),
  ).toBeGreaterThan(1)
  await expect(page.getByText(/маршрут по OSM-сети/).first()).toBeVisible()
  await expect(page.getByLabel('Задания инженера')).toBeVisible()
  const mapBox = await page.locator('[data-route-map]').boundingBox()
  const assignmentsBox = await page.getByLabel('Задания инженера').boundingBox()
  expect(mapBox).not.toBeNull()
  expect(assignmentsBox).not.toBeNull()
  expect(mapBox!.y + mapBox!.height).toBeLessThanOrEqual(assignmentsBox!.y + 1)
  await page.screenshot({ path: '/artifacts/engineer-route-1440.png', fullPage: true })
})

test('dispatcher can retry exact geometry after a controlled timeout', async ({ page }, testInfo) => {
  test.setTimeout(90_000)
  test.skip(testInfo.project.name !== 'desktop-1440')
  await page.route('https://tiles.openfreemap.org/**', (route) => route.abort())
  let failNextGeometry = true
  await page.route('**/api/v1/plans/*/engineers/*/route', async (route) => {
    if (failNextGeometry) {
      failNextGeometry = false
      await route.fulfill({
        status: 504,
        contentType: 'application/json',
        body: JSON.stringify({
          code: 'routing_provider_timeout',
          message: 'Точный маршрут не получен за отведённое время. Повторите запрос.',
          details: {},
          request_id: 'e2e-routing-timeout',
        }),
      })
      return
    }
    await route.continue()
  })

  await page.goto('/planning')
  const routeMap = page.getByLabel('Карта расчётных маршрутов')
  await expect(routeMap).toHaveAttribute('data-routing-quality', 'unavailable', { timeout: 15_000 })
  await expect(page.getByRole('alert')).toContainText('Точный маршрут не получен за отведённое время')
  await page.getByRole('button', { name: 'Повторить' }).click()
  await expect(routeMap).toHaveAttribute('data-routing-quality', 'exact', { timeout: 45_000 })
})

test('duplicate CSV row is explained and can be skipped explicitly', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop-1440')
  await signIn(page)
  await page.goto('/data')

  const csv = [
    'Заявка;Тип заявки BK;Тип заявки HD;Начало;Окончание;Район;Адрес;Подключение;Гигабитное подключение',
    '1;Подключение;Тест;17.08.2026 10:00;17.08.2026 12:00;Кузьминки;Москва, ул. Тестовая, д. 1;;Нет',
    '1;Подключение;Тест;17.08.2026 12:00;17.08.2026 14:00;Кузьминки;Москва, ул. Тестовая, д. 2;;Нет',
  ].join('\n')
  await page.locator('input[type="file"]').setInputFiles({
    name: 'заявки-с-дублем.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from(csv, 'utf-8'),
  })
  await page.getByRole('button', { name: 'Проверить и импортировать' }).click()

  const alert = page.getByRole('alert')
  await expect(alert).toContainText('Строка 3: ID 1 уже встречался в строке 2.')
  await expect(alert).toContainText('Остальные 1 строк корректны')
  await expect(
    page.getByRole('button', { name: 'Пропустить дубликаты (1) и импортировать' }),
  ).toBeVisible()
  await page.screenshot({ path: '/artifacts/import-duplicate-warning-1440.png', fullPage: true })
})

test('dispatcher can inspect all routes on the analytics gantt', async ({ page }, testInfo) => {
  await signIn(page)
  await page.goto('/analytics')
  await expect(page.getByLabel('Аналитика маршрутов')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByLabel('Диаграмма Ганта маршрутов инженеров')).toBeVisible()
  const legend = page.getByLabel('Легенда диаграммы Ганта')
  await expect(legend).toContainText('Дорога')
  await expect(legend).toContainText('Ожидание')
  await expect(legend).toContainText('Работа')
  await expect(legend).toContainText('Авария')
  await expect(legend).toContainText('Свободное время смены')
  await expect(page.getByLabel('Сводная таблица маршрутов')).toBeVisible()
  expect(await page.locator('[data-gantt-engineer]').count()).toBeGreaterThan(5)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1)).toBe(true)
  const ganttScroll = page.locator('[data-gantt-scroll]')
  expect(await ganttScroll.evaluate((element) => element.scrollWidth >= element.clientWidth)).toBe(true)

  // Маршрут с заявками: сегменты хронологически слева направо, между работами — дорога.
  const busyTrack = page.locator('[data-gantt-track]').filter({ has: page.locator('[data-gantt-segment="travel"]') }).first()
  await expect(busyTrack).toBeVisible()
  const kinds = await busyTrack.locator('[data-gantt-segment]').evaluateAll((nodes) =>
    nodes.map((node) => node.getAttribute('data-gantt-segment') ?? ''),
  )
  expect(kinds.length).toBeGreaterThan(0)
  expect(kinds).toContain('travel')
  expect(kinds.some((kind) => kind === 'service' || kind === 'emergency')).toBeTruthy()

  const lefts = await busyTrack.locator('[data-gantt-segment]').evaluateAll((nodes) =>
    nodes.map((node) => Number.parseFloat((node as HTMLElement).style.left || '0')),
  )
  for (let index = 1; index < lefts.length; index += 1) {
    const currentLeft = lefts[index]
    const previousLeft = lefts[index - 1]
    expect(currentLeft).toBeDefined()
    expect(previousLeft).toBeDefined()
    expect(currentLeft!).toBeGreaterThanOrEqual(previousLeft! - 0.05)
  }
  for (let index = 0; index < kinds.length - 1; index += 1) {
    const current = kinds[index]
    const next = kinds[index + 1]
    if ((current === 'service' || current === 'emergency') && (next === 'service' || next === 'emergency')) {
      throw new Error('две работы подряд без дороги между точками')
    }
  }

  await expect(page.locator('[data-gantt-segment="free"]').first()).toBeVisible()
  await page.screenshot({ path: `/artifacts/analytics-${testInfo.project.name}.png`, fullPage: true })
})
