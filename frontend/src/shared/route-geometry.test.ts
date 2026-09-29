import { describe, expect, it } from 'vitest'

import { routeLocations } from './route-geometry'
import type { EngineerRoute, ServiceRequest } from './types'

const route = {
  engineer_id: 'engineer-1',
  engineer_name: 'Инженер',
  transport: 'bicycle',
  start_location: { latitude: 55.7, longitude: 37.6 },
  stops: [{ request_id: 'request-1', location: { latitude: 55.71, longitude: 37.61 } }],
} as EngineerRoute

describe('routeLocations', () => {
  it('prefers the immutable plan snapshot over current request coordinates', () => {
    const requests = new Map([
      ['request-1', { coordinates: { latitude: 59, longitude: 30 } } as ServiceRequest],
    ])
    expect(routeLocations(route, requests)).toEqual([
      route.start_location,
      { latitude: 55.71, longitude: 37.61 },
    ])
  })

  it('falls back to request coordinates for legacy plans', () => {
    const legacy = { ...route, stops: [{ request_id: 'request-1' }] } as EngineerRoute
    const requests = new Map([
      ['request-1', { coordinates: { latitude: 55.72, longitude: 37.62 } } as ServiceRequest],
    ])
    expect(routeLocations(legacy, requests).at(-1)).toEqual({ latitude: 55.72, longitude: 37.62 })
  })
})
