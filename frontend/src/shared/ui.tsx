import type { ButtonHTMLAttributes, HTMLAttributes, PropsWithChildren, ReactNode } from 'react'

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'danger'
}

export function Button({ className = '', variant = 'primary', ...props }: ButtonProps) {
  const colors =
    variant === 'secondary'
      ? 'bg-white text-primary ring-1 ring-violet-200 hover:bg-violet-50'
      : variant === 'danger'
        ? 'bg-red-600 text-white hover:bg-red-700 focus:ring-red-600'
        : 'bg-primary text-white hover:bg-violet-700'
  return (
    <button
      className={`rounded-lg px-4 py-2.5 text-sm font-semibold shadow-sm transition focus:outline-none focus:ring-2 focus:ring-primary focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 ${colors} ${className}`}
      {...props}
    />
  )
}

export function Card({ children, className = '', ...props }: PropsWithChildren<HTMLAttributes<HTMLElement>>) {
  return <section className={`rounded-xl border border-slate-200 bg-white shadow-sm ${className}`} {...props}>{children}</section>
}

export function Badge({
  children,
  tone = 'slate',
}: {
  children: ReactNode
  tone?: 'slate' | 'gray' | 'green' | 'amber' | 'red' | 'violet' | 'blue'
}) {
  const colors = {
    slate: 'bg-slate-100 text-slate-700',
    gray: 'bg-slate-100 text-slate-700',
    green: 'bg-green-50 text-green-700',
    amber: 'bg-amber-50 text-amber-700',
    red: 'bg-red-50 text-red-700',
    violet: 'bg-violet-50 text-violet-700',
    blue: 'bg-blue-50 text-blue-700',
  }
  return <span className={`inline-flex rounded-full px-2.5 py-1 text-xs font-semibold ${colors[tone]}`}>{children}</span>
}

/** Пустое состояние: что случилось и какой следующий шаг, а не голое «нет данных». */
export function EmptyState({ title, description, action }: { title: string; description: string; action?: ReactNode }) {
  return (
    <div className="flex min-h-52 flex-col items-center justify-center rounded-xl border border-dashed border-slate-300 bg-white p-8 text-center">
      <div className="mb-3 rounded-full bg-violet-50 p-3 text-xl text-primary">⌁</div>
      <h2 className="font-semibold text-ink">{title}</h2>
      <p className="mt-1 max-w-md text-sm text-muted">{description}</p>
      {action ? <div className="mt-4">{action}</div> : null}
    </div>
  )
}

/**
 * Знак аварии. Авария выделяется двумя признаками сразу — цветом и знаком: цвет
 * замечается боковым зрением, знак различим при цветовой слепоте и на чёрно-белой печати.
 */
export function EmergencyIcon({ className = 'h-3.5 w-3.5' }: { className?: string }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" className={`inline-block shrink-0 fill-current ${className}`}>
      <path d="M13.5 2 4 14h6.5L9 22l11-13h-6.8L13.5 2Z" />
    </svg>
  )
}
