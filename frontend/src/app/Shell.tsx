import { Link, Outlet, useNavigate, useRouterState } from '@tanstack/react-router'
import { useEffect, useMemo, useState } from 'react'
import teamLogo from '../assets/team-shtatny-intellect.png'
import { logout } from '../features/auth/auth-state'
import { DatasetProvider } from '../shared/dataset-context'
import { useActiveDataset } from '../shared/dataset-state'
import { serviceZoneVisual } from '../shared/service-zones'
import type { DatasetSummary } from '../shared/types'

const navigation = [
  { to: '/planning', label: 'Планирование', icon: '⌁' },
  { to: '/analytics', label: 'Аналитика', icon: '▥' },
  { to: '/requests', label: 'Заявки', icon: '▤' },
  { to: '/engineers', label: 'Инженеры', icon: '◉' },
  { to: '/norms', label: 'Нормативы', icon: '≡' },
  { to: '/data', label: 'Данные', icon: '◇' },
] as const

export function Shell() {
  // Logout only when password auth is enabled (AUTH_ENABLED=true).
  const [showLogout, setShowLogout] = useState(false)
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const { apiClient } = await import('../shared/api')
        const status = await apiClient.authStatus()
        if (!cancelled) setShowLogout(Boolean(status.auth_enabled && status.authenticated))
      } catch {
        if (!cancelled) setShowLogout(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <DatasetProvider>
      <ShellContent showLogout={showLogout} />
    </DatasetProvider>
  )
}

function ZoneSwitcher({ datasets, activeDataset, onSelect }: {
  datasets: DatasetSummary[]
  activeDataset: DatasetSummary | undefined
  onSelect: (datasetId: string) => void
}) {
  const zoneDatasets = useMemo(() => {
    const byZone = new Map<string, DatasetSummary>()
    for (const item of datasets) {
      const key = item.service_zone ?? item.id
      const current = byZone.get(key)
      if (!current || item.id === activeDataset?.id || (!current.current_plan_id && item.current_plan_id)) {
        byZone.set(key, item)
      }
    }
    return [...byZone.values()]
  }, [activeDataset?.id, datasets])

  return (
    <div
      role="group"
      aria-label="Зона обслуживания"
      className="flex max-w-full gap-1 overflow-x-auto rounded-xl bg-slate-100 p-1"
    >
      {zoneDatasets.map((item) => {
        const active = item.id === activeDataset?.id
        const visual = serviceZoneVisual(item.service_zone)
        const name = item.service_zone_name ?? item.title
        return (
          <button
            key={item.id}
            type="button"
            aria-pressed={active}
            onClick={() => onSelect(item.id)}
            className={`flex min-h-9 shrink-0 items-center gap-2 rounded-lg border bg-white px-2.5 text-sm font-medium shadow-sm transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1 ${active ? `ring-1 ${visual.selected}` : visual.idle}`}
          >
            <span aria-hidden className={`h-5 w-1 rounded-full ${visual.indicator}`} />
            <span>{name}</span>
          </button>
        )
      })}
    </div>
  )
}

function ShellContent({ showLogout }: { showLogout: boolean }) {
  const pathname = useRouterState({ select: (state) => state.location.pathname })
  const navigate = useNavigate()
  const { datasetsQuery, dataset, selectDataset } = useActiveDataset()

  async function onLogout() {
    await logout()
    await navigate({ to: '/login' })
  }
  return (
    <div className="min-h-screen bg-slate-50 text-ink">
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-56 flex-col bg-sidebar px-3 py-5 text-white lg:flex">
        <div className="mb-8 flex items-center gap-3 px-3">
          <div className="grid h-10 w-10 place-items-center rounded-xl bg-primary text-sm font-bold">TR</div>
          <div><div className="font-semibold">Task Router</div><div className="text-xs text-slate-400">Диспетчерская</div></div>
        </div>
        <nav className="space-y-1" aria-label="Основная навигация">
          {navigation.map((item) => {
            const active = pathname.startsWith(item.to) || (item.to === '/planning' && pathname.startsWith('/routes/'))
            return (
              <Link
                key={item.to}
                to={item.to}
                className={`flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm ${active ? 'bg-violet-600 font-semibold' : 'text-slate-300 hover:bg-slate-800 hover:text-white'}`}
              >
                <span aria-hidden>{item.icon}</span>{item.label}
              </Link>
            )
          })}
        </nav>
        <div className="mt-auto space-y-3">
          <button
            type="button"
            onClick={() => void onLogout()}
            className="w-full rounded-lg border border-slate-700 px-3 py-2 text-left text-sm text-slate-300 transition hover:bg-slate-800 hover:text-white"
          >
            Выйти
          </button>
          <div className="rounded-lg border border-slate-700 p-3 text-xs leading-5 text-slate-400">
            Расчётная модель<br /><span className="text-slate-200">3 зоны · 1 день</span>
          </div>
          <div
            aria-label="Разработано командой Штатный интеллект"
            className="flex items-center gap-3 rounded-xl border border-violet-400/20 bg-gradient-to-br from-violet-500/10 via-slate-900/0 to-fuchsia-500/10 p-2.5"
          >
            <img
              src={teamLogo}
              alt=""
              aria-hidden="true"
              className="h-12 w-12 shrink-0 rounded-full object-contain shadow-[0_0_18px_rgba(168,85,247,0.18)] ring-1 ring-fuchsia-400/30"
            />
            <div className="min-w-0">
              <div className="text-[10px] font-medium uppercase leading-4 tracking-[0.08em] text-slate-400">
                Разработано командой
              </div>
              <div className="text-xs font-semibold leading-4 text-slate-100">
                Штатный интеллект
              </div>
            </div>
          </div>
        </div>
      </aside>
      <div className="min-w-0 max-w-full lg:pl-56">
        <header className="sticky top-0 z-20 flex min-h-16 flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-white/95 px-4 py-2 backdrop-blur md:px-6">
          <div>
            <h1 className="font-semibold">Task Router Диспетчерская</h1>
            <p className="text-xs text-muted">Помощник диспетчера · расчётные данные</p>
          </div>
          <div className="ml-auto flex max-w-full items-center gap-2">
            <ZoneSwitcher
              datasets={datasetsQuery.data ?? []}
              activeDataset={dataset}
              onSelect={selectDataset}
            />
            <span className="hidden rounded-full bg-amber-50 px-3 py-1 text-xs font-medium text-amber-700 sm:inline">Демо-режим</span>
          </div>
        </header>
        <main className="min-w-0 max-w-full p-4 md:p-6"><Outlet /></main>
        <nav className="fixed inset-x-0 bottom-0 z-40 grid grid-cols-6 border-t bg-white p-1 lg:hidden">
          {navigation.map((item) => <Link key={item.to} to={item.to} className="p-2 text-center text-xs text-slate-600">{item.label}</Link>)}
        </nav>
      </div>
    </div>
  )
}
