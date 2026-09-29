import { describe, expect, it } from 'vitest'

import type { ServiceRequest } from '../../shared/types'
import { buildRequestPatchPayload } from './request-patch'

function serviceRequest(sourceFields: ServiceRequest['source_fields']): ServiceRequest {
  return {
    id: 'request-1',
    external_id: '73572',
    input_order: 0,
    address: 'Москва',
    district: 'Кузьминки',
    coordinates: { latitude: 55.7, longitude: 37.7 },
    coordinate_source: 'verified',
    duration_minutes: 30,
    expected_duration_minutes: 20,
    window_start: '2026-08-17T14:00:00+03:00',
    window_end: '2026-08-17T16:00:00+03:00',
    required_skill: 'local',
    required_transport: null,
    priority: 'normal',
    status: 'new',
    source_fields: sourceFields,
  }
}

describe('buildRequestPatchPayload', () => {
  it('omits duration owned by a norm while saving status and priority', () => {
    const request = serviceRequest({
      _enrichment: { norm_id: 'local_repair' },
    })

    expect(
      buildRequestPatchPayload(request, 7, {
        duration_minutes: 20,
        priority: 'normal',
        status: 'cancelled',
      }),
    ).toEqual({
      expected_revision: 7,
      priority: 'normal',
      status: 'cancelled',
    })
  })

  it('keeps duration editable for a request without a bound norm', () => {
    const request = serviceRequest({})

    expect(
      buildRequestPatchPayload(request, 7, {
        duration_minutes: 25,
        priority: 'urgent',
        status: 'new',
      }),
    ).toEqual({
      expected_revision: 7,
      duration_minutes: 25,
      priority: 'urgent',
      status: 'new',
    })
  })
})
