import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { Fragment, useCallback, useMemo, useState, type FormEvent } from 'react'

import { apiClient } from '../../shared/api'
import { useActiveDataset } from '../../shared/dataset-state'
import type {
  Algorithm,
  Coordinates,
  EngineerRoute,
  NormRow,
  Plan,
  ServiceRequest,
  TravelNormMode,
} from '../../shared/types'
import { transportText } from '../../shared/transport'
import { Badge, Button, Card, EmergencyIcon, EmptyState } from '../../shared/ui'
import { HowPlanWasBuilt, MethodReference, PlanStatusStrip } from './PlanOverview'
import {
  changedRequestIdsFromDiff,
  isEmergencyRequest,
  isReplanPlan,
  requestWorkClass,
  WORK_CLASS_LABELS,
  workClassBadgeTone,
} from './request-kind'
import { IncomingAddressField } from './IncomingAddressField'
import { addressFromDirectory, zoneAddresses, type IncomingAddress, type ZoneAddress } from './incoming-address'
import { RouteMap } from './RouteMap'
import { RouteTimeline, TravelLegMinutes } from './RouteTimeline'
import { UnassignedPanel } from './UnassignedPanel'
import { describeDistanceChange } from './comparison-metrics'
import {
  algorithmTitle,
  clock,
  kmText,
  minutesText,
  planNormMode,
  planOrigin,
  plural,
  routeSummary,
  selectionDecisions,
  shiftLoadPercent,
  terminationText,
  visitReasons,
  type RouteSummary,
  type VisitReason,
} from './plan-explain'

const EMPTY_REQUESTS: ServiceRequest[] = []
const EMPTY_STRINGS: string[] = []
const EMPTY_ROUTES: EngineerRoute[] = []
// Виды текущего плана набора — как CURRENT_PLAN_KINDS на бэкенде. manual_insert — план
// после ручного назначения отказанной заявки: без него страница осталась бы на старом плане.
const CURRENT_PLAN_KINDS = ['optimized', 'baseline_fallback', 'replan_optimized', 'replan_baseline_fallback', 'manual_insert']

function time(value: string) {
  return new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Moscow' }).format(new Date(value))
}

function km(meters: number) {
  return `${(meters / 1000).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} км`
}

function clientEventId() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
}

