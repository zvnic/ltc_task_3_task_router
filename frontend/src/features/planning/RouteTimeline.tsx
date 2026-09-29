import type { EngineerRoute, PlanMetrics, RouteStop, ServiceRequest } from '../../shared/types'
import { Badge, Card, EmergencyIcon } from '../../shared/ui'
import {
  isEmergencyRequest,
  requestHighlightKind,
  requestWorkClass,
  transportLabel,
  WORK_CLASS_LABELS,
  workClassBadgeTone,
  type RequestHighlightKind,
  type WorkClass,
} from './request-kind'

type TimelineStopExtras = {
  workClass?: WorkClass
  highlight?: RequestHighlightKind
  reserveMinutes?: number
}

function routeTime(value: string) {
  return new Intl.DateTimeFormat('ru-RU', {
    hour: '2-digit',
    minute: '2-digit',
    timeZone: 'Europe/Moscow',
  }).format(new Date(value))
}

function stopNodeClass(highlight: RequestHighlightKind) {
  switch (highlight) {
    case 'emergency':
      return 'border-red-600 bg-red-600 text-white ring-4 ring-red-200'
    case 'changed':
      return 'border-amber-600 bg-amber-500 text-white ring-4 ring-amber-200'
    case 'protected':
      return 'border-violet-700 bg-violet-600 text-white ring-4 ring-violet-200'
    default:
      return 'border-violet-600 bg-white text-violet-700'
  }
}

function stopCardClass(highlight: RequestHighlightKind) {
  switch (highlight) {
    case 'emergency':
      return 'border-red-200 bg-red-50'
    case 'changed':
      return 'border-amber-200 bg-amber-50'
    case 'protected':
      return 'border-violet-200 bg-violet-50'
    default:
      return 'border-slate-100 bg-slate-50'
  }
}

function highlightBadge(highlight: RequestHighlightKind) {
  if (highlight === 'emergency') {
    return (
      <Badge tone="red">
        <span className="inline-flex items-center gap-1"><EmergencyIcon className="h-3 w-3" />Авария</span>
      </Badge>
    )
  }
  if (highlight === 'changed') return <Badge tone="amber">Изменено</Badge>
  if (highlight === 'protected') return <Badge tone="violet">Защищено</Badge>
  return null
}

type LegNormExcess = { travelMinutes: number; normMinutes: number; excessMinutes: number }

/**
 * Превышение норматива дороги на плече до визита; null — норматива нет или он соблюдён.
 * Формула та же, что у бэкенда (metrics.route_norm_excess): дорога строго дольше
 * норматива, превышение — их разность. Поэтому отметки на плечах сходятся с KPI плана.
 */
function legNormExcess(stop: RouteStop): LegNormExcess | null {
  const norm = stop.facts.travel_norm_minutes
  if (typeof norm !== 'number' || !Number.isFinite(norm)) return null
  const travel = stop.travel_minutes_from_previous
  if (travel <= norm) return null
  return { travelMinutes: travel, normMinutes: norm, excessMinutes: travel - norm }
}

/**
 * Подпись плеча длиннее норматива. В справочном режиме (advisory) норматив — время
 * слота в графике, а не предел поездки: подпись нейтральная, без «превышения».
 */
function legNormText(leg: LegNormExcess, advisory = false) {
  return advisory
    ? `Дорога ${leg.travelMinutes} мин — дольше норматива слота ${leg.normMinutes} мин на ${leg.excessMinutes} мин, норматив справочный`
    : `Дорога ${leg.travelMinutes} мин — норматив ${leg.normMinutes}, превышение +${leg.excessMinutes} мин`
}

function tripsWord(count: number) {
  const tens = count % 100
  const units = count % 10
  if (tens >= 11 && tens <= 14) return 'поездок'
  if (units === 1) return 'поездка'
  if (units >= 2 && units <= 4) return 'поездки'
  return 'поездок'
}

function routeNormExcess(route: EngineerRoute) {
  let count = 0
  let minutes = 0
  for (const stop of route.stops) {
    const leg = legNormExcess(stop)
    if (!leg) continue
    count += 1
    minutes += leg.excessMinutes
  }
  return { count, minutes }
}

