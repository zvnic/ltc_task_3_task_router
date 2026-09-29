import type { Engineer, EngineerRoute, ServiceRequest } from '../../shared/types'
import { isEmergencyRequest } from '../planning/request-kind'

export type GanttSegmentKind = 'travel' | 'wait' | 'service' | 'emergency' | 'free'

export type GanttSegment = {
  key: string
  kind: GanttSegmentKind
  start: string
  end: string
  title: string
  label?: string
}

export function dateValue(value: string) {
  return new Date(value).getTime()
}

export function time(value: string | undefined) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Moscow' }).format(new Date(value))
}

/** stop.departure_at в API — выезд *к* заявке, не *с* неё; после работ якорь — service_end_at. */
export function buildRouteGanttSegments(
  engineer: Engineer,
  route: EngineerRoute | undefined,
  requestById: Map<string, ServiceRequest>,
): GanttSegment[] {
  const segments: GanttSegment[] = []
  const push = (segment: GanttSegment) => {
    if (dateValue(segment.end) <= dateValue(segment.start)) return
    segments.push(segment)
  }

  if (!route || route.stops.length === 0) {
    push({
      key: `${engineer.id}-free-shift`,
      kind: 'free',
      start: engineer.shift_start,
      end: engineer.shift_end,
      title: `Свободное время смены: ${time(engineer.shift_start)}–${time(engineer.shift_end)}`,
    })
    return segments
  }

  let cursor = engineer.shift_start
  // route.departure_at — момент, когда бригада свободна (начало смены). К первому визиту
  // она выезжает в последний момент (stops[0].departure_at): время до выезда — свободное,
  // а не дорога.
  const firstLeaveAt = route.stops[0]?.departure_at ?? route.departure_at
  if (dateValue(firstLeaveAt) > dateValue(cursor)) {
    push({
      key: `${engineer.id}-free-start`,
      kind: 'free',
      start: cursor,
      end: firstLeaveAt,
      title: `Свободное время смены: ${time(cursor)}–${time(firstLeaveAt)}`,
    })
  }
  cursor = firstLeaveAt

  for (const stop of route.stops) {
    const request = requestById.get(stop.request_id)
    const externalId = request?.external_id ?? String(stop.sequence)
    const emergency = request ? isEmergencyRequest(request) : false

    push({
      key: `${stop.request_id}-travel`,
      kind: 'travel',
      start: cursor,
      end: stop.arrival_at,
      title: `Дорога к заявке ${externalId}: ${time(cursor)}–${time(stop.arrival_at)}, ${stop.travel_minutes_from_previous} мин`,
    })

    if (dateValue(stop.service_start_at) > dateValue(stop.arrival_at)) {
      push({
        key: `${stop.request_id}-wait`,
        kind: 'wait',
        start: stop.arrival_at,
        end: stop.service_start_at,
        title: `Ожидание: ${time(stop.arrival_at)}–${time(stop.service_start_at)}, ${stop.wait_minutes} мин`,
      })
    }

    push({
      key: `${stop.request_id}-service`,
      kind: emergency ? 'emergency' : 'service',
      start: stop.service_start_at,
      end: stop.service_end_at,
      title: `${emergency ? 'Авария' : 'Работа'} ${externalId}: ${time(stop.service_start_at)}–${time(stop.service_end_at)}${request?.address ? ` · ${request.address}` : ''}`,
      label: externalId,
    })

    cursor = stop.service_end_at
  }

  const finishAt = route.finish_at || cursor
  if (dateValue(engineer.shift_end) > dateValue(finishAt)) {
    push({
      key: `${engineer.id}-free-end`,
      kind: 'free',
      start: finishAt,
      end: engineer.shift_end,
      title: `Свободное время смены: ${time(finishAt)}–${time(engineer.shift_end)}`,
    })
  }

  return segments
}
