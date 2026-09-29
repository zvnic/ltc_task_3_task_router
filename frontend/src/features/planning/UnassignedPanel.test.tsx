import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type {
  EngineerRoute,
  Plan,
  PlanMetrics,
  RequestPlacement,
  ServiceRequest,
  UnassignedRequest,
} from '../../shared/types'
import { PlacementOptions, UnassignedPanel } from './UnassignedPanel'

const DAY = '2026-08-17'

function request(
  id: string,
  overrides: Partial<ServiceRequest> & Pick<ServiceRequest, 'window_end'>,
): ServiceRequest {
  return {
    id,
    external_id: id.toUpperCase(),
    input_order: 1,
    address: `Москва, адрес ${id}`,
    district: 'ЮВАО',
    coordinates: { latitude: 55.7, longitude: 37.7 },
    coordinate_source: 'synthetic',
    duration_minutes: 60,
    window_start: `${DAY}T09:00:00+03:00`,
    required_skill: 'local',
    required_transport: null,
    priority: 'normal',
    status: 'new',
    source_fields: {},
    ...overrides,
  }
}

function unassigned(item: ServiceRequest): UnassignedRequest {
  return {
    request_id: item.id,
    external_id: item.external_id,
    priority: item.priority,
    reason_code: 'schedule_conflict',
    explanation: 'Подходящие инженеры есть, но визит конфликтует с их расписанием.',
    details: {},
  }
}

const METRICS: PlanMetrics = {
  assigned_count: 0,
  unassigned_count: 0,
  urgent_unassigned_count: 0,
  normal_unassigned_count: 0,
  emergency_unassigned_count: 0,
  connection_unassigned_count: 0,
  routine_unassigned_count: 0,
  used_engineers_count: 0,
  total_distance_meters: 0,
  total_travel_minutes: 0,
}

function plan(
  items: UnassignedRequest[],
  routes: EngineerRoute[] = [],
  metrics: PlanMetrics = METRICS,
): Plan {
  return {
    id: 'plan-1',
    dataset_id: 'dataset-1',
    input_revision: 3,
    parent_plan_id: null,
    baseline_plan_id: null,
    kind: 'optimized',
    created_at: `${DAY}T08:00:00+03:00`,
    model_info: {},
    result: {
      algorithm: 'ortools',
      solution_status: 'feasible',
      termination_reason: 'time_limit',
      elapsed_ms: 10,
      coordinate_quality: 'synthetic',
      routes,
      unassigned: items,
      metrics,
    },
    metrics,
  }
}

/** Маршрут с одним плечом на 38 мин при нормативе дороги 20. */
function routeWithExcess(): EngineerRoute {
  return {
    engineer_id: 'engineer-1',
    engineer_name: 'Бригада Мельников',
    transport: 'bicycle',
    start_location: { latitude: 55.69, longitude: 37.68 },
    departure_at: `${DAY}T09:00:00+03:00`,
    stops: [{
      request_id: 'stop-1',
      sequence: 1,
      arrival_at: `${DAY}T09:38:00+03:00`,
      service_start_at: `${DAY}T09:38:00+03:00`,
      service_end_at: `${DAY}T10:38:00+03:00`,
      departure_at: `${DAY}T10:38:00+03:00`,
      wait_minutes: 0,
      travel_minutes_from_previous: 38,
      distance_meters_from_previous: 7600,
      facts: { travel_norm_minutes: 20, travel_norm_exceeded: true, travel_norm_excess_minutes: 18 },
    }],
    distance_meters: 7600,
    travel_minutes: 38,
    service_minutes: 60,
    wait_minutes: 0,
    finish_at: `${DAY}T10:38:00+03:00`,
    explanation: '',
  }
}

const METRICS_WITH_EXCESS: PlanMetrics = {
  ...METRICS,
  assigned_count: 1,
  norm_violation_count: 1,
  norm_excess_minutes: 18,
}

function renderPanel(target: Plan, requests: ServiceRequest[], datasetRevision = 3) {
  const client = new QueryClient()
  return renderToStaticMarkup(
    <QueryClientProvider client={client}>
      <UnassignedPanel plan={target} requests={requests} datasetId="dataset-1" datasetRevision={datasetRevision} />
    </QueryClientProvider>,
  )
}

function orderOf(markup: string, externalIds: string[]) {
  return externalIds.map((id) => markup.indexOf(`data-unassigned-request="${id}"`))
}

