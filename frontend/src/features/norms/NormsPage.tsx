import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'

import { apiClient } from '../../shared/api'
import { useActiveDataset } from '../../shared/dataset-state'
import type { NormRow } from '../../shared/types'
import { Badge, Button, Card, EmptyState } from '../../shared/ui'

type EditableNorm = Pick<
  NormRow,
  'norm_id' | 'title' | 'travel_minutes' | 'technical_minutes' | 'paperwork_minutes' | 'expected_service_minutes'
>

export function NormsPage() {
  const queryClient = useQueryClient()
  const { dataset } = useActiveDataset()
  const [draft, setDraft] = useState<{ key: string; rows: EditableNorm[] }>({ key: '', rows: [] })
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const normsQuery = useQuery({
    queryKey: ['norms', dataset?.id],
    queryFn: () => apiClient.norms(dataset!.id),
    enabled: Boolean(dataset?.id),
  })

  const queryKey = `${dataset?.id ?? ''}:${normsQuery.data?.version ?? ''}`
  const rows = draft.key === queryKey
    ? draft.rows
    : normsQuery.data?.rows.map((row) => ({ ...row })) ?? []

  const mutation = useMutation({
    mutationFn: () => {
      if (!dataset || !normsQuery.data) throw new Error('Активный набор не выбран')
      return apiClient.patchNorms(dataset.id, {
        expected_revision: normsQuery.data.revision,
        rows: rows.map(({ norm_id, travel_minutes, technical_minutes, paperwork_minutes, expected_service_minutes }) => ({
          norm_id,
          travel_minutes,
          technical_minutes,
          paperwork_minutes,
          expected_service_minutes,
        })),
      })
    },
    onSuccess: async (result) => {
      setDraft({ key: `${dataset?.id ?? ''}:${result.version}`, rows: result.rows.map((row) => ({ ...row })) })
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['norms', dataset?.id] }),
        queryClient.invalidateQueries({ queryKey: ['datasets'] }),
        queryClient.invalidateQueries({ queryKey: ['dataset', dataset?.id] }),
        queryClient.invalidateQueries({ queryKey: ['requests', dataset?.id] }),
        queryClient.invalidateQueries({ queryKey: ['plans', dataset?.id] }),
      ])
      setMessage(`Нормативы сохранены. Обновлено заявок: ${result.affected_requests ?? 0}. Старые планы помечены как устаревшие.`)
      setError(null)
    },
    onError: (reason: Error) => {
      setMessage(null)
      setError(reason.message)
    },
  })

  function updateRow(index: number, field: keyof Omit<EditableNorm, 'norm_id' | 'title'>, value: number) {
    setDraft({
      key: queryKey,
      rows: rows.map((row, rowIndex) => (
        rowIndex === index ? { ...row, [field]: Number.isFinite(value) ? value : 0 } : row
      )),
    })
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    mutation.mutate()
  }

  if (!dataset) {
    return <EmptyState title="Нет активного набора" description="Выберите зону, чтобы настроить нормативы." />
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-ink">Нормативы</h1>
        <p className="mt-1 text-sm text-muted">
          Текущие нормы зоны «{dataset.service_zone_name ?? dataset.title}». Изменение повышает revision набора и применяется ко всем решателям.
        </p>
      </div>

      {(message || error) && (
        <div className={`rounded-xl border px-4 py-3 text-sm ${error ? 'border-red-200 bg-red-50 text-red-800' : 'border-emerald-200 bg-emerald-50 text-emerald-800'}`}>
          {error ?? message}
        </div>
      )}

      <Card className="p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold">Источник и правила расчёта</h2>
            <p className="mt-1 text-sm text-muted">
              Дорога ограничивает каждое плечо. Безопасный норматив (технические работы + документы) защищает следующие визиты от опоздания; ожидаемое время показывает вероятное раннее освобождение.
            </p>
          </div>
          {normsQuery.data && (
            <div className="flex flex-wrap gap-2">
              <Badge tone="violet">{normsQuery.data.version}</Badge>
              <Badge>{normsQuery.data.source_kind === 'customer_file' ? 'исходный файл' : 'настройки диспетчера'}</Badge>
            </div>
          )}
        </div>
        {normsQuery.data && (
          <div className="mt-3 rounded-lg bg-slate-50 p-3 text-xs text-slate-600">
            {normsQuery.data.source_name} · SHA-256 {normsQuery.data.source_sha256.slice(0, 16)}… · revision {normsQuery.data.revision}
          </div>
        )}
      </Card>

      <Card className="overflow-hidden">
        {normsQuery.isLoading ? (
          <p className="p-5 text-sm text-muted">Загрузка нормативов…</p>
        ) : (
          <form onSubmit={submit}>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-left text-sm">
                <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-600">
                  <tr>
                    <th className="p-3">Вид работы</th>
                    <th className="p-3">Дорога, мин</th>
                    <th className="p-3">Технические работы, мин</th>
                    <th className="p-3">Документы, мин</th>
                    <th className="p-3">Ожидаемо, мин</th>
                    <th className="p-3">Базовый норматив, мин</th>
                    <th className="p-3">Безопасно в решателе, мин</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, index) => {
                    const service = row.technical_minutes + row.paperwork_minutes
                    const base = row.travel_minutes + service
                    return (
                      <tr key={row.norm_id} className="border-t align-middle">
                        <td className="p-3 font-semibold text-ink">{row.title}</td>
                        {(['travel_minutes', 'technical_minutes', 'paperwork_minutes'] as const).map((field) => (
                          <td key={field} className="p-3">
                            <input
                              aria-label={`${row.title}: ${field}`}
                              type="number"
                              min={field === 'travel_minutes' ? 1 : 0}
                              max={field === 'travel_minutes' ? 240 : 720}
                              value={row[field]}
                              onChange={(event) => updateRow(index, field, event.currentTarget.valueAsNumber)}
                              className="w-28 rounded-lg border border-slate-300 px-3 py-2 focus:border-violet-500 focus:outline-none focus:ring-2 focus:ring-violet-100"
                            />
                          </td>
                        ))}
                        <td className="p-3">
                          <input
                            aria-label={`${row.title}: expected_service_minutes`}
                            type="number"
                            min={1}
                            max={service}
                            value={row.expected_service_minutes}
                            onChange={(event) => updateRow(index, 'expected_service_minutes', event.currentTarget.valueAsNumber)}
                            className="w-28 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 focus:border-amber-500 focus:outline-none focus:ring-2 focus:ring-amber-100"
                          />
                        </td>
                        <td className="p-3 font-semibold">{base}</td>
                        <td className="p-3 font-semibold text-violet-700">
                          {service}
                          {service > row.expected_service_minutes ? (
                            <span className="ml-1 text-xs font-normal text-amber-700">(+{service - row.expected_service_minutes} резерв)</span>
                          ) : null}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
            <div className="flex flex-wrap items-center justify-between gap-3 border-t bg-slate-50 px-5 py-4">
              <p className="max-w-2xl text-xs text-muted">
                После сохранения пересчитайте план. Все старые версии останутся доступны, но не будут считаться актуальными для нового revision.
              </p>
              <Button type="submit" disabled={mutation.isPending || rows.length !== 4}>
                {mutation.isPending ? 'Сохранение…' : 'Сохранить нормативы'}
              </Button>
            </div>
          </form>
        )}
      </Card>
    </div>
  )
}
