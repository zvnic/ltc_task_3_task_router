export type Coordinates = { latitude: number; longitude: number }

export type RequestStatus =
  | 'new'
  | 'assigned'
  | 'en_route'
  | 'arrived'
  | 'in_progress'
  | 'completed'
  | 'issue'
  | 'rescheduled'
  | 'cancelled'

export type Algorithm = {
  id: string
  title: string
  version: string
  description: string
  formula: string
  approach: string
  advantages: string[]
  disadvantages: string[]
}

export type DatasetSummary = {
  id: string
  title: string
  planning_date: string
  timezone: string
  revision: number
  service_zone: string | null
  service_zone_name: string | null
  request_count: number
  current_plan_id: string | null
}

export type Dataset = DatasetSummary & {
  office: Coordinates
  assumptions: {
    office_address?: string
    service_zone?: string
    service_zone_name?: string
    norm_version?: string
    norms_config?: NormConfig
    [key: string]: unknown
  }
  import_report: Record<string, unknown>
  engineer_count: number
}

export type NormRow = {
  norm_id: string
  title: string
  travel_minutes: number
  technical_minutes: number
  paperwork_minutes: number
  expected_service_minutes: number
  base_minutes: number
}

export type NormConfig = {
  revision: number
  version: string
  source_name: string
  source_sha256: string
  source_kind: 'customer_file' | 'dispatcher_settings'
  affected_requests?: number
  rows: NormRow[]
}

/** Найденный по адресу вариант для новой заявки (GET /geocode). */
export type GeocodeCandidate = { address: string; district: string; coordinates: Coordinates }
export type GeocodeResult = { query: string; provider: string; attribution: string; items: GeocodeCandidate[] }

export type ServiceRequest = {
  id: string
  external_id: string
  input_order: number
  address: string
  district: string
  coordinates: Coordinates
  // verified — подтверждена диспетчером или заказчиком, geocoded — найдена по адресу
  // в OpenStreetMap, synthetic — демо-точка района
  coordinate_source: 'synthetic' | 'geocoded' | 'verified'
  duration_minutes: number
  expected_duration_minutes?: number | null
  window_start: string
  window_end: string
  required_skill: 'local' | 'connection' | 'emergency'
  required_transport: string | null
  required_equipment?: Record<string, number>
  priority: 'normal' | 'urgent'
  status: RequestStatus
  completion_deadline?: string | null
  source_fields: Record<string, string | number | boolean | null | Record<string, unknown>>
  assigned_engineer_id?: string | null
  assigned_engineer_name?: string | null
  eta?: string | null
  unassigned_reason?: string | null
  plan_is_stale?: boolean
}

export type Engineer = {
  id: string
  external_id: string
  input_order: number
  name: string
  start_location: Coordinates
  shift_start: string
  shift_end: string
  skills: string[]
  transport: string
  equipment_inventory?: Record<string, number>
  is_synthetic: boolean
}

/**
 * Факты остановки — пояснения расчёта. Бэкенд кладёт сюда значения разных типов
 * (dict[str, Any]), поэтому ключи, которые читает интерфейс, описаны явно.
 */
export type RouteStopFacts = {
  travel_mode_from_previous?: string
  travel_mode_reason?: string
  // Норматив дороги до визита, мин; null — у заявки норматива нет.
  travel_norm_minutes?: number | null
  // Плечо до визита дольше норматива: бывает в TRAVEL_NORM_MODE=advisory и soft.
  travel_norm_exceeded?: boolean
  // max(0, дорога − норматив), мин; у планов до появления поля его нет.
  travel_norm_excess_minutes?: number
  [key: string]: unknown
}

export type RouteStop = {
  request_id: string
  sequence: number
  location?: Coordinates | null
  arrival_at: string
  service_start_at: string
  expected_service_end_at?: string | null
  service_end_at: string
  departure_at: string
  wait_minutes: number
  travel_minutes_from_previous: number
  distance_meters_from_previous: number
  reserve_minutes?: number
  // Коды объяснения визита от бэкенда: skill_match, time_window_feasible, gap_insertion…
  explanation_codes?: string[]
  facts: RouteStopFacts
}

export type EngineerRoute = {
  engineer_id: string
  engineer_name: string
  transport: string
  routing_method?: string
  routing_quality?: 'estimated' | 'cached' | 'exact'
  start_location: Coordinates
  departure_at: string
  stops: RouteStop[]
  distance_meters: number
  travel_minutes: number
  service_minutes: number
  wait_minutes: number
  finish_at: string
  explanation: string
}

export type DetailedRouteSegment = {
  sequence: number
  duration_seconds: number
  distance_meters: number
  geometry: { type: 'LineString'; coordinates: [number, number][] }
}

export type DetailedEngineerRoute = {
  engineer_id: string
  transport_type: string
  routing_method: string
  routing_quality: 'exact' | 'cached' | 'estimated'
  total_duration_seconds: number
  total_distance_meters: number
  geometry: { type: 'LineString'; coordinates: [number, number][] }
  segments: DetailedRouteSegment[]
}

export type PlanMetrics = {
  assigned_count: number
  unassigned_count: number
  urgent_unassigned_count: number
  normal_unassigned_count: number
  emergency_unassigned_count: number
  connection_unassigned_count: number
  routine_unassigned_count: number
  // Визиты, доехавшие дольше норматива дороги; у старых планов поля нет.
  norm_violation_count?: number
  // Сумма минут дороги сверх норматива по всем визитам. Планы, посчитанные до появления
  // поля, отдают 0 даже при превышениях — тогда показываем только их число.
  norm_excess_minutes?: number
  used_engineers_count: number
  total_distance_meters: number
  total_travel_minutes: number
}

