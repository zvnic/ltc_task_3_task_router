import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import { Link, useParams } from '@tanstack/react-router'

import { apiClient } from '../../shared/api'
import { transportText } from '../../shared/transport'
import type { EngineerRoute, ServiceRequest } from '../../shared/types'
import { Badge, Card, EmptyState } from '../../shared/ui'
import { isEmergencyRequest } from './request-kind'
import { RouteMap } from './RouteMap'
import { RouteTimeline } from './RouteTimeline'
import { planNormMode } from './plan-explain'

const EMPTY_REQUESTS: ServiceRequest[] = []
const EMPTY_ROUTES: EngineerRoute[] = []

function time(value: string) {
  return new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Moscow' }).format(new Date(value))
}

function date(value: string) {
  return new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' }).format(new Date(`${value}T12:00:00`))
}

function distance(meters: number) {
  return `${(meters / 1000).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} км`
}

function minutesBetween(start: string, end: string) {
  return Math.max(0, Math.round((new Date(end).getTime() - new Date(start).getTime()) / 60_000))
}

export function EngineerRoutePage() {
  const { planId, engineerId } = useParams({ from: '/_app/routes/$planId/$engineerId' })
  const planQuery = useQuery({ queryKey: ['plan', planId], queryFn: () => apiClient.plan(planId) })
  const datasetId = planQuery.data?.dataset_id
  const datasetQuery = useQuery({ queryKey: ['dataset', datasetId], queryFn: () => apiClient.dataset(datasetId!), enabled: Boolean(datasetId) })
  const requestsQuery = useQuery({ queryKey: ['requests', datasetId], queryFn: () => apiClient.requests(datasetId!), enabled: Boolean(datasetId) })
  const engineersQuery = useQuery({ queryKey: ['engineers', datasetId], queryFn: () => apiClient.engineers(datasetId!), enabled: Boolean(datasetId) })

  const plan = planQuery.data
  const dataset = datasetQuery.data
  const route = plan?.result.routes.find((item) => item.engineer_id === engineerId)
  const engineer = engineersQuery.data?.find((item) => item.id === engineerId)
  const detailedRouteQuery = useQuery({
    queryKey: ['engineer-route-geometry', planId, engineerId],
    queryFn: ({ signal }) => apiClient.engineerRoute(planId, engineerId, signal),
    enabled: Boolean(route?.stops.length),
    retry: false,
    staleTime: Number.POSITIVE_INFINITY,
  })

  const requestItems = requestsQuery.data?.items
  const allRequests = useMemo(() => requestItems ?? EMPTY_REQUESTS, [requestItems])
  const mapRoutes = useMemo(() => (route ? [route] : EMPTY_ROUTES), [route])
  const requestById = useMemo(
    () => new Map(allRequests.map((request) => [request.id, request])),
    [allRequests],
  )
  const mapRequests = useMemo(() => {
    if (!route) return EMPTY_REQUESTS
    return route.stops.flatMap((stop) => {
      const request = requestById.get(stop.request_id)
      return request ? [request] : []
    })
  }, [route, requestById])

  // Skeleton only on the very first load (no cached data yet).
  // After plan/route exist, never return skeleton on refetch — keep RouteMap mounted.
  const firstLoad =
    (planQuery.isPending && !planQuery.data) ||
    (Boolean(datasetId) &&
      ((datasetQuery.isPending && !datasetQuery.data) ||
        (engineersQuery.isPending && !engineersQuery.data) ||
        (requestsQuery.isPending && !requestsQuery.data)))

  const canShowRoute = Boolean(plan && route && dataset && engineer)
  if (firstLoad && !canShowRoute) {
    return (
      <div className="grid gap-4">
        <div className="h-28 animate-pulse rounded-xl bg-slate-200" />
        <div className="h-[420px] animate-pulse rounded-xl bg-slate-200 md:h-[520px]" />
      </div>
    )
  }

  if (planQuery.isError && !planQuery.data) {
    return <EmptyState title="Маршрут не загружен" description={planQuery.error.message} />
  }

  // Soft error: refetch failures must not unmount RouteMap once data was shown.
  const backgroundError =
    (datasetQuery.isError && !datasetQuery.data ? datasetQuery.error.message : null) ||
    (requestsQuery.isError && !requestsQuery.data ? requestsQuery.error.message : null) ||
    (engineersQuery.isError && !engineersQuery.data ? engineersQuery.error.message : null)

  if (!plan || !dataset || !route || !engineer) {
    return <EmptyState title="Маршрут инженера не найден" description="Проверьте ссылку или вернитесь к планированию." />
  }
  const shiftMinutes = minutesBetween(engineer.shift_start, engineer.shift_end)
  const occupiedMinutes = route.travel_minutes + route.service_minutes + route.wait_minutes
  const utilization = shiftMinutes ? occupiedMinutes / shiftMinutes * 100 : 0

  return (
    <div className="min-w-0 max-w-full space-y-4 pb-16 md:pb-0" aria-label="Маршрут конкретного инженера">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <Link to="/planning" className="text-sm font-medium text-primary hover:underline">← Все маршруты</Link>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <h2 className="text-2xl font-semibold">Маршрут: {route.engineer_name}</h2>
            <Badge tone={plan.model_info.simulation ? 'slate' : 'green'}>{plan.model_info.simulation ? 'симуляция' : 'опубликован'}</Badge>
          </div>
          <p className="mt-1 text-sm text-muted">{date(dataset.planning_date)} · {dataset.service_zone_name ?? dataset.title} · версия данных {plan.input_revision}</p>
          <p className="mt-1 text-sm font-medium text-slate-700" data-route-transport={route.transport}>{transportText(route.transport)} · оценочная геометрия</p>
        </div>
        <Card className="px-4 py-3 text-sm">
          <div className="text-xs uppercase tracking-wide text-muted">Смена</div>
          <div className="mt-1 font-semibold">{time(engineer.shift_start)}–{time(engineer.shift_end)}</div>
        </Card>
      </div>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4 xl:grid-cols-6">
        <Metric label="Визиты" value={route.stops.length} />
        <Metric label="Пробег" value={distance(route.distance_meters)} />
        <Metric label="Дорога" value={`${route.travel_minutes} мин`} />
        <Metric label="Работа" value={`${route.service_minutes} мин`} />
        <Metric label="Ожидание" value={`${route.wait_minutes} мин`} />
        <Metric label="Загрузка смены" value={`${utilization.toLocaleString('ru-RU', { maximumFractionDigits: 1 })}%`} />
      </div>

      <RouteTimeline route={route} requests={mapRequests} officeAddress={dataset.assumptions.office_address} normAdvisory={planNormMode(plan) === 'advisory'} />
      {backgroundError ? (
        <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900" role="status">
          Фоновые данные: {backgroundError}. Показаны последние успешные данные.
        </div>
      ) : null}
      {/* Keep RouteMap layout stable: no fetch banner above the map (avoids resize flicker). */}
      <RouteMap
        routes={mapRoutes}
        requests={mapRequests}
        selectedEngineerId={route.engineer_id}
        onSelectEngineer={() => undefined}
        startAddress={dataset.assumptions.office_address}
        detailedRoute={detailedRouteQuery.data}
        routingPending={detailedRouteQuery.isPending && detailedRouteQuery.isFetching}
        routingError={detailedRouteQuery.error?.message}
        onRetryRouting={() => { void detailedRouteQuery.refetch() }}
      />
      <RouteSchedule route={route} requests={requestById} officeAddress={dataset.assumptions.office_address} planningDate={dataset.planning_date} />
    </div>
  )
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return <Card className="p-4"><div className="text-xs font-medium uppercase tracking-wide text-muted">{label}</div><div className="mt-1 text-xl font-semibold">{value}</div></Card>
}

