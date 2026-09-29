import { describe, expect, it } from 'vitest'

import type { Algorithm, EngineerRoute, Plan, RouteStop, SelectionReason, ServiceRequest } from '../../shared/types'
import {
  planNormMode,
  planOrigin,
  plural,
  routeSummary,
  selectionDecisions,
  shiftLoadPercent,
  straightKm,
  terminationText,
  visitReasons,
} from './plan-explain'

const DAY = '2026-08-17'

const ALGORITHMS = [
  { id: 'baseline_v1', title: 'Последовательный baseline' },
  { id: 'ortools_gls_v1', title: 'OR-Tools Guided Local Search' },
  { id: 'insertion_ls_v1', title: 'Вставка и локальные переносы' },
] as Algorithm[]

const REQUEST: ServiceRequest = {
  id: 'request-1',
  external_id: '94447',
  input_order: 1,
  address: 'г. Москва, ул. Затонная, д. 11 к 3',
  district: 'Нагатинский затон',
  coordinates: { latitude: 55.68, longitude: 37.68 },
  coordinate_source: 'synthetic',
  duration_minutes: 70,
  window_start: `${DAY}T10:00:00+03:00`,
  window_end: `${DAY}T12:00:00+03:00`,
  required_skill: 'connection',
  required_transport: null,
  priority: 'normal',
  status: 'assigned',
  source_fields: {},
}

const STOP: RouteStop = {
  request_id: 'request-1',
  sequence: 1,
  arrival_at: `${DAY}T10:00:00+03:00`,
  service_start_at: `${DAY}T10:00:00+03:00`,
  expected_service_end_at: `${DAY}T11:00:00+03:00`,
  service_end_at: `${DAY}T11:10:00+03:00`,
  departure_at: `${DAY}T09:36:00+03:00`,
  wait_minutes: 0,
  travel_minutes_from_previous: 24,
  distance_meters_from_previous: 8300,
  reserve_minutes: 10,
  explanation_codes: ['skill_match', 'transport_match', 'time_window_feasible', 'travel_norm_exceeded'],
  facts: {
    required_skill: 'connection',
    travel_mode_from_previous: 'car',
    travel_norm_minutes: 20,
    travel_norm_exceeded: true,
    shift_end: `${DAY}T17:59:00+03:00`,
  },
}

const ROUTE: EngineerRoute = {
  engineer_id: 'engineer-1',
  engineer_name: 'Капитанчук Александр',
  transport: 'car',
  start_location: { latitude: 55.64, longitude: 37.6 },
  departure_at: `${DAY}T08:00:00+03:00`,
  stops: [STOP],
  distance_meters: 8300,
  travel_minutes: 24,
  service_minutes: 70,
  wait_minutes: 0,
  finish_at: `${DAY}T11:10:00+03:00`,
  explanation: '',
}

describe('почему выбран метод', () => {
  it('называет решающий показатель и значения обоих планов', () => {
    const selection: SelectionReason = {
      selected_algorithm: 'insertion_ls_v1',
      selected_tuple: [0, 0, 1, 12, 168_500],
      selected_plan_key: [0, 0, 1, 0, 12, 168_500],
      plan_key_criteria: ['emergency_unassigned', 'connection_unassigned', 'routine_unassigned', 'norm_excess_minutes', 'used_engineers', 'distance_meters'],
      competitors: [
        { algorithm_id: 'ortools_gls_v1', objective_tuple: [0, 1, 0, 11, 123_100], plan_key: [0, 1, 0, 0, 11, 123_100], decisive_metric: 'connection_unassigned' },
        { algorithm_id: 'baseline_v1', objective_tuple: [1, 15, 20, 12, 108_000], plan_key: [1, 15, 20, 0, 12, 108_000], decisive_metric: 'emergency_unassigned' },
      ],
    }

    expect(selectionDecisions(selection, ALGORITHMS)).toEqual([
      { competitor: 'OR-Tools Guided Local Search', text: 'подключений без бригады: 0 против 1' },
      { competitor: 'Последовательный baseline', text: 'аварий без бригады: 0 против 1' },
    ])
  })

  it('пробег показывает в километрах, ручное назначение — как решение диспетчера', () => {
    const byDistance: SelectionReason = {
      selected_algorithm: 'ortools_gls_v1',
      selected_tuple: [],
      selected_plan_key: [0, 0, 0, 0, 8, 174_400],
      competitors: [{ algorithm_id: 'insertion_ls_v1', objective_tuple: [], plan_key: [0, 0, 0, 0, 8, 200_600], decisive_metric: 'distance_meters' }],
    }
    expect(selectionDecisions(byDistance, ALGORITHMS)[0]?.text).toBe('расчётный пробег: 174,4 км против 200,6 км')

    const byTravel: SelectionReason = {
      selected_algorithm: 'ortools_gls_v1',
      selected_tuple: [],
      selected_plan_key: [0, 0, 0, 0, 8, 1_210, 180_000],
      competitors: [{ algorithm_id: 'insertion_ls_v1', objective_tuple: [], plan_key: [0, 0, 0, 0, 8, 1_305, 174_400], decisive_metric: 'travel_minutes' }],
    }
    expect(selectionDecisions(byTravel, ALGORITHMS)[0]?.text).toBe('минут в пути: 1 210 против 1 305')

    const manual: SelectionReason = { ...byDistance, decided_by: 'dispatcher', competitors: [{ algorithm_id: 'ortools_gls_v1', objective_tuple: [], decisive_metric: 'manual_assignment' }] }
    expect(selectionDecisions(manual, ALGORITHMS)[0]?.text).toBe('заявку назначил диспетчер вручную')
  })
})

