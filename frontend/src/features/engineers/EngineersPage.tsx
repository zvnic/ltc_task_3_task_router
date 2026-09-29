import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'

import { apiClient } from '../../shared/api'
import { useActiveDataset } from '../../shared/dataset-state'
import { TRANSPORT_PRESENTATION, transportText } from '../../shared/transport'
import type { Engineer } from '../../shared/types'
import { Badge, Button, Card, EmptyState } from '../../shared/ui'

const skills = { local: 'Локальные', connection: 'Подключение', emergency: 'Аварийные' } as Record<string, string>

function shiftTime(value: string) {
  return new Date(value).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Moscow' })
}

export function EngineersPage() {
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<Engineer | null>(null)
  const { dataset } = useActiveDataset()
  const engineers = useQuery({ queryKey: ['engineers', dataset?.id], queryFn: () => apiClient.engineers(dataset!.id), enabled: Boolean(dataset) })
  if (!dataset) return <EmptyState title="Нет набора данных" description="Бригады появятся после загрузки данных зоны на странице «Данные»." />
  return <div className="space-y-4"><div><h2 className="text-xl font-semibold">Инженеры · {dataset.service_zone_name ?? dataset.title}</h2><p className="text-sm text-muted">Условные исполнители · навыки, смены и тип перемещения демонстрационные</p></div><div className="grid gap-3 lg:grid-cols-2">{engineers.data?.map((engineer) => <button key={engineer.id} onClick={() => setSelected(engineer)} className="text-left"><Card className="p-4 transition hover:border-violet-200 hover:shadow-sm"><div className="flex items-start justify-between gap-3"><div><h3 className="font-semibold">{engineer.name}</h3><p className="mt-1 text-sm text-muted" data-engineer-transport={engineer.transport}>{transportText(engineer.transport)}</p></div><Badge tone="amber">демо</Badge></div><div className="mt-3 flex flex-wrap gap-1.5">{engineer.skills.map((skill) => <Badge key={skill} tone="violet">{skills[skill]}</Badge>)}</div><div className="mt-4 border-t pt-3 text-sm text-slate-600">Смена {shiftTime(engineer.shift_start)}–{shiftTime(engineer.shift_end)}</div></Card></button>)}</div>{selected && <EngineerDialog engineer={selected} revision={dataset.revision} onClose={() => setSelected(null)} onSaved={async () => { setSelected(null); await queryClient.invalidateQueries({ queryKey: ['datasets'] }); await queryClient.invalidateQueries({ queryKey: ['engineers'] }) }} />}</div>
}

function EngineerDialog({ engineer, revision, onClose, onSaved }: { engineer: Engineer; revision: number; onClose: () => void; onSaved: () => Promise<void> }) {
  const [name, setName] = useState(engineer.name)
  const [selectedTransport, setSelectedTransport] = useState(engineer.transport)
  const mutation = useMutation({ mutationFn: () => apiClient.patchEngineer(engineer.id, { expected_revision: revision, name, transport: selectedTransport }), onSuccess: onSaved })
  function submit(event: FormEvent) { event.preventDefault(); mutation.mutate() }
  return <div className="fixed inset-0 z-50 grid place-items-center bg-slate-950/40 p-4" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}><section role="dialog" aria-modal="true" aria-labelledby="engineer-title" className="w-full max-w-md rounded-2xl bg-white p-6 shadow-2xl"><div className="flex justify-between"><div><div className="text-xs font-semibold uppercase tracking-wide text-primary">Инженер</div><h2 id="engineer-title" className="mt-1 text-xl font-semibold">Редактирование</h2></div><button onClick={onClose} aria-label="Закрыть" className="rounded p-2 text-xl text-muted hover:bg-slate-100">×</button></div><form className="mt-5 space-y-4" onSubmit={submit}><label className="block text-sm font-medium">Название<input value={name} onChange={(event) => setName(event.target.value)} required className="mt-1 w-full rounded-lg border px-3 py-2" /></label><label className="block text-sm font-medium">Тип перемещения<select value={selectedTransport} onChange={(event) => setSelectedTransport(event.target.value)} className="mt-1 w-full rounded-lg border px-3 py-2">{Object.entries(TRANSPORT_PRESENTATION).map(([value, item]) => <option key={value} value={value}>{item.icon} {item.label}</option>)}</select></label><p className="text-xs text-muted">Тип перемещения зафиксирован за инженером и влияет на время и длину переездов. Изменение повысит ревизию данных; старые планы сохранят прежний режим.</p>{mutation.isError && <div role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{mutation.error.message}</div>}<div className="flex justify-end gap-2"><button type="button" onClick={onClose} className="rounded-lg px-4 py-2 text-sm font-semibold text-slate-600 hover:bg-slate-100">Отмена</button><Button type="submit" disabled={mutation.isPending}>{mutation.isPending ? 'Сохраняем…' : 'Сохранить'}</Button></div></form></section></div>
}