function RouteSchedule({ route, requests, officeAddress, planningDate }: {
  route: EngineerRoute
  requests: Map<string, ServiceRequest>
  officeAddress?: string
  planningDate: string
}) {
  return (
    <Card aria-label="Задания инженера" className="overflow-hidden">
      <div className="border-b px-5 py-4">
        <h3 className="font-semibold">Задания на {date(planningDate)}</h3>
        <p className="mt-1 text-sm text-muted">Выезд {time(route.stops[0]?.departure_at ?? route.departure_at)} · {officeAddress ?? 'офис зоны'} · финиш {time(route.finish_at)} на последней заявке (без возврата в офис)</p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[920px] text-sm">
          <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-muted"><tr><th className="p-4">№</th><th>Заявка</th><th>Адрес</th><th>Прибытие</th><th>Работа</th><th>Дорога</th><th>Статус</th></tr></thead>
          <tbody>{route.stops.map((stop) => {
            const request = requests.get(stop.request_id)
            const emergency = request ? isEmergencyRequest(request) : false
            return <tr key={stop.request_id} className="border-t"><td className="p-4 font-semibold text-primary">{stop.sequence}</td><td className="font-medium">{request?.external_id ?? '—'}</td><td className="max-w-md pr-4">{request?.address ?? '—'}</td><td>{time(stop.arrival_at)}</td><td>{time(stop.service_start_at)}–{time(stop.service_end_at)}</td><td>{stop.travel_minutes_from_previous} мин</td><td>{emergency ? <Badge tone="red">Авария</Badge> : <Badge>Обычная</Badge>}</td></tr>
          })}</tbody>
        </table>
      </div>
      <div className="border-t bg-slate-50 px-5 py-3 text-sm text-slate-600">{route.explanation}</div>
    </Card>
  )
}
