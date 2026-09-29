import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useId, useState, type KeyboardEvent } from 'react'

import { ApiError, apiClient } from '../../shared/api'
import type {
  EngineerRoute,
  Plan,
  RequestPlacement,
  ServiceRequest,
  UnassignedRequest,
} from '../../shared/types'
import { Badge, Button } from '../../shared/ui'
import { requestWorkClass, WORK_CLASS_LABELS, workClassBadgeTone, type WorkClass } from './request-kind'
import { NormExcessRoutes, NormExcessTotal } from './RouteTimeline'

// Порядок решения отказов повторяет цель решателя: аварии, подключения, затем ремонты
// и дозаказы (у решателя это один класс routine).
const WORK_CLASS_RANK: Record<WorkClass, number> = {
  emergency: 0,
  connection: 1,
  local: 2,
  add_order: 2,
}

const RANK_GROUPS = [
  { rank: 0, label: 'Аварии', tone: 'red' },
  { rank: 1, label: 'Подключения', tone: 'blue' },
  { rank: 2, label: 'Ремонты и дозаказы', tone: 'amber' },
] as const

const RANK_ACCENT: Record<number, string> = {
  0: 'border-l-red-500',
  1: 'border-l-blue-500',
  2: 'border-l-amber-400',
}

const RESOLVED_STATUS_LABELS = {
  rescheduled: 'Перенесена',
  cancelled: 'Отменена',
} as const

type ResolvedStatus = keyof typeof RESOLVED_STATUS_LABELS

// Что диспетчер может сделать при каждой причине отказа. Сама причина приходит с бэкенда.
const REASON_HINTS: Record<string, string> = {
  no_required_skill:
    'В зоне нет бригады с таким навыком. Передайте заявку соседней зоне или перенесите её.',
  no_required_transport:
    'Нет бригады с нужным транспортом. Уточните, обязателен ли он; иначе перенесите заявку.',
  no_required_equipment:
    'У подходящих бригад не хватает оборудования. Пополните запас или перенесите заявку.',
  no_feasible_time:
    'Визит не помещается в окно клиента. Согласуйте с клиентом другое время и перенесите заявку.',
  walking_leg_too_long:
    'Пешком до адреса не дойти. Проверьте ручное назначение бригаде на транспорте; если мест нет — перенесите.',
  bicycle_leg_too_long:
    'На велосипеде до адреса дальше 12 км. Проверьте ручное назначение бригаде на машине или ОТ; если мест нет — перенесите.',
  travel_norm_exceeded:
    'Дорога дольше норматива. Можно назначить вручную с превышением норматива или перенести заявку.',
  schedule_conflict:
    'Бригады заняты в это окно. Проверьте свободные интервалы через ручное назначение; если их нет — перенесите.',
  baseline_order_conflict:
    'Это ограничение последовательного baseline. Назначьте вручную или пересчитайте план другим методом.',
  not_selected_within_limit:
    'Решателю не хватило времени. Назначьте вручную или пересчитайте план.',
  preempted_by_urgent:
    'Заявка уступила место аварии. Назначьте её вручную в свободный интервал или перенесите.',
}

const DEFAULT_HINT =
  'Проверьте свободные интервалы через ручное назначение; если их нет — перенесите заявку.'

const VISIBLE_PLACEMENTS = 8

type Entry = {
  item: UnassignedRequest
  request: ServiceRequest | undefined
  workClass: WorkClass
  rank: number
  windowStart: string | null
  windowEnd: string | null
  resolvedStatus: ResolvedStatus | null
}

type Confirmation = { requestId: string; status: ResolvedStatus }

function time(value: string) {
  return new Intl.DateTimeFormat('ru-RU', {
    hour: '2-digit',
    minute: '2-digit',
    timeZone: 'Europe/Moscow',
  }).format(new Date(value))
}

function signedKm(meters: number) {
  const value = (Math.abs(meters) / 1000).toLocaleString('ru-RU', { maximumFractionDigits: 1 })
  return `${meters < 0 ? '−' : '+'}${value} км`
}

