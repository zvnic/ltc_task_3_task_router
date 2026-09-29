import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { EngineerRoute, PlanMetrics, RouteStop, ServiceRequest } from '../../shared/types'
import { NormExcessRoutes, NormExcessTotal, RouteTimeline, TravelLegMinutes } from './RouteTimeline'

const DAY = '2026-08-17'

function makeStop(sequence: number, travel: number, norm: number | null): RouteStop {
  return {
    request_id: `request-${sequence}`,
    sequence,
    arrival_at: `${DAY}T1${sequence}:00:00+03:00`,
    service_start_at: `${DAY}T1${sequence}:00:00+03:00`,
    service_end_at: `${DAY}T1${sequence}:40:00+03:00`,
    departure_at: `${DAY}T1${sequence}:40:00+03:00`,
    wait_minutes: 0,
    travel_minutes_from_previous: travel,
    distance_meters_from_previous: travel * 300,
    facts: { travel_norm_minutes: norm, travel_norm_exceeded: norm !== null && travel > norm },
  }
}

function makeRoute(stops: RouteStop[], name = 'Бригада Мельников', id = 'engineer-1'): EngineerRoute {
  return {
    engineer_id: id,
    engineer_name: name,
    transport: 'car',
    start_location: { latitude: 55.69, longitude: 37.68 },
    departure_at: `${DAY}T09:30:00+03:00`,
    stops,
    distance_meters: 0,
    travel_minutes: stops.reduce((total, item) => total + item.travel_minutes_from_previous, 0),
    service_minutes: 40 * stops.length,
    wait_minutes: 0,
    finish_at: `${DAY}T18:00:00+03:00`,
    explanation: '',
  }
}

const METRICS: PlanMetrics = {
  assigned_count: 10,
  unassigned_count: 0,
  urgent_unassigned_count: 0,
  normal_unassigned_count: 0,
  emergency_unassigned_count: 0,
  connection_unassigned_count: 0,
  routine_unassigned_count: 0,
  used_engineers_count: 2,
  total_distance_meters: 12_000,
  total_travel_minutes: 90,
}

describe('RouteTimeline', () => {
  it('renders one emergency badge when highlight and work class mean the same thing', () => {
    const request: ServiceRequest = {
      id: 'request-1',
      external_id: 'URG-147883',
      input_order: 1,
      address: 'Москва, Волгоградский проспект, 128 к 5',
      district: 'ЮВАО',
      coordinates: { latitude: 55.7, longitude: 37.7 },
      coordinate_source: 'synthetic',
      duration_minutes: 80,
      window_start: '2026-08-17T14:00:00+03:00',
      window_end: '2026-08-17T15:00:00+03:00',
      required_skill: 'emergency',
      required_transport: null,
      priority: 'urgent',
      status: 'assigned',
      source_fields: {
        'Тип заявки': 'Авария',
        _enrichment: JSON.stringify({ work_class: 'emergency' }),
      },
    }
    const route: EngineerRoute = {
      engineer_id: 'engineer-1',
      engineer_name: 'Бригада Мельников',
      transport: 'public_transport',
      start_location: { latitude: 55.69, longitude: 37.68 },
      departure_at: '2026-08-17T13:30:00+03:00',
      stops: [{
        request_id: request.id,
        sequence: 1,
        arrival_at: '2026-08-17T14:00:00+03:00',
        service_start_at: '2026-08-17T14:00:00+03:00',
        expected_service_end_at: '2026-08-17T14:50:00+03:00',
        service_end_at: '2026-08-17T15:20:00+03:00',
        departure_at: '2026-08-17T13:30:00+03:00',
        wait_minutes: 0,
        travel_minutes_from_previous: 30,
        distance_meters_from_previous: 8000,
        reserve_minutes: 30,
        facts: {},
      }],
      distance_meters: 8000,
      travel_minutes: 30,
      service_minutes: 80,
      wait_minutes: 0,
      finish_at: '2026-08-17T15:20:00+03:00',
      explanation: '',
    }

    const markup = renderToStaticMarkup(
      <RouteTimeline route={route} requests={[request]} officeAddress="Офис зоны" />,
    )

    expect(markup.match(/>Авария</g)).toHaveLength(2)
    expect(markup).toContain('Легенда корректировки')
    expect(markup).toContain('Резерв 30 мин')
    expect(markup).toContain('слот до 15:20')
    // Норматива у визита нет — плечо без пометки, как раньше.
    expect(markup).toContain('>30 мин<')
    expect(markup).not.toContain('data-travel-norm-exceeded')
  })

  it('подписывает плечо сверх норматива текстом и aria-label, а не только цветом', () => {
    // Плечо 1 в нормативе, плечо 2 — 38 мин при нормативе 20, плечо 3 без норматива.
    const target = makeRoute([makeStop(1, 15, 20), makeStop(2, 38, 20), makeStop(3, 50, null)])

    const markup = renderToStaticMarkup(<RouteTimeline route={target} requests={[]} />)

    expect(markup.match(/data-travel-norm-exceeded="true"/g)).toHaveLength(1)
    expect(markup).toContain('role="note" aria-label="Дорога 38 мин — норматив 20, превышение +18 мин"')
    expect(markup).toContain('дорога 38 мин')
    expect(markup).toContain('норматив 20, превышение +18 мин')
    // Значок — только украшение: смысл несёт подпись.
    expect(markup).toContain('<span aria-hidden="true">⚠ </span>')
    // Плечо ровно в нормативе и плечо без норматива не помечены.
    expect(markup).toContain('>15 мин<')
    expect(markup).toContain('>50 мин<')
    expect(markup).toContain('data-route-norm-excess="1"')
    expect(markup).toContain('Дорога сверх норматива: 1 плечо, всего +18 мин')
  })

  it('не помечает плечо, равное нормативу: превышение — только строго дольше', () => {
    const markup = renderToStaticMarkup(<TravelLegMinutes stop={makeStop(1, 20, 20)} />)

    expect(markup).toBe('20 мин')
  })

  it('в таблице расписания даёт ту же подпись превышения, что и в дереве', () => {
    const markup = renderToStaticMarkup(<TravelLegMinutes stop={makeStop(4, 552, 20)} />)

    expect(markup).toContain('data-travel-norm-exceeded="true"')
    expect(markup).toContain('aria-label="Дорога 552 мин — норматив 20, превышение +532 мин"')
    expect(markup).toContain('552 мин')
    expect(markup).toContain('норматив 20, превышение +532 мин')
  })
})

