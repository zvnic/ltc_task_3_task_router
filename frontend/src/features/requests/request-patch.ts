import type { ServiceRequest } from '../../shared/types'

type RequestDraft = Pick<ServiceRequest, 'duration_minutes' | 'priority' | 'status'>

export function isNormControlledRequest(request: ServiceRequest): boolean {
  const enrichment = request.source_fields._enrichment
  if (!enrichment || typeof enrichment !== 'object' || Array.isArray(enrichment)) return false
  const normId = enrichment.norm_id
  return typeof normId === 'string' && Boolean(normId.trim())
}

export function buildRequestPatchPayload(
  request: ServiceRequest,
  revision: number,
  draft: RequestDraft,
) {
  return {
    expected_revision: revision,
    ...(isNormControlledRequest(request) ? {} : { duration_minutes: draft.duration_minutes }),
    priority: draft.priority,
    status: draft.status,
  }
}
