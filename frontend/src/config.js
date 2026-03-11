/**
 * API Configuration
 * In production, the backend runs on a separate server.
 * In development, Vite proxy forwards /api to localhost:8000.
 */
const config = {
  // In production, set this to your backend server URL (e.g., https://api.sakecompany.com)
  // In development, leave empty to use Vite proxy
  API_BASE_URL: import.meta.env.VITE_API_BASE_URL || '',
}

export default config
