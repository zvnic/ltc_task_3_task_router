import type { PlanDiff, ServiceRequest } from '../../shared/types'
import { transportPresentation } from '../../shared/transport'

export type RequestHighlightKind = 'emergency' | 'changed' | 'protected' | 'normal'

export type WorkClass = 'local' | 'connection' | 'add_order' | 'emergency'

export const WORK_CLASS_LABELS: Record<WorkClass, string> = {
  local: 'Локальная',
  connection: 'Подключение',
  add_order: 'Дозаказ',
  emergency: 'Авария',
}

const WORK_CLASS_VALUES = new Set<string>(['local', 'connection', 'add_order', 'emergency'])

export function isEmergencyRequest(request: Pick<ServiceRequest, 'priority' | 'source_fields'>) {
  const sourceTypes = [request.source_fields['Тип заявки'], request.source_fields['Тип']].filter(Boolean).map(String)
  return (
    request.priority === 'urgent'
    || sourceTypes.some((value) => value.toLocaleLowerCase('ru-RU').includes('авар'))
  )
}

function parseEnrichment(
  sourceFields: ServiceRequest['source_fields'] | undefined,
): Record<string, unknown> | null {
  const raw = sourceFields?._enrichment
  if (!raw) return null
  try {
    const parsed: unknown = typeof raw === 'string' ? JSON.parse(raw) : raw
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>
    }
  } catch {
    return null
  }
  return null
}

function workClassFromTypeLabel(value: string): WorkClass | null {
  const normalized = value.trim().toLocaleLowerCase('ru-RU')
  if (!normalized) return null
  if (normalized.includes('авар') || normalized.includes('emergency')) return 'emergency'
  if (normalized.includes('локал') || normalized.includes('local')) return 'local'
  if (normalized.includes('подключ') || normalized.includes('connection')) return 'connection'
  if (normalized.includes('дозаказ') || normalized.includes('add_order') || normalized.includes('add-order')) {
    return 'add_order'
  }
  return null
}

/** Classify request for badges: enrichment → urgent/emergency → skill → type labels → add_order. */
export function requestWorkClass(
  request: Pick<ServiceRequest, 'priority' | 'source_fields' | 'required_skill'>,
): WorkClass {
  const enrichment = parseEnrichment(request.source_fields)
  const enriched = enrichment?.work_class
  if (typeof enriched === 'string' && WORK_CLASS_VALUES.has(enriched)) {
    return enriched as WorkClass
  }

  if (request.priority === 'urgent' || isEmergencyRequest(request)) {
    return 'emergency'
  }

  if (request.required_skill === 'local') return 'local'
  if (request.required_skill === 'connection') return 'connection'
  if (request.required_skill === 'emergency') return 'emergency'

  const typeCandidates = [
    enrichment?.type_raw,
    request.source_fields?.['Тип заявки'],
    request.source_fields?.['Тип'],
    request.source_fields?.['type'],
    request.source_fields?.['Подтип'],
    request.source_fields?.['Type'],
  ]
  for (const candidate of typeCandidates) {
    if (typeof candidate !== 'string') continue
    const mapped = workClassFromTypeLabel(candidate)
    if (mapped) return mapped
  }

  return 'add_order'
}

export function workClassBadgeTone(workClass: WorkClass): 'red' | 'amber' | 'blue' | 'green' | 'gray' {
  switch (workClass) {
    case 'emergency':
      return 'red'
    case 'connection':
      return 'blue'
    case 'add_order':
      return 'amber'
    case 'local':
      return 'green'
    default:
      return 'gray'
  }
}

export function transportLabel(code: string | null | undefined): string | null {
  if (!code) return null
  return transportPresentation(code).shortLabel
}

/** True when plan.kind is a replan/correction of a working-day route. */
export function isReplanPlan(kind: string): boolean {
  const normalized = kind.trim().toLocaleLowerCase('en-US')
  return normalized.startsWith('replan_') || normalized.includes('replan')
}

export function requestHighlightKind(args: {
  request: Pick<ServiceRequest, 'priority' | 'source_fields'>
  protectedIds?: Iterable<string> | Set<string>
  changedIds?: Iterable<string> | Set<string>
  requestId?: string
}): RequestHighlightKind {
  if (isEmergencyRequest(args.request)) return 'emergency'
  const id = args.requestId
  if (id) {
    const changed = args.changedIds instanceof Set ? args.changedIds : new Set(args.changedIds ?? [])
    if (changed.has(id)) return 'changed'
    const protectedIds = args.protectedIds instanceof Set ? args.protectedIds : new Set(args.protectedIds ?? [])
    if (protectedIds.has(id)) return 'protected'
  }
  return 'normal'
}

/** Request IDs that moved engineer or sequence, or became newly assigned. */
export function changedRequestIdsFromDiff(diff: PlanDiff | null | undefined): string[] {
  if (!diff?.changes?.length) return []
  const ids: string[] = []
  for (const change of diff.changes) {
    const engineerChanged = change.previous_engineer_id !== change.new_engineer_id
    const sequenceChanged = change.previous_sequence !== change.new_sequence
    const newlyAssigned = change.previous_engineer_id == null && change.new_engineer_id != null
    if (engineerChanged || sequenceChanged || newlyAssigned) {
      ids.push(change.request_id)
    }
  }
  return ids
}
