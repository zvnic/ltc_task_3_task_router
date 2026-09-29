import { describe, expect, it } from 'vitest'

import { getHouseNumberLabel } from './map-style'

describe('getHouseNumberLabel', () => {
  it.each([
    ['Город Москва, ул.Боровая, д. 14', 'д.14'],
    ['г. Москва, ул Юных Ленинцев, д 83с 4', 'д.83с'],
    ['Город Москва, ул.3-я Карачаровская, д. 8 к 2', 'д.8к2'],
    ['ул. Ленина, д.31 к 6', 'д.31к6'],
    ['ул. Ленина, д.31к2', 'д.31к2'],
    ['пр-т Мира, дом 12', 'д.12'],
  ])('extracts a Russian house number from %s', (address, expected) => {
    expect(getHouseNumberLabel(address)).toBe(expected)
  })

  it('returns an empty label when the address has no house number', () => {
    expect(getHouseNumberLabel('Москва')).toBe('')
  })

  it('falls back to the last standalone number', () => {
    expect(getHouseNumberLabel('ул. Строителей 42')).toBe('д.42')
  })
})