function signedMinutes(minutes: number) {
  return `${minutes < 0 ? '−' : '+'}${Math.abs(minutes)} мин`
}

function detailString(details: Record<string, unknown>, key: string): string | null {
  const value = details[key]
  return typeof value === 'string' && value ? value : null
}

function toEntry(item: UnassignedRequest, request: ServiceRequest | undefined): Entry {
  // Класс работ — только из requestWorkClass. Если заявки нет в списке (например, список
  // ещё грузится), собираем для неё минимальную карточку из деталей отказа.
  const skill = detailString(item.details, 'required_skill')
  const workClass = requestWorkClass(
    request ?? {
      priority: item.priority === 'urgent' ? 'urgent' : 'normal',
      source_fields: {},
      required_skill: skill === 'connection' || skill === 'emergency' ? skill : 'local',
    },
  )
  const status = request?.status
  return {
    item,
    request,
    workClass,
    rank: WORK_CLASS_RANK[workClass],
    windowStart: request?.window_start ?? detailString(item.details, 'window_start'),
    windowEnd: request?.window_end ?? detailString(item.details, 'window_end'),
    resolvedStatus: status === 'rescheduled' || status === 'cancelled' ? status : null,
  }
}

function windowEndMs(entry: Entry) {
  const value = entry.windowEnd ? Date.parse(entry.windowEnd) : Number.NaN
  return Number.isNaN(value) ? Number.POSITIVE_INFINITY : value
}

/** Аварии → подключения → ремонты; внутри класса выше тот, у кого раньше закрывается окно. */
function compareEntries(left: Entry, right: Entry) {
  if (left.rank !== right.rank) return left.rank - right.rank
  const leftEnd = windowEndMs(left)
  const rightEnd = windowEndMs(right)
  if (leftEnd !== rightEnd) return leftEnd < rightEnd ? -1 : 1
  return left.item.external_id.localeCompare(right.item.external_id, 'ru')
}

function positionText(position: number, route: EngineerRoute | undefined) {
  const stops = route?.stops ?? []
  const first = stops[0]
  const last = stops[stops.length - 1]
  if (!first || !last) return 'единственный визит бригады'
  if (position <= 0) return `первым, перед визитом ${first.sequence}`
  if (position >= stops.length) return `последним, после визита ${last.sequence}`
  const before = stops[position - 1]
  const after = stops[position]
  return `между визитами ${before?.sequence ?? position} и ${after?.sequence ?? position + 1}`
}

function placementKey(placement: Pick<RequestPlacement, 'engineer_id' | 'position'>) {
  return `${placement.engineer_id}:${placement.position}`
}

function actionErrorText(error: Error, action: 'assign' | 'status' = 'assign') {
  if (error instanceof ApiError) {
    if (error.code === 'stale_input') {
      // Перенос и отмена сверяют только ревизию набора: после обновления данных их можно
      // повторить сразу. Ручному назначению нужен план по текущей ревизии.
      return action === 'status'
        ? 'Набор успел измениться. Данные обновлены — повторите действие.'
        : 'Набор изменился после расчёта плана. Данные обновлены: пересчитайте план и повторите.'
    }
    if (error.code === 'stale_plan') {
      return 'У набора уже есть более новый план — он загружен, назначайте в нём.'
    }
  }
  return error.message
}

