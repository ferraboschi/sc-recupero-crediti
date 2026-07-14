/**
 * Authenticated fetch wrapper.
 * Automatically adds JWT token from localStorage to all API requests.
 * If token is expired/invalid (401), clears auth and reloads the page.
 */

import config from '../config'

// Stessa sorgente di api/client.js: in dev il proxy Vite inoltra /api al
// backend locale; in prod VITE_API_BASE_URL punta al server Render.
// (Il vecchio fallback hardcoded all'URL di produzione faceva sì che i test
// locali colpissero silenziosamente il backend LIVE.)
export const API_BASE = config.API_BASE_URL ? `${config.API_BASE_URL}/api` : '/api'

export function getAuthHeaders() {
  const token = localStorage.getItem('sc_token')
  return token ? { Authorization: `Bearer ${token}` } : {}
}

export async function apiFetch(path, options = {}) {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  const headers = {
    ...getAuthHeaders(),
    ...(options.headers || {}),
  }

  // Add Content-Type for JSON bodies (but not for FormData)
  if (options.body && !(options.body instanceof FormData) && !headers['Content-Type']) {
    headers['Content-Type'] = 'application/json'
  }

  const res = await fetch(url, { ...options, headers })

  // If unauthorized, clear token and reload → shows login
  if (res.status === 401) {
    localStorage.removeItem('sc_token')
    localStorage.removeItem('sc_user')
    localStorage.removeItem('sc_token_expires')
    window.location.reload()
    throw new Error('Sessione scaduta')
  }

  return res
}