describe('UnassignedPanel', () => {
  it('ставит аварию выше подключения и ремонта, а внутри класса — раньше закрывающееся окно', () => {
    // Ремонт закрывается раньше всех, но класс важнее окна.
    const repair = request('repair', { window_end: `${DAY}T10:00:00+03:00` })
    const connection = request('connection', {
      required_skill: 'connection',
      window_end: `${DAY}T11:00:00+03:00`,
    })
    const lateEmergency = request('emergency-late', {
      priority: 'urgent',
      required_skill: 'emergency',
      window_end: `${DAY}T18:00:00+03:00`,
    })
    const earlyEmergency = request('emergency-early', {
      priority: 'urgent',
      required_skill: 'emergency',
      window_end: `${DAY}T15:00:00+03:00`,
    })
    const requests = [repair, connection, lateEmergency, earlyEmergency]

    const markup = renderPanel(plan(requests.map(unassigned)), requests)

    const positions = orderOf(markup, ['EMERGENCY-EARLY', 'EMERGENCY-LATE', 'CONNECTION', 'REPAIR'])
    expect(positions.every((position) => position >= 0)).toBe(true)
    expect([...positions].sort((left, right) => left - right)).toEqual(positions)
    expect(markup).toContain('Аварии: 2')
    expect(markup).toContain('Что можно сделать')
    expect(markup).toContain('конфликтует с их расписанием')
  })

  it('показывает явное предупреждение только у варианта, нарушающего норматив дороги', () => {
    const route: EngineerRoute = {
      engineer_id: 'engineer-1',
      engineer_name: 'Бригада Мельников',
      transport: 'car',
      start_location: { latitude: 55.69, longitude: 37.68 },
      departure_at: `${DAY}T09:00:00+03:00`,
      stops: [1, 2].map((sequence) => ({
        request_id: `stop-${sequence}`,
        sequence,
        arrival_at: `${DAY}T10:00:00+03:00`,
        service_start_at: `${DAY}T10:00:00+03:00`,
        service_end_at: `${DAY}T11:00:00+03:00`,
        departure_at: `${DAY}T11:00:00+03:00`,
        wait_minutes: 0,
        travel_minutes_from_previous: 15,
        distance_meters_from_previous: 4000,
        facts: {},
      })),
      distance_meters: 8000,
      travel_minutes: 30,
      service_minutes: 120,
      wait_minutes: 0,
      finish_at: `${DAY}T13:00:00+03:00`,
      explanation: '',
    }
    const base: Omit<RequestPlacement, 'position' | 'norm_exceeded' | 'added_distance_meters'> = {
      engineer_id: 'engineer-1',
      engineer_name: 'Бригада Мельников',
      arrival_at: `${DAY}T11:20:00+03:00`,
      service_start_at: `${DAY}T11:30:00+03:00`,
      service_end_at: `${DAY}T12:30:00+03:00`,
      added_travel_minutes: 12,
    }
    const placements: RequestPlacement[] = [
      { ...base, position: 1, added_distance_meters: 2300, norm_exceeded: false },
      { ...base, position: 2, added_distance_meters: 5100, norm_exceeded: true },
    ]

    const markup = renderToStaticMarkup(
      <PlacementOptions
        placements={placements}
        externalId="REQ-1"
        routesByEngineer={new Map([[route.engineer_id, route]])}
        onChoose={() => undefined}
      />,
    )

    expect(markup.match(/Нарушает норматив дороги/g)).toHaveLength(1)
    const violating = markup.slice(markup.indexOf('data-placement-norm-exceeded="true"'))
    expect(violating).toContain('Нарушает норматив дороги')
    expect(violating).toContain('Назначить с превышением')
    expect(markup).toContain('между визитами 1 и 2')
    expect(markup).toContain('последним, после визита 2')
    expect(markup).toContain('+2,3 км')
    expect(markup).toContain('из них с превышением норматива дороги: 1')
  })

  it('без отказов показывает пустое состояние, а не пустую таблицу', () => {
    const markup = renderPanel(plan([]), [])

    expect(markup).toContain('Все заявки назначены')
    expect(markup).not.toContain('<table')
    expect(markup).not.toContain('<ol')
    // Превышений нет — и блока о них нет.
    expect(markup).not.toContain('data-norm-excess-note')
  })

  it('при превышениях норматива говорит, где они отмечены, и называет бригады', () => {
    const markup = renderPanel(plan([], [routeWithExcess()], METRICS_WITH_EXCESS), [])

    expect(markup).toContain('Все заявки назначены')
    // Прежняя неправда: в маршрутах бригад превышения никак не были видны.
    expect(markup).not.toContain('Они видны в маршрутах бригад')
    expect(markup).toContain('data-norm-excess-note')
    expect(markup).toContain('Превышений норматива: 1 (всего +18 мин)')
    expect(markup).toContain('в линейном дереве маршрута над картой')
    expect(markup).toContain('в столбце «Дорога» таблицы расписания')
    expect(markup).toContain('Бригада Мельников: 1 плечо, +18 мин')
  })

  it('показывает превышения норматива и рядом с открытыми отказами', () => {
    const open = request('open', { window_end: `${DAY}T12:00:00+03:00` })

    const markup = renderPanel(plan([unassigned(open)], [routeWithExcess()], METRICS_WITH_EXCESS), [open])

    expect(markup).toContain('data-unassigned-request="OPEN"')
    expect(markup).toContain('Превышений норматива: 1 (всего +18 мин)')
    expect(markup).toContain('Бригада Мельников: 1 плечо, +18 мин')
  })

  it('отменённую заявку уводит из открытых отказов и предупреждает об устаревшем плане', () => {
    const cancelled = request('cancelled', {
      status: 'cancelled',
      window_end: `${DAY}T10:00:00+03:00`,
    })
    const open = request('open', { window_end: `${DAY}T12:00:00+03:00` })

    const markup = renderPanel(plan([unassigned(cancelled), unassigned(open)]), [cancelled, open], 4)

    expect(markup).toContain('data-unassigned-request="OPEN"')
    expect(markup).not.toContain('data-unassigned-request="CANCELLED"')
    expect(markup).toContain('data-resolved-request="CANCELLED"')
    expect(markup).toContain('Отменена')
    expect(markup).toContain('набор уже на ревизии 4')
  })
})