export function UnassignedPanel({
  plan,
  requests,
  datasetId,
  datasetRevision,
}: {
  plan: Plan
  requests: ServiceRequest[]
  datasetId: string
  datasetRevision: number
}) {
  const queryClient = useQueryClient()
  const [pickerRequestId, setPickerRequestId] = useState<string | null>(null)
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null)
  const [lastAction, setLastAction] = useState<string | null>(null)

  const byId = new Map(requests.map((request) => [request.id, request]))
  const entries = plan.result.unassigned
    .map((item) => toEntry(item, byId.get(item.request_id)))
    .sort(compareEntries)
  const openEntries = entries.filter((entry) => entry.resolvedStatus === null)
  const resolvedEntries = entries.filter((entry) => entry.resolvedStatus !== null)
  const stale = plan.input_revision !== datasetRevision
  const routesByEngineer = new Map(plan.result.routes.map((route) => [route.engineer_id, route]))

  async function refreshAfterChange() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['datasets'] }),
      queryClient.invalidateQueries({ queryKey: ['dataset', datasetId] }),
      queryClient.invalidateQueries({ queryKey: ['plans', datasetId] }),
      queryClient.invalidateQueries({ queryKey: ['requests', datasetId] }),
    ])
  }

  const assignMutation = useMutation({
    mutationFn: ({ entry, placement }: { entry: Entry; placement: RequestPlacement }) =>
      apiClient.assignRequest(plan.id, {
        request_id: entry.item.request_id,
        engineer_id: placement.engineer_id,
        position: placement.position,
        expected_revision: datasetRevision,
      }),
    onSuccess: async (_created, { entry, placement }) => {
      setPickerRequestId(null)
      setLastAction(
        `Заявка ${entry.item.external_id} назначена: ${placement.engineer_name}, прибытие ${time(placement.arrival_at)}. Создан новый план с ручным назначением.`,
      )
      await refreshAfterChange()
    },
    onError: async (error) => {
      if (error instanceof ApiError && error.status === 409) await refreshAfterChange()
    },
  })

  const statusMutation = useMutation({
    mutationFn: ({ entry, status }: { entry: Entry; status: ResolvedStatus }) =>
      apiClient.setRequestStatus(entry.item.request_id, {
        expected_revision: datasetRevision,
        status,
      }),
    onSuccess: async (_saved, { entry, status }) => {
      setConfirmation(null)
      setPickerRequestId(null)
      setLastAction(
        `Заявка ${entry.item.external_id}: ${RESOLVED_STATUS_LABELS[status].toLocaleLowerCase('ru-RU')}. Пересчитайте план, чтобы маршруты учли изменение.`,
      )
      await refreshAfterChange()
    },
    onError: async (error) => {
      if (error instanceof ApiError && error.status === 409) await refreshAfterChange()
    },
  })

  const busy = assignMutation.isPending || statusMutation.isPending
  const assignErrorFor = assignMutation.isError ? assignMutation.variables?.entry.item.request_id : null
  const statusErrorFor = statusMutation.isError ? statusMutation.variables?.entry.item.request_id : null

  if (!entries.length) {
    return (
      <div className="space-y-3">
        <div role="status" className="rounded-lg bg-green-50 p-4 text-sm text-green-800" data-unassigned-empty>
          <p className="font-semibold">Все заявки назначены — решений по отказам не требуется.</p>
          {lastAction && <p className="mt-1 text-green-900/80">{lastAction}</p>}
        </div>
        <NormExcessNote plan={plan} />
      </div>
    )
  }

  const counts = RANK_GROUPS.map((group) => ({
    ...group,
    count: openEntries.filter((entry) => entry.rank === group.rank).length,
  })).filter((group) => group.count > 0)

  return (
    <div className="space-y-4" data-unassigned-panel>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="max-w-3xl">
          <h3 className="font-semibold">Решения по отказам</h3>
          <p className="mt-1 text-xs text-muted">
            Сначала аварии, затем подключения, ремонты и дозаказы; внутри класса выше заявка, у
            которой раньше закрывается окно. Сначала назначайте вручную, потом переносите и
            отменяйте: перенос и отмена меняют ревизию набора, и дальше план нужно пересчитать.
          </p>
        </div>
        {counts.length > 0 && (
          <div className="flex flex-wrap gap-2" aria-label="Открытые отказы по классам">
            {counts.map((group) => (
              <Badge key={group.rank} tone={group.tone}>{`${group.label}: ${group.count}`}</Badge>
            ))}
          </div>
        )}
      </div>

      <NormExcessNote plan={plan} />

      {stale && (
        <div role="status" className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          {`План рассчитан по ревизии ${plan.input_revision}, набор уже на ревизии ${datasetRevision}. `}
          Ручное назначение станет доступно после пересчёта плана; перенос и отмена работают.
        </div>
      )}

      {lastAction && (
        <div role="status" className="rounded-lg border border-green-200 bg-green-50 p-3 text-sm text-green-900">
          {lastAction}
        </div>
      )}

      {openEntries.length ? (
        <ol className="space-y-3" aria-label="Отказанные заявки по срочности">
          {openEntries.map((entry) => {
            const requestId = entry.item.request_id
            return (
              <UnassignedCard
                key={requestId}
                entry={entry}
                planId={plan.id}
                routesByEngineer={routesByEngineer}
                stale={stale}
                busy={busy}
                pickerOpen={pickerRequestId === requestId}
                onTogglePicker={() => {
                  setConfirmation(null)
                  setPickerRequestId((current) => (current === requestId ? null : requestId))
                }}
                confirmStatus={confirmation?.requestId === requestId ? confirmation.status : null}
                onAskStatus={(status) => {
                  statusMutation.reset()
                  setPickerRequestId(null)
                  setConfirmation({ requestId, status })
                }}
                onCancelConfirm={() => setConfirmation(null)}
                onConfirmStatus={(status) => statusMutation.mutate({ entry, status })}
                statusPending={statusMutation.isPending && statusMutation.variables?.entry.item.request_id === requestId}
                onChoosePlacement={(placement) => {
                  setLastAction(null)
                  assignMutation.mutate({ entry, placement })
                }}
                pendingPlacementKey={
                  assignMutation.isPending && assignMutation.variables?.entry.item.request_id === requestId
                    ? placementKey(assignMutation.variables.placement)
                    : null
                }
                error={
                  assignErrorFor === requestId && assignMutation.error
                    ? actionErrorText(assignMutation.error)
                    : statusErrorFor === requestId && statusMutation.error
                      ? actionErrorText(statusMutation.error, 'status')
                      : null
                }
              />
            )
          })}
        </ol>
      ) : (
        <div role="status" className="rounded-lg bg-green-50 p-4 text-sm text-green-800">
          Открытых отказов нет: все отказанные заявки перенесены или отменены.
        </div>
      )}

      {resolvedEntries.length > 0 && <ResolvedList entries={resolvedEntries} />}
    </div>
  )
}

