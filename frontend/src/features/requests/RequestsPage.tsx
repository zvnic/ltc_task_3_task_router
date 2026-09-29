import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearch } from '@tanstack/react-router'
import { createColumnHelper, flexRender, getCoreRowModel, useReactTable } from '@tanstack/react-table'
import { useMemo, useState, type FormEvent } from 'react'

import { apiClient } from '../../shared/api'
import { useActiveDataset } from '../../shared/dataset-state'
import type { RequestStatus, ServiceRequest } from '../../shared/types'
import { Badge, Button, Card, EmptyState } from '../../shared/ui'
import {
  requestWorkClass,
  transportLabel,
  WORK_CLASS_LABELS,
  workClassBadgeTone,
} from '../planning/request-kind'

const skillLabels = { local: 'Локальные', connection: 'Подключение', emergency: 'Аварийные' } as const
const statusLabels: Record<RequestStatus, string> = {
  new: 'Новая',
  assigned: 'Направлена',
  en_route: 'В пути',
  arrived: 'На месте',
  in_progress: 'В работе',
  completed: 'Выполнена',
  issue: 'Проблема',
  rescheduled: 'Перенесена',
  cancelled: 'Отменена',
}
const column = createColumnHelper<ServiceRequest>()
const columns = [
  column.accessor('external_id', { header: 'ID' }),
  column.accessor('address', {
    header: 'Адрес',
    cell: (info) => <span className="block min-w-72">{info.getValue()}</span>,
  }),
  column.accessor('district', { header: 'Район' }),
  column.accessor('required_skill', {
    header: 'Навык',
    cell: (info) => skillLabels[info.getValue()],
  }),
  column.display({
    id: 'work_class',
    header: 'Класс',
    cell: (info) => {
      const request = info.row.original
      const workClass = requestWorkClass(request)
      const transport = transportLabel(request.required_transport)
      return (
        <div className="flex flex-wrap items-center gap-1">
          <Badge tone={workClassBadgeTone(workClass)}>{WORK_CLASS_LABELS[workClass]}</Badge>
          {transport ? <Badge tone="gray">{transport}</Badge> : null}
        </div>
      )
    },
  }),
  column.accessor('window_start', {
    header: 'Окно',
    cell: (info) =>
      new Date(info.getValue()).toLocaleTimeString('ru-RU', {
        hour: '2-digit',
        minute: '2-digit',
        timeZone: 'Europe/Moscow',
      }),
  }),
  column.accessor('duration_minutes', {
    header: 'Работа / слот',
    cell: (info) => {
      const request = info.row.original
      const expected = request.expected_duration_minutes ?? request.duration_minutes
      return expected < request.duration_minutes
        ? <span>{expected} мин <span className="text-xs text-amber-700">/ {request.duration_minutes} мин</span></span>
        : `${request.duration_minutes} мин`
    },
  }),
  column.accessor('priority', {
    header: 'Приоритет',
    cell: (info) => (
      <Badge tone={info.getValue() === 'urgent' ? 'red' : 'slate'}>
        {info.getValue() === 'urgent' ? 'Срочная' : 'Обычная'}
      </Badge>
    ),
  }),
  column.accessor('status', {
    header: 'Статус',
    cell: (info) => statusLabels[info.getValue()],
  }),
  column.accessor('assigned_engineer_name', {
    header: 'Назначение',
    cell: (info) => info.getValue() ?? <span className="text-amber-700">Не назначена</span>,
  }),
  column.accessor('eta', {
    header: 'ETA',
    cell: (info) =>
      info.getValue()
        ? new Date(info.getValue()!).toLocaleTimeString('ru-RU', {
            hour: '2-digit',
            minute: '2-digit',
            timeZone: 'Europe/Moscow',
          })
        : '—',
  }),
]

type RequestsSearch = {
  q: string
  district: string
  priority: string
  request: string
}

