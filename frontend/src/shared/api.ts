import type {
  Algorithm,
  Dataset,
  DatasetSummary,
  DetailedEngineerRoute,
  Engineer,
  GeocodeResult,
  NormConfig,
  Plan,
  PlanComparison,
  PlanDiff,
  PlanningDirectories,
  RequestPlacements,
  ServiceRequest,
} from './types'

type RequestsResponse = { items: ServiceRequest[]; total: number }
const ROUTE_GEOMETRY_CLIENT_TIMEOUT_MS = 50_000

export class ApiError extends Error {
  status: number
  code?: string
  body?: unknown

  constructor(message: string, status: number, code?: string, body?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.body = body
  }
}

type ErrorPayload = {
  message?: unknown
  detail?: unknown
  code?: string
} | null

const NETWORK_ERROR_MESSAGE = 'Нет связи с сервером. Проверьте подключение и повторите.'

/**
 * Текст для ответа, в котором сервер не прислал своего сообщения. Диспетчер не должен
 * видеть «HTTP 500» или «Request failed with status code 404»: только что случилось и
 * что делать дальше.
 */
export function fallbackErrorMessage(status: number): string {
  if (status === 401) return 'Сессия закончилась. Войдите снова.'
  if (status === 403) return 'Для этого действия нет доступа.'
  if (status === 404) return 'Данные не найдены: возможно, их уже удалили. Обновите страницу.'
  if (status === 409) return 'Данные успели измениться. Обновите страницу и повторите.'
  if (status === 422) return 'Сервер не принял данные. Проверьте заполнение полей.'
  if (status === 408 || status === 504) return 'Сервер не успел ответить. Повторите через минуту.'
  if (status >= 500) return `Сервер временно не отвечает (код ${status}). Повторите через минуту.`
  return `Запрос не выполнен (код ${status}). Повторите позже.`
}

function errorMessage(payload: ErrorPayload, status: number): string {
  // detail у FastAPI бывает списком ошибок валидации — такой текст показывать нельзя
  for (const value of [payload?.message, payload?.detail]) {
    if (typeof value === 'string' && value.trim()) return value
  }
  return fallbackErrorMessage(status)
}

async function send(url: string, init: RequestInit): Promise<Response> {
  try {
    return await fetch(url, init)
  } catch (error) {
    // отмену запроса вызывающий код различает сам; всё остальное — нет сети
    if (isAbortError(error)) throw error
    throw new ApiError(NETWORK_ERROR_MESSAGE, 0, 'network_error')
  }
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await send(`/api/v1${path}`, {
    ...init,
    credentials: 'include',
    headers: { 'content-type': 'application/json', ...init?.headers },
  })
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as ErrorPayload
    const message = errorMessage(payload, response.status)
    let code = payload?.code
    if (
      response.status === 401 &&
      !path.startsWith('/auth/login') &&
      !path.startsWith('/auth/status')
    ) {
      code = code ?? 'unauthorized'
    }
    throw new ApiError(message, response.status, code, payload)
  }
  if (response.status === 204) {
    return undefined as T
  }
  return response.json() as Promise<T>
}

async function upload<T>(
  path: string,
  file: File,
  fields: Record<string, string> = {},
): Promise<T> {
  const body = new FormData()
  body.append('file', file)
  for (const [key, value] of Object.entries(fields)) {
    body.append(key, value)
  }
  const response = await send(`/api/v1${path}`, {
    method: 'POST',
    body,
    credentials: 'include',
  })
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as ErrorPayload
    throw new ApiError(errorMessage(payload, response.status), response.status, payload?.code, payload)
  }
  return response.json() as Promise<T>
}

function isAbortError(error: unknown): boolean {
  return typeof error === 'object'
    && error !== null
    && 'name' in error
    && error.name === 'AbortError'
}

async function engineerRoute(
  planId: string,
  engineerId: string,
  querySignal?: AbortSignal,
): Promise<DetailedEngineerRoute> {
  const controller = new AbortController()
  let timedOut = false
  const timeoutId = globalThis.setTimeout(() => {
    timedOut = true
    controller.abort()
  }, ROUTE_GEOMETRY_CLIENT_TIMEOUT_MS)
  const cancelRequest = () => controller.abort(querySignal?.reason)
  if (querySignal?.aborted) cancelRequest()
  else querySignal?.addEventListener('abort', cancelRequest, { once: true })

  try {
    return await api<DetailedEngineerRoute>(
      `/plans/${planId}/engineers/${encodeURIComponent(engineerId)}/route`,
      { signal: controller.signal },
    )
  } catch (error) {
    if (timedOut && isAbortError(error)) {
      throw new ApiError(
        'Точный маршрут не получен за отведённое время. Повторите запрос.',
        504,
        'routing_request_timeout',
      )
    }
    throw error
  } finally {
    globalThis.clearTimeout(timeoutId)
    querySignal?.removeEventListener('abort', cancelRequest)
  }
}

export type DatasetCreateResponse = {
  dataset_id: string
  title: string
  revision: number
  import_report: Record<string, unknown>
}

export type ImportIssue = {
  line: number
  code: string
  external_id?: string
  first_line?: number
  value?: string
}