export function PlanningPage() {
  const queryClient = useQueryClient()
  const { datasetsQuery, dataset } = useActiveDataset()
  const requestsQuery = useQuery({ queryKey: ['requests', dataset?.id], queryFn: () => apiClient.requests(dataset!.id), enabled: Boolean(dataset) })
  const datasetDetailQuery = useQuery({ queryKey: ['dataset', dataset?.id], queryFn: () => apiClient.dataset(dataset!.id), enabled: Boolean(dataset) })
  const plansQuery = useQuery({ queryKey: ['plans', dataset?.id], queryFn: () => apiClient.plans(dataset!.id), enabled: Boolean(dataset) })
  const algorithmsQuery = useQuery({ queryKey: ['algorithms'], queryFn: apiClient.algorithms })
  const selectedPlan = useMemo(() => plansQuery.data?.find((plan) => CURRENT_PLAN_KINDS.includes(plan.kind)) ?? plansQuery.data?.[0], [plansQuery.data])
  const [selectedEngineerId, setSelectedEngineerId] = useState<string | null>(null)
  const [tab, setTab] = useState<'schedule' | 'comparison' | 'unassigned'>('schedule')
  const [requestOpen, setRequestOpen] = useState(false)
  const [algorithmId, setAlgorithmId] = useState('ortools_gls_v1')
  const [compareAll, setCompareAll] = useState(true)
  const selectEngineer = useCallback((id: string) => setSelectedEngineerId(id), [])
  const engineersQuery = useQuery({ queryKey: ['engineers', dataset?.id], queryFn: () => apiClient.engineers(dataset!.id), enabled: Boolean(dataset) })
  // «Не назначено» ведёт к причинам отказов: открываем вкладку и прокручиваем к ней.
  const showUnassigned = useCallback(() => {
    setTab('unassigned')
    document.getElementById('plan-details')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [])
  const planMutation = useMutation({
    mutationFn: () => apiClient.createPlan(
      dataset!.id,
      compareAll
        ? (algorithmsQuery.data ?? []).map((item) => item.id)
        : [algorithmId],
    ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['datasets'] })
      await queryClient.invalidateQueries({ queryKey: ['plans', dataset?.id] })
    },
  })
  const requestMutation = useMutation({
    mutationFn: (payload: unknown) => apiClient.replan(selectedPlan!.id, payload),
    onSuccess: async () => {
      setRequestOpen(false)
      await queryClient.invalidateQueries({ queryKey: ['datasets'] })
      await queryClient.invalidateQueries({ queryKey: ['plans', dataset?.id] })
      await queryClient.invalidateQueries({ queryKey: ['requests', dataset?.id] })
    },
  })
  // Hooks must run before any early returns (Rules of Hooks).
  const parentPlanIdForDiff = selectedPlan?.parent_plan_id ?? null
  const planDiffQuery = useQuery({
    queryKey: ['diff', selectedPlan?.id, parentPlanIdForDiff],
    queryFn: () => apiClient.diff(selectedPlan!.id, parentPlanIdForDiff!),
    enabled: Boolean(selectedPlan?.id && parentPlanIdForDiff),
  })
  const changedRequestIds = useMemo(
    () => changedRequestIdsFromDiff(planDiffQuery.data),
    [planDiffQuery.data],
  )
  const plan = selectedPlan as Plan | undefined
  const routes = plan?.result.routes ?? EMPTY_ROUTES
  const selectedRoute = routes.find((route) => route.engineer_id === selectedEngineerId && route.stops.length > 0)
    ?? routes.find((route) => route.stops.length > 0)
    ?? routes[0]
  const activeEngineerId = selectedRoute?.engineer_id ?? null
  const detailedRouteQuery = useQuery({
    queryKey: ['engineer-route-geometry', plan?.id, activeEngineerId],
    queryFn: ({ signal }) => apiClient.engineerRoute(plan!.id, activeEngineerId!, signal),
    enabled: Boolean(plan?.id && activeEngineerId && selectedRoute?.stops.length),
    retry: false,
    staleTime: Number.POSITIVE_INFINITY,
  })
  const officeLocation = datasetDetailQuery.data?.office
  const incomingAddresses = useMemo(
    () => zoneAddresses(requestsQuery.data?.items ?? EMPTY_REQUESTS, officeLocation),
    [requestsQuery.data, officeLocation],
  )

  if (datasetsQuery.isPending && !datasetsQuery.data) return <div className="grid gap-4"><div className="h-24 animate-pulse rounded-xl bg-slate-200" /><div className="h-[560px] animate-pulse rounded-xl bg-slate-200" /></div>
  if (datasetsQuery.isError) {
    return (
      <EmptyState
        title="Не удалось загрузить наборы данных"
        description={datasetsQuery.error.message}
        action={<Button variant="secondary" onClick={() => void datasetsQuery.refetch()}>Повторить</Button>}
      />
    )
  }
  if (!dataset) {
    return (
      <EmptyState
        title="Нет набора данных"
        description="Загрузите заявки и бригады зоны на странице «Данные» — после этого здесь появится расчёт маршрутов."
        action={<Link to="/data" className={LINK_BUTTON}>Перейти к данным</Link>}
      />
    )
  }

  const replan = isReplanPlan(plan?.kind ?? '')
  const eventKind = plan?.model_info.event_kind === 'normal' ? 'normal' : 'urgent'
  const protectedRequestIds = plan?.model_info.protected_request_ids ?? EMPTY_STRINGS
  const normMode = planNormMode(plan)
  const requests = requestsQuery.data?.items ?? EMPTY_REQUESTS
  const algorithms = algorithmsQuery.data ?? []
  const engineerById = new Map((engineersQuery.data ?? []).map((engineer) => [engineer.id, engineer]))
  const requestById = new Map(requests.map((request) => [request.id, request]))
  return (
    <div className="space-y-4 pb-16 md:pb-0">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-xl font-semibold">{dataset.title}</h2>
            <Badge tone="amber">координаты демо</Badge>
            {datasetDetailQuery.data?.assumptions.is_demo_enrichment ? (
              <Badge tone="gray">обогащение демо</Badge>
            ) : null}
            {plan?.model_info.simulation && <Badge tone="slate">симуляция</Badge>}
            {replan && <Badge tone="red">Корректировка маршрута</Badge>}
            {plan?.kind === 'manual_insert' && <Badge tone="violet">Ручное назначение</Badge>}
            {replan && protectedRequestIds.length > 0 && (
              <Badge tone="violet">Защищено: {protectedRequestIds.length}</Badge>
            )}
            {replan && changedRequestIds.length > 0 && (
              <Badge tone="amber">Изменено: {changedRequestIds.length}</Badge>
            )}
            {plan && plan.input_revision !== dataset.revision && (
              <Badge tone="red">план по версии данных {plan.input_revision}</Badge>
            )}
          </div>
          <p className="mt-1 text-sm text-muted">Дата {new Date(`${dataset.planning_date}T12:00:00`).toLocaleDateString('ru-RU')} · версия данных {dataset.revision}</p>
          {datasetDetailQuery.data?.assumptions.start_policy ? (
            <p className="mt-1 max-w-3xl text-xs text-muted">
              {String(datasetDetailQuery.data.assumptions.start_policy)}
            </p>
          ) : null}
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <label className="text-xs font-medium text-muted">Метод
            <select aria-label="Метод расчёта" value={algorithmId} onChange={(event) => { setAlgorithmId(event.target.value); setCompareAll(false) }} className="mt-1 block rounded-lg border bg-white px-3 py-2 text-sm text-ink">
              {(algorithmsQuery.data ?? []).map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}
            </select>
          </label>
          <label className="flex items-center gap-2 rounded-lg border bg-white px-3 py-2 text-sm"><input type="checkbox" checked={compareAll} onChange={(event) => setCompareAll(event.target.checked)} />Сравнить все</label>
          {plan && <Button variant="secondary" onClick={() => setRequestOpen(true)}>Добавить заявку в течение дня</Button>}
          <Button onClick={() => planMutation.mutate()} disabled={planMutation.isPending || plansQuery.isLoading}>{plansQuery.isLoading ? 'Загружаем план…' : planMutation.isPending ? 'Рассчитываем…' : plan ? 'Пересчитать план' : 'Рассчитать план'}</Button>
        </div>
      </div>
      {planMutation.isError && <div role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{planMutation.error.message}</div>}
      <PlanStatusStrip
        plan={plan}
        requestCount={plan ? plan.metrics.assigned_count + plan.metrics.unassigned_count : dataset.request_count}
        engineerCount={routes.length || (datasetDetailQuery.data?.engineer_count ?? 0)}
        normMode={normMode}
        onShowUnassigned={showUnassigned}
      />
      {replan && plan && (
        <Card className="border-red-200 bg-red-50 p-4" data-replan-banner aria-label="Баннер корректировки маршрута">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="font-semibold text-red-800">Введена корректировка работающего маршрута</div>
              <p className="mt-1 text-sm text-red-900/80">
                {eventKind === 'normal'
                  ? normalInsertionText(normMode)
                  : `После аварийной заявки день пересчитан. Выполненные и начатые визиты сохранены${protectedRequestIds.length > 0 ? ` (${protectedRequestIds.length})` : ''}; изменена только ещё не начатая часть дня${changedRequestIds.length > 0 ? ` (${changedRequestIds.length} сдвигов)` : ''}.`}
              </p>
            </div>
            {planDiffQuery.data && (
              <div className="flex flex-wrap gap-2 text-xs">
                <Badge tone="amber">Δ назначено: {planDiffQuery.data.assigned_delta >= 0 ? '+' : ''}{planDiffQuery.data.assigned_delta}</Badge>
                <Badge tone="slate">Δ км: {(planDiffQuery.data.distance_delta_meters / 1000).toLocaleString('ru-RU', { maximumFractionDigits: 1 })}</Badge>
              </div>
            )}
          </div>
        </Card>
      )}
      {!plan ? (
        <EmptyState
          title="План ещё не построен"
          description="Три метода посчитают один снимок данных, независимый валидатор проверит каждый план, а лучший по приоритетам заказчика будет опубликован."
          action={
            <Button onClick={() => planMutation.mutate()} disabled={planMutation.isPending || plansQuery.isLoading}>
              {planMutation.isPending ? 'Рассчитываем…' : 'Рассчитать маршруты'}
            </Button>
          }
        />
      ) : (
        <>
          <HowPlanWasBuilt plan={plan} algorithms={algorithms} normMode={normMode} />
          <div className="grid min-w-0 max-w-full gap-4 xl:min-h-[680px] xl:grid-cols-[minmax(0,320px)_minmax(0,1fr)]">
            <Card className="flex min-h-0 flex-col overflow-hidden xl:h-[680px]" aria-label="Список маршрутов инженеров">
              <div className="border-b p-4">
                <div className="flex items-start justify-between gap-2">
                  <h3 className="font-semibold">Маршруты</h3>
                  <Badge tone="green">{planMethodLabel(plan, algorithms)}</Badge>
                </div>
                <p className="mt-1 text-xs text-muted">{`${terminationText(plan.result.termination_reason)} · ${plan.result.elapsed_ms.toLocaleString('ru-RU')} мс`}</p>
              </div>
              <div className="min-h-0 max-h-[360px] flex-1 overflow-y-auto p-2 xl:max-h-none">
                {routes.filter((route) => route.stops.length > 0).map((route) => (
                  <RouteRow
                    key={route.engineer_id}
                    route={route}
                    planId={plan.id}
                    selected={route.engineer_id === activeEngineerId}
                    load={shiftLoadPercent(route, engineerById.get(route.engineer_id))}
                    emergencies={route.stops.filter((stop) => {
                      const request = requestById.get(stop.request_id)
                      return request ? isEmergencyRequest(request) : false
                    }).length}
                    onClick={() => setSelectedEngineerId(route.engineer_id)}
                  />
                ))}
              </div>
            </Card>
            <div className="min-w-0 max-w-full">
              <RouteMap
                routes={routes}
                requests={requestsQuery.data?.items ?? EMPTY_REQUESTS}
                selectedEngineerId={activeEngineerId}
                onSelectEngineer={selectEngineer}
                startAddress={datasetDetailQuery.data?.assumptions.office_address}
                detailedRoute={detailedRouteQuery.data}
                routingPending={detailedRouteQuery.isPending && detailedRouteQuery.isFetching}
                routingError={detailedRouteQuery.error?.message}
                onRetryRouting={() => { void detailedRouteQuery.refetch() }}
                protectedRequestIds={protectedRequestIds}
                changedRequestIds={changedRequestIds}
              />
            </div>
          </div>
          <RouteTimeline
            route={selectedRoute}
            requests={requests}
            officeAddress={datasetDetailQuery.data?.assumptions.office_address}
            protectedRequestIds={protectedRequestIds}
            changedRequestIds={changedRequestIds}
            normAdvisory={normMode === 'advisory'}
          />
          <Card id="plan-details" className="scroll-mt-20" aria-label="Расписание и сравнение маршрутов">
            <div className="flex gap-1 overflow-x-auto border-b px-3 pt-3">
              <Tab active={tab === 'schedule'} onClick={() => setTab('schedule')}>Расписание</Tab>
              <Tab active={tab === 'comparison'} onClick={() => setTab('comparison')}>Сравнение</Tab>
              <Tab active={tab === 'unassigned'} onClick={() => setTab('unassigned')}>Неназначенные ({plan.result.unassigned.length})</Tab>
            </div>
            <div className="p-4">
              {tab === 'schedule' && (
                <Schedule
                  key={selectedRoute?.engineer_id}
                  route={selectedRoute}
                  requests={requests}
                  normMode={normMode}
                  protectedIds={new Set(protectedRequestIds)}
                />
              )}
              {tab === 'comparison' && <Comparison plan={plan} plans={plansQuery.data ?? []} algorithms={algorithms} requests={requests} normMode={normMode} />}
              {tab === 'unassigned' && <UnassignedPanel plan={plan} requests={requests} datasetId={dataset.id} datasetRevision={dataset.revision} />}
            </div>
          </Card>
        </>
      )}
      <MethodReference algorithm={algorithms.find((item) => item.id === algorithmId)} plan={plan} normMode={normMode} />
      {requestOpen && plan && (
        <IncomingRequestDialog
          planningDate={dataset.planning_date}
          revision={dataset.revision}
          inputOrder={dataset.request_count}
          addresses={incomingAddresses}
          office={officeLocation}
          normMode={normMode}
          normRows={datasetDetailQuery.data?.assumptions.norms_config?.rows ?? []}
          pending={requestMutation.isPending}
          error={requestMutation.error?.message}
          onClose={() => setRequestOpen(false)}
          onSubmit={(payload) => requestMutation.mutate(payload)}
        />
      )}
    </div>
  )
}