describe('NormExcessTotal — строка KPI плана', () => {
  it('показывает число превышений и их сумму в минутах', () => {
    const markup = renderToStaticMarkup(
      <NormExcessTotal metrics={{ ...METRICS, norm_violation_count: 3, norm_excess_minutes: 45 }} />,
    )

    expect(markup).toContain('Превышений норматива: 3 (всего +45 мин)')
    expect(markup).toContain('data-norm-excess-total="3"')
  })

  it('без превышений ничего не выводит', () => {
    expect(renderToStaticMarkup(<NormExcessTotal metrics={METRICS} />)).toBe('')
    expect(renderToStaticMarkup(<NormExcessTotal metrics={{ ...METRICS, norm_violation_count: 0 }} />)).toBe('')
    expect(renderToStaticMarkup(<NormExcessTotal />)).toBe('')
  })

  it('у старого плана без суммы минут не выдумывает «+0 мин»', () => {
    const markup = renderToStaticMarkup(
      <NormExcessTotal metrics={{ ...METRICS, norm_violation_count: 2, norm_excess_minutes: 0 }} />,
    )

    expect(markup).toContain('Превышений норматива: 2<')
    expect(markup).not.toContain('всего')
  })
})

describe('справочный норматив (TRAVEL_NORM_MODE=advisory)', () => {
  it('в ленте показывает поездку длиннее норматива спокойно, без «превышения» и ⚠', () => {
    const target = makeRoute([makeStop(1, 15, 20), makeStop(2, 38, 20)])

    const markup = renderToStaticMarkup(<RouteTimeline route={target} requests={[]} normAdvisory />)

    expect(markup).toContain('data-travel-norm-exceeded="true"')
    expect(markup).toContain('aria-label="Дорога 38 мин — дольше норматива слота 20 мин на 18 мин, норматив справочный"')
    expect(markup).toContain('норматив слота 20 · +18')
    expect(markup).not.toContain('⚠')
    expect(markup).not.toContain('превышение')
    expect(markup).not.toContain('bg-red-500')
    expect(markup).toContain('Дольше норматива слота: 1 поездка, +18 мин — норматив справочный')
  })

  it('выезд в ленте — фактический выезд к первому визиту, а не начало смены', () => {
    const target = makeRoute([makeStop(1, 15, 20)])

    const markup = renderToStaticMarkup(<RouteTimeline route={target} requests={[]} />)

    // маршрут доступен с 09:30, но к первому визиту бригада выехала в 11:40
    expect(markup).toContain('Маршрут по шагам')
    expect(markup).toContain('11:40–18:00')
    expect(markup).not.toContain('09:30')
  })

  it('в строке KPI пишет «дольше норматива слота» нейтрально', () => {
    const markup = renderToStaticMarkup(
      <NormExcessTotal metrics={{ ...METRICS, norm_violation_count: 3, norm_excess_minutes: 45 }} advisory />,
    )

    expect(markup).toContain('Дольше норматива слота: 3 поездки, +45 мин — справочно')
    expect(markup).not.toContain('Превышений')
  })
})

describe('NormExcessRoutes', () => {
  it('перечисляет бригады с превышениями, сначала с наибольшим', () => {
    const routes = [
      makeRoute([makeStop(1, 25, 20)], 'Бригада Андреев', 'engineer-a'),
      makeRoute([makeStop(1, 10, 20)], 'Бригада Борисов', 'engineer-b'),
      makeRoute([makeStop(1, 60, 20), makeStop(2, 30, 20)], 'Бригада Волков', 'engineer-c'),
    ]

    const markup = renderToStaticMarkup(<NormExcessRoutes routes={routes} />)

    expect(markup).toContain('Бригада Волков: 2 плеча, +50 мин')
    expect(markup).toContain('Бригада Андреев: 1 плечо, +5 мин')
    expect(markup).not.toContain('Борисов')
    expect(markup.indexOf('Волков')).toBeLessThan(markup.indexOf('Андреев'))
  })
})
