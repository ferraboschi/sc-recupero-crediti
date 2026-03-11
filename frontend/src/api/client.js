import axios from 'axios'
import config from '../config'

const client = axios.create({
  baseURL: config.API_BASE_URL ? `${config.API_BASE_URL}/api` : '/api',
  timeout: 30000,
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

// Response interceptor
client.interceptors.response.use(
  response => response,
  error => {
    if (error.response) {
      // Server responded with error status
      console.error('API Error:', error.response.status, error.response.data)
    } else if (error.request) {
      // Request was made but no response received
      console.error('Network Error:', error.request)
    } else {
      console.error('Error:', error.message)
    }
    return Promise.reject(error)
  }
)

export default client