/** Чем получен план — для заголовка списка маршрутов. */
function planMethodLabel(plan: Plan, algorithms: Algorithm[]) {
  const origin = planOrigin(plan)
  if (origin === 'gap_insertion') return 'Вставка в свободный интервал'
  if (origin === 'manual') return 'Ручное назначение'
  return algorithmTitle(plan.model_info.algorithm_id ?? plan.result.algorithm, algorithms)
}

const LINK_BUTTON = 'inline-flex rounded-lg bg-primary px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-violet-700 focus:outline-none focus:ring-2 focus:ring-primary focus:ring-offset-2'

function normalInsertionText(normMode: TravelNormMode) {
  return normMode === 'soft'
    ? 'Обычная заявка встроена только в свободный интервал: сначала место без превышения норматива дороги или с наименьшим превышением, при равенстве — с минимальным дополнительным временем в пути; обещанные визиты не сдвинуты.'
    : 'Обычная заявка встроена только в свободный интервал — место с минимальным дополнительным временем в пути; обещанные визиты не сдвинуты.'
}

const INCOMING_WORK_TYPES = [
  { normId: 'local_repair', label: 'Локальная заявка / ремонт', skill: 'local' },
  { normId: 'connection_basic', label: 'Подключение', skill: 'connection' },
  { normId: 'add_order', label: 'Дозаказ оборудования', skill: 'connection' },
] as const

