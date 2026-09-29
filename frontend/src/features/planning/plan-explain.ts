import type {
  Algorithm,
  EngineerRoute,
  Plan,
  RouteStop,
  SelectionReason,
  ServiceRequest,
  TravelNormMode,
} from '../../shared/types'
import { transportPresentation } from '../../shared/transport'

/**
 * Объяснения расчёта человеческим языком. Всё, что здесь выводится, взято из паспорта
 * плана и фактов остановок, которые пишет бэкенд: интерфейс ничего не додумывает.
 */

export function clock(value: string) {
  return new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Moscow' }).format(new Date(value))
}

/** Расстояние по прямой, км — для подсказок интерфейса, не для расчёта маршрута. */
export function straightKm(
  origin: { latitude: number; longitude: number },
  destination: { latitude: number; longitude: number },
) {
  const radians = (value: number) => (value * Math.PI) / 180
  const dLat = radians(destination.latitude - origin.latitude)
  const dLon = radians(destination.longitude - origin.longitude)
  const a = Math.sin(dLat / 2) ** 2
    + Math.cos(radians(origin.latitude)) * Math.cos(radians(destination.latitude)) * Math.sin(dLon / 2) ** 2
  return 2 * 6371 * Math.asin(Math.sqrt(a))
}

/** Число со словом в нужной форме: plural(71, ['заявка', 'заявки', 'заявок']) → «71 заявка». */
export function plural(count: number, [one, few, many]: [string, string, string]) {
  const tens = count % 100
  const units = count % 10
  const word = tens >= 11 && tens <= 14 ? many : units === 1 ? one : units >= 2 && units <= 4 ? few : many
  return `${count} ${word}`
}

export function kmText(meters: number) {
  return `${(meters / 1000).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} км`
}

/** Короткое плечо — в метрах: «0 км» между соседними домами ничего не говорит. */
export function distanceText(meters: number) {
  return meters < 1000 ? `${meters.toLocaleString('ru-RU')} м` : kmText(meters)
}

export function minutesText(minutes: number) {
  if (minutes < 60) return `${minutes} мин`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? `${hours} ч ${rest} мин` : `${hours} ч`
}

/** Роль норматива дороги в этом плане. Планы до 28.09 паспорта режима не знали — это soft. */
export function planNormMode(plan?: Plan): TravelNormMode {
  const mode = plan?.model_info.experiment?.travel_norm_mode
  return mode === 'advisory' || mode === 'hard' ? mode : 'soft'
}

const TERMINATION_TEXT: Record<string, string> = {
  input_order_completed: 'заявки разобраны строго в порядке поступления',
  time_limit: 'поиск остановлен по лимиту времени',
  local_neighborhood_exhausted: 'перестановки больше не улучшают план',
  time_limit_or_local_optimum: 'поиск остановлен по лимиту времени или в локальном оптимуме',
  solver_no_solution: 'решатель не нашёл решения — взят базовый план',
  solver_schedule_rejected: 'расписание решателя не прошло проверку — взят базовый план',
  no_eligible_requests: 'нет заявок, которые может взять хоть одна бригада',
  empty_solver_input: 'нечего планировать',
  manual_request_inserted_without_reordering: 'заявка вставлена диспетчером без перестановки визитов',
  normal_request_inserted_without_reordering: 'обычная заявка вставлена в свободный интервал без перестановки визитов',
}

/**
 * Как получен план: расчётом методов (в том числе пересчётом остатка дня после
 * аварии), вставкой обычной заявки в свободный интервал или ручным назначением.
 */
export function planOrigin(plan: Plan): 'solvers' | 'gap_insertion' | 'manual' {
  if (plan.kind === 'manual_insert') return 'manual'
  if (plan.model_info.event_kind === 'normal') return 'gap_insertion'
  return 'solvers'
}

export function terminationText(code: string) {
  return TERMINATION_TEXT[code] ?? code
}

export function algorithmTitle(algorithmId: string | undefined, algorithms: Algorithm[]) {
  if (!algorithmId) return 'метод не указан'
  return algorithms.find((algorithm) => algorithm.id === algorithmId)?.title ?? algorithmId
}

// Ступени ключа сравнения планов — тот же порядок, что PLAN_KEY_LABELS на бэкенде.
export const PLAN_KEY_LABELS = [
  'emergency_unassigned',
  'connection_unassigned',
  'routine_unassigned',
  'norm_excess_minutes',
  'used_engineers',
  'travel_minutes',
  'distance_meters',
] as const

