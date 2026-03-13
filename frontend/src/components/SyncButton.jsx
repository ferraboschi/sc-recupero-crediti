import React, { useState } from 'react'
import client from '../api/client'

export default function SyncButton() {
  const [loading, setLoading] = useState(false)
  const [message, setMessage] = useState('')

  const handleSync = async () => {
    setLoading(true)
    setMessage('')
    try {
      await client.post('/sync/full')
      setMessage('Sync avviato')
      setTimeout(() => setMessage(''), 3000)
    } catch (error) {
      setMessage(`Errore: ${error.message}`)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex items-center gap-2">
      <button
        onClick={handleSync}
        disabled={loading}
        className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
          loading
            ? 'bg-dark-card text-txt-muted cursor-not-allowed'
            : 'sc-btn-secondary'
        }`}
      >
        <svg className={`w-3.5 h-3.5 ${loading ? 'animate-spin-slow' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
        </svg>
        {loading ? 'Sync...' : 'Sync'}
      </button>
      {message && (
        <span className={`text-xs ${message.includes('Errore') ? 'text-accent-red' : 'text-accent-green'}`}>
          {message}
        </span>
      )}
    </div>
  )
}
