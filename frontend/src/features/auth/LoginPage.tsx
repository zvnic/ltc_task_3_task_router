import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { useNavigate } from '@tanstack/react-router'
import { apiClient, ApiError } from '../../shared/api'
import { Button, Card } from '../../shared/ui'
import { setAuthCache } from './auth-state'

export function LoginPage() {
  const navigate = useNavigate()
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [checking, setChecking] = useState(true)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const status = await apiClient.authStatus()
        if (cancelled) return
        if (!status.auth_enabled || status.authenticated) {
          setAuthCache(true)
          await navigate({ to: '/planning', replace: true })
          return
        }
      } catch {
        // keep form if status probe fails
      } finally {
        if (!cancelled) setChecking(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [navigate])

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setPending(true)
    try {
      await apiClient.login(password)
      setAuthCache(true)
      await navigate({ to: '/planning' })
    } catch (err) {
      setAuthCache(false)
      if (err instanceof ApiError && err.status === 401) {
        setError('Неверный пароль')
      } else {
        setError(err instanceof Error ? err.message : 'Не удалось войти')
      }
    } finally {
      setPending(false)
    }
  }

  if (checking) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-950 text-sm text-slate-400">
        Проверка доступа…
      </div>
    )
  }

  return (
    <div className="flex min-h-screen bg-sidebar text-white">
      <div className="relative hidden w-[42%] flex-col justify-between overflow-hidden border-r border-slate-800 p-10 lg:flex">
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_20%_20%,rgba(124,58,237,0.35),transparent_45%),radial-gradient(circle_at_80%_70%,rgba(59,130,246,0.18),transparent_40%)]" />
        <div className="relative flex items-center gap-3">
          <div className="grid h-12 w-12 place-items-center rounded-xl bg-primary text-base font-bold shadow-lg shadow-violet-900/40">
            TR
          </div>
          <div>
            <div className="text-lg font-semibold tracking-tight">Task Router</div>
            <div className="text-sm text-slate-400">Диспетчерская</div>
          </div>
        </div>
        <div className="relative space-y-4">
          <h1 className="max-w-sm text-3xl font-semibold leading-tight tracking-tight">
            Планирование маршрутов инженеров
          </h1>
          <p className="max-w-sm text-sm leading-6 text-slate-400">
            Доступ только по паролю. Сессия хранится в защищённой cookie и действует 12 часов.
          </p>
        </div>
        <p className="relative text-xs text-slate-500">Beeline · внутренний контур</p>
      </div>

      <div className="flex flex-1 items-center justify-center bg-slate-50 px-4 py-10 text-ink">
        <Card className="w-full max-w-md border-slate-200 p-8 shadow-xl shadow-slate-200/60">
          <div className="mb-6 flex items-center gap-3 lg:hidden">
            <div className="grid h-10 w-10 place-items-center rounded-xl bg-primary text-sm font-bold text-white">
              TR
            </div>
            <div>
              <div className="font-semibold">Task Router</div>
              <div className="text-xs text-muted">Диспетчерская</div>
            </div>
          </div>
          <h2 className="text-xl font-semibold tracking-tight">Вход</h2>
          <p className="mt-1 text-sm text-muted">Введите пароль для доступа к диспетчерской</p>

          <form className="mt-6 space-y-4" onSubmit={onSubmit}>
            <label className="block space-y-1.5">
              <span className="text-sm font-medium text-slate-700">Пароль</span>
              <input
                type="password"
                name="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm shadow-sm outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/30"
                placeholder="••••••••"
              />
            </label>

            {error ? (
              <div
                role="alert"
                className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
              >
                {error}
              </div>
            ) : null}

            <Button type="submit" className="w-full" disabled={pending || !password}>
              {pending ? 'Вход…' : 'Войти'}
            </Button>
          </form>
        </Card>
      </div>
    </div>
  )
}