function UnassignedCard({
  entry,
  planId,
  routesByEngineer,
  stale,
  busy,
  pickerOpen,
  onTogglePicker,
  confirmStatus,
  onAskStatus,
  onCancelConfirm,
  onConfirmStatus,
  statusPending,
  onChoosePlacement,
  pendingPlacementKey,
  error,
}: {
  entry: Entry
  planId: string
  routesByEngineer: Map<string, EngineerRoute>
  stale: boolean
  busy: boolean
  pickerOpen: boolean
  onTogglePicker: () => void
  confirmStatus: ResolvedStatus | null
  onAskStatus: (status: ResolvedStatus) => void
  onCancelConfirm: () => void
  onConfirmStatus: (status: ResolvedStatus) => void
  statusPending: boolean
  onChoosePlacement: (placement: RequestPlacement) => void
  pendingPlacementKey: string | null
  error: string | null
}) {
  const pickerId = useId()
  const { item, request, workClass, rank, windowStart, windowEnd } = entry
  const hint = REASON_HINTS[item.reason_code] ?? DEFAULT_HINT
  return (
    <li
      className={`rounded-lg border border-l-4 border-slate-200 bg-white p-4 ${RANK_ACCENT[rank] ?? ''}`}
      data-unassigned-request={item.external_id}
      data-work-class={workClass}
      data-reason-code={item.reason_code}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-semibold">{item.external_id}</span>
            <Badge tone={workClassBadgeTone(workClass)}>{WORK_CLASS_LABELS[workClass]}</Badge>
          </div>
          {request?.address && <p className="mt-1 text-sm text-slate-700">{request.address}</p>}
        </div>
        {windowEnd && (
          <div className="shrink-0 text-right text-xs text-muted">
            <div>Окно клиента</div>
            <div className="text-sm font-semibold text-ink">
              {windowStart ? `${time(windowStart)}–` : 'до '}{time(windowEnd)}
            </div>
          </div>
        )}
      </div>

      <div className="mt-3 grid gap-2 text-sm md:grid-cols-2">
        <div className="rounded-md bg-amber-50 p-3 text-amber-900">
          <div className="text-xs font-semibold uppercase tracking-wide text-amber-700">Почему не назначена</div>
          <p className="mt-1">{item.explanation}</p>
        </div>
        <div className="rounded-md bg-slate-50 p-3 text-slate-800">
          <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">Что можно сделать</div>
          <p className="mt-1">{hint}</p>
        </div>
      </div>

      {confirmStatus ? (
        <StatusConfirmation
          externalId={item.external_id}
          status={confirmStatus}
          pending={statusPending}
          onConfirm={() => onConfirmStatus(confirmStatus)}
          onCancel={onCancelConfirm}
        />
      ) : (
        <div className="mt-3 flex flex-wrap gap-2" role="group" aria-label={`Действия с заявкой ${item.external_id}`}>
          <Button
            variant="secondary"
            onClick={onTogglePicker}
            disabled={busy || stale}
            aria-expanded={pickerOpen}
            aria-controls={pickerOpen ? pickerId : undefined}
            title={stale ? 'Сначала пересчитайте план: набор изменился после расчёта.' : undefined}
          >
            {pickerOpen ? 'Скрыть варианты' : 'Назначить вручную'}
          </Button>
          <Button variant="secondary" onClick={() => onAskStatus('rescheduled')} disabled={busy}>
            Перенести
          </Button>
          <button
            type="button"
            onClick={() => onAskStatus('cancelled')}
            disabled={busy}
            className="rounded-lg px-4 py-2.5 text-sm font-semibold text-red-700 ring-1 ring-red-200 transition hover:bg-red-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-600 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Отменить заявку
          </button>
        </div>
      )}

      {error && <div role="alert" className="mt-3 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</div>}

      {pickerOpen && !stale && (
        <div id={pickerId} className="mt-3">
          <PlacementPicker
            planId={planId}
            requestId={item.request_id}
            externalId={item.external_id}
            routesByEngineer={routesByEngineer}
            disabled={busy}
            pendingKey={pendingPlacementKey}
            onChoose={onChoosePlacement}
          />
        </div>
      )}
    </li>
  )
}

function StatusConfirmation({
  externalId,
  status,
  pending,
  onConfirm,
  onCancel,
}: {
  externalId: string
  status: ResolvedStatus
  pending: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  const titleId = useId()
  const descriptionId = useId()
  const cancel = status === 'cancelled'
  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape' && !pending) onCancel()
  }
  return (
    <div
      role="alertdialog"
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      onKeyDown={onKeyDown}
      className={`mt-3 rounded-lg border p-3 ${cancel ? 'border-red-200 bg-red-50' : 'border-violet-200 bg-violet-50'}`}
    >
      <p id={titleId} className={`font-semibold ${cancel ? 'text-red-900' : 'text-violet-950'}`}>
        {cancel ? `Отменить заявку ${externalId}?` : `Перенести заявку ${externalId} на другой день?`}
      </p>
      <div id={descriptionId} className="mt-1 space-y-1 text-sm text-slate-800">
        <p>
          {cancel
            ? 'Заявка получит статус «Отменена» и выйдет из планирования. В пределах этого дня отмену не вернуть.'
            : 'Заявка получит статус «Перенесена» и выйдет из планирования этого дня. Новую дату согласуйте с клиентом.'}
        </p>
        <p className="text-xs text-muted">
          Набор получит новую ревизию: ручное назначение других отказов станет доступно после пересчёта плана.
        </p>
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button variant={cancel ? 'danger' : 'primary'} onClick={onConfirm} disabled={pending}>
          {pending ? 'Сохраняем…' : cancel ? 'Да, отменить заявку' : 'Да, перенести'}
        </Button>
        <button
          type="button"
          autoFocus
          onClick={onCancel}
          disabled={pending}
          className="rounded-lg px-4 py-2.5 text-sm font-semibold text-slate-700 hover:bg-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:cursor-not-allowed disabled:opacity-50"
        >
          Не менять
        </button>
      </div>
    </div>
  )
}

function PlacementPicker({
  planId,
  requestId,
  externalId,
  routesByEngineer,
  disabled,
  pendingKey,
  onChoose,
}: {
  planId: string
  requestId: string
  externalId: string
  routesByEngineer: Map<string, EngineerRoute>
  disabled: boolean
  pendingKey: string | null
  onChoose: (placement: RequestPlacement) => void
}) {
  // План иммутабелен, поэтому варианты для пары «план, заявка» не устаревают.
  const placementsQuery = useQuery({
    queryKey: ['placements', planId, requestId],
    queryFn: () => apiClient.placements(planId, requestId),
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
  })
  if (placementsQuery.isPending) {
    return <p role="status" className="text-sm text-muted">Ищем свободные интервалы в маршрутах бригад…</p>
  }
  if (placementsQuery.isError) {
    return (
      <div role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">
        {actionErrorText(placementsQuery.error)}
      </div>
    )
  }
  return (
    <PlacementOptions
      placements={placementsQuery.data.placements}
      externalId={externalId}
      routesByEngineer={routesByEngineer}
      disabled={disabled}
      pendingKey={pendingKey}
      onChoose={onChoose}
    />
  )
}

/** Варианты ручной вставки: бригада, место, прибытие, прирост пути и предупреждение о нормативе. */
export function PlacementOptions({
  placements,
  externalId,
  routesByEngineer,
  disabled = false,
  pendingKey = null,
  onChoose,
}: {
  placements: RequestPlacement[]
  externalId: string
  routesByEngineer: Map<string, EngineerRoute>
  disabled?: boolean
  pendingKey?: string | null
  onChoose: (placement: RequestPlacement) => void
}) {
  const [showAll, setShowAll] = useState(false)
  if (!placements.length) {
    return (
      <div role="status" className="rounded-lg border border-slate-200 bg-slate-50 p-3 text-sm text-slate-700">
        Свободного интервала без сдвига чужих визитов нет ни у одной бригады. Перенесите заявку или
        пересчитайте план.
      </div>
    )
  }
  const visible = showAll ? placements : placements.slice(0, VISIBLE_PLACEMENTS)
  const exceededCount = placements.filter((placement) => placement.norm_exceeded).length
  return (
    <div className="space-y-2">
      <p className="text-xs text-muted">
        Вариантов: {placements.length}
        {exceededCount > 0 ? `, из них с превышением норматива дороги: ${exceededCount}` : ''}. Прежние
        визиты не сдвигаются; порядок — по приросту времени в пути, при равенстве — пробега.
      </p>
      <ul className="space-y-2" aria-label={`Варианты назначения заявки ${externalId}`}>
        {visible.map((placement) => {
          const where = positionText(placement.position, routesByEngineer.get(placement.engineer_id))
          const exceeded = placement.norm_exceeded
          const key = placementKey(placement)
          return (
            <li
              key={key}
              className={`rounded-lg border p-3 ${exceeded ? 'border-red-200 bg-red-50/60' : 'border-slate-200 bg-white'}`}
              data-placement-norm-exceeded={String(exceeded)}
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="font-semibold">{placement.engineer_name}</div>
                  <div className="text-xs text-muted">{where}</div>
                  <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-4">
                    <div>
                      <dt className="text-muted">Прибытие</dt>
                      <dd className="font-medium text-ink">{time(placement.arrival_at)}</dd>
                    </div>
                    <div>
                      <dt className="text-muted">Работа</dt>
                      <dd className="font-medium text-ink">
                        {time(placement.service_start_at)}–{time(placement.service_end_at)}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-muted">Прирост пробега</dt>
                      <dd className="font-medium text-ink">{signedKm(placement.added_distance_meters)}</dd>
                    </div>
                    <div>
                      <dt className="text-muted">Прирост дороги</dt>
                      <dd className="font-medium text-ink">{signedMinutes(placement.added_travel_minutes)}</dd>
                    </div>
                  </dl>
                  {exceeded && (
                    <p className="mt-2 flex items-start gap-1.5 text-sm font-semibold text-red-800" data-norm-warning>
                      <span aria-hidden="true">⚠</span>
                      <span>Нарушает норматив дороги: плечо длиннее допустимого, визит попадёт в нарушения.</span>
                    </p>
                  )}
                </div>
                <Button
                  variant={exceeded ? 'danger' : 'primary'}
                  onClick={() => onChoose(placement)}
                  disabled={disabled}
                  aria-label={`Назначить заявку ${externalId}: ${placement.engineer_name}, ${where}${exceeded ? ', с превышением норматива дороги' : ''}`}
                >
                  {pendingKey === key ? 'Назначаем…' : exceeded ? 'Назначить с превышением' : 'Назначить'}
                </Button>
              </div>
            </li>
          )
        })}
      </ul>
      {placements.length > VISIBLE_PLACEMENTS && (
        <button
          type="button"
          onClick={() => setShowAll((value) => !value)}
          aria-expanded={showAll}
          className="rounded-md px-2 py-1 text-sm font-semibold text-primary hover:bg-violet-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
        >
          {showAll ? 'Показать первые варианты' : `Показать все варианты (${placements.length})`}
        </button>
      )}
    </div>
  )
}

/** Превышения норматива дороги в назначенных визитах: сколько, где отмечены и у каких бригад. */
function NormExcessNote({ plan }: { plan: Plan }) {
  if ((plan.metrics.norm_violation_count ?? 0) <= 0) return null
  return (
    <section
      aria-label="Превышения норматива дороги"
      className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-900"
      data-norm-excess-note
    >
      <NormExcessTotal metrics={plan.metrics} className="text-sm" />
      <p className="mt-1">
        Плечи сверх норматива подписаны «норматив …, превышение +… мин» в линейном дереве
        маршрута над картой и в столбце «Дорога» таблицы расписания. Откройте бригаду в
        списке «Маршруты».
      </p>
      <NormExcessRoutes routes={plan.result.routes} />
    </section>
  )
}

function ResolvedList({ entries }: { entries: Entry[] }) {
  const headingId = useId()
  return (
    <section aria-labelledby={headingId} className="rounded-lg border border-slate-200 bg-slate-50 p-3">
      <h4 id={headingId} className="text-sm font-semibold">Решено после расчёта плана ({entries.length})</h4>
      <p className="mt-1 text-xs text-muted">
        Эти заявки вышли из планирования. Пересчитайте план, чтобы маршруты и счётчики учли изменения.
      </p>
      <ul className="mt-2 space-y-1 text-sm">
        {entries.map((entry) => (
          <li key={entry.item.request_id} className="flex flex-wrap items-center gap-2" data-resolved-request={entry.item.external_id}>
            <span className="font-medium">{entry.item.external_id}</span>
            {entry.resolvedStatus && (
              <Badge tone={entry.resolvedStatus === 'cancelled' ? 'red' : 'slate'}>
                {RESOLVED_STATUS_LABELS[entry.resolvedStatus]}
              </Badge>
            )}
            {entry.request?.address && <span className="min-w-0 truncate text-muted">{entry.request.address}</span>}
          </li>
        ))}
      </ul>
    </section>
  )
}
