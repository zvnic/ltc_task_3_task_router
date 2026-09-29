import type { Coordinates, EngineerRoute, ServiceRequest } from './types'

export function routeStopLocation(
  stop: EngineerRoute['stops'][number],
  requestById: ReadonlyMap<string, Pick<ServiceRequest, 'coordinates'>>,
): Coordinates | null {
  return stop.location ?? requestById.get(stop.request_id)?.coordinates ?? null
}

export function routeLocations(
  route: EngineerRoute,
  requestById: ReadonlyMap<string, Pick<ServiceRequest, 'coordinates'>>,
): Coordinates[] {
  return [
    route.start_location,
    ...route.stops.flatMap((stop) => {
      const location = routeStopLocation(stop, requestById)
      return location ? [location] : []
    }),
  ]
}