function legsWord(count: number) {
  const tens = count % 100
  const units = count % 10
  if (tens >= 11 && tens <= 14) return 'плеч'
  if (units === 1) return 'плечо'
  if (units >= 2 && units <= 4) return 'плеча'
  return 'плеч'
}

/**
 * Подпись плеча сверх норматива. Смысл несут текст и aria-label, а не только красный
 * цвет и значок: «дорога 38 мин — норматив 20, превышение +18 мин».
 */
function LegNormCaption({
  leg,
  withLabel,
  advisory = false,
  detailClassName = '',
}: {
  leg: LegNormExcess
  withLabel: boolean
  advisory?: boolean
  detailClassName?: string
}) {
  return (
    <>
      <span className={`block ${advisory ? 'font-medium' : 'font-semibold'}`}>
        {advisory ? null : <span aria-hidden="true">⚠ </span>}
        {`${withLabel ? 'дорога ' : ''}${leg.travelMinutes} мин`}
      </span>
      <span className={`block ${detailClassName}`}>
        {advisory
          ? `норматив слота ${leg.normMinutes} · +${leg.excessMinutes}`
          : `норматив ${leg.normMinutes}, превышение +${leg.excessMinutes} мин`}
      </span>
    </>
  )
}

/** Время дороги до визита для таблицы расписания: плечо сверх норматива помечено подписью. */
export function TravelLegMinutes({ stop, advisory = false }: { stop: RouteStop; advisory?: boolean }) {
  const leg = legNormExcess(stop)
  if (!leg) return <>{`${stop.travel_minutes_from_previous} мин`}</>
  const text = legNormText(leg, advisory)
  return (
    <span
      role="note"
      aria-label={text}
      title={text}
      data-travel-norm-exceeded="true"
      className={`inline-block ${advisory ? 'text-slate-700' : 'text-red-800'}`}
    >
      <LegNormCaption
        leg={leg}
        withLabel={false}
        advisory={advisory}
        detailClassName={advisory ? 'text-xs text-muted' : 'text-xs'}
      />
    </span>
  )
}

/** Переход между узлами ленты маршрута: время дороги до следующего визита. */
function LegConnector({ stop, advisory = false }: { stop: RouteStop | null; advisory?: boolean }) {
  const leg = stop ? legNormExcess(stop) : null
  if (!stop || !leg) {
    return (
      <div className="mt-5 flex w-16 flex-col items-center">
        <div className="h-0.5 w-full bg-slate-300" />
        <div className="mt-1 text-[10px] text-muted">{stop ? `${stop.travel_minutes_from_previous} мин` : ''}</div>
      </div>
    )
  }
  const text = legNormText(leg, advisory)
  return (
    <div
      className="mt-5 flex w-32 flex-col items-center"
      role="note"
      aria-label={text}
      title={text}
      data-travel-norm-exceeded="true"
    >
      <div className={advisory ? 'h-0.5 w-full bg-slate-400' : 'h-1 w-full rounded-full bg-red-500'} />
      <div className={`mt-1 px-1 text-center text-[10px] leading-tight ${advisory ? 'text-slate-600' : 'text-red-800'}`}>
        <LegNormCaption leg={leg} withLabel advisory={advisory} />
      </div>
    </div>
  )
}

/** «Превышений норматива: N (всего +M мин)» по метрикам плана; при N = 0 ничего не выводит. */
export function NormExcessTotal({
  metrics,
  className = 'text-xs',
  advisory = false,
}: {
  metrics?: PlanMetrics
  className?: string
  advisory?: boolean
}) {
  const count = metrics?.norm_violation_count ?? 0
  if (count <= 0) return null
  // У планов, посчитанных до появления norm_excess_minutes, бэкенд отдаёт 0: сумму не
  // выдумываем и показываем только число превышений.
  const minutes = metrics?.norm_excess_minutes ?? 0
  if (advisory) {
    return (
      <p className={`text-slate-600 ${className}`} data-norm-excess-total={count}>
        {`Дольше норматива слота: ${count} ${tripsWord(count)}${minutes > 0 ? `, +${minutes} мин` : ''} — справочно`}
      </p>
    )
  }
  return (
    <p
      className={`flex items-start gap-1 font-semibold text-red-800 ${className}`}
      data-norm-excess-total={count}
    >
      <span aria-hidden="true">⚠</span>
      <span>{`Превышений норматива: ${count}${minutes > 0 ? ` (всего +${minutes} мин)` : ''}`}</span>
    </p>
  )
}

