import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState, type FormEvent } from 'react'
import { apiClient } from '../../shared/api'
import type { ImportPreview } from '../../shared/api'
import { useActiveDataset } from '../../shared/dataset-state'
import { Badge, Button, Card, EmptyState } from '../../shared/ui'
import { canSkipDuplicateRows, importIssueText } from './import-preview'

export function DataPage() {
  const queryClient = useQueryClient()
  const { dataset: activeDataset, selectDataset, datasetsQuery } = useActiveDataset()
  const [file, setFile] = useState<File | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [importPreview, setImportPreview] = useState<ImportPreview | null>(null)

  const detailQuery = useQuery({
    queryKey: ['dataset', activeDataset?.id],
    queryFn: () => apiClient.dataset(activeDataset!.id),
    enabled: Boolean(activeDataset?.id),
  })

  const importMutation = useMutation({
    mutationFn: async ({ skipDuplicateRows }: { skipDuplicateRows: boolean }) => {
      if (!file) throw new Error('Выберите CSV-файл')
      const title = file.name.replace(/\.csv$/i, '').trim() || 'Импорт'
      return apiClient.commitImport(file, title, skipDuplicateRows)
    },
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: ['datasets'] })
      selectDataset(result.dataset_id)
      setFile(null)
      setImportPreview(null)
      const accepted = result.import_report?.valid_count
      const skippedDuplicates = result.import_report?.skipped_duplicate_rows
      const acceptedText = typeof accepted === 'number' ? `, принято строк: ${accepted}` : ''
      const skippedText = typeof skippedDuplicates === 'number' && skippedDuplicates > 0
        ? `, пропущено дубликатов: ${skippedDuplicates}`
        : ''
      setMessage(
        `Импорт «${result.title}» (rev ${result.revision})${acceptedText}${skippedText}`,
      )
      setError(null)
    },
    onError: (err: Error) => {
      setMessage(null)
      setError(err.message)
    },
  })

  const previewMutation = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error('Выберите CSV-файл')
      return apiClient.previewImport(file)
    },
    onSuccess: (preview) => {
      setImportPreview(preview)
      setMessage(null)
      setError(null)
      if (preview.errors.length === 0) {
        importMutation.mutate({ skipDuplicateRows: false })
      }
    },
    onError: (err: Error) => {
      setImportPreview(null)
      setMessage(null)
      setError(err.message)
    },
  })

  const deleteDatasetMutation = useMutation({
    mutationFn: (id: string) => apiClient.deleteDataset(id),
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: ['datasets'] })
      await queryClient.invalidateQueries({ queryKey: ['dataset'] })
      await queryClient.invalidateQueries({ queryKey: ['plans'] })
      const remaining = (datasetsQuery.data ?? []).filter((item) => item.id !== result.dataset_id)
      if (remaining[0]) selectDataset(remaining[0].id)
      else selectDataset('')
      setMessage(
        `Удалён набор «${result.title}»: заявок ${result.deleted.requests}, инженеров ${result.deleted.engineers}, планов ${result.deleted.plans}`,
      )
      setError(null)
    },
    onError: (err: Error) => {
      setMessage(null)
      setError(err.message)
    },
  })

  const deleteZoneMutation = useMutation({
    mutationFn: (zoneCode: string) => apiClient.deleteZone(zoneCode),
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: ['datasets'] })
      await queryClient.invalidateQueries({ queryKey: ['dataset'] })
      await queryClient.invalidateQueries({ queryKey: ['plans'] })
      const deletedIds = new Set(result.deleted_datasets.map((item) => item.dataset_id))
      const remaining = (datasetsQuery.data ?? []).filter((item) => !deletedIds.has(item.id))
      if (remaining[0]) selectDataset(remaining[0].id)
      else selectDataset('')
      setMessage(
        `Зона «${result.zone_title}»: удалено наборов ${result.count}. Можно заново импортировать CSV.`,
      )
      setError(null)
    },
    onError: (err: Error) => {
      setMessage(null)
      setError(err.message)
    },
  })

  const busy =
    previewMutation.isPending
    || importMutation.isPending
    || deleteDatasetMutation.isPending
    || deleteZoneMutation.isPending

  const assumptionsPreview = useMemo(() => {
    const assumptions = detailQuery.data?.assumptions
    if (!assumptions || typeof assumptions !== 'object') return [] as Array<[string, string]>
    return Object.entries(assumptions as Record<string, unknown>)
      .slice(0, 12)
      .map(([key, value]) => [
        key,
        typeof value === 'string' ? value : JSON.stringify(value),
      ] as [string, string])
  }, [detailQuery.data?.assumptions])

  const zoneCode = activeDataset?.service_zone ?? null
  const zoneName = activeDataset?.service_zone_name ?? zoneCode ?? 'зона'
  const zoneDatasetCount = useMemo(() => {
    if (!zoneCode) return 0
    return (datasetsQuery.data ?? []).filter((item) => item.service_zone === zoneCode).length
  }, [datasetsQuery.data, zoneCode])

  function onImport(event: FormEvent) {
    event.preventDefault()
    previewMutation.mutate()
  }

  function onFileChange(nextFile: File | null) {
    setFile(nextFile)
    setImportPreview(null)
    setMessage(null)
    setError(null)
  }

  function onDeleteDataset() {
    const dataset = activeDataset
    if (!dataset) return
    const ok = window.confirm(
      `Удалить набор «${dataset.title}»?\n\nБудут удалены все заявки, инженеры, планы и события этого снимка. Действие необратимо.\nПосле удаления можно заново импортировать CSV.`,
    )
    if (!ok) return
    deleteDatasetMutation.mutate(dataset.id)
  }

  function onDeleteZone() {
    if (!zoneCode) return
    const ok = window.confirm(
      `Удалить ВСЕ наборы зоны «${zoneName}» (${zoneCode})?\n\nБудут удалены все связанные заявки, инженеры, планы и события (${zoneDatasetCount} набор(ов)).\nДействие необратимо. После очистки загрузите новый CSV.`,
    )
    if (!ok) return
    deleteZoneMutation.mutate(zoneCode)
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-ink">Данные и допущения</h1>
        <p className="mt-1 text-sm text-muted">
          Три независимых контура · активная зона выбирается в верхней панели
        </p>
      </div>

      {(message || error) && (
        <div
          className={`rounded-xl border px-4 py-3 text-sm ${
            error ? 'border-red-200 bg-red-50 text-red-800' : 'border-emerald-200 bg-emerald-50 text-emerald-800'
          }`}
        >
          {error ?? message}
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {(datasetsQuery.data ?? []).map((dataset) => {
          const active = dataset.id === activeDataset?.id
          return (
            <button
              key={dataset.id}
              type="button"
              onClick={() => selectDataset(dataset.id)}
              className={`rounded-xl border px-4 py-3 text-left transition ${
                active
                  ? 'border-amber-300 bg-amber-50 shadow-sm'
                  : 'border-slate-200 bg-white hover:border-slate-300'
              }`}
            >
              <div className="flex items-center gap-2">
                <span
                  className="h-5 w-1 rounded-full bg-amber-400"
                  style={
                    dataset.service_zone === 'yugo_vostok'
                      ? { background: '#34d399' }
                      : dataset.service_zone === 'yugocenter'
                        ? { background: '#a78bfa' }
                        : undefined
                  }
                />
                <span className="font-semibold text-ink">{dataset.title}</span>
              </div>
              <p className="mt-1 text-sm text-muted">
                {dataset.request_count} заявок · revision {dataset.revision}
              </p>
            </button>
          )
        })}
        {datasetsQuery.isLoading && <p className="text-sm text-muted">Загрузка наборов…</p>}
        {!datasetsQuery.isLoading && (datasetsQuery.data?.length ?? 0) === 0 && (
          <EmptyState title="Нет наборов" description="Импортируйте CSV или загрузите демо-данные." />
        )}
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card className="p-5">
          <h2 className="text-lg font-semibold text-ink">Импорт CSV</h2>
          <p className="mt-1 text-sm text-muted">
            Загрузка создаёт новый снимок набора. Чтобы заменить данные зоны — сначала удалите старые
            наборы ниже.
          </p>
          <form className="mt-4 space-y-3" onSubmit={onImport}>
            <input
              type="file"
              accept=".csv,text/csv"
              disabled={busy}
              onChange={(event) => onFileChange(event.target.files?.[0] ?? null)}
              className="block w-full text-sm text-muted file:mr-3 file:rounded-lg file:border-0 file:bg-violet-50 file:px-3 file:py-2 file:text-sm file:font-medium file:text-primary"
            />
            {file && <p className="text-xs text-muted">Файл: {file.name}</p>}
            {importPreview && importPreview.errors.length > 0 && (
              <div
                role="alert"
                className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950"
              >
                <p className="font-semibold">Файл проверен: найдены проблемы</p>
                <ul className="mt-2 list-disc space-y-1 pl-5">
                  {importPreview.errors.map((issue) => (
                    <li key={`${issue.line}-${issue.code}-${issue.external_id ?? ''}`}>
                      {importIssueText(issue)}
                    </li>
                  ))}
                </ul>
                {canSkipDuplicateRows(importPreview) ? (
                  <div className="mt-3 space-y-2">
                    <p>
                      Остальные {importPreview.valid_count} строк корректны. Можно пропустить
                      повторные строки и продолжить импорт.
                    </p>
                    <Button
                      type="button"
                      variant="secondary"
                      disabled={busy}
                      onClick={() => importMutation.mutate({ skipDuplicateRows: true })}
                    >
                      {importMutation.isPending
                        ? 'Импорт…'
                        : `Пропустить дубликаты (${importPreview.errors.length}) и импортировать`}
                    </Button>
                  </div>
                ) : (
                  <p className="mt-3">Исправьте указанные строки и выберите файл повторно.</p>
                )}
              </div>
            )}
            <Button type="submit" disabled={busy || !file}>
              {previewMutation.isPending ? 'Проверка…' : 'Проверить и импортировать'}
            </Button>
          </form>
        </Card>

        <Card className="p-5">
          <h2 className="text-lg font-semibold text-ink">Активный набор</h2>
          {datasetsQuery.isLoading && <p className="mt-2 text-sm text-muted">Загрузка…</p>}
          {!datasetsQuery.isLoading && !activeDataset && (
            <p className="mt-2 text-sm text-muted">Выберите набор в списке выше.</p>
          )}
          {activeDataset && (
            <div className="mt-3 space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-base font-semibold text-ink">{activeDataset.title}</span>
                <Badge>заявок {activeDataset.request_count}</Badge>
                <Badge tone="slate">rev {activeDataset.revision}</Badge>
                {activeDataset.service_zone && (
                  <Badge tone="violet">{activeDataset.service_zone}</Badge>
                )}
              </div>
              {detailQuery.data && (
                <div className="grid grid-cols-2 gap-3 text-sm">
                  <div>
                    <p className="text-xs uppercase tracking-wide text-muted">Заявки</p>
                    <p className="font-semibold text-ink">{detailQuery.data.request_count}</p>
                  </div>
                  <div>
                    <p className="text-xs uppercase tracking-wide text-muted">Инженеры</p>
                    <p className="font-semibold text-ink">{detailQuery.data.engineer_count}</p>
                  </div>
                  <div>
                    <p className="text-xs uppercase tracking-wide text-muted">Часовой пояс</p>
                    <p className="font-semibold text-ink">{detailQuery.data.timezone}</p>
                  </div>
                  <div>
                    <p className="text-xs uppercase tracking-wide text-muted">Дата</p>
                    <p className="font-semibold text-ink">{detailQuery.data.planning_date}</p>
                  </div>
                </div>
              )}
              {detailQuery.data?.import_report && (
                <div className="rounded-lg bg-slate-50 p-3 text-sm">
                  <p className="font-medium text-ink">Отчёт импорта</p>
                  <pre className="mt-1 max-h-40 overflow-auto text-xs text-muted">
                    {JSON.stringify(detailQuery.data.import_report, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          )}
        </Card>
      </div>

      {assumptionsPreview.length > 0 && (
        <Card className="p-5">
          <h2 className="text-lg font-semibold text-ink">Демонстрационные допущения</h2>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            {assumptionsPreview.map(([key, value]) => (
              <div key={key} className="rounded-lg border border-amber-100 bg-amber-50/60 p-3">
                <p className="text-xs font-semibold uppercase tracking-wide text-amber-700">{key}</p>
                <p className="mt-1 text-sm text-ink">{value}</p>
              </div>
            ))}
          </div>
        </Card>
      )}

      <Card className="border-red-200 p-5">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold text-red-700">Очистка данных зоны</h2>
            <p className="mt-1 max-w-2xl text-sm text-muted">
              Удаляет заявки, инженеров, планы и события выбранного набора (или всех наборов зоны).
              Нужно перед повторной загрузкой CSV с обновлёнными данными. Действие необратимо.
            </p>
          </div>
        </div>

        {activeDataset ? (
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <div className="mr-auto text-sm text-muted">
              Активный набор: <span className="font-medium text-ink">{activeDataset.title}</span>
              {' · '}
              {activeDataset.request_count} заявок
              {zoneCode ? ` · зона ${zoneName}` : ''}
            </div>
            <Button type="button" variant="danger" disabled={busy} onClick={onDeleteDataset}>
              {deleteDatasetMutation.isPending ? 'Удаление…' : 'Удалить набор'}
            </Button>
            {zoneCode && (
              <Button type="button" variant="danger" disabled={busy} onClick={onDeleteZone}>
                {deleteZoneMutation.isPending
                  ? 'Удаление зоны…'
                  : `Удалить все наборы зоны «${zoneName}»`}
              </Button>
            )}
          </div>
        ) : (
          <p className="mt-4 text-sm text-muted">Выберите набор, чтобы очистить данные перед новым импортом.</p>
        )}
      </Card>
    </div>
  )
}
