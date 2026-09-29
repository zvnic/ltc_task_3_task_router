import { describe, expect, it } from 'vitest'

import type { Engineer, EngineerRoute, RouteStop } from '../../shared/types'
import { buildRouteGanttSegments } from './gantt'

const DAY = '2026-09-28'
const at = (clock: string) => `${DAY}T${clock}:00+03:00`

const ENGINEER: Engineer = {
  id: 'engineer-1',
  external_id: 'E-1',
  input_order: 1,
  name: 'Бригада ОТ',
  start_location: { latitude: 55.7, longitude: 37.6 },
  shift_start: at('09:00'),
  shift_end: at('21:30'),
  skills: ['connection'],
  transport: 'public_transport',
  is_synthetic: false,
}

function makeStop(sequence: number, leave: string, arrive: string, start: string, end: string): RouteStop {
  return {
    request_id: `request-${sequence}`,
    sequence,
    arrival_at: at(arrive),
    service_start_at: at(start),
    service_end_at: at(end),
    departure_at: at(leave),
    wait_minutes: 0,
    travel_minutes_from_previous: 15,
    distance_meters_from_previous: 3_000,
    facts: {},
  }
}

function makeRoute(stops: RouteStop[]): EngineerRoute {
  return {
    engineer_id: ENGINEER.id,
    engineer_name: ENGINEER.name,
    transport: ENGINEER.transport,
    start_location: ENGINEER.start_location,
    // момент, когда бригада свободна, — начало смены, а не выезд к первому визиту
    departure_at: at('09:00'),
    stops,
    distance_meters: 3_000,
    travel_minutes: 15,
    service_minutes: 40,
    wait_minutes: 0,
    finish_at: stops.at(-1)?.service_end_at ?? at('09:00'),
    explanation: '',
  }
}

describe('buildRouteGanttSegments', () => {
  it('draws the time before a late first departure as free, not as travel', () => {
    const route = makeRoute([makeStop(1, '15:45', '16:00', '16:00', '16:40')])

    const segments = buildRouteGanttSegments(ENGINEER, route, new Map())

    expect(segments.map((segment) => [segment.kind, segment.start, segment.end])).toEqual([
      ['free', at('09:00'), at('15:45')],
      ['travel', at('15:45'), at('16:00')],
      ['service', at('16:00'), at('16:40')],
      ['free', at('16:40'), at('21:30')],
    ])
  })

  it('starts travel at the shift start when the crew leaves right away', () => {
    const route = makeRoute([makeStop(1, '09:00', '09:15', '09:15', '09:55')])

    const segments = buildRouteGanttSegments(ENGINEER, route, new Map())

    expect(segments[0]).toMatchObject({ kind: 'travel', start: at('09:00'), end: at('09:15') })
  })
})
