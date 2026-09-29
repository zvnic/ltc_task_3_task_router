import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useMemo } from 'react'

import { apiClient } from '../../shared/api'
import { useActiveDataset } from '../../shared/dataset-state'
import type { Engineer, EngineerRoute, Plan, ServiceRequest } from '../../shared/types'
import { Badge, Card, EmptyState } from '../../shared/ui'
import { terminationText } from '../planning/plan-explain'
import { isEmergencyRequest } from '../planning/request-kind'
import { buildRouteGanttSegments, dateValue, time } from './gantt'

const timelineColors = {
  travel: 'bg-sky-500',
  wait: 'bg-amber-400',
  service: 'bg-violet-600',
  emergency: 'bg-red-600',
  free: 'bg-slate-300',
}

type AnalyticsRow = { engineer: Engineer; route?: EngineerRoute }

function minutesBetween(start: string, end: string) {
  return Math.max(0, Math.round((dateValue(end) - dateValue(start)) / 60_000))
}

function fullDateTime(value: string) {
  return new Intl.DateTimeFormat('ru-RU', {
    day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: 'Europe/Moscow',
  }).format(new Date(value))
}

function km(meters: number | undefined) {
  return meters == null ? '—' : `${(meters / 1000).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} км`
}

function percent(value: number | null | undefined) {
  return value == null ? '—' : `${value.toLocaleString('ru-RU', { maximumFractionDigits: 1 })}%`
}

// Качество координат плана — худший источник среди его заявок.
function coordinateQualityText(value: string) {
  return ({ verified: 'подтверждённые', geocoded: 'по адресам (OpenStreetMap)', synthetic: 'есть демо-точки районов' } as Record<string, string>)[value] ?? value
}

function transportName(value: string) {
  return ({ car: 'Автомобиль', public_transport: 'Общественный транспорт', bicycle: 'Велосипед', walking: 'Пешком' } as Record<string, string>)[value] ?? value
}

function currentPlan(plans: Plan[] | undefined, currentPlanId: string | null | undefined) {
  return plans?.find((plan) => plan.id === currentPlanId)
    ?? plans?.find((plan) => ['optimized', 'baseline_fallback', 'replan_optimized', 'replan_baseline_fallback'].includes(plan.kind))
    ?? plans?.[0]
}

function buildRows(engineers: Engineer[], routes: EngineerRoute[]) {
  const routeByEngineer = new Map(routes.map((route) => [route.engineer_id, route]))
  const knownEngineerIds = new Set(engineers.map((engineer) => engineer.id))
  const rows: AnalyticsRow[] = engineers.map((engineer) => ({ engineer, route: routeByEngineer.get(engineer.id) }))
  for (const route of routes) {
    if (knownEngineerIds.has(route.engineer_id)) continue
    rows.push({
      engineer: {
        id: route.engineer_id,
        external_id: route.engineer_id,
        input_order: rows.length,
        name: route.engineer_name,
        start_location: route.start_location,
        shift_start: route.departure_at,
        shift_end: route.finish_at,
        skills: [],
        transport: route.transport,
        is_synthetic: true,
      },
      route,
    })
  }
  return rows.sort((left, right) => left.engineer.input_order - right.engineer.input_order)
}