export type ImportPreview = {
  total_rows: number
  valid_count: number
  skipped_empty: number
  errors: ImportIssue[]
  service_zone: { code: string; name: string } | null
  preview: Array<Record<string, unknown>>
}

export const apiClient = {
  authStatus: () => api<{ authenticated: boolean; auth_enabled: boolean }>('/auth/status'),
  login: (password: string) =>
    api<{ ok: boolean; authenticated: boolean; auth_enabled: boolean }>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),
  logout: () =>
    api<{ authenticated: boolean; auth_enabled: boolean }>('/auth/logout', {
      method: 'POST',
    }),

  algorithms: () => api<Algorithm[]>('/algorithms'),
  directories: () => api<PlanningDirectories>('/directories'),
  datasets: () => api<DatasetSummary[]>('/datasets'),
  dataset: (id: string) => api<Dataset>(`/datasets/${id}`),
  norms: (id: string) => api<NormConfig>(`/datasets/${id}/norms`),
  patchNorms: (
    id: string,
    payload: {
      expected_revision: number
      rows: Array<{
        norm_id: string
        travel_minutes: number
        technical_minutes: number
        paperwork_minutes: number
        expected_service_minutes: number
      }>
    },
  ) => api<NormConfig>(`/datasets/${id}/norms`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  }),
  requests: (id: string, filters?: { search?: string; district?: string; priority?: string }) => {
    const params = new URLSearchParams()
    if (filters?.search) params.set('search', filters.search)
    if (filters?.district) params.set('district', filters.district)
    if (filters?.priority) params.set('priority', filters.priority)
    const query = params.size ? `?${params.toString()}` : ''
    return api<RequestsResponse>(`/datasets/${id}/requests${query}`)
  },
  engineers: (id: string) => api<Engineer[]>(`/datasets/${id}/engineers`),
  // Координаты адреса новой заявки: только по кнопке «Найти», без автодополнения —
  // так требует правило публичного Nominatim.
  geocode: (query: string) => api<GeocodeResult>(`/geocode?q=${encodeURIComponent(query)}`),
  plans: (id: string) => api<Plan[]>(`/datasets/${id}/plans`),
  plan: (id: string) => api<Plan>(`/plans/${id}`),
  engineerRoute,
  comparison: (id: string) => api<PlanComparison>(`/plans/${id}/comparison`),
  diff: (id: string, basePlanId: string) =>
    api<PlanDiff>(`/plans/${id}/diff?base_plan_id=${encodeURIComponent(basePlanId)}`),
  createPlan: (id: string, algorithmIds: string[]) =>
    api<{
      plan_id: string
      baseline_plan_id: string
      selected_algorithm: string
      run_plan_ids: Record<string, string>
    }>(`/datasets/${id}/plans`, {
      method: 'POST',
      body: JSON.stringify({ algorithm_ids: algorithmIds }),
    }),
  replan: (planId: string, payload: unknown) =>
    api<{ event_id: string; plan: Plan; protected_request_ids: string[] }>(`/plans/${planId}/replan`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  // Места, куда отказанную заявку можно вставить без сдвига прежних визитов.
  placements: (planId: string, requestId: string) =>
    api<RequestPlacements>(
      `/plans/${planId}/placements?request_id=${encodeURIComponent(requestId)}`,
    ),
  // Ручное назначение: новый план kind="manual_insert", исходный план не меняется.
  assignRequest: (
    planId: string,
    payload: {
      request_id: string
      engineer_id: string
      position: number
      expected_revision: number
    },
  ) => api<Plan>(`/plans/${planId}/assign`, { method: 'POST', body: JSON.stringify(payload) }),
  // Только статус: длительность у заявок под нормативом менять нельзя (norm_controlled_duration).
  setRequestStatus: (
    id: string,
    payload: { expected_revision: number; status: 'rescheduled' | 'cancelled' },
  ) => api<{ revision: number }>(`/requests/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  previewImport: (file: File) =>
    upload<ImportPreview>('/datasets/import/preview', file),
  commitImport: (file: File, title: string, skipDuplicateRows = false) =>
    upload<DatasetCreateResponse>('/datasets/import', file, {
      title,
      skip_duplicate_rows: String(skipDuplicateRows),
    }),
  patchRequest: (
    id: string,
    payload: {
      expected_revision: number
      duration_minutes: number
      expected_duration_minutes?: number
      priority: 'normal' | 'urgent'
      status: import('./types').RequestStatus
    },
  ) => api<{ revision: number }>(`/requests/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  patchEngineer: (
    id: string,
    payload: { expected_revision: number; name: string; transport: string },
  ) => api<{ revision: number }>(`/engineers/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteDataset: (id: string) =>
    api<{
      dataset_id: string
      title: string
      deleted: {
        plans: number
        events: number
        requests: number
        engineers: number
        reference_assignments: number
      }
    }>(`/datasets/${id}`, { method: 'DELETE' }),
  deleteZone: (zoneCode: string) =>
    api<{
      zone_code: string
      zone_title: string
      count: number
      deleted_datasets: Array<{
        dataset_id: string
        title: string
        deleted: {
          plans: number
          events: number
          requests: number
          engineers: number
          reference_assignments: number
        }
      }>
    }>(`/zones/${encodeURIComponent(zoneCode)}`, { method: 'DELETE' }),
}
