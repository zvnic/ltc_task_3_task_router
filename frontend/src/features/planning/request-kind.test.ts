import { describe, expect, it } from 'vitest'

import type { PlanDiff, ServiceRequest } from '../../shared/types'
import {
  changedRequestIdsFromDiff,
  isEmergencyRequest,
  isReplanPlan,
  requestHighlightKind,
  requestWorkClass,
  transportLabel,
  WORK_CLASS_LABELS,
  workClassBadgeTone,
} from './request-kind'

function baseRequest(overrides: Partial<ServiceRequest> = {}): ServiceRequest {
  return {
    id: 'req-1',
    external_id: 'ext-1',
    input_order: 0,
    address: 'Test',
    district: 'Test',
    coordinates: { latitude: 55.7, longitude: 37.6 },
    coordinate_source: 'synthetic',
    duration_minutes: 60,
    window_start: '2024-01-01T09:00:00+03:00',
    window_end: '2024-01-01T18:00:00+03:00',
    required_skill: 'local',
    required_transport: null,
    priority: 'normal',
    status: 'new',
    source_fields: {},
    ...overrides,
  }
}

describe('isEmergencyRequest', () => {
  it('detects urgent priority', () => {
    expect(isEmergencyRequest(baseRequest({ priority: 'urgent' }))).toBe(true)
  })

  it('detects emergency type label', () => {
    expect(
      isEmergencyRequest(
        baseRequest({
          priority: 'normal',
          source_fields: { 'Тип заявки': 'Авария' },
        }),
      ),
    ).toBe(true)
  })

  it('returns false for ordinary local work', () => {
    expect(
      isEmergencyRequest(
        baseRequest({
          priority: 'normal',
          source_fields: { 'Тип': 'Локальная' },
        }),
      ),
    ).toBe(false)
  })
})

describe('requestWorkClass', () => {
  it('prefers source_fields._enrichment.work_class', () => {
    expect(
      requestWorkClass(
        baseRequest({
          priority: 'urgent',
          required_skill: 'emergency',
          source_fields: {
            _enrichment: JSON.stringify({ work_class: 'connection', type_raw: 'Авария' }),
          },
        }),
      ),
    ).toBe('connection')
  })

  it('maps urgent priority to emergency when enrichment missing', () => {
    expect(requestWorkClass(baseRequest({ priority: 'urgent', required_skill: 'local' }))).toBe(
      'emergency',
    )
  })

  it('maps required_skill when no enrichment', () => {
    expect(requestWorkClass(baseRequest({ required_skill: 'connection' }))).toBe('connection')
    expect(requestWorkClass(baseRequest({ required_skill: 'local' }))).toBe('local')
  })

  it('maps Russian type labels from source_fields', () => {
    expect(
      requestWorkClass(
        baseRequest({
          required_skill: '' as ServiceRequest['required_skill'],
          source_fields: { 'Тип': 'Подключение' },
        }),
      ),
    ).toBe('connection')
    expect(
      requestWorkClass(
        baseRequest({
          required_skill: '' as ServiceRequest['required_skill'],
          source_fields: { 'Тип заявки': 'Дозаказ' },
        }),
      ),
    ).toBe('add_order')
    expect(
      requestWorkClass(
        baseRequest({
          required_skill: '' as ServiceRequest['required_skill'],
          source_fields: { 'Тип': 'Локальная' },
        }),
      ),
    ).toBe('local')
  })

  it('falls back to add_order', () => {
    expect(
      requestWorkClass(
        baseRequest({
          required_skill: '' as ServiceRequest['required_skill'],
          source_fields: {},
        }),
      ),
    ).toBe('add_order')
  })
})

describe('workClassBadgeTone / labels / transportLabel', () => {
  it('returns expected badge tones', () => {
    expect(workClassBadgeTone('emergency')).toBe('red')
    expect(workClassBadgeTone('connection')).toBe('blue')
    expect(workClassBadgeTone('add_order')).toBe('amber')
    expect(workClassBadgeTone('local')).toBe('green')
  })

  it('exposes Russian labels', () => {
    expect(WORK_CLASS_LABELS.emergency).toBe('Авария')
    expect(WORK_CLASS_LABELS.local).toBe('Локальная')
  })

  it('maps transport codes', () => {
    expect(transportLabel('car')).toBe('Авто')
    expect(transportLabel('public_transport')).toBe('ОТ')
    expect(transportLabel('walking')).toBe('Пешком')
    expect(transportLabel('bicycle')).toBe('Вело')
    expect(transportLabel(null)).toBeNull()
    expect(transportLabel(undefined)).toBeNull()
  })
})

describe('isReplanPlan', () => {
  it('detects replan kinds', () => {
    expect(isReplanPlan('replan_midday')).toBe(true)
    expect(isReplanPlan('initial')).toBe(false)
  })
})

describe('requestHighlightKind', () => {
  it('prioritizes emergency over changed/protected', () => {
    expect(
      requestHighlightKind({
        request: baseRequest({ priority: 'urgent' }),
        requestId: 'req-1',
        changedIds: ['req-1'],
        protectedIds: ['req-1'],
      }),
    ).toBe('emergency')
  })

  it('marks changed then protected then normal', () => {
    expect(
      requestHighlightKind({
        request: baseRequest(),
        requestId: 'req-1',
        changedIds: ['req-1'],
      }),
    ).toBe('changed')
    expect(
      requestHighlightKind({
        request: baseRequest(),
        requestId: 'req-1',
        protectedIds: ['req-1'],
      }),
    ).toBe('protected')
    expect(requestHighlightKind({ request: baseRequest(), requestId: 'req-1' })).toBe('normal')
  })
})

describe('changedRequestIdsFromDiff', () => {
  it('collects moved and newly assigned request ids', () => {
    const diff = {
      changes: [
        {
          request_id: 'a',
          previous_engineer_id: 'e1',
          new_engineer_id: 'e2',
          previous_sequence: 1,
          new_sequence: 1,
        },
        {
          request_id: 'b',
          previous_engineer_id: 'e1',
          new_engineer_id: 'e1',
          previous_sequence: 1,
          new_sequence: 2,
        },
        {
          request_id: 'c',
          previous_engineer_id: null,
          new_engineer_id: 'e1',
          previous_sequence: null,
          new_sequence: 1,
        },
        {
          request_id: 'd',
          previous_engineer_id: 'e1',
          new_engineer_id: 'e1',
          previous_sequence: 3,
          new_sequence: 3,
        },
      ],
    } as PlanDiff
    expect(changedRequestIdsFromDiff(diff)).toEqual(['a', 'b', 'c'])
  })

  it('handles empty diff', () => {
    expect(changedRequestIdsFromDiff(undefined)).toEqual([])
    expect(changedRequestIdsFromDiff(null)).toEqual([])
  })
})
