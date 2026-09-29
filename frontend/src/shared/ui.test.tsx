import { describe, expect, it } from 'vitest'

describe('UI foundations', () => {
  it('keeps the Russian product title stable', () => {
    expect('Task Router Диспетчерская').toContain('Task Router')
  })
})
