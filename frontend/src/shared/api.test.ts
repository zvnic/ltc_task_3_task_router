import { afterEach, describe, expect, it, vi } from 'vitest'

import { apiClient } from './api'

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function abortableFetch() {
  return vi.fn((_input: string | URL | Request, init?: RequestInit) => {
    return new Promise<Response>((_resolve, reject) => {
      const signal = init?.signal
      if (!signal) throw new Error('Expected an AbortSignal')
      const rejectAbort = () => reject(signal.reason)
      if (signal.aborted) rejectAbort()
      else signal.addEventListener('abort', rejectAbort, { once: true })
    })
  })
}

describe('ошибки API понятны диспетчеру', () => {
  it('без сообщения сервера показывает, что случилось, а не «HTTP 500»', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('oops', { status: 500 })))
    await expect(apiClient.datasets()).rejects.toMatchObject({
      status: 500,
      message: 'Сервер временно не отвечает (код 500). Повторите через минуту.',
    })
  })

  it('берёт сообщение сервера, а список ошибок валидации не выводит', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({ message: 'Набор не найден.' }, { status: 404 })))
    await expect(apiClient.datasets()).rejects.toMatchObject({ message: 'Набор не найден.' })

    vi.stubGlobal('fetch', vi.fn(async () => Response.json({ detail: [{ loc: ['body'], msg: 'x' }] }, { status: 422 })))
    await expect(apiClient.datasets()).rejects.toMatchObject({
      message: 'Сервер не принял данные. Проверьте заполнение полей.',
    })
  })

  it('обрыв сети превращает в понятную ошибку', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch') }))
    await expect(apiClient.datasets()).rejects.toMatchObject({
      code: 'network_error',
      message: 'Нет связи с сервером. Проверьте подключение и повторите.',
    })
  })
})

describe('engineer route API', () => {
  it('cancels the network request when TanStack Query becomes inactive', async () => {
    const fetchMock = abortableFetch()
    vi.stubGlobal('fetch', fetchMock)
    const queryController = new AbortController()

    const request = apiClient.engineerRoute('plan-1', 'engineer-1', queryController.signal)
    queryController.abort()

    await expect(request).rejects.toMatchObject({ name: 'AbortError' })
    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('turns a client deadline into an actionable routing error', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', abortableFetch())

    const request = apiClient.engineerRoute('plan-1', 'engineer-1')
    const assertion = expect(request).rejects.toEqual(
      expect.objectContaining({
        code: 'routing_request_timeout',
        status: 504,
      }),
    )
    await vi.advanceTimersByTimeAsync(50_000)
    await assertion
  })
})