describe('почему визит у этой бригады', () => {
  it('перечисляет навык, окно, поздний выезд, дорогу и резерв', () => {
    const reasons = visitReasons({ stop: STOP, request: REQUEST, route: ROUTE, normMode: 'advisory' })
    const byLabel = Object.fromEntries(reasons.map((reason) => [reason.label, reason.text]))

    expect(byLabel['Навык']).toBe('нужен навык «подключения» — у бригады он есть')
    expect(byLabel['Окно клиента']).toBe('10:00–12:00, начало работ 10:00 — в окне')
    expect(byLabel['Выезд']).toBe('в 09:36 — в последний момент, чтобы не ждать у двери')
    expect(byLabel['Дорога']).toContain('24 мин, 8,3 км (автомобиль)')
    expect(byLabel['Норматив']).toBe('дольше норматива слота 20 мин на 4 мин — норматив справочный и план не ограничивает')
    expect(byLabel['Работы']).toBe('10:00–11:00, в графике до 11:10: 10 мин резерва на задержку')
    expect(byLabel['Смена']).toBe('работы кончаются до конца смены в 17:59')
  })

  it('в режиме soft называет превышение штрафуемым, защищённый визит — защищённым', () => {
    const reasons = visitReasons({
      stop: STOP,
      request: REQUEST,
      route: ROUTE,
      normMode: 'soft',
      protectedIds: new Set(['request-1']),
    })
    const byLabel = Object.fromEntries(reasons.map((reason) => [reason.label, reason.text]))

    expect(byLabel['Норматив']).toContain('штрафуется')
    expect(byLabel['Защита']).toContain('пересчёт его не трогает')
  })
})

describe('сводка и загрузка маршрута', () => {
  it('выезд берёт у первого визита, а не из начала смены', () => {
    expect(routeSummary(ROUTE)).toMatchObject({ visits: 1, departureAt: STOP.departure_at, travelMinutes: 24, serviceMinutes: 70 })
  })

  it('загрузка — работа и дорога от длины смены', () => {
    expect(shiftLoadPercent(ROUTE, { shift_start: `${DAY}T08:00:00+03:00`, shift_end: `${DAY}T18:00:00+03:00` })).toBe(16)
    expect(shiftLoadPercent(ROUTE)).toBeNull()
  })
})

describe('паспорт плана', () => {
  it('план без режима норматива в паспорте считался в soft', () => {
    expect(planNormMode({ model_info: {} } as Plan)).toBe('soft')
    expect(planNormMode({ model_info: { experiment: { travel_norm_mode: 'advisory' } } } as unknown as Plan)).toBe('advisory')
  })

  it('различает расчёт методами, вставку обычной заявки и ручное назначение', () => {
    expect(planOrigin({ kind: 'optimized', model_info: {} } as Plan)).toBe('solvers')
    expect(planOrigin({ kind: 'replan_optimized', model_info: { event_kind: 'urgent' } } as unknown as Plan)).toBe('solvers')
    expect(planOrigin({ kind: 'replan_optimized', model_info: { event_kind: 'normal' } } as unknown as Plan)).toBe('gap_insertion')
    expect(planOrigin({ kind: 'manual_insert', model_info: {} } as Plan)).toBe('manual')
    expect(terminationText('normal_request_inserted_without_reordering')).toBe('обычная заявка вставлена в свободный интервал без перестановки визитов')
  })

  it('причину остановки поиска переводит, неизвестную оставляет как есть', () => {
    expect(terminationText('time_limit_or_local_optimum')).toBe('поиск остановлен по лимиту времени или в локальном оптимуме')
    expect(terminationText('custom_reason')).toBe('custom_reason')
  })

  it('короткое плечо показывает в метрах', () => {
    const short = { ...STOP, sequence: 2, travel_minutes_from_previous: 3, distance_meters_from_previous: 140, facts: { ...STOP.facts, travel_mode_reason: 'short_car_leg_walk' } }
    const text = visitReasons({ stop: short, request: REQUEST, route: ROUTE, normMode: 'advisory' }).find((reason) => reason.label === 'Дорога')?.text
    expect(text).toContain('3 мин, 140 м (пешком от места парковки)')
  })

  it('пешее плечо бригады на общественном транспорте объясняет выбором быстрого', () => {
    const walk = { ...STOP, sequence: 2, travel_minutes_from_previous: 14, distance_meters_from_previous: 920, facts: { ...STOP.facts, travel_mode_reason: 'short_transit_leg_walk' } }
    const text = visitReasons({ stop: walk, request: REQUEST, route: ROUTE, normMode: 'advisory' }).find((reason) => reason.label === 'Дорога')?.text
    expect(text).toContain('14 мин, 920 м (пешком — не дольше, чем на транспорте)')
  })

  it('склоняет существительное по числу', () => {
    expect(plural(71, ['заявка', 'заявки', 'заявок'])).toBe('71 заявка')
    expect(plural(83, ['заявка', 'заявки', 'заявок'])).toBe('83 заявки')
    expect(plural(66, ['заявка', 'заявки', 'заявок'])).toBe('66 заявок')
    expect(plural(12, ['бригада', 'бригады', 'бригад'])).toBe('12 бригад')
    expect(plural(1, ['визит', 'визита', 'визитов'])).toBe('1 визит')
  })

  it('расстояние по прямой считает по гаверсинусу', () => {
    expect(straightKm({ latitude: 55.75, longitude: 37.62 }, { latitude: 55.75, longitude: 37.62 })).toBe(0)
    expect(straightKm({ latitude: 55.7048, longitude: 37.7662 }, { latitude: 55.7105, longitude: 37.7705 })).toBeCloseTo(0.69, 1)
  })
})
