import axios from 'axios'
import config from '../config'

const client = axios.create({
  baseURL: config.API_BASE_URL ? `${config.API_BASE_URL}/api` : '/api',
  timeout: 90000,
})

// Request interceptor
client.interceptors.request.use(
  config => {
    return config
  },
  error => {
    return Promise.reject(error)
  }
)

// Retry logic for cold starts (502/503) and network errors
// Render free tier can take 30-60s+ to wake from sleep
const MAX_RETRIES = 6
const RETRY_DELAY = 5000 // 5 seconds between retries (6 retries × 5s = 30s max wait)

function shouldRetry(error) {
  // Retry on 502/503 (Render cold start returns 502 HTML page)
  if (error.response && [502, 503].includes(error.response.status)) return true
  // Retry on network errors (server not yet responding)
  if (!error.response && error.request) return true
  return false
}

function delay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

// Response interceptor with auto-retry
client.interceptors.response.use(
  response => response,
  async error => {
    const cfg = error.config
    if (!cfg) return Promise.reject(error)

    cfg.__retryCount = cfg.__retryCount || 0

    if (shouldRetry(error) && cfg.__retryCount < MAX_RETRIES) {
      cfg.__retryCount += 1
      console.log(`[Retry ${cfg.__retryCount}/${MAX_RETRIES}] ${cfg.method?.toUpperCase()} ${cfg.url} — waiting ${RETRY_DELAY / 1000}s...`)
      await delay(RETRY_DELAY)
      return client(cfg)
    }

    // Final failure — log and reject
    if (error.response) {
      console.error('API Error:', error.response.status, error.response.data)
    } else if (error.request) {
      console.error('Network Error:', error.request)
    } else {
      console.error('Error:', error.message)
    }
    return Promise.reject(error)
  }
)

export default client