function IncomingRequestDialog({
  planningDate,
  revision,
  inputOrder,
  addresses,
  office,
  normMode,
  normRows,
  pending,
  error,
  onClose,
  onSubmit,
}: {
  planningDate: string
  revision: number
  inputOrder: number
  addresses: ZoneAddress[]
  office?: Coordinates
  normMode: TravelNormMode
  normRows: NormRow[]
  pending: boolean
  error?: string
  onClose: () => void
  onSubmit: (payload: unknown) => void
}) {
  const [priority, setPriority] = useState<'urgent' | 'normal'>('urgent')
  const [normId, setNormId] = useState('local_repair')
  // Пока диспетчер не тронул адрес, предлагается ближний к офису адрес справочника —
  // он может догрузиться уже после открытия окна.
  const [editedLocation, setEditedLocation] = useState<IncomingAddress | null>(null)
  const location: IncomingAddress = editedLocation
    ?? (addresses[0]
      ? addressFromDirectory(addresses[0])
      : { address: '', district: '', coordinates: null, origin: 'manual', coordinateSource: 'verified' })
  const locationReady = Boolean(location.address.trim() && location.district.trim() && location.coordinates)
  const [eventTime, setEventTime] = useState('14:20')
  const [windowStart, setWindowStart] = useState('14:20')
  const [windowEnd, setWindowEnd] = useState('16:00')
  const selectedNormId = priority === 'urgent' ? 'tkd_emergency' : normId
  const norm = normRows.find((row) => row.norm_id === selectedNormId)
  const safeMinutes = norm ? norm.technical_minutes + norm.paperwork_minutes : 80
  const defaultExpected = norm?.expected_service_minutes ?? safeMinutes
  const [expectedMinutes, setExpectedMinutes] = useState(defaultExpected)

  function changePriority(value: 'urgent' | 'normal') {
    setPriority(value)
    const nextNormId = value === 'urgent' ? 'tkd_emergency' : normId
    const nextNorm = normRows.find((row) => row.norm_id === nextNormId)
    const nextSafe = nextNorm ? nextNorm.technical_minutes + nextNorm.paperwork_minutes : 80
    setExpectedMinutes(nextNorm?.expected_service_minutes ?? nextSafe)
  }

  function changeNorm(value: string) {
    setNormId(value)
    const nextNorm = normRows.find((row) => row.norm_id === value)
    const nextSafe = nextNorm ? nextNorm.technical_minutes + nextNorm.paperwork_minutes : 80
    setExpectedMinutes(nextNorm?.expected_service_minutes ?? nextSafe)
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!location.coordinates || !locationReady) return
    const eventId = clientEventId()
    const workType = INCOMING_WORK_TYPES.find((item) => item.normId === normId)
    const urgent = priority === 'urgent'
    const effectiveNormId = urgent ? 'tkd_emergency' : normId
    onSubmit({
      event_id: eventId,
      idempotency_key: `${priority}-${eventId}`,
      event_time: `${planningDate}T${eventTime}:00+03:00`,
      expected_revision: revision,
      request: {
        id: `request-${eventId}`,
        external_id: `${urgent ? 'URG' : 'NEW'}-${Date.now().toString().slice(-6)}`,
        input_order: inputOrder,
        address: location.address.trim(),
        district: location.district.trim(),
        coordinates: location.coordinates,
        coordinate_source: location.coordinateSource,
        duration_minutes: safeMinutes,
        expected_duration_minutes: Math.min(expectedMinutes, safeMinutes),
        window_start: `${planningDate}T${windowStart}:00+03:00`,
        window_end: `${planningDate}T${windowEnd}:00+03:00`,
        required_skill: urgent ? 'emergency' : workType?.skill ?? 'local',
        required_transport: null,
        required_equipment: {},
        priority,
        completion_deadline: null,
        source_fields: {
          origin: 'dispatcher simulation form',
          coordinates_origin: location.origin,
          _enrichment: { norm_id: effectiveNormId },
        },
      },
    })
  }

  const reserve = Math.max(0, safeMinutes - expectedMinutes)
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-slate-950/40 p-4" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
      <section role="dialog" aria-modal="true" aria-labelledby="incoming-title" className="max-h-[92vh] w-full max-w-xl overflow-y-auto rounded-2xl bg-white p-6 shadow-2xl">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 id="incoming-title" className="text-lg font-semibold">Новая заявка в течение дня</h2>
            <p className="mt-1 text-sm text-muted">
              {normMode === 'soft'
                ? 'Авария имеет максимальный приоритет. Обычная заявка попадёт только в свободный интервал без сдвига обещанных визитов: сначала место без превышения норматива дороги или с наименьшим превышением, при равенстве — с минимальным дополнительным временем в пути.'
                : 'Авария имеет максимальный приоритет: остаток дня пересчитывается, начатые визиты и выезды не трогаются. Обычная заявка попадёт только в свободный интервал без сдвига обещанных визитов — туда, где меньше всего дополнительного времени в пути.'}
            </p>
          </div>
          <button aria-label="Закрыть" onClick={onClose} className="rounded p-2 text-slate-500 hover:bg-slate-100">×</button>
        </div>
        <form className="mt-5 space-y-4" onSubmit={submit}>
          <fieldset>
            <legend className="text-sm font-medium">Приоритет</legend>
            <div className="mt-2 grid grid-cols-2 gap-2">
              <button type="button" aria-pressed={priority === 'urgent'} onClick={() => changePriority('urgent')} className={`rounded-lg border px-3 py-2 text-left text-sm font-semibold ${priority === 'urgent' ? 'border-red-400 bg-red-50 text-red-800' : 'border-slate-200'}`}>
                <span className="inline-flex items-center gap-1"><EmergencyIcon />Аварийная</span>
                <span className="block text-xs font-normal opacity-80">как можно скорее</span>
              </button>
              <button type="button" aria-pressed={priority === 'normal'} onClick={() => changePriority('normal')} className={`rounded-lg border px-3 py-2 text-left text-sm font-semibold ${priority === 'normal' ? 'border-violet-400 bg-violet-50 text-violet-800' : 'border-slate-200'}`}>
                Обычная
                <span className="block text-xs font-normal opacity-80">в свободное окно</span>
              </button>
            </div>
          </fieldset>
          {priority === 'normal' && (
            <label className="block text-sm font-medium">Тип работ
              <select value={normId} onChange={(event) => changeNorm(event.target.value)} className="mt-1 w-full rounded-lg border px-3 py-2">
                {INCOMING_WORK_TYPES.map((item) => <option key={item.normId} value={item.normId}>{item.label}</option>)}
              </select>
            </label>
          )}
          <IncomingAddressField addresses={addresses} office={office} value={location} onChange={setEditedLocation} />
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <label className="text-sm font-medium">Поступила<input type="time" value={eventTime} onChange={(event) => setEventTime(event.target.value)} required className="mt-1 w-full rounded-lg border px-3 py-2" /></label>
            <label className="text-sm font-medium">Окно с<input type="time" value={windowStart} onChange={(event) => setWindowStart(event.target.value)} required className="mt-1 w-full rounded-lg border px-3 py-2" /></label>
            <label className="text-sm font-medium">Окно до<input type="time" value={windowEnd} onChange={(event) => setWindowEnd(event.target.value)} required className="mt-1 w-full rounded-lg border px-3 py-2" /></label>
          </div>
          <div className="grid gap-3 rounded-xl border border-amber-200 bg-amber-50 p-3 sm:grid-cols-2">
            <label className="text-sm font-medium">Ожидаемая работа, мин
              <input type="number" min={1} max={safeMinutes} value={expectedMinutes} onChange={(event) => setExpectedMinutes(Number(event.target.value))} className="mt-1 w-full rounded-lg border border-amber-300 px-3 py-2" />
            </label>
            <div className="text-sm">
              <div className="font-medium">Безопасный слот: {safeMinutes} мин</div>
              <div className="mt-1 text-amber-800">Защитный резерв: {reserve} мин</div>
              <div className="mt-1 text-xs text-muted">Решатель резервирует полный слот, поэтому следующие клиенты не получают риск опоздания.</div>
            </div>
          </div>
          {priority === 'urgent' && <div className="rounded-lg bg-red-50 p-3 text-sm text-red-800">Завершение аварии — не позднее 100 минут от поступления. Начатые выезды и работы закреплены, остальной хвост может быть перестроен.</div>}
          {error && <div role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</div>}
          <div className="flex justify-end gap-2">
            <button type="button" onClick={onClose} className="rounded-lg px-4 py-2 text-sm font-semibold text-slate-600 hover:bg-slate-100">Отмена</button>
            <Button type="submit" disabled={pending || !locationReady || expectedMinutes < 1 || expectedMinutes > safeMinutes}>{pending ? 'Пересчитываем…' : 'Добавить и пересчитать'}</Button>
          </div>
        </form>
      </section>
    </div>
  )
}

