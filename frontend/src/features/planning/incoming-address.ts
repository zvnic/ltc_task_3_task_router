import type { Coordinates, ServiceRequest } from '../../shared/types'
import { straightKm } from './plan-explain'

export type ZoneAddress = {
  id: string
  address: string
  district: string
  coordinates: Coordinates
  coordinateSource: ServiceRequest['coordinate_source']
  officeKm: number | null
}

/** Откуда у адреса новой заявки координата. */
export type AddressOrigin = 'directory' | 'geocoder' | 'manual'

export type IncomingAddress = {
  address: string
  district: string
  coordinates: Coordinates | null
  origin: AddressOrigin
  coordinateSource: ServiceRequest['coordinate_source']
}

/** Дальше этого от офиса точка почти наверняка введена с ошибкой (перепутаны широта и долгота). */
export const FAR_FROM_OFFICE_KM = 150

/**
 * Справочник адресов зоны для подсказок: адреса заявок с их координатами. Ближние к
 * офису идут первыми: аварию надо закончить за 100 минут, и у них больше шансов на
 * свободную бригаду.
 */
export function zoneAddresses(requests: ServiceRequest[], office?: Coordinates): ZoneAddress[] {
  const seen = new Set<string>()
  const items: ZoneAddress[] = []
  for (const request of requests) {
    if (seen.has(request.address)) continue
    seen.add(request.address)
    items.push({
      id: request.id,
      address: request.address,
      district: request.district,
      coordinates: request.coordinates,
      coordinateSource: request.coordinate_source,
      officeKm: office ? straightKm(office, request.coordinates) : null,
    })
  }
  return items.sort((left, right) => (left.officeKm ?? 0) - (right.officeKm ?? 0) || left.address.localeCompare(right.address, 'ru'))
}

export function addressFromDirectory(item: ZoneAddress): IncomingAddress {
  return {
    address: item.address,
    district: item.district,
    coordinates: item.coordinates,
    origin: 'directory',
    coordinateSource: item.coordinateSource,
  }
}

/** Адрес из справочника, если текст совпал с ним дословно (без учёта регистра и лишних пробелов). */
export function findDirectoryAddress(addresses: ZoneAddress[], text: string): ZoneAddress | undefined {
  const key = normalizeAddress(text)
  if (!key) return undefined
  return addresses.find((item) => normalizeAddress(item.address) === key)
}

function normalizeAddress(text: string) {
  return text.trim().replace(/\s+/g, ' ').toLocaleLowerCase('ru-RU')
}

/**
 * Координаты из строки «широта, долгота» — так их копируют из Яндекс и Google Карт
 * («55.707821, 37.751864»). Через точку с запятой допускается и десятичная запятая:
 * «55,7078; 37,7519». Порядок — сначала широта; вне диапазонов — null.
 */
export function parseCoordinates(text: string): Coordinates | null {
  const trimmed = text.trim()
  if (!trimmed) return null
  const parts = trimmed.includes(';')
    ? trimmed.split(';').map((part) => part.trim().replace(',', '.'))
    : trimmed.split(/[\s,]+/).filter(Boolean)
  if (parts.length !== 2) return null
  if (!parts.every((part) => /^[-+]?\d+(\.\d+)?$/.test(part))) return null
  const latitude = Number(parts[0])
  const longitude = Number(parts[1])
  if (Math.abs(latitude) > 90 || Math.abs(longitude) > 180) return null
  return { latitude, longitude }
}

export function formatCoordinates(coordinates: Coordinates) {
  return `${coordinates.latitude.toFixed(6)}, ${coordinates.longitude.toFixed(6)}`
}

/**
 * Район для точки, введённой координатами: район ближайшего адреса справочника, если
 * он не дальше `maxKm`, иначе пусто — диспетчер впишет сам.
 */
export function nearestDistrict(addresses: ZoneAddress[], coordinates: Coordinates, maxKm = 2): string {
  let best: { district: string; km: number } | null = null
  for (const item of addresses) {
    const km = straightKm(item.coordinates, coordinates)
    if (km <= maxKm && (!best || km < best.km)) best = { district: item.district, km }
  }
  return best?.district ?? ''
}
