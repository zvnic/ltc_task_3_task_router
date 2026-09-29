import { useQuery } from '@tanstack/react-query'
import type { ReactNode } from 'react'

import { apiClient } from '../../shared/api'
import type { Algorithm, Plan, PlanningDirectories, TravelNormMode } from '../../shared/types'
import { Badge, Card, EmergencyIcon } from '../../shared/ui'
import { NormExcessTotal } from './RouteTimeline'
import { isReplanPlan } from './request-kind'
import { algorithmTitle, kmText, planOrigin, plural, selectionDecisions, terminationText } from './plan-explain'

function percentText(value: number) {
  return `${value.toLocaleString('ru-RU', { maximumFractionDigits: 0 })} %`
}

type Tone = 'neutral' | 'success' | 'danger'

const TILE_TONE: Record<Tone, string> = {
  neutral: 'border-slate-200 bg-white',
  success: 'border-emerald-200 bg-emerald-50/70',
  danger: 'border-red-300 bg-red-50',
}

const VALUE_TONE: Record<Tone, string> = {
  neutral: 'text-ink',
  success: 'text-emerald-800',
  danger: 'text-red-800',
}

function StatusTile({
  label,
  value,
  note,
  tone = 'neutral',
  meter,
  onClick,
  actionLabel,
  children,
}: {
  label: string
  value: ReactNode
  note?: ReactNode
  tone?: Tone
  meter?: number
  onClick?: () => void
  actionLabel?: string
  children?: ReactNode
}) {
  const content = (
    <>
      <div className="text-xs font-medium uppercase tracking-wide text-muted">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${VALUE_TONE[tone]}`}>{value}</div>
      {meter !== undefined && (
        <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-200" aria-hidden="true">
          <div
            className={`h-full rounded-full ${tone === 'success' ? 'bg-emerald-500' : 'bg-violet-500'}`}
            style={{ width: `${Math.max(0, Math.min(100, meter))}%` }}
          />
        </div>
      )}
      {note ? <div className="mt-1 text-xs text-slate-600">{note}</div> : null}
      {children}
    </>
  )
  const className = `min-w-0 rounded-xl border p-3 text-left shadow-sm ${TILE_TONE[tone]}`
  if (!onClick) return <div className={className}>{content}</div>
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={actionLabel}
      className={`${className} transition hover:shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary`}
    >
      {content}
    </button>
  )
}

/**
 * Строка состояния плана: качество плана одной строкой чисел, которую диспетчер
 * читает первой. «Не назначено» красное, пока есть отказы, и ведёт к их причинам.
 */
export function PlanStatusStrip({
  plan,
  requestCount,
  engineerCount,
  normMode,
  onShowUnassigned,
}: {
  plan?: Plan
  requestCount: number
  engineerCount: number
  normMode: TravelNormMode
  onShowUnassigned: () => void
}) {
  const metrics = plan?.metrics
  const kpis = plan?.model_info.kpis
  const assigned = metrics?.assigned_count ?? 0
  const unassigned = metrics?.unassigned_count ?? 0
  const assignedShare = requestCount ? Math.round((assigned / requestCount) * 100) : 0
  const utilization = kpis?.used_engineer_utilization_percent
  const emergencies = metrics?.emergency_unassigned_count ?? 0
  const rest = [
    metrics?.connection_unassigned_count ? `подключений ${metrics.connection_unassigned_count}` : null,
    metrics?.routine_unassigned_count ? `ремонтов и дозаказов ${metrics.routine_unassigned_count}` : null,
  ].filter(Boolean)
  return (
    <section aria-label="Состояние плана" className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
      <StatusTile
        label="Назначено"
        value={metrics ? `${assigned} из ${requestCount}` : '—'}
        tone={metrics && !unassigned ? 'success' : 'neutral'}
        meter={metrics ? assignedShare : undefined}
        note={metrics ? `${assignedShare} % заявок дня у бригад` : 'план ещё не рассчитан'}
      />
      <StatusTile
        label="Не назначено"
        value={metrics ? unassigned : '—'}
        tone={!metrics ? 'neutral' : unassigned ? 'danger' : 'success'}
        onClick={metrics && unassigned ? onShowUnassigned : undefined}
        actionLabel={`Не назначено: ${unassigned}. Показать причины`}
        note={
          !metrics ? undefined : unassigned ? (
            <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
              {emergencies > 0 && (
                <span className="inline-flex items-center gap-1 font-semibold text-red-800">
                  <EmergencyIcon />
                  {`аварий ${emergencies}`}
                </span>
              )}
              {rest.length > 0 && <span>{rest.join(' · ')}</span>}
              <span className="font-medium text-red-800 underline underline-offset-2">причины →</span>
            </span>
          ) : 'все заявки у бригад'
        }
      />
      <StatusTile
        label="Бригады"
        value={metrics ? `${metrics.used_engineers_count} из ${engineerCount}` : '—'}
        note={metrics ? `в резерве ${Math.max(0, engineerCount - metrics.used_engineers_count)}` : undefined}
      />
      <StatusTile
        label="Загрузка бригад"
        value={utilization == null ? '—' : percentText(utilization)}
        meter={utilization ?? undefined}
        note="работа ÷ смены занятых бригад"
      />
      <StatusTile
        label="Расчётный пробег"
        value={metrics ? kmText(metrics.total_distance_meters) : '—'}
        note={
          kpis?.distance_per_assigned_meters
            ? `≈ ${kmText(kpis.distance_per_assigned_meters)} на заявку`
            : undefined
        }
      >
        <NormExcessTotal metrics={metrics} advisory={normMode === 'advisory'} className="mt-1 text-xs" />
      </StatusTile>
    </section>
  )
}

function Step({ number, title, children }: { number: number; title: string; children: ReactNode }) {
  return (
    <li className="min-w-0 rounded-lg border border-slate-100 bg-slate-50 p-3">
      <div className="flex items-center gap-2">
        <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-violet-600 text-xs font-semibold text-white">
          {number}
        </span>
        <span className="text-sm font-semibold text-ink">{title}</span>
      </div>
      <div className="mt-2 space-y-1 text-sm leading-5 text-slate-700">{children}</div>
    </li>
  )
}

function comparisonOrder(normMode: TravelNormMode) {
  const steps = ['аварии без бригады', 'подключения', 'ремонты и дозаказы']
  if (normMode === 'soft') steps.push('минуты дороги сверх норматива')
  steps.push('задействованные бригады', 'время в пути', 'пробег')
  return steps.join(' → ')
}

/**
 * «Как построен план»: четыре шага расчёта с числами из паспорта этого плана.
 * В отличие от промышленных диспетчерских, где автоназначение — чёрный ящик,
 * здесь видно, на каких данных считали, чем, как проверили и почему выбрали.
 */
export function HowPlanWasBuilt({
  plan,
  algorithms,
  normMode,
}: {
  plan: Plan
  algorithms: Algorithm[]
  normMode: TravelNormMode
}) {
  const experiment = plan.model_info.experiment
  const selection = plan.model_info.selection_reason
  const selectedId = plan.model_info.algorithm_id ?? selection?.selected_algorithm ?? plan.result.algorithm
  const selectedTitle = algorithmTitle(selectedId, algorithms)
  const methodIds = experiment?.algorithm_ids ?? []
  const decisions = selection ? selectionDecisions(selection, algorithms) : []
  const violations = plan.model_info.kpis?.validator_violations ?? 0
  const requestCount = experiment?.request_count ?? plan.metrics.assigned_count + plan.metrics.unassigned_count
  const engineerCount = experiment?.engineer_count ?? plan.result.routes.length
  const costMode = selection?.objective_mode === 'cost'
  const origin = planOrigin(plan)
  const urgentReplan = origin === 'solvers' && isReplanPlan(plan.kind)
  const protectedCount = plan.model_info.protected_request_ids?.length ?? 0
  return (
    <Card className="p-4" aria-label="Как построен план">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h3 className="font-semibold">Как построен план</h3>
        <p className="text-xs text-muted">Каждый шаг взят из паспорта плана — его можно перепроверить</p>
      </div>
      <ol className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <Step number={1} title="Данные">
          <p>{`${plural(requestCount, ['заявка', 'заявки', 'заявок'])} и ${plural(engineerCount, ['бригада', 'бригады', 'бригад'])} зоны — один снимок данных для всех методов.`}</p>
          <p className="text-xs text-muted">
            {experiment ? `Снимок ${experiment.snapshot_sha256.slice(0, 8)} · ` : ''}координаты и бригады демонстрационные
          </p>
        </Step>
        {origin === 'gap_insertion' ? (
          <Step number={2} title="Вставка">
            <p>Новая обычная заявка: перебраны все места во всех маршрутах.</p>
            <p className="text-xs text-slate-600">{`Прежние визиты не сдвигаются — ни бригада, ни порядок, ни время (${protectedCount}).`}</p>
          </Step>
        ) : origin === 'manual' ? (
          <Step number={2} title="Ручное назначение">
            <p>Диспетчер выбрал бригаду и место для отказанной заявки из допустимых вариантов.</p>
            <p className="text-xs text-slate-600">Прежние визиты не сдвигаются.</p>
          </Step>
        ) : (
          <Step number={2} title={urgentReplan ? 'Пересчёт остатка дня' : 'Расчёт'}>
            {urgentReplan && <p className="text-xs text-slate-600">{`Начатые и выполненные визиты защищены (${protectedCount}), пересчитана только ещё не начатая часть дня.`}</p>}
            {methodIds.length > 1 ? (
              <>
                <p>{`${plural(methodIds.length, ['метод', 'метода', 'методов'])} на этом снимке${experiment ? `, по ${experiment.time_limit_seconds} с каждый` : ''}:`}</p>
                <ul className="list-inside list-disc text-xs text-slate-600">
                  {methodIds.map((id) => <li key={id}>{algorithmTitle(id, algorithms)}</li>)}
                </ul>
              </>
            ) : (
              <p>{`Метод «${selectedTitle}»${experiment ? `, лимит ${experiment.time_limit_seconds} с` : ''}.`}</p>
            )}
          </Step>
        )}
        <Step number={3} title="Проверка">
          <p>Независимый валидатор заново считает расписание и все ограничения: навык, транспорт, оборудование, окно, смену, срок аварии.</p>
          <p className={`font-semibold ${violations ? 'text-red-800' : 'text-emerald-800'}`}>
            {violations ? `Нарушений: ${violations}` : 'Нарушений нет — план опубликован'}
          </p>
        </Step>
        <Step number={4} title="Выбор">
          {origin === 'gap_insertion' ? (
            <p>
              {normMode === 'soft'
                ? 'Место без превышения норматива дороги или с наименьшим превышением, при равенстве — с минимальным приростом времени в пути.'
                : 'Место с минимальным приростом времени в пути, при равенстве — пробега.'}
            </p>
          ) : origin === 'manual' ? (
            <p>Решение диспетчера — план не сравнивался автоматически.</p>
          ) : (
            <p>
              {'Выбран '}
              <strong>{`«${selectedTitle}»`}</strong>
              {decisions.length ? ':' : '.'}
            </p>
          )}
          {origin === 'solvers' && decisions.length > 0 && (
            <ul className="space-y-0.5 text-xs text-slate-600" aria-label="Почему выбран этот метод">
              {decisions.map((decision) => (
                <li key={decision.competitor}>{`против «${decision.competitor}»: ${decision.text}`}</li>
              ))}
            </ul>
          )}
          <p className="text-xs text-muted">{terminationText(plan.result.termination_reason)}</p>
        </Step>
      </ol>
      <p className="mt-3 text-xs leading-5 text-slate-600">
        {costMode
          ? 'Планы сравниваются по условной стоимости: бригады, километры, минуты в пути и штрафы за пропуски.'
          : `Как сравниваются планы: ${comparisonOrder(normMode)}. Следующий показатель решает, только когда предыдущие равны.`}
        {normMode === 'advisory' &&
          ' Норматив дороги 20 мин справочный: это время слота в графике, поездку считает модель дороги (разъяснение организаторов 21.09).'}
      </p>
    </Card>
  )
}

const VALIDATOR_CHECKS = [
  'навык бригады совпадает с видом работ',
  'транспорт — если заявка его требует',
  'оборудования хватает на все визиты бригады',
  'работы начинаются внутри окна клиента',
  'работы заканчиваются до конца смены',
  'авария закончена за 100 минут от поступления',
  'пешее плечо не длиннее 1 км, велосипедное — не длиннее 12 км',
  'каждая заявка — ровно у одной бригады, время и пробег сходятся с моделью дороги',
]

/** Расшифровка обозначений формулы метода — по тому, что в ней встречается. */
function formulaLegend(formula: string) {
  const parts: string[] = []
  if (formula.includes('feasible')) {
    parts.push('i — заявка в порядке поступления, j* — первая бригада, которой она по силам, Rj ⊕ i — заявка встаёт в конец её маршрута')
  }
  if (formula.includes('ΔT')) parts.push('p* — место вставки с наименьшим приростом времени в пути ΔT, при равенстве — пробега ΔD')
  if (formula.includes('min lex')) {
    parts.push('U — заявки без бригады по классам, K — задействованные бригады, T — время в пути, D — пробег; min lex — сначала минимум первого, при равенстве — следующего')
  }
  return parts.join('. ')
}

function speedText(value: number | null | undefined) {
  return value == null ? '—' : value.toLocaleString('ru-RU')
}

function TravelModel({ directories }: { directories?: PlanningDirectories }) {
  const model = directories?.travel_model
  return (
    <section aria-label="Модель дороги" className="min-w-0">
      <h4 className="text-sm font-semibold">Как считается дорога</h4>
      <p className="mt-1 text-xs leading-5 text-slate-600">
        {model?.road_network
          ? 'Расстояние между адресами зоны — по дорогам OpenStreetMap: для машины по проезжей части, пешком и на велосипеде — по своим сетям. Для общественного транспорта и нового адреса вне справочника — по прямой × коэффициент пути. '
          : 'Расстояние — по прямой × коэффициент пути. '}
        Время — по скорости режима плюс минуты на парковку или посадку. Внутри МКАД скорость
        городская, за МКАД — загородная.
      </p>
      {directories ? (
        <div className="mt-2 overflow-x-auto">
          <table className="w-full min-w-[320px] text-xs tabular-nums">
            <thead>
              <tr className="text-left text-muted">
                <th className="py-1 pr-2 font-medium">Режим</th>
                <th className="py-1 pr-2 font-medium">Город, км/ч</th>
                <th className="py-1 pr-2 font-medium">За МКАД</th>
                <th className="py-1 pr-2 font-medium">Путь ×</th>
                <th className="py-1 font-medium">+ мин</th>
              </tr>
            </thead>
            <tbody>
              {directories.transports.map((item) => (
                <tr key={item.code} className="border-t border-slate-100">
                  <td className="py-1 pr-2">{item.label}</td>
                  <td className="py-1 pr-2">{speedText(item.speed_kmh)}</td>
                  <td className="py-1 pr-2">
                    {item.suburban_speed_kmh ? speedText(item.suburban_speed_kmh) : 'как в городе'}
                    {item.suburban_boarding_minutes ? ` + ${item.suburban_boarding_minutes} мин посадка` : ''}
                  </td>
                  <td className="py-1 pr-2">{speedText(item.path_factor)}</td>
                  <td className="py-1">{item.access_minutes ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="mt-2 text-xs text-muted">Загружаем параметры модели…</p>
      )}
      {model && (
        <p className="mt-2 text-xs leading-5 text-slate-600">
          {`Машина паркуется и проходит к соседнему дому пешком, если до него не больше ${model.car_local_walk_meters} м. `}
          {model.bicycle_leg_limit_meters
            ? `Плечо пешком — не длиннее ${(model.walking_leg_limit_meters / 1000).toLocaleString('ru-RU')} км, на велосипеде — ${(model.bicycle_leg_limit_meters / 1000).toLocaleString('ru-RU')} км. `
            : ''}
          {`МКАД в модели — эллипс ${model.mkad_semi_axes_km[0].toLocaleString('ru-RU')} × ${model.mkad_semi_axes_km[1].toLocaleString('ru-RU')} км. `}
          {'Бригада на общественном транспорте идёт пешком, если так не дольше; посадка на электричку или автобус добавляется, когда за МКАД больше '}
          {`${(model.suburban_boarding_min_km ?? 0.5).toLocaleString('ru-RU')} км пути. `}
          {model.road_network
            ? `Модель: ${model.route_estimation_method}; расстояния — ${model.road_network.source}, ${model.road_network.attribution}.`
            : `Модель: ${model.route_estimation_method}. Это оценка, а не дорожный граф: линия на карте строится по сети OSM.`}
        </p>
      )}
    </section>
  )
}

function NormPolicy({ normMode, factor }: { normMode: TravelNormMode; factor?: number }) {
  return (
    <section aria-label="Норматив дороги" className="min-w-0">
      <h4 className="text-sm font-semibold">Норматив дороги</h4>
      <p className="mt-1 text-xs leading-5 text-slate-600">
        {normMode === 'advisory'
          ? '20 минут — время слота в графике при постановке заявки; при распределении его заменяет расчётное время в пути (организаторы, 21.09). Поездку в Домодедово или Каширу организаторы ошибкой не считают (22.09). Поездки длиннее норматива показываются серым и план не ограничивают.'
          : normMode === 'soft'
            ? `Превышение норматива допустимо до ×${(factor ?? 2).toLocaleString('ru-RU')} и штрафуется: минуты сверх норматива хуже лишней бригады, но лучше отказа.`
            : 'Норматив — запрет: поездка длиннее норматива недопустима, заявка уходит в отказ.'}
      </p>
    </section>
  )
}

/**
 * «Методы и модель расчёта»: описание метода, формула, сильные стороны и
 * ограничения, модель дороги, проверки валидатора и роль норматива — всё, что
 * определяет результат, открыто на экране, а не только в коде.
 */
export function MethodReference({
  algorithm,
  plan,
  normMode,
}: {
  algorithm?: Algorithm
  plan?: Plan
  normMode: TravelNormMode
}) {
  const directoriesQuery = useQuery({ queryKey: ['directories'], queryFn: apiClient.directories, staleTime: Number.POSITIVE_INFINITY })
  const experiment = plan?.model_info.experiment
  return (
    <Card className="p-4" aria-label="Методы и модель расчёта">
      <div className="text-xs font-semibold uppercase tracking-wide text-violet-700">Методы и модель расчёта</div>
      {algorithm && (
        <section className="mt-2" aria-label="Описание выбранного метода">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h3 className="font-semibold">{algorithm.title}</h3>
              <p className="mt-1 max-w-4xl text-sm text-slate-600">{algorithm.approach}</p>
            </div>
            <Badge tone="slate">{`${algorithm.id} · v${algorithm.version}`}</Badge>
          </div>
          <div className="mt-3 overflow-x-auto rounded-lg bg-slate-950 px-3 py-2 font-mono text-xs text-slate-100" aria-label="Формула метода">
            {algorithm.formula}
          </div>
          {formulaLegend(algorithm.formula) && (
            <p className="mt-1 text-xs text-muted">{formulaLegend(algorithm.formula)}</p>
          )}
          <div className="mt-3 grid gap-4 md:grid-cols-2">
            <div>
              <div className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Сильные стороны</div>
              <ul className="mt-1 space-y-1 text-sm text-slate-700">
                {algorithm.advantages.map((item) => <li key={item}>+ {item}</li>)}
              </ul>
            </div>
            <div>
              <div className="text-xs font-semibold uppercase tracking-wide text-amber-700">Ограничения</div>
              <ul className="mt-1 space-y-1 text-sm text-slate-700">
                {algorithm.disadvantages.map((item) => <li key={item}>− {item}</li>)}
              </ul>
            </div>
          </div>
        </section>
      )}
      <div className="mt-4 grid gap-5 border-t border-slate-100 pt-4 lg:grid-cols-3">
        <TravelModel directories={directoriesQuery.data} />
        <section aria-label="Что проверяет валидатор" className="min-w-0">
          <h4 className="text-sm font-semibold">Что проверяет валидатор</h4>
          <ul className="mt-1 space-y-0.5 text-xs leading-5 text-slate-600">
            {VALIDATOR_CHECKS.map((item) => <li key={item}>✓ {item}</li>)}
          </ul>
          <p className="mt-1 text-xs font-medium text-slate-700">Любое расхождение — план не публикуется.</p>
        </section>
        <div className="min-w-0 space-y-3">
          <NormPolicy normMode={normMode} factor={experiment?.travel_norm_max_factor} />
          {experiment && (
            <section aria-label="Воспроизводимость расчёта">
              <h4 className="text-sm font-semibold">Воспроизводимость</h4>
              <p className="mt-1 break-all font-mono text-[11px] leading-5 text-slate-600">
                {`снимок ${experiment.snapshot_sha256.slice(0, 12)} · матрица ${experiment.matrix_sha256.slice(0, 12)} · конфигурация ${experiment.config_sha256.slice(0, 12)} · лимит ${experiment.time_limit_seconds} с`}
              </p>
            </section>
          )}
        </div>
      </div>
    </Card>
  )
}
