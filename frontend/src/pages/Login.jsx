import React, { useState } from 'react'

const API = import.meta.env.VITE_API_URL || 'https://sc-recupero-api.onrender.com/api'

export default function Login({ onLogin }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    setLoading(true)

    try {
      const res = await fetch(`${API}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })

      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || 'Credenziali non valide')
      }

      const data = await res.json()
      localStorage.setItem('sc_token', data.access_token)
      localStorage.setItem('sc_user', data.user)
      localStorage.setItem('sc_token_expires', data.expires_at)
      onLogin(data)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen bg-dark-bg flex items-center justify-center p-4">
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="flex flex-col items-center mb-8">
          <div className="w-14 h-14 rounded-2xl bg-accent-teal flex items-center justify-center text-dark-bg font-bold text-2xl mb-4">
            SC
          </div>
          <h1 className="text-xl font-semibold text-txt-primary">Recupero Crediti</h1>
          <p className="text-sm text-txt-muted mt-1">Accedi per continuare</p>
        </div>

        {/* Login Card */}
        <form onSubmit={handleSubmit} className="sc-card p-6 space-y-5">
          {/* Error */}
          {error && (
            <div className="bg-accent-red/10 border border-accent-red/20 rounded-lg px-4 py-3 text-sm text-accent-red">
              {error}
            </div>
          )}

          {/* Username */}
          <div>
            <label className="block text-xs font-medium text-txt-label uppercase tracking-wider mb-2">
              Utente
            </label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="sc-input w-full"
              placeholder="Nome utente"
              autoComplete="username"
              autoFocus
              required
            />
          </div>

          {/* Password */}
          <div>
            <label className="block text-xs font-medium text-txt-label uppercase tracking-wider mb-2">
              Password
            </label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="sc-input w-full"
              placeholder="Password"
              autoComplete="current-password"
              required
            />
          </div>

          {/* Submit */}
          <button
            type="submit"
            disabled={loading || !username || !password}
            className="w-full py-2.5 rounded-lg bg-accent-teal text-dark-bg font-semibold text-sm
              hover:brightness-110 transition-all disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {loading ? (
              <span className="flex items-center justify-center gap-2">
                <svg className="animate-spin w-4 h-4" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
                Accesso...
              </span>
            ) : (
              'Accedi'
            )}
          </button>
        </form>

        {/* Footer */}
        <p className="text-center text-xs text-txt-muted mt-6">
          Sake Company &middot; Gestione Crediti
        </p>
      </div>
    </div>
  )
}