export function AnalyticsPage() {
  const { datasetsQuery, dataset } = useActiveDataset()
  const plansQuery = useQuery({ queryKey: ['plans', dataset?.id], queryFn: () => apiClient.plans(dataset!.id), enabled: Boolean(dataset) })
  const requestsQuery = useQuery({ queryKey: ['requests', dataset?.id], queryFn: () => apiClient.requests(dataset!.id), enabled: Boolean(dataset) })
  const engineersQuery = useQuery({ queryKey: ['engineers', dataset?.id], queryFn: () => apiClient.engineers(dataset!.id), enabled: Boolean(dataset) })
  const plan = currentPlan(plansQuery.data, dataset?.current_plan_id)
  const rows = useMemo(() => buildRows(engineersQuery.data ?? [], plan?.result.routes ?? []), [engineersQuery.data, plan])

  if (datasetsQuery.isLoading || plansQuery.isLoading || requestsQuery.isLoading || engineersQuery.isLoading) {
    return <div className="grid gap-4"><div className="h-28 animate-pulse rounded-xl bg-slate-200" /><div className="h-[620px] animate-pulse rounded-xl bg-slate-200" /></div>
  }
  const error = datasetsQuery.error ?? plansQuery.error ?? requestsQuery.error ?? engineersQuery.error
  if (error) return <EmptyState title="Не удалось загрузить аналитику" description={error.message} />
  if (!dataset) return <EmptyState title="Нет набора данных" description="Загрузите заявки и бригады зоны на странице «Данные»." action={<Link to="/data" className="inline-flex rounded-lg bg-primary px-4 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-violet-700">Перейти к данным</Link>} />
  if (!plan) return <EmptyState title="Нет опубликованного плана" description="Аналитика строится по рассчитанному плану: сначала рассчитайте маршруты." action={<Link to="/planning" className="inline-flex rounded-lg bg-primary px-4 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-violet-700">Перейти к планированию</Link>} />

  const requests = requestsQuery.data?.items ?? []
  const requestsById = new Map(requests.map((request) => [request.id, request]))
  const totalServiceMinutes = rows.reduce((total, row) => total + (row.route?.service_minutes ?? 0), 0)
  const totalWaitMinutes = rows.reduce((total, row) => total + (row.route?.wait_minutes ?? 0), 0)
  const urgentAssigned = rows.reduce((total, row) => total + (row.route?.stops.filter((stop) => {
    const request = requestsById.get(stop.request_id)
    return request ? isEmergencyRequest(request) : false
  }).length ?? 0), 0)
  const latestRoute = rows.filter((row) => row.route?.stops.length).sort((left, right) => dateValue(right.route!.finish_at) - dateValue(left.route!.finish_at))[0]
  const busiestRoute = rows.filter((row) => row.route).sort((left, right) => routeLoad(right.route) - routeLoad(left.route))[0]
  const kpis = plan.model_info.kpis

  return (
    <div className="space-y-4 pb-16 md:pb-0" aria-label="Аналитика маршрутов">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-xl font-semibold">Аналитика · {dataset.service_zone_name ?? dataset.title}</h2>
            <Badge tone="violet">расчётный план</Badge>
            {plan.model_info.simulation && <Badge tone="amber">симуляция</Badge>}
            {plan.input_revision !== dataset.revision && <Badge tone="red">план по версии данных {plan.input_revision}</Badge>}
          </div>
          <p className="mt-1 text-sm text-muted">Общий временной разрез всех инженеров за {new Date(`${dataset.planning_date}T12:00:00`).toLocaleDateString('ru-RU')}</p>
        </div>
        <Link to="/planning" className="rounded-lg bg-white px-4 py-2 text-sm font-semibold text-primary ring-1 ring-violet-200 hover:bg-violet-50">Открыть планирование</Link>
      </div>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4 xl:grid-cols-8">
        <Metric label="Назначено" value={`${plan.metrics.assigned_count}/${dataset.request_count}`} />
        <Metric label="Срочных назначено" value={urgentAssigned} tone={urgentAssigned ? 'red' : undefined} />
        <Metric label="Инженеров в работе" value={`${plan.metrics.used_engineers_count}/${rows.length}`} />
        <Metric label="Расчётный пробег" value={km(plan.metrics.total_distance_meters)} />
        <Metric label="В дороге" value={`${plan.metrics.total_travel_minutes} мин`} tone="blue" />
        <Metric label="Работа на точках" value={`${totalServiceMinutes} мин`} tone="violet" />
        <Metric label="Ожидание" value={`${totalWaitMinutes} мин`} tone="amber" />
        <Metric label="Загрузка" value={percent(kpis?.used_engineer_utilization_percent)} />
      </div>

      <Card className="overflow-hidden" aria-label="Диаграмма Ганта маршрутов инженеров">
        <div className="flex flex-wrap items-start justify-between gap-3 border-b p-4">
          <div>
            <h3 className="font-semibold">Диаграмма Ганта выполнения маршрутов</h3>
            <p className="mt-1 text-xs text-muted">
              Каждый маршрут — хронология смены: дорога между точками, ожидание окна, работа/авария и свободное время. Это план, а не фактический GPS-трек.
            </p>
          </div>
          <div className="flex flex-wrap gap-3 text-xs text-slate-600" aria-label="Легенда диаграммы Ганта">
            <Legend color="bg-sky-500" label="Дорога" />
            <Legend color="bg-amber-400" label="Ожидание" />
            <Legend color="bg-violet-600" label="Работа" />
            <Legend color="bg-red-600" label="Авария" />
            <Legend color="bg-slate-300" label="Свободное время смены" />
          </div>
        </div>
        <GanttChart rows={rows} requests={requests} />
      </Card>

      <div className="grid gap-4 xl:grid-cols-3">
        <Card className="p-5 xl:col-span-2" aria-label="Итоги загрузки инженеров">
          <h3 className="font-semibold">Картина дня</h3>
          <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Insight label="Позже всех завершает" value={latestRoute?.engineer.name ?? '—'} detail={latestRoute?.route ? `${time(latestRoute.route.finish_at)} · ${latestRoute.route.stops.length} заявок` : 'Нет маршрутов'} />
            <Insight label="Максимальная загрузка" value={busiestRoute?.engineer.name ?? '—'} detail={busiestRoute?.route ? `${routeLoad(busiestRoute.route)} мин в маршруте` : 'Нет маршрутов'} />
            <Insight label="Среднее заявок" value={plan.metrics.used_engineers_count ? (plan.metrics.assigned_count / plan.metrics.used_engineers_count).toLocaleString('ru-RU', { maximumFractionDigits: 1 }) : '—'} detail="на задействованного инженера" />
            <Insight label="Неназначено" value={String(plan.metrics.unassigned_count)} detail={`срочных: ${plan.metrics.urgent_unassigned_count}`} warning={plan.metrics.unassigned_count > 0} />
          </div>
        </Card>
        <PlanPassport plan={plan} />
      </div>

      <RouteSummaryTable rows={rows} requestsById={requestsById} planId={plan.id} />
    </div>
  )
}

