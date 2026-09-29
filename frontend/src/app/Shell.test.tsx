import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { LogoutButton } from './Shell'

describe('LogoutButton', () => {
  it('is hidden when authentication is disabled', () => {
    const markup = renderToStaticMarkup(<LogoutButton visible={false} onLogout={vi.fn()} />)

    expect(markup).toBe('')
  })

  it('is shown when authentication is enabled', () => {
    const markup = renderToStaticMarkup(<LogoutButton visible onLogout={vi.fn()} />)

    expect(markup).toContain('Выйти')
  })
})