// Планы, посчитанные до ступени «время в пути», хранят ключ из шести ступеней.
const LEGACY_PLAN_KEY_LABELS = PLAN_KEY_LABELS.filter((label) => label !== 'travel_minutes')

const METRIC_NAME: Record<string, string> = {
  emergency_unassigned: 'аварий без бригады',
  connection_unassigned: 'подключений без бригады',
  routine_unassigned: 'ремонтов и дозаказов без бригады',
  norm_excess_minutes: 'минут дороги сверх норматива',
  used_engineers: 'задействованных бригад',
  travel_minutes: 'минут в пути',
  distance_meters: 'расчётный пробег',
}

function metricValue(metric: string, value: number) {
  return metric === 'distance_meters' ? kmText(value) : value.toLocaleString('ru-RU')
}

export type Decision = { competitor: string; text: string }

/**
 * Почему выбранный план лучше каждого конкурента: первая ступень ключа, где они
 * разошлись, и значения обоих планов на ней — «аварий без бригады: 0 против 1».
 */
export function selectionDecisions(selection: SelectionReason, algorithms: Algorithm[]): Decision[] {
  const criteria =
    selection.plan_key_criteria ??
    (selection.selected_plan_key?.length === LEGACY_PLAN_KEY_LABELS.length ? LEGACY_PLAN_KEY_LABELS : [...PLAN_KEY_LABELS])
  return selection.competitors.map((competitor) => {
    const name = algorithmTitle(competitor.algorithm_id, algorithms)
    const metric = competitor.decisive_metric
    if (selection.decided_by === 'dispatcher' || metric === 'manual_assignment') {
      return { competitor: name, text: 'заявку назначил диспетчер вручную' }
    }
    if (metric === 'tie') return { competitor: name, text: 'показатели равны — взят первый по порядку метод' }
    if (metric === 'cost') return { competitor: name, text: 'ниже условная стоимость плана' }
    const index = criteria.indexOf(metric)
    const own = index >= 0 ? selection.selected_plan_key?.[index] : undefined
    const other = index >= 0 ? competitor.plan_key?.[index] : undefined
    const label = METRIC_NAME[metric] ?? metric
    if (own === undefined || other === undefined) return { competitor: name, text: `решил показатель «${label}»` }
    return { competitor: name, text: `${label}: ${metricValue(metric, own)} против ${metricValue(metric, other)}` }
  })
}

const SKILL_TEXT: Record<string, string> = {
  local: 'локальные работы',
  connection: 'подключения',
  emergency: 'аварийные работы',
}

export type VisitReason = { label: string; text: string; tone?: 'ok' | 'info' | 'note' }

/**
 * Почему визит стоит у этой бригады и в это время — по кодам объяснения и фактам
 * остановки, которые бэкенд записал при расчёте и которые проверил валидатор.
 */
