import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { ServiceRequest } from '../../shared/types'
import { IncomingAddressField } from './IncomingAddressField'
import {
  addressFromDirectory,
  findDirectoryAddress,
  formatCoordinates,
  nearestDistrict,
  parseCoordinates,
  zoneAddresses,
} from './incoming-address'

const OFFICE = { latitude: 55.7048, longitude: 37.7662 }

function request(id: string, address: string, district: string, latitude: number, longitude: number): ServiceRequest {
  return {
    id,
    external_id: id.toUpperCase(),
    input_order: 1,
    address,
    district,
    coordinates: { latitude, longitude },
    coordinate_source: 'synthetic',
    duration_minutes: 30,
    window_start: '2026-09-29T09:00:00+03:00',
    window_end: '2026-09-29T18:00:00+03:00',
    required_skill: 'local',
    required_transport: null,
    priority: 'normal',
    status: 'new',
    source_fields: {},
  }
}

const REQUESTS = [
  request('far', 'Город Москва, ул. Таганская, д. 1', 'Таганский', 55.7417, 37.6536),
  request('near', 'Город Москва, пр-кт.Волгоградский, д. 97 к 1', 'Кузьминки', 55.7051, 37.7668),
  request('dup', 'Город Москва, пр-кт.Волгоградский, д. 97 к 1', 'Кузьминки', 55.7051, 37.7668),
]

describe('parseCoordinates', () => {
  it('reads coordinates copied from a map', () => {
    expect(parseCoordinates('55.707821, 37.751864')).toEqual({ latitude: 55.707821, longitude: 37.751864 })
    expect(parseCoordinates(' 55.7078 37.7519 ')).toEqual({ latitude: 55.7078, longitude: 37.7519 })
    expect(parseCoordinates('55,7078; 37,7519')).toEqual({ latitude: 55.7078, longitude: 37.7519 })
  })

  it('rejects text that is not exactly latitude and longitude', () => {
    expect(parseCoordinates('')).toBeNull()
    expect(parseCoordinates('55.7')).toBeNull()
    expect(parseCoordinates('55,7078, 37,7519')).toBeNull()
    expect(parseCoordinates('Волгоградский 97')).toBeNull()
    expect(parseCoordinates('95.1, 37.5')).toBeNull()
  })

  it('formats coordinates back in the same order', () => {
    expect(formatCoordinates({ latitude: 55.7078213, longitude: 37.7518641 })).toBe('55.707821, 37.751864')
    expect(parseCoordinates(formatCoordinates({ latitude: 55.7078213, longitude: 37.7518641 }))).toEqual({
      latitude: 55.707821,
      longitude: 37.751864,
    })
  })
})

describe('zone address directory', () => {
  const addresses = zoneAddresses(REQUESTS, OFFICE)

  it('deduplicates addresses and puts the nearest to the office first', () => {
    expect(addresses.map((item) => item.id)).toEqual(['near', 'far'])
    expect(addressFromDirectory(addresses[0]!)).toMatchObject({ origin: 'directory', coordinateSource: 'synthetic', district: 'Кузьминки' })
  })

  it('matches typed text to a directory address regardless of case and spaces', () => {
    expect(findDirectoryAddress(addresses, '  город москва,  пр-кт.Волгоградский, д. 97 к 1')?.id).toBe('near')
    expect(findDirectoryAddress(addresses, 'Город Москва, пр-кт.Волгоградский, д. 99')).toBeUndefined()
  })

  it('suggests the district of the nearest directory address only nearby', () => {
    expect(nearestDistrict(addresses, { latitude: 55.7078, longitude: 37.7519 })).toBe('Кузьминки')
    expect(nearestDistrict(addresses, { latitude: 54.8337, longitude: 38.1519 })).toBe('')
  })
})

describe('IncomingAddressField', () => {
  const addresses = zoneAddresses(REQUESTS, OFFICE)

  it('shows a free text address with directory suggestions and a demo coordinate note', () => {
    const html = renderToStaticMarkup(
      <IncomingAddressField addresses={addresses} office={OFFICE} value={addressFromDirectory(addresses[0]!)} onChange={() => undefined} />,
    )
    expect(html).toContain('<datalist')
    expect(html).toContain('value="Город Москва, пр-кт.Волгоградский, д. 97 к 1"')
    expect(html).toContain('Найти')
    expect(html).toContain('Координата из справочника зоны (демо-точка района)')
  })

  it('asks for coordinates when a new address has none', () => {
    const html = renderToStaticMarkup(
      <IncomingAddressField
        addresses={addresses}
        office={OFFICE}
        value={{ address: 'Москва, Волгоградский проспект, 97к1', district: '', coordinates: null, origin: 'manual', coordinateSource: 'verified' }}
        onChange={() => undefined}
      />,
    )
    expect(html).toContain('Координаты не заданы')
  })

  it('warns when the point is implausibly far from the office', () => {
    const html = renderToStaticMarkup(
      <IncomingAddressField
        addresses={addresses}
        office={OFFICE}
        value={{ address: 'Перепутаны широта и долгота', district: 'Кузьминки', coordinates: { latitude: 37.7519, longitude: 55.7078 }, origin: 'geocoder', coordinateSource: 'verified' }}
        onChange={() => undefined}
      />,
    )
    expect(html).toContain('дальше 150 км от офиса')
  })
})