function RouteRow({
  route,
  planId,
  selected,
  load,
  emergencies,
  onClick,
}: {
  route: EngineerRoute
  planId: string
  selected: boolean
  load: number | null
  emergencies: number
  onClick: () => void
}) {
  return <div className={`mb-1 flex items-start gap-2 rounded-lg p-3 transition ${selected ? 'bg-violet-50 ring-1 ring-violet-200' : 'hover:bg-slate-50'}`}><button onClick={onClick} className="min-w-0 flex-1 text-left"><span className="flex min-w-0 items-center gap-2"><span className="truncate text-sm font-semibold">{route.engineer_name}</span>{emergencies > 0 && <span className="inline-flex shrink-0 items-center gap-0.5 rounded-full bg-red-100 px-1.5 py-0.5 text-[11px] font-semibold text-red-700" title={`Аварий в маршруте: ${emergencies}`}><EmergencyIcon className="h-3 w-3" />{emergencies}</span>}</span><span className="mt-1 block text-xs text-slate-600" data-route-transport={route.transport}>{transportText(route.transport, true)}</span><span className="mt-2 flex gap-3 text-xs text-muted"><span>{plural(route.stops.length, ['визит', 'визита', 'визитов'])}</span><span>до {time(route.finish_at)}</span></span>{load !== null && <span className="mt-2 flex items-center gap-2 text-[11px] text-muted" title="Работа и дорога ÷ длительность смены"><span className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-200"><span className="block h-full rounded-full bg-violet-500" style={{ width: `${load}%` }} /></span><span className="tabular-nums">загрузка {load} %</span></span>}</button><span className="flex shrink-0 flex-col items-end gap-1"><span className="text-xs text-muted">{km(route.distance_meters)}</span><Link to="/routes/$planId/$engineerId" params={{ planId, engineerId: route.engineer_id }} aria-label={`Открыть маршрут инженера ${route.engineer_name}`} title="Страница маршрута инженера" className="grid h-7 w-7 place-items-center rounded-md text-primary hover:bg-violet-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"><svg aria-hidden viewBox="0 0 24 24" className="h-4 w-4 fill-none stroke-current" strokeWidth="2"><path d="M10 13a5 5 0 0 0 7.1.1l2-2a5 5 0 0 0-7.1-7.1l-1.1 1.1" /><path d="M14 11a5 5 0 0 0-7.1-.1l-2 2A5 5 0 0 0 12 20l1.1-1.1" /></svg></Link></span></div>
}

function Tab({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return <button onClick={onClick} className={`whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium ${active ? 'border-primary text-primary' : 'border-transparent text-muted hover:text-ink'}`}>{children}</button>
}

function WorkClassBadge({ request }: { request: ServiceRequest }) {
  if (isEmergencyRequest(request)) {
    return <Badge tone="red"><span className="inline-flex items-center gap-1"><EmergencyIcon className="h-3 w-3" />Авария</span></Badge>
  }
  const workClass = requestWorkClass(request)
  return <Badge tone={workClassBadgeTone(workClass)}>{WORK_CLASS_LABELS[workClass]}</Badge>
}

function VisitReasonList({ title, reasons }: { title: string; reasons: VisitReason[] }) {
  return (
    <section aria-label={title}>
      <div className="text-xs font-semibold uppercase tracking-wide text-violet-700">{title}</div>
      <dl className="mt-2 grid gap-x-6 gap-y-1.5 text-sm md:grid-cols-2">
        {reasons.map((reason) => (
          <div key={reason.label} className="flex min-w-0 gap-2">
            <dt className={`w-28 shrink-0 font-medium ${reason.tone === 'note' ? 'text-slate-500' : 'text-slate-800'}`}>
              {reason.tone === 'ok' ? <span aria-hidden="true" className="text-emerald-600">✓ </span> : null}
              {reason.label}
            </dt>
            <dd className="min-w-0 text-slate-700">{reason.text}</dd>
          </div>
        ))}
      </dl>
      <p className="mt-2 text-xs text-muted">Каждое из этих условий заново проверил валидатор, прежде чем план был опубликован.</p>
    </section>
  )
}

function RouteSummaryBar({ name, summary, explanation }: { name: string; summary: RouteSummary; explanation: string }) {
  const items: Array<[string, string]> = [
    ['Визитов', String(summary.visits)],
    ['Пробег', kmText(summary.distanceMeters)],
    ['В пути', minutesText(summary.travelMinutes)],
    ['Работы', minutesText(summary.serviceMinutes)],
    ['Ожидание', summary.waitMinutes ? minutesText(summary.waitMinutes) : 'нет'],
    ['Выезд', clock(summary.departureAt)],
    ['Финиш', clock(summary.finishAt)],
  ]
  // Своё объяснение бэкенд пишет для корректировок: что защищено и что пересчитано.
  const extra = explanation.includes('возврат в стартовую точку') ? null : explanation
  return (
    <div className="mt-4 rounded-lg bg-slate-50 p-3" aria-label={`Сводка маршрута: ${name}`}>
      <dl className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
        {items.map(([label, value]) => (
          <div key={label}>
            <dt className="text-xs text-muted">{label}</dt>
            <dd className="font-semibold tabular-nums text-ink">{value}</dd>
          </div>
        ))}
      </dl>
      <p className="mt-2 text-xs text-muted">{extra ?? 'Возврат в офис после последнего визита не учитывается.'}</p>
    </div>
  )
}

function Schedule({
  route,
  requests,
  normMode,
  protectedIds,
}: {
  route?: EngineerRoute
  requests: ServiceRequest[]
  normMode: TravelNormMode
  protectedIds: Set<string>
}) {
  const [openId, setOpenId] = useState<string | null>(null)
  if (!route?.stops.length) return <p className="text-sm text-muted">Выберите бригаду в списке «Маршруты» — здесь появится её расписание.</p>
  const byId = new Map(requests.map((request) => [request.id, request]))
  return (
    <div>
      {/* relative: скрытый заголовок sr-only позиционирован абсолютно и без него
          вылезал бы из прокручиваемой таблицы, растягивая страницу на планшете */}
      <div className="relative overflow-x-auto">
        <table className="w-full min-w-[1000px] text-sm">
          <thead>
            <tr className="border-b text-left text-xs uppercase tracking-wide text-muted">
              <th className="pb-3 pr-2">№</th>
              <th className="pb-3 pr-2">Заявка</th>
              <th className="pb-3 pr-2">Адрес</th>
              <th className="pb-3 pr-2">Окно клиента</th>
              <th className="pb-3 pr-2">Прибытие</th>
              <th className="pb-3 pr-2">Ожидание</th>
              <th className="pb-3 pr-2">Работы</th>
              <th className="pb-3 pr-2">Дорога</th>
              <th className="pb-3"><span className="sr-only">Объяснение</span></th>
            </tr>
          </thead>
          <tbody>
            {route.stops.map((stop) => {
              const request = byId.get(stop.request_id)
              const emergency = request ? isEmergencyRequest(request) : false
              const expectedEnd = stop.expected_service_end_at ?? stop.service_end_at
              const open = openId === stop.request_id
              return (
                <Fragment key={stop.request_id}>
                  <tr className={`border-b ${emergency ? 'bg-red-50' : ''}`} data-emergency-row={emergency || undefined}>
                    <td className={`py-3 pr-2 font-semibold ${emergency ? 'border-l-4 border-red-500 pl-2 text-red-700' : 'text-primary'}`}>{stop.sequence}</td>
                    <td className="py-3 pr-2">
                      <div className="font-medium">{request?.external_id ?? stop.request_id}</div>
                      {request && <div className="mt-1"><WorkClassBadge request={request} /></div>}
                    </td>
                    <td className="max-w-xs py-3 pr-2">{request?.address}</td>
                    <td className="py-3 pr-2 tabular-nums">{request ? `${clock(request.window_start)}–${clock(request.window_end)}` : '—'}</td>
                    <td className="py-3 pr-2 tabular-nums">{clock(stop.arrival_at)}</td>
                    <td className="py-3 pr-2 tabular-nums">{stop.wait_minutes > 0 ? `${stop.wait_minutes} мин` : '—'}</td>
                    <td className="py-3 pr-2 tabular-nums">
                      {clock(stop.service_start_at)}–{clock(expectedEnd)}
                      {stop.reserve_minutes ? <div className="text-xs text-amber-700">{`в графике до ${clock(stop.service_end_at)}`}</div> : null}
                    </td>
                    <td className="py-3 pr-2"><TravelLegMinutes stop={stop} advisory={normMode === 'advisory'} /></td>
                    <td className="py-3 text-right">
                      <button
                        type="button"
                        aria-expanded={open}
                        onClick={() => setOpenId(open ? null : stop.request_id)}
                        className="whitespace-nowrap rounded-md px-2 py-1 text-xs font-semibold text-primary hover:bg-violet-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                      >
                        {open ? 'Скрыть' : 'Почему эта бригада'}
                      </button>
                    </td>
                  </tr>
                  {open && (
                    <tr className="border-b bg-violet-50/50">
                      <td colSpan={9} className="px-3 py-3">
                        <VisitReasonList
                          title={`Почему заявка ${request?.external_id ?? stop.request_id} назначена на «${route.engineer_name}»`}
                          reasons={visitReasons({ stop, request, route, normMode, protectedIds })}
                        />
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>
      <RouteSummaryBar name={route.engineer_name} summary={routeSummary(route)} explanation={route.explanation} />
    </div>
  )
}

function Comparison({
  plan,
  plans,
  algorithms,
  requests,
  normMode,
}: {
  plan: Plan
  plans: Plan[]
  algorithms: Algorithm[]
  requests: ServiceRequest[]
  normMode: TravelNormMode
}) {
  const engineerName = new Map(plan.result.routes.map((route) => [route.engineer_id, route.engineer_name]))
  const externalId = new Map(requests.map((request) => [request.id, request.external_id]))
  const placeText = (engineerId: string | null, sequence: number | null, missing: string) =>
    engineerId ? `${engineerName.get(engineerId) ?? engineerId}, ${sequence ?? '—'}-й по порядку` : missing
  const comparison = useQuery({
    queryKey: ['comparison', plan.id],
    queryFn: () => apiClient.comparison(plan.id),
    enabled: Boolean(plan.baseline_plan_id),
  })
  const diff = useQuery({
    queryKey: ['diff', plan.id, plan.parent_plan_id],
    queryFn: () => apiClient.diff(plan.id, plan.parent_plan_id!),
    enabled: Boolean(plan.parent_plan_id),
  })

  const value = comparison.data
  const baselineRun = plans.find((item) => item.id === plan.baseline_plan_id)
  const storedRuns = plans.filter(
    (item) => item.kind === 'algorithm_run' && item.baseline_plan_id === plan.baseline_plan_id,
  )
  const candidateRuns = storedRuns.length ? storedRuns : plan.parent_plan_id ? [plan] : []
  const runs = baselineRun ? [baselineRun, ...candidateRuns] : candidateRuns
  const expectedAlgorithmIds = algorithms.map((algorithm) => algorithm.id)
  const calculatedAlgorithmIds = new Set(runs.map(methodName))
  const missingAlgorithmIds = expectedAlgorithmIds.filter((id) => !calculatedAlgorithmIds.has(id))
  const distanceChange = value
    ? describeDistanceChange(value.distance_saving_meters, value.distance_saving_percent)
    : null
  const protectedCount = plan.model_info.protected_request_ids?.length ?? 0
  const experiment = plan.model_info.experiment
  const selection = plan.model_info.selection_reason
  const showReplanDiff = Boolean(plan.parent_plan_id) || isReplanPlan(plan.kind)
  const normalEvent = plan.model_info.event_kind === 'normal'
  const manualInsert = plan.kind === 'manual_insert'
  const changes = diff.data?.changes ?? []
  const visibleChanges = changes.slice(0, 12)

  return (
    <div className="space-y-4">
      {showReplanDiff && (
        <Card className="border-violet-200 bg-violet-50/60">
          <h3 className="text-base font-semibold text-violet-950">Что изменилось после корректировки</h3>
          <p className="mt-1 text-sm text-violet-900">
            {manualInsert
              ? 'Отказанная заявка назначена диспетчером вручную в свободный интервал; прежние визиты не сдвинуты.'
              : `Выполненные и начатые визиты сохранены (${protectedCount}), перестроена только ещё не начатая часть дня.`}
          </p>
          {diff.isLoading ? (
            <p className="mt-3 text-sm text-muted">Загружаем изменения относительно исходного плана…</p>
          ) : diff.data ? (
            <>
              <div className="mt-3 grid gap-2 sm:grid-cols-3">
                <div className="rounded-lg border border-violet-100 bg-white/80 p-3 text-sm">
                  <div className="text-xs uppercase text-muted">Сдвигов визитов</div>
                  <div className="mt-1 text-lg font-semibold">{diff.data.changes.length}</div>
                </div>
                <div className="rounded-lg border border-violet-100 bg-white/80 p-3 text-sm">
                  <div className="text-xs uppercase text-muted">Δ назначено</div>
                  <div className="mt-1 text-lg font-semibold">
                    {diff.data.assigned_delta > 0 ? `+${diff.data.assigned_delta}` : diff.data.assigned_delta}
                  </div>
                </div>
                <div className="rounded-lg border border-violet-100 bg-white/80 p-3 text-sm">
                  <div className="text-xs uppercase text-muted">Δ пробег</div>
                  <div className="mt-1 text-lg font-semibold">{km(diff.data.distance_delta_meters)}</div>
                </div>
              </div>
              {visibleChanges.length > 0 ? (
                <ul className="mt-3 space-y-1.5 text-sm text-slate-800">
                  {visibleChanges.map((change) => (
                    <li
                      key={`${change.request_id}-${change.previous_sequence}-${change.new_sequence}`}
                      className="rounded-md border border-violet-100 bg-white/70 px-3 py-2 text-xs sm:text-sm"
                    >
                      <span className="font-semibold text-violet-950">{`Заявка ${externalId.get(change.request_id) ?? change.request_id}`}</span>
                      <span className="text-muted">: </span>
                      <span>{placeText(change.previous_engineer_id, change.previous_sequence, 'не была назначена')}</span>
                      <span className="text-muted"> → </span>
                      <span>{placeText(change.new_engineer_id, change.new_sequence, 'без бригады')}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-3 text-sm text-slate-700">
                  Прежние визиты не сдвинуты: новая заявка встала в свободный интервал.
                </p>
              )}
              {changes.length > visibleChanges.length && (
                <p className="mt-2 text-xs text-muted">Показаны 12 из {changes.length} изменений.</p>
              )}
            </>
          ) : diff.isError ? (
            <p className="mt-3 text-sm text-rose-700">Не удалось загрузить изменения плана.</p>
          ) : null}
        </Card>
      )}

      {!plan.baseline_plan_id ? (
        <p className="text-sm text-muted">Для этой версии плана сравнение с базовым вариантом недоступно.</p>
      ) : comparison.isLoading ? (
        <p className="text-sm text-muted">Загружаем сравнение…</p>
      ) : comparison.isError ? (
        <div role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">
          {comparison.error.message}
        </div>
      ) : value ? (
        <>
          <div className="grid gap-3 md:grid-cols-4">
            <ComparisonMetric
              label="Назначено выбранным методом"
              baseline={value.baseline.assigned_count}
              selected={value.selected.assigned_count}
            />
            <ComparisonMetric
              label="Задействовано инженеров"
              baseline={value.baseline.used_engineers_count}
              selected={value.selected.used_engineers_count}
            />
            <ComparisonMetric
              label="Расчётный пробег выбранного метода"
              selected={km(value.selected.total_distance_meters)}
              note={`базовый вариант: ${km(value.baseline.total_distance_meters)}`}
            />
            <ComparisonMetric
              label={distanceChange?.label ?? 'Изменение пробега'}
              selected={distanceChange?.value}
              tone={distanceChange?.tone}
            />
          </div>

          {runs.length > 0 && (
            <>
              {!showReplanDiff && <div className={`rounded-lg border p-3 text-sm ${missingAlgorithmIds.length ? 'border-amber-200 bg-amber-50 text-amber-900' : 'border-green-200 bg-green-50 text-green-900'}`}>
                <strong>Методы в этом расчёте: {runs.length} из {expectedAlgorithmIds.length || runs.length}.</strong>{' '}
                {missingAlgorithmIds.length
                  ? `Не рассчитаны: ${missingAlgorithmIds.map((id) => algorithmTitle(id, algorithms)).join(', ')}. Нажмите «Сравнить все» и пересчитайте план.`
                  : 'Базовый вариант, эвристическая вставка и OR-Tools рассчитаны на одном снимке данных.'}
              </div>}
              <KpiCharts runs={runs} algorithms={algorithms} />
              <div className="overflow-x-auto rounded-lg border">
                <table className="w-full min-w-[1080px] text-sm">
                  <thead className="bg-slate-50 text-left text-xs uppercase text-muted">
                    <tr>
                      <th className="p-3">Метод</th>
                      <th>Назначено</th>
                      <th>Аварий без бригады</th>
                      <th>Бригад</th>
                      <th>Пробег</th>
                      <th>Км на заявку</th>
                      <th>Загрузка</th>
                      <th title="Коэффициент вариации нагрузки: чем меньше, тем ровнее">Равномерность, CV</th>
                      <th>Расчёт</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((run) => {
                      const kpis = run.model_info.kpis
                      return (
                        <tr key={run.id} className="border-t">
                          <td className="p-3 font-medium">
                            <span className="mr-2">{algorithmTitle(methodName(run), algorithms)}</span>
                            {Boolean(run.model_info.selected) && <Badge tone="green">выбран</Badge>}
                          </td>
                          <td>{run.metrics.assigned_count}</td>
                          <td>{run.metrics.emergency_unassigned_count}</td>
                          <td>{run.metrics.used_engineers_count}</td>
                          <td>{km(run.metrics.total_distance_meters)}</td>
                          <td>
                            {kpis?.distance_per_assigned_meters == null
                              ? '—'
                              : km(kpis.distance_per_assigned_meters)}
                          </td>
                          <td>{formatPercent(kpis?.used_engineer_utilization_percent)}</td>
                          <td>{kpis?.workload_cv?.toFixed(2) ?? '—'}</td>
                          <td>{run.result.elapsed_ms} мс</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </>
          )}

          {selection && planOrigin(plan) === 'solvers' && <SelectionExplanation selection={selection} algorithms={algorithms} />}

          {experiment && (
            <div className="rounded-lg bg-slate-50 p-3 text-xs text-slate-600">
              <strong>Воспроизводимость:</strong> снимок {experiment.snapshot_sha256.slice(0, 10)} · матрица{' '}
              {experiment.matrix_sha256.slice(0, 10)} · нормы {experiment.norm_versions.join(', ') || 'без версии'} ·
              лимит {experiment.time_limit_seconds} с.
            </div>
          )}

          <p className="rounded-lg bg-violet-50 p-3 text-sm text-violet-900">
            Сравнение выполнено на одном снимке: <strong>{value.same_snapshot ? 'да' : 'нет'}</strong>. Методы
            сравниваются по порядку: пропущенные аварии → пропущенные подключения → пропущенные ремонты и дозаказы
            → число задействованных инженеров → время в пути → расчётный пробег. Следующий показатель учитывается только при
            равенстве предыдущих.
          </p>

          <p className="text-xs text-muted">{value.note}</p>
        </>
      ) : null}

      {plan.parent_plan_id && (
        <div className="rounded-lg border p-3 text-sm">
          {manualInsert ? (
            <><strong>После ручного назначения:</strong> диспетчер выбрал бригаду и место в маршруте для отказанной заявки. Исполнители, порядок и время работ прежних визитов не изменились.</>
          ) : normalEvent ? (
            <><strong>После обычной новой заявки:</strong> существующие визиты защищены — {protectedCount}. Система ищет свободный интервал, не меняя исполнителей, порядок и обещанное время: {normMode === 'soft' ? 'сначала место без превышения норматива дороги или с наименьшим превышением, при равенстве — с минимальным приростом времени в пути.' : 'место с минимальным приростом времени в пути, при равенстве — пробега.'}</>
          ) : (
            <><strong>После аварийного события:</strong> защищено начатых/завершённых визитов — {protectedCount}, изменено заявок — {diff.data?.changes.length ?? '…'}, затронуто инженеров — {diff.data?.changed_engineer_ids.length ?? '…'}. Инженер завершает текущий выезд и работу; система меняет только ещё не начатую часть дня.</>
          )}
        </div>
      )}
    </div>
  )
}

function formatPercent(value: number | null | undefined) {
  return value == null ? '—' : `${value.toLocaleString('ru-RU', { maximumFractionDigits: 1 })}%`
}

function methodName(run: Plan) {
  return String(run.model_info.algorithm_id ?? run.result.algorithm)
}


function KpiCharts({ runs, algorithms }: { runs: Plan[]; algorithms: Algorithm[] }) {
  const completeRuns = runs.filter((run) => run.model_info.kpis)
  if (!completeRuns.length) return null
  const maxDistance = Math.max(...completeRuns.map((run) => run.metrics.total_distance_meters), 1)
  return <div className="grid gap-4 xl:grid-cols-2"><div className="rounded-lg border p-4"><h4 className="font-semibold">Охват заявок</h4><p className="mt-1 text-xs text-muted">Сколько заявок назначено из общего числа; срочные заявки показаны отдельным процентом.</p><div className="mt-4 space-y-4">{completeRuns.map((run) => { const kpis = run.model_info.kpis!; const total = run.metrics.assigned_count + run.metrics.unassigned_count; return <div key={run.id}><div className="mb-1 flex justify-between gap-3 text-xs"><span className="font-medium">{algorithmTitle(methodName(run), algorithms)}</span><span>{run.metrics.assigned_count} из {total} · {formatPercent(kpis.completion_rate_percent)} · срочные {formatPercent(kpis.urgent_completion_rate_percent)}</span></div><div className="h-3 overflow-hidden rounded-full bg-slate-100"><div className="h-full rounded-full bg-violet-500" style={{ width: `${Math.min(kpis.completion_rate_percent, 100)}%` }} /></div></div> })}</div></div><div className="rounded-lg border p-4"><h4 className="font-semibold">Структура времени маршрутов</h4><p className="mt-1 text-xs text-muted">Доли работы на объектах, дороги и ожидания только внутри построенных маршрутов.</p><div className="mt-4 space-y-4">{completeRuns.map((run) => { const kpis = run.model_info.kpis!; const total = Math.max(kpis.total_service_minutes + run.metrics.total_travel_minutes + kpis.total_wait_minutes, 1); return <div key={run.id}><div className="mb-1 flex justify-between gap-3 text-xs"><span className="font-medium">{algorithmTitle(methodName(run), algorithms)}</span><span>работа / доступные смены: {formatPercent(kpis.used_engineer_utilization_percent)}</span></div><div className="flex h-3 overflow-hidden rounded-full bg-slate-100"><div className="bg-emerald-500" style={{ width: `${kpis.total_service_minutes / total * 100}%` }} /><div className="bg-amber-400" style={{ width: `${run.metrics.total_travel_minutes / total * 100}%` }} /><div className="bg-slate-400" style={{ width: `${kpis.total_wait_minutes / total * 100}%` }} /></div></div> })}<div className="mt-3 flex gap-4 text-xs text-muted"><span className="text-emerald-700">● работа</span><span className="text-amber-600">● дорога</span><span className="text-slate-500">● ожидание</span></div></div></div><div aria-label="Сравнение охвата и расчётного пробега по методам" className="rounded-lg border p-4 xl:col-span-2"><h4 className="font-semibold">Охват и расчётный пробег по методам</h4><p className="mt-1 text-xs text-muted">Точные значения без скрытой шкалы. Сначала решатель максимизирует охват по классам заявок; время в пути и пробег сравниваются последними.</p><div className="mt-4 space-y-4">{completeRuns.map((run) => { const total = run.metrics.assigned_count + run.metrics.unassigned_count; const coverage = total ? run.metrics.assigned_count / total * 100 : 0; const distanceShare = run.metrics.total_distance_meters / maxDistance * 100; return <div key={run.id} className="grid gap-2 md:grid-cols-[minmax(180px,1fr)_minmax(240px,2fr)_minmax(240px,2fr)]"><div className="text-sm font-medium">{algorithmTitle(methodName(run), algorithms)}{Boolean(run.model_info.selected) && <span className="ml-2 text-xs text-green-700">выбран</span>}</div><div><div className="mb-1 flex justify-between text-xs"><span>Назначено</span><strong>{run.metrics.assigned_count} из {total} ({formatPercent(coverage)})</strong></div><div className="h-2 overflow-hidden rounded-full bg-slate-100"><div className="h-full rounded-full bg-violet-500" style={{ width: `${coverage}%` }} /></div></div><div><div className="mb-1 flex justify-between text-xs"><span>Расчётный пробег</span><strong>{km(run.metrics.total_distance_meters)}</strong></div><div className="h-2 overflow-hidden rounded-full bg-slate-100"><div className="h-full rounded-full bg-sky-500" style={{ width: `${distanceShare}%` }} /></div></div></div> })}</div></div></div>
}

function SelectionExplanation({ selection, algorithms }: { selection: NonNullable<Plan['model_info']['selection_reason']>; algorithms: Algorithm[] }) {
  const decisions = selectionDecisions(selection, algorithms)
  return (
    <div className="rounded-lg border border-green-200 bg-green-50 p-3 text-sm text-green-900">
      <strong>{`Почему выбран «${algorithmTitle(selection.selected_algorithm, algorithms)}»: `}</strong>
      {decisions.map((decision) => `против «${decision.competitor}» — ${decision.text}`).join('; ')}.
    </div>
  )
}

function ComparisonMetric({ label, baseline, selected, note, tone = 'neutral' }: { label: string; baseline?: number; selected: string | number | undefined; note?: string; tone?: 'positive' | 'negative' | 'neutral' }) {
  const toneClass = tone === 'positive' ? 'text-green-700' : tone === 'negative' ? 'text-red-700' : ''
  return <div className="rounded-lg bg-slate-50 p-4"><div className="text-xs text-muted">{label}</div><div className={`mt-1 font-semibold ${toneClass}`}>{selected ?? '—'}</div>{baseline !== undefined && <div className="mt-1 text-xs text-muted">базовый вариант: {baseline}</div>}{note && <div className="mt-1 text-xs text-muted">{note}</div>}</div>
}
