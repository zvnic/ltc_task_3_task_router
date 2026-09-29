import { apiClient } from '../../shared/api'

const CACHE_KEY = 'beeline_auth_ok'

export function getAuthCache(): boolean {
  try {
    return localStorage.getItem(CACHE_KEY) === '1'
  } catch {
    return false
  }
}

export function setAuthCache(ok: boolean): void {
  try {
    if (ok) localStorage.setItem(CACHE_KEY, '1')
    else localStorage.removeItem(CACHE_KEY)
  } catch {
    /* ignore quota / private mode */
  }
}

export async function checkAuthenticated(): Promise<boolean> {
  try {
    const status = await apiClient.authStatus()
    setAuthCache(status.authenticated)
    return status.authenticated
  } catch {
    setAuthCache(false)
    return false
  }
}

export async function login(password: string): Promise<void> {
  await apiClient.login(password)
  setAuthCache(true)
}

export async function logout(): Promise<void> {
  try {
    await apiClient.logout()
  } finally {
    setAuthCache(false)
  }
}
