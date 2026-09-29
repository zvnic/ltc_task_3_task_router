import { describe, expect, it } from 'vitest'

import { describeDistanceChange } from './comparison-metrics'

describe('describeDistanceChange', () => {
  it('does not call a distance increase a saving', () => {
    expect(describeDistanceChange(-12_600, -8.7)).toEqual({
      label: 'Пробег больше базового варианта',
      value: '12,6 км · 8,7%',
      tone: 'negative',
    })
  })

  it('describes a real reduction as less distance', () => {
    expect(describeDistanceChange(7_500, 5.2)).toEqual({
      label: 'Пробег меньше базового варианта',
      value: '7,5 км · 5,2%',
      tone: 'positive',
    })
  })
})