function routeLoad(route: EngineerRoute | undefined) {
  return route ? route.travel_minutes + route.service_minutes + route.wait_minutes : 0
}

function Metric({ label, value, tone }: { label: string; value: string | number; tone?: 'blue' | 'violet' | 'amber' | 'red' }) {
  const colors = { blue: 'text-sky-700', violet: 'text-violet-700', amber: 'text-amber-700', red: 'text-red-700' }
  return <Card className="min-w-0 p-4"><div className="text-[11px] font-medium uppercase tracking-wide text-muted">{label}</div><div className={`mt-1 truncate text-xl font-semibold ${tone ? colors[tone] : 'text-ink'}`}>{value}</div></Card>
}

function Legend({ color, label }: { color: string; label: string }) {
  return <span className="flex items-center gap-1.5"><span className={`h-2.5 w-2.5 rounded-sm ${color}`} />{label}</span>
}

function GanttChart({ rows, requests }: { rows: AnalyticsRow[]; requests: ServiceRequest[] }) {
  const requestById = new Map(requests.map((request) => [request.id, request]))
  const allSegments = rows.map(({ engineer, route }) => ({
    engineer,
    route,
    segments: buildRouteGanttSegments(engineer, route, requestById),
  }))
  const start = Math.min(
    ...rows.map((row) => dateValue(row.engineer.shift_start)),
    ...allSegments.flatMap((row) => row.segments.map((segment) => dateValue(segment.start))),
  )
  const end = Math.max(
    ...rows.map((row) => dateValue(row.engineer.shift_end)),
    ...allSegments.flatMap((row) => row.segments.map((segment) => dateValue(segment.end))),
  )
  const duration = Math.max(end - start, 60_000)
  const left = (value: string) => `${Math.max(0, Math.min(100, ((dateValue(value) - start) / duration) * 100))}%`
  const width = (from: string, to: string) => `${Math.max(0.22, ((dateValue(to) - dateValue(from)) / duration) * 100)}%`
  const firstHour = Math.ceil(start / 3_600_000) * 3_600_000
  const ticks: number[] = []
  for (let tick = firstHour; tick <= end; tick += 3_600_000) ticks.push(tick)

  return (
    <div className="max-w-full overflow-x-auto" data-gantt-scroll>
      <div className="min-w-[1120px]">
        <div className="grid grid-cols-[230px_minmax(860px,1fr)] border-b bg-slate-50">
          <div className="border-r px-4 py-3 text-xs font-semibold uppercase tracking-wide text-muted">Инженер и маршрут</div>
          <div className="relative h-12">
            {ticks.map((tick) => (
              <div key={tick} className="absolute inset-y-0 border-l border-slate-200" style={{ left: `${((tick - start) / duration) * 100}%` }}>
                <span className="absolute left-1 top-3 text-[11px] font-medium text-slate-500">{time(new Date(tick).toISOString())}</span>
              </div>
            ))}
          </div>
        </div>
        {allSegments.map(({ engineer, route, segments }) => {
          const shiftWidth = width(engineer.shift_start, engineer.shift_end)
          return (
            <div key={engineer.id} data-gantt-engineer className="grid min-h-[66px] grid-cols-[230px_minmax(860px,1fr)] border-b last:border-0">
              <div className="border-r px-4 py-3">
                <div className="truncate text-sm font-semibold" title={engineer.name}>{engineer.name}</div>
                <div className="mt-1 flex gap-2 text-[11px] text-muted">
                  <span>{route?.stops.length ?? 0} заявок</span>
                  <span>{km(route?.distance_meters)}</span>
                  <span>до {time(route?.finish_at)}</span>
                </div>
              </div>
              <div className="relative my-3 h-10 overflow-hidden" data-gantt-track aria-label={`Хронология смены: ${engineer.name}`}>
                <div
                  className="absolute top-2 h-6 rounded bg-slate-100"
                  style={{ left: left(engineer.shift_start), width: shiftWidth }}
                  title={`Смена ${time(engineer.shift_start)}–${time(engineer.shift_end)}`}
                />
                {segments.map((segment) => {
                  const color = timelineColors[segment.kind]
                  const showLabel = Boolean(segment.label) && (segment.kind === 'service' || segment.kind === 'emergency')
                  return (
                    <span
                      key={segment.key}
                      data-gantt-segment={segment.kind}
                      className={`absolute top-2 flex h-6 items-center overflow-hidden px-1 text-[10px] font-semibold ${
                        segment.kind === 'free' ? 'text-slate-600' : 'text-white'
                      } ${color}`}
                      style={{ left: left(segment.start), width: width(segment.start, segment.end) }}
                      title={segment.title}
                      aria-label={segment.title}
                    >
                      {showLabel ? segment.label : null}
                    </span>
                  )
                })}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function Insight({ label, value, detail, warning = false }: { label: string; value: string; detail: string; warning?: boolean }) {
  return <div className={`rounded-lg p-3 ${warning ? 'bg-amber-50' : 'bg-slate-50'}`}><div className="text-xs text-muted">{label}</div><div className={`mt-1 truncate font-semibold ${warning ? 'text-amber-800' : ''}`} title={value}>{value}</div><div className="mt-1 text-xs text-muted">{detail}</div></div>
}

function PlanPassport({ plan }: { plan: Plan }) {
  const experiment = plan.model_info.experiment
  return <Card className="p-5" aria-label="Паспорт расчётного плана"><div className="flex items-center justify-between gap-2"><h3 className="font-semibold">Паспорт плана</h3><Badge tone={plan.result.solution_status === 'valid' ? 'green' : 'amber'}>{plan.result.solution_status === 'valid' ? 'проверен валидатором' : plan.result.solution_status}</Badge></div><dl className="mt-4 space-y-2 text-sm"><Detail label="Алгоритм" value={String(plan.model_info.algorithm_id ?? plan.result.algorithm)} /><Detail label="Завершение поиска" value={terminationText(plan.result.termination_reason)} /><Detail label="Расчёт" value={`${plan.result.elapsed_ms} мс`} /><Detail label="Версия данных" value={String(plan.input_revision)} /><Detail label="Создан" value={fullDateTime(plan.created_at)} /><Detail label="Координаты" value={coordinateQualityText(plan.result.coordinate_quality)} />{experiment && <><Detail label="Эксперимент" value={experiment.experiment_id.slice(0, 12)} /><Detail label="Снимок" value={experiment.snapshot_sha256.slice(0, 12)} /><Detail label="Матрица" value={experiment.matrix_sha256.slice(0, 12)} /></>}</dl></Card>
}

function Detail({ label, value }: { label: string; value: string }) {
  return <div className="grid grid-cols-[110px_minmax(0,1fr)] gap-3"><dt className="text-muted">{label}</dt><dd className="truncate text-right font-medium" title={value}>{value}</dd></div>
}

function RouteSummaryTable({ rows, requestsById, planId }: { rows: AnalyticsRow[]; requestsById: Map<string, ServiceRequest>; planId: string }) {
  return <Card className="overflow-hidden" aria-label="Сводная таблица маршрутов"><div className="border-b p-4"><h3 className="font-semibold">Все инженеры и маршруты</h3><p className="mt-1 text-xs text-muted">Полная загрузка смены и последовательность заявок опубликованного плана.</p></div><div className="overflow-x-auto"><table className="w-full min-w-[1380px] text-sm"><thead className="bg-slate-50 text-left text-[11px] uppercase tracking-wide text-muted"><tr><th className="p-3">Инженер</th><th>Транспорт</th><th>Смена</th><th>Статус</th><th>Визиты</th><th>Аварии</th><th>Маршрут</th><th>Пробег</th><th>Дорога</th><th>Работа</th><th>Ожидание</th><th>Загрузка смены</th><th>Последовательность</th><th /></tr></thead><tbody>{rows.map(({ engineer, route }) => {
    const shiftMinutes = minutesBetween(engineer.shift_start, engineer.shift_end)
    const load = routeLoad(route)
    const emergencyCount = route?.stops.filter((stop) => { const request = requestsById.get(stop.request_id); return request ? isEmergencyRequest(request) : false }).length ?? 0
    const sequence = route?.stops.map((stop) => requestsById.get(stop.request_id)?.external_id ?? `№${stop.sequence}`).join(' → ') ?? '—'
    return <tr key={engineer.id} className="border-t align-top"><td className="p-3 font-semibold">{engineer.name}</td><td>{transportName(engineer.transport)}</td><td>{time(engineer.shift_start)}–{time(engineer.shift_end)}</td><td>{route?.stops.length ? <Badge tone="green">в маршруте</Badge> : <Badge>свободен</Badge>}</td><td>{route?.stops.length ?? 0}</td><td>{emergencyCount || '—'}</td><td>{route?.stops.length ? `${time(route.stops[0]?.departure_at ?? route.departure_at)}–${time(route.finish_at)}` : '—'}</td><td>{km(route?.distance_meters)}</td><td>{route ? `${route.travel_minutes} мин` : '—'}</td><td>{route ? `${route.service_minutes} мин` : '—'}</td><td>{route ? `${route.wait_minutes} мин` : '—'}</td><td><div className="w-28"><div className="mb-1 text-xs font-medium">{shiftMinutes ? percent((load / shiftMinutes) * 100) : '—'}</div><div className="h-1.5 overflow-hidden rounded bg-slate-200"><div className="h-full bg-violet-600" style={{ width: `${Math.min(100, shiftMinutes ? (load / shiftMinutes) * 100 : 0)}%` }} /></div></div></td><td className="max-w-sm"><div className="truncate pr-4" title={sequence}>{sequence}</div></td><td>{route && <Link to="/routes/$planId/$engineerId" params={{ planId, engineerId: engineer.id }} className="font-medium text-primary hover:underline">Открыть</Link>}</td></tr>
  })}</tbody></table></div></Card>
}