export function RequestsPage() {
  const queryClient = useQueryClient()
  const search = useSearch({ strict: false }) as RequestsSearch
  const navigate = useNavigate()
  const { dataset } = useActiveDataset()

  function patchSearch(patch: Partial<RequestsSearch>) {
    void navigate({
      to: '/requests',
      // TanStack Router search updater typing is overly strict without generated route tree.
      search: ((previous: RequestsSearch) => ({ ...previous, ...patch })) as never,
      replace: true,
    })
  }

  const requests = useQuery({
    queryKey: ['requests', dataset?.id, search.q, search.district, search.priority],
    queryFn: () =>
      apiClient.requests(dataset!.id, {
        search: search.q,
        district: search.district,
        priority: search.priority,
      }),
    enabled: Boolean(dataset),
  })
  const selected = requests.data?.items.find((item) => item.id === search.request)
  const districts = useMemo(
    () => [...new Set((requests.data?.items ?? []).map((item) => item.district))].sort(),
    [requests.data],
  )
  const table = useReactTable({
    data: requests.data?.items ?? [],
    columns,
    getCoreRowModel: getCoreRowModel(),
  })

  if (!dataset) {
    return <EmptyState title="Нет набора" description="Подготовьте демоданные командой make seed." />
  }

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-xl font-semibold">Заявки · {dataset.service_zone_name ?? dataset.title}</h2>
        <p className="text-sm text-muted">
          {requests.data?.total ?? 0} заявок · сортировка интерфейса не меняет входной порядок
        </p>
      </div>
      <Card className="p-3">
        <div className="grid gap-2 md:grid-cols-[1fr_220px_180px]">
          <input
            aria-label="Поиск заявок"
            value={search.q}
            onChange={(event) => patchSearch({ q: event.target.value, request: '' })}
            placeholder="Поиск ID или адреса"
            className="rounded-lg border px-3 py-2 text-sm"
          />
          <select
            aria-label="Район"
            value={search.district}
            onChange={(event) => patchSearch({ district: event.target.value, request: '' })}
            className="rounded-lg border px-3 py-2 text-sm"
          >
            <option value="">Все районы</option>
            {districts.map((district) => (
              <option key={district}>{district}</option>
            ))}
          </select>
          <select
            aria-label="Приоритет"
            value={search.priority}
            onChange={(event) => patchSearch({ priority: event.target.value, request: '' })}
            className="rounded-lg border px-3 py-2 text-sm"
          >
            <option value="">Все приоритеты</option>
            <option value="normal">Обычные</option>
            <option value="urgent">Срочные</option>
          </select>
        </div>
      </Card>
      <Card className="overflow-auto">
        <table className="min-w-full text-sm">
          <thead className="bg-slate-50 text-left text-muted">
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id}>
                {headerGroup.headers.map((header) => (
                  <th key={header.id} className="px-3 py-2 font-medium">
                    {flexRender(header.column.columnDef.header, header.getContext())}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map((row) => (
              <tr
                key={row.id}
                className={`cursor-pointer border-t hover:bg-violet-50 ${search.request === row.original.id ? 'bg-violet-50' : ''}`}
                onClick={() => patchSearch({ request: row.original.id })}
              >
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id} className="px-3 py-2 align-top">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      {selected && (
        <RequestDrawer
          request={selected}
          revision={dataset.revision}
          onClose={() => patchSearch({ request: '' })}
          onSaved={async () => {
            await queryClient.invalidateQueries({ queryKey: ['requests'] })
            await queryClient.invalidateQueries({ queryKey: ['dataset'] })
            await queryClient.invalidateQueries({ queryKey: ['datasets'] })
          }}
        />
      )}
    </div>
  )
}

function RequestDrawer({
  request,
  revision,
  onClose,
  onSaved,
}: {
  request: ServiceRequest
  revision: number
  onClose: () => void
  onSaved: () => Promise<void>
}) {
  const [duration, setDuration] = useState(request.duration_minutes)
  const [priority, setPriority] = useState<'normal' | 'urgent'>(request.priority)
  const [status, setStatus] = useState<RequestStatus>(request.status)
  const mutation = useMutation({
    mutationFn: () =>
      apiClient.patchRequest(request.id, {
        expected_revision: revision,
        duration_minutes: duration,
        priority,
        status,
      }),
    onSuccess: onSaved,
  })
  function submit(event: FormEvent) {
    event.preventDefault()
    mutation.mutate()
  }
  return (
    <div
      className="fixed inset-0 z-50 bg-slate-950/30"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={`Заявка ${request.external_id}`}
        className="absolute inset-y-0 right-0 w-full max-w-md overflow-y-auto bg-white p-6 shadow-2xl"
      >
        <div className="flex items-start justify-between">
          <div>
            <div className="text-xs font-semibold uppercase tracking-wide text-primary">Заявка</div>
            <h2 className="mt-1 text-xl font-semibold">{request.external_id}</h2>
          </div>
          <button onClick={onClose} aria-label="Закрыть" className="rounded p-2 text-xl text-muted hover:bg-slate-100">
            ×
          </button>
        </div>
        <dl className="mt-6 space-y-4 text-sm">
          <div>
            <dt className="text-muted">Адрес</dt>
            <dd className="mt-1 font-medium">{request.address}</dd>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <dt className="text-muted">Навык</dt>
              <dd className="font-medium">{skillLabels[request.required_skill]}</dd>
            </div>
            <div>
              <dt className="text-muted">Исполнитель</dt>
              <dd className="font-medium">{request.assigned_engineer_name ?? 'Не назначен'}</dd>
            </div>
            <div>
              <dt className="text-muted">Класс работ</dt>
              <dd className="mt-1 font-medium">
                <Badge tone={workClassBadgeTone(requestWorkClass(request))}>
                  {WORK_CLASS_LABELS[requestWorkClass(request)]}
                </Badge>
              </dd>
            </div>
            <div>
              <dt className="text-muted">Транспорт</dt>
              <dd className="font-medium">
                {request.required_transport
                  ? (transportLabel(request.required_transport) ?? request.required_transport)
                  : '—'}
              </dd>
            </div>
          </div>
          {request.unassigned_reason && (
            <div className="rounded-lg bg-amber-50 p-3 text-amber-900">
              <dt className="font-semibold">Причина</dt>
              <dd className="mt-1">{request.unassigned_reason}</dd>
            </div>
          )}
        </dl>
        <form className="mt-6 space-y-4 border-t pt-5" onSubmit={submit}>
          <h3 className="font-semibold">Редактирование исходных данных</h3>
          <label className="block text-sm font-medium">
            Длительность, мин
            <input
              type="number"
              min={1}
              max={720}
              value={duration}
              onChange={(event) => setDuration(Number(event.target.value))}
              className="mt-1 w-full rounded-lg border px-3 py-2"
            />
          </label>
          <label className="block text-sm font-medium">
            Приоритет
            <select
              value={priority}
              onChange={(event) => setPriority(event.target.value as 'normal' | 'urgent')}
              className="mt-1 w-full rounded-lg border px-3 py-2"
            >
              <option value="normal">Обычная</option>
              <option value="urgent">Срочная</option>
            </select>
          </label>
          <label className="block text-sm font-medium">
            Статус
            <select
              value={status}
              onChange={(event) => setStatus(event.target.value as RequestStatus)}
              className="mt-1 w-full rounded-lg border px-3 py-2"
            >
              {Object.entries(statusLabels).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </label>
          {request.plan_is_stale && (
            <p className="text-xs text-amber-700">Текущий план уже построен по предыдущей revision.</p>
          )}
          {mutation.isError && (
            <div role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">
              {mutation.error.message}
            </div>
          )}
          <Button type="submit" disabled={mutation.isPending}>
            {mutation.isPending ? 'Сохраняем…' : 'Сохранить'}
          </Button>
        </form>
      </aside>
    </div>
  )
}