/** Отказанная заявка плана: причина уже сформулирована бэкендом человеческим языком. */
export type UnassignedRequest = {
  request_id: string
  external_id: string
  priority: string
  priority_class?: string
  reason_code: string
  explanation: string
  details: Record<string, unknown>
}

/** Место ручной вставки отказанной заявки: прежние визиты при этом не двигаются. */
export type RequestPlacement = {
  engineer_id: string
  engineer_name: string
  // Индекс в маршруте бригады: 0 — перед первым визитом, len(stops) — после последнего.
  position: number
  arrival_at: string
  service_start_at: string
  service_end_at: string
  // Приросты бывают отрицательными: короткое плечо автобригада проходит пешком.
  added_distance_meters: number
  added_travel_minutes: number
  // Хотя бы одно из новых плеч идёт дольше норматива дороги.
  norm_exceeded: boolean
  // Прирост минут сверх норматива по маршруту бригады; бывает отрицательным, в hard — 0.
  added_norm_excess_minutes?: number
}

export type RequestPlacements = {
  request_id: string
  placements: RequestPlacement[]
}

export type SolverKpis = {
  completion_rate_percent: number
  urgent_completion_rate_percent: number | null
  distance_per_assigned_meters: number | null
  travel_minutes_per_assigned: number | null
  productive_utilization_percent: number
  used_engineer_utilization_percent: number
  travel_share_percent: number
  workload_cv: number
  workload_spread_minutes: number
  total_service_minutes: number
  total_wait_minutes: number
  available_shift_minutes: number
  makespan_minutes: number
  requests_per_used_engineer: number | null
  validator_violations: number
  travel_gap_to_best_known_percent?: number | null
  distance_gap_to_best_known_percent: number | null
}

/**
 * Роль норматива дороги в расчёте: advisory — справочный (показывается, не ограничивает),
 * soft — штраф за минуты сверх норматива, hard — запрет плеча.
 */
export type TravelNormMode = 'advisory' | 'soft' | 'hard'

/** Параметры модели дороги для режима транспорта — те же числа, которыми считает решатель. */
export type TransportModel = {
  code: string
  label: string
  speed_kmh: number
  path_factor: number
  suburban_speed_kmh?: number | null
  suburban_boarding_minutes?: number | null
  access_minutes?: number
  min_travel_minutes?: number
}

export type PlanningDirectories = {
  transports: TransportModel[]
  travel_model?: {
    route_estimation_method: string
    // Справочник расстояний по дорогам OpenStreetMap; null — всё по прямой × коэффициент.
    road_network?: {
      source: string
      attribution: string
      profiles: string[]
      points_by_zone: Record<string, number>
    } | null
    mkad_semi_axes_km: [number, number]
    car_local_walk_meters: number
    suburban_boarding_min_km?: number
    walking_leg_limit_meters: number
    // Предел велосипедного плеча; у бэкенда до него поля нет.
    bicycle_leg_limit_meters?: number
    travel_norm_mode: TravelNormMode
    travel_norm_max_factor: number
  }
}

export type SelectionReason = {
  selected_algorithm: string
  selected_tuple: number[]
  // Ключ, по которому планы сравнивались, и подписи его ступеней (PLAN_KEY_LABELS).
  selected_plan_key?: number[]
  plan_key_criteria?: string[]
  objective_mode?: string
  // Ручное назначение: решил диспетчер, а не цель.
  decided_by?: string
  reason_text?: string
  competitors: Array<{
    algorithm_id: string
    objective_tuple: number[]
    plan_key?: number[]
    decisive_metric: string
  }>
}

export type Plan = {
  id: string
  dataset_id: string
  input_revision: number
  parent_plan_id: string | null
  baseline_plan_id: string | null
  kind: string
  created_at: string
  model_info: {
    protected_request_ids?: string[]
    simulation?: boolean
    algorithm_id?: string
    selected?: boolean
    kpis?: SolverKpis
    selection_reason?: SelectionReason
    experiment?: {
      experiment_id: string
      algorithm_ids: string[]
      snapshot_sha256: string
      matrix_sha256: string
      config_sha256: string
      norm_versions: string[]
      time_limit_seconds: number
      // Как считалась дорога и чем был норматив дороги в этом расчёте.
      route_estimation_method?: string
      travel_norm_mode?: TravelNormMode
      travel_norm_max_factor?: number
      request_count?: number
      engineer_count?: number
    }
    [key: string]: unknown
  }
  result: {
    algorithm: string
    solution_status: string
    termination_reason: string
    elapsed_ms: number
    coordinate_quality: string
    routes: EngineerRoute[]
    unassigned: UnassignedRequest[]
    metrics: PlanMetrics
  }
  metrics: PlanMetrics
}

export type PlanComparison = {
  same_snapshot: boolean
  baseline: PlanMetrics
  selected: PlanMetrics
  distance_saving_meters: number
  distance_saving_percent: number | null
  note: string
}

export type PlanDiff = {
  changes: Array<{
    request_id: string
    previous_engineer_id: string | null
    new_engineer_id: string | null
    previous_sequence: number | null
    new_sequence: number | null
    previous_eta: string | null
    new_eta: string | null
  }>
  changed_engineer_ids: string[]
  distance_delta_meters: number
  assigned_delta: number
}