/** Бригады, у которых есть плечи сверх норматива: сначала с наибольшим превышением. */
export function NormExcessRoutes({ routes }: { routes: EngineerRoute[] }) {
  const rows = routes
    .map((route) => ({ route, ...routeNormExcess(route) }))
    .filter((row) => row.count > 0)
    .sort(
      (left, right) =>
        right.minutes - left.minutes ||
        left.route.engineer_name.localeCompare(right.route.engineer_name, 'ru'),
    )
  if (!rows.length) return null
  return (
    <ul className="mt-1 flex flex-wrap gap-x-4 gap-y-1" aria-label="Бригады с превышением норматива дороги">
      {rows.map(({ route, count, minutes }) => (
        <li key={route.engineer_id} data-norm-excess-route={route.engineer_id}>
          {`${route.engineer_name}: ${count} ${legsWord(count)}, +${minutes} мин`}
        </li>
      ))}
    </ul>
  )
}


function StopMetaBadges({ node }: { node: TimelineStopExtras }) {
  const showWorkClass = Boolean(
    node.workClass && !(node.workClass === 'emergency' && node.highlight === 'emergency'),
  )
  if (!showWorkClass && !node.reserveMinutes) return null
  return (
    <div className="mt-1 flex flex-wrap gap-1">
      {node.workClass && showWorkClass ? (
        <Badge tone={workClassBadgeTone(node.workClass)}>{WORK_CLASS_LABELS[node.workClass]}</Badge>
      ) : null}
      {node.reserveMinutes ? <Badge tone="amber">Резерв {node.reserveMinutes} мин</Badge> : null}
    </div>
  )
}