export function visitReasons(args: {
  stop: RouteStop
  request?: ServiceRequest
  route: EngineerRoute
  normMode: TravelNormMode
  protectedIds?: Set<string>
}): VisitReason[] {
  const { stop, request, route, normMode, protectedIds } = args
  const codes = new Set(stop.explanation_codes ?? [])
  const facts = stop.facts
  const reasons: VisitReason[] = []
  const skill = String(facts.required_skill ?? request?.required_skill ?? '')
  if (codes.has('skill_match') || codes.has('gap_insertion') || codes.has('manual_gap_insertion') || skill) {
    reasons.push({ label: 'Навык', text: `нужен навык «${SKILL_TEXT[skill] ?? skill}» — у бригады он есть`, tone: 'ok' })
  }
  const transport = transportPresentation(route.transport).label.toLocaleLowerCase('ru-RU')
  reasons.push({
    label: 'Транспорт',
    text: request?.required_transport
      ? `заявка требует: ${transportPresentation(request.required_transport).label.toLocaleLowerCase('ru-RU')} — совпадает`
      : `особых требований нет, бригада едет: ${transport}`,
    tone: 'ok',
  })
  if (request) {
    const waitText = stop.wait_minutes > 0 ? `; приезжает в ${clock(stop.arrival_at)} и ждёт ${stop.wait_minutes} мин` : ''
    reasons.push({
      label: 'Окно клиента',
      text: `${clock(request.window_start)}–${clock(request.window_end)}, начало работ ${clock(stop.service_start_at)} — в окне${waitText}`,
      tone: 'ok',
    })
  }
  if (stop.sequence === 1 && stop.departure_at !== route.departure_at) {
    reasons.push({ label: 'Выезд', text: `в ${clock(stop.departure_at)} — в последний момент, чтобы не ждать у двери`, tone: 'info' })
  }
  const legMode = String(facts.travel_mode_from_previous ?? route.transport)
  const legText = facts.travel_mode_reason === 'short_car_leg_walk'
    ? 'пешком от места парковки'
    : facts.travel_mode_reason === 'short_transit_leg_walk'
      ? 'пешком — не дольше, чем на транспорте'
      : transportPresentation(legMode).label.toLocaleLowerCase('ru-RU')
  reasons.push({
    label: 'Дорога',
    text: `${stop.travel_minutes_from_previous} мин, ${distanceText(stop.distance_meters_from_previous)} (${legText}); расчёт: расстояние по прямой × коэффициент пути, скорость режима`,
    tone: 'info',
  })
  const norm = facts.travel_norm_minutes
  if (typeof norm === 'number' && stop.travel_minutes_from_previous > norm) {
    const excess = stop.travel_minutes_from_previous - norm
    reasons.push({
      label: 'Норматив',
      text: normMode === 'advisory'
        ? `дольше норматива слота ${norm} мин на ${excess} мин — норматив справочный и план не ограничивает`
        : `дольше норматива ${norm} мин на ${excess} мин — в этом режиме превышение штрафуется`,
      tone: 'note',
    })
  }
  const reserve = stop.reserve_minutes ?? 0
  const expectedEnd = stop.expected_service_end_at ?? stop.service_end_at
  reasons.push({
    label: 'Работы',
    text: reserve > 0
      ? `${clock(stop.service_start_at)}–${clock(expectedEnd)}, в графике до ${clock(stop.service_end_at)}: ${reserve} мин резерва на задержку`
      : `${clock(stop.service_start_at)}–${clock(stop.service_end_at)}`,
    tone: 'ok',
  })
  if (typeof facts.completion_deadline === 'string') {
    reasons.push({ label: 'Срок аварии', text: `закончить до ${clock(facts.completion_deadline)} — 100 мин от поступления`, tone: 'ok' })
  }
  if (typeof facts.shift_end === 'string') {
    reasons.push({ label: 'Смена', text: `работы кончаются до конца смены в ${clock(facts.shift_end)}`, tone: 'ok' })
  }
  if (codes.has('manual_gap_insertion')) {
    reasons.push({ label: 'Решение', text: 'назначено диспетчером вручную в свободный интервал', tone: 'info' })
  } else if (codes.has('gap_insertion')) {
    reasons.push({ label: 'Решение', text: 'вставлено в свободный интервал; прежние визиты не сдвинуты', tone: 'info' })
  }
  if (protectedIds?.has(stop.request_id)) {
    reasons.push({ label: 'Защита', text: 'визит начат или выполнен до события — пересчёт его не трогает', tone: 'info' })
  }
  return reasons
}

/**
 * Фактический выезд бригады — к первому визиту. `route.departure_at` — момент, с которого
 * бригада свободна (начало смены): к первому визиту она выезжает в последний момент.
 */
export function routeDepartureAt(route: Pick<EngineerRoute, 'departure_at' | 'stops'>) {
  return route.stops[0]?.departure_at ?? route.departure_at
}

export type RouteSummary = {
  visits: number
  distanceMeters: number
  travelMinutes: number
  serviceMinutes: number
  waitMinutes: number
  departureAt: string
  finishAt: string
}

/** Сводка маршрута: визиты, пробег, дорога, работа, ожидание, фактический выезд и финиш. */
export function routeSummary(route: EngineerRoute): RouteSummary {
  return {
    visits: route.stops.length,
    distanceMeters: route.distance_meters,
    travelMinutes: route.travel_minutes,
    serviceMinutes: route.service_minutes,
    waitMinutes: route.wait_minutes,
    departureAt: routeDepartureAt(route),
    finishAt: route.finish_at,
  }
}

/** Доля смены, занятая работой и дорогой, в процентах; null — смена неизвестна. */
export function shiftLoadPercent(route: EngineerRoute, shift?: { shift_start: string; shift_end: string }) {
  if (!shift) return null
  const minutes = (new Date(shift.shift_end).getTime() - new Date(shift.shift_start).getTime()) / 60_000
  if (minutes <= 0) return null
  return Math.min(100, Math.round(((route.service_minutes + route.travel_minutes) / minutes) * 100))
}