export function RouteTimeline({
  route,
  requests,
  officeAddress,
  protectedRequestIds = [],
  changedRequestIds = [],
  normAdvisory = false,
}: {
  route?: EngineerRoute
  requests: ServiceRequest[]
  officeAddress?: string
  protectedRequestIds?: string[]
  changedRequestIds?: string[]
  // Норматив дороги справочный: плечи длиннее него показываются нейтрально.
  normAdvisory?: boolean
}) {
  if (!route?.stops.length) return null
  const requestById = new Map(requests.map((request) => [request.id, request]))
  const protectedSet = new Set(protectedRequestIds)
  const changedSet = new Set(changedRequestIds)
  const lastStop = route.stops.at(-1)!
  // Фактический выезд — к первому визиту, а не начало смены: бригада не ждёт у двери.
  const departureAt = route.stops[0]?.departure_at ?? route.departure_at
  const nodes = [
    {
      id: 'start',
      kind: 'start' as const,
      number: '0',
      title: 'Офис зоны',
      address: officeAddress ?? 'Стартовая точка инженера',
      primaryTime: routeTime(departureAt),
      secondaryTime: 'Выезд',
      highlight: 'normal' as RequestHighlightKind,
    },
    ...route.stops.map((stop) => {
      const request = requestById.get(stop.request_id)
      const highlight = request
        ? requestHighlightKind({ request, protectedIds: protectedSet, changedIds: changedSet, requestId: stop.request_id })
        : protectedSet.has(stop.request_id)
          ? 'protected'
          : changedSet.has(stop.request_id)
            ? 'changed'
            : 'normal'
      return {
        id: stop.request_id,
        kind: 'stop' as const,
        number: String(stop.sequence),
        title: request ? `Заявка ${request.external_id}` : `Визит ${stop.sequence}`,
        address: request?.address ?? 'Адрес из снимка плана',
        primaryTime: routeTime(stop.arrival_at),
        secondaryTime: stop.reserve_minutes && stop.expected_service_end_at
          ? `ожидаемо ${routeTime(stop.service_start_at)}–${routeTime(stop.expected_service_end_at)} · слот до ${routeTime(stop.service_end_at)}`
          : `${routeTime(stop.service_start_at)}–${routeTime(stop.service_end_at)}`,
        stop,
        reserveMinutes: stop.reserve_minutes ?? 0,
        highlight,
        emergency: request ? isEmergencyRequest(request) : false,
        workClass: request ? requestWorkClass(request) : undefined,
      }
    }),
    {
      id: 'finish',
      kind: 'finish' as const,
      number: '✓',
      title: 'Финиш маршрута',
      address: requestById.get(lastStop.request_id)?.address ?? 'Последняя заявка',
      primaryTime: routeTime(route.finish_at),
      secondaryTime: 'Без возврата в офис',
      highlight: 'normal' as RequestHighlightKind,
    },
  ]
  // Плечо, которое ведёт в узел: у визита оно есть, у финиша — нет.
  const legInto = (index: number) => {
    const nextNode = nodes[index + 1]
    return nextNode?.kind === 'stop' ? nextNode.stop : null
  }
  const showLegend = nodes.some((node) => node.kind === 'stop' && node.highlight !== 'normal')
  const hasReserve = route.stops.some((stop) => (stop.reserve_minutes ?? 0) > 0)
  const normExcess = routeNormExcess(route)
  return (
    <Card className="w-full min-w-0 max-w-full overflow-hidden" aria-label="Линейное выполнение маршрута" data-replan-timeline={showLegend || undefined}>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b px-4 py-3">
        <div>
          <h3 className="font-semibold">Маршрут по шагам</h3>
          <p className="text-xs text-muted">{route.engineer_name} · {transportLabel(route.transport)} · {routeTime(departureAt)}–{routeTime(route.finish_at)}</p>
          {normExcess.count > 0 && (normAdvisory ? (
            <p className="mt-1 text-xs text-slate-600" data-route-norm-excess={normExcess.count}>
              {`Дольше норматива слота: ${normExcess.count} ${tripsWord(normExcess.count)}, +${normExcess.minutes} мин — норматив справочный`}
            </p>
          ) : (
            <p className="mt-1 flex items-center gap-1 text-xs font-semibold text-red-800" data-route-norm-excess={normExcess.count}>
              <span aria-hidden="true">⚠</span>
              <span>{`Дорога сверх норматива: ${normExcess.count} ${legsWord(normExcess.count)}, всего +${normExcess.minutes} мин`}</span>
            </p>
          ))}
        </div>
        {(showLegend || hasReserve) && (
          <div className="flex flex-wrap gap-2 text-xs" aria-label="Легенда корректировки">
            <span className="inline-flex items-center gap-1 text-red-700"><EmergencyIcon className="h-3 w-3" />Авария</span>
            <span className="inline-flex items-center gap-1"><span className="h-2.5 w-2.5 rounded-full bg-amber-500" />Изменено</span>
            <span className="inline-flex items-center gap-1"><span className="h-2.5 w-2.5 rounded-full bg-violet-600" />Защищено</span>
            {hasReserve && <span className="inline-flex items-center gap-1"><span className="h-2.5 w-2.5 rounded-full bg-amber-300" />Защитный резерв</span>}
          </div>
        )}
      </div>
      <div className="overflow-x-auto px-4 py-4">
        <div className="flex w-max items-start">
          {nodes.map((node, index) => (
            <div key={node.id} className="flex items-start">
              <div className="w-44 text-center">
                <div className={`mx-auto grid h-10 w-10 place-items-center rounded-full border-2 text-sm font-semibold ${
                  node.kind === 'stop' ? stopNodeClass(node.highlight) : 'border-slate-300 bg-slate-100 text-slate-700'
                }`}>
                  {node.number}
                </div>
                <div className={`mt-2 rounded-lg border px-2 py-2 text-left ${
                  node.kind === 'stop' ? stopCardClass(node.highlight) : 'border-slate-100 bg-white'
                }`}>
                  <div className="flex flex-wrap items-center gap-1">
                    <div className="text-xs font-semibold text-ink">{node.title}</div>
                    {node.kind === 'stop' && highlightBadge(node.highlight)}
                  </div>
                  {node.kind === 'stop' ? (
                    <StopMetaBadges node={node as TimelineStopExtras} />
                  ) : null}
                  <div className="mt-1 line-clamp-2 text-[11px] text-muted">{node.address}</div>
                  <div className="mt-1 text-xs font-medium text-ink">{node.primaryTime}</div>
                  <div className="text-[11px] text-muted">{node.secondaryTime}</div>
                </div>
              </div>
              {index < nodes.length - 1 && <LegConnector stop={legInto(index)} advisory={normAdvisory} />}
            </div>
          ))}
        </div>
      </div>
    </Card>
  )
}
