import React, { useState, useEffect, useCallback } from 'react'

const API = import.meta.env.VITE_API_URL || 'https://sc-recupero-api.onrender.com/api'

function StatusBadge({ status }) {
  const colors = {
    ok: 'bg-green-100 text-green-800',
    healthy: 'bg-green-100 text-green-800',
    configured: 'bg-green-100 text-green-800',
    error: 'bg-red-100 text-red-800',
    degraded: 'bg-yellow-100 text-yellow-800',
    warning: 'bg-yellow-100 text-yellow-800',
    not_configured: 'bg-slate-100 text-slate-600',
    unknown: 'bg-slate-100 text-slate-500',
    info: 'bg-blue-100 text-blue-800',
  }
  const labels = {
    ok: 'Attivo',
    healthy: 'Operativo',
    configured: 'Configurato',
    error: 'Errore',
    degraded: 'Degradato',
    warning: 'Attenzione',
    not_configured: 'Non configurato',
    unknown: 'Sconosciuto',
    info: 'Info',
  }
  return (
    <span className={`px-2.5 py-1 rounded-full text-xs font-semibold ${colors[status] || colors.unknown}`}>
      {labels[status] || status}
    </span>
  )
}

function AlertIcon({ level }) {
  if (level === 'critical') return <span className="text-red-600 text-lg">&#9888;</span>
  if (level === 'error') return <span className="text-red-500 text-lg">&#9888;</span>
  if (level === 'warning') return <span className="text-yellow-500 text-lg">&#9888;</span>
  return <span className="text-blue-500 text-lg">&#9432;</span>
}

function timeAgo(isoStr) {
  if (!isoStr) return 'Mai'
  const d = new Date(isoStr + (isoStr.endsWith('Z') ? '' : 'Z'))
  const now = new Date()
  const diff = (now - d) / 1000
  if (diff < 60) return 'Adesso'
  if (diff < 3600) return `${Math.floor(diff / 60)} min fa`
  if (diff < 86400) return `${Math.floor(diff / 3600)} ore fa`
  return `${Math.floor(diff / 86400)} giorni fa`
}

export default function System() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [syncing, setSyncing] = useState(false)

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch(`${API}/system`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setData(await res.json())
      setError(null)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchData() }, [fetchData])

  const triggerSync = async () => {
    setSyncing(true)
    const beforeSync = data?.sync?.invoices?.last_sync || ''
    try {
      await fetch(`${API}/sync/full`, { method: 'POST' })
      // Poll every 5s, stop when sync timestamp changes or max 3 min
      let attempts = 0
      const poll = setInterval(async () => {
        attempts++
        try {
          const res = await fetch(`${API}/system`)
          if (res.ok) {
            const d = await res.json()
            setData(d)
            const afterSync = d?.sync?.invoices?.last_sync || ''
            if (afterSync && afterSync !== beforeSync) {
              clearInterval(poll)
              setSyncing(false)
            }
          }
        } catch { /* ignore polling errors */ }
        if (attempts >= 36) {
          clearInterval(poll)
          setSyncing(false)
          fetchData()
        }
      }, 5000)
    } catch {
      setSyncing(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
        <span className="ml-3 text-slate-600">Caricamento diagnostica...</span>
      </div>
    )
  }

  if (error && !data) {
    return (
      <div className="bg-red-50 border border-red-200 rounded-xl p-6 text-red-700">
        <p className="font-bold">Errore di connessione</p>
        <p className="text-sm mt-1">{error}</p>
        <button onClick={fetchData} className="mt-3 px-4 py-2 bg-red-600 text-white rounded-lg text-sm hover:bg-red-700">
          Riprova
        </button>
      </div>
    )
  }

  const { database, connectors, sync, integrity, scheduler, alerts } = data

  const criticals = alerts.filter(a => a.level === 'critical')
  const errors = alerts.filter(a => a.level === 'error')
  const warnings = alerts.filter(a => a.level === 'warning')

  return (
    <div className="space-y-6 max-w-7xl">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-slate-900">Sistema</h2>
          <p className="text-sm text-slate-500 mt-1">
            Diagnostica e stato di allineamento — aggiornato {timeAgo(data.timestamp)}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <StatusBadge status={data.status} />
          <button
            onClick={triggerSync}
            disabled={syncing}
            className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
              syncing
                ? 'bg-slate-200 text-slate-500 cursor-not-allowed'
                : 'bg-blue-600 text-white hover:bg-blue-700'
            }`}
          >
            {syncing ? 'Sync in corso...' : 'Forza Sync Completo'}
          </button>
          <button
            onClick={fetchData}
            className="px-4 py-2 bg-slate-100 text-slate-700 rounded-lg text-sm font-medium hover:bg-slate-200 transition-colors"
          >
            Aggiorna
          </button>
        </div>
      </div>

      {/* Alerts */}
      {alerts.length > 0 && (
        <div className="space-y-2">
          {criticals.map((a, i) => (
            <div key={`c-${i}`} className="bg-red-50 border border-red-200 rounded-xl p-4 flex items-start gap-3">
              <AlertIcon level="critical" />
              <div>
                <p className="font-semibold text-red-800 text-sm">{a.component}</p>
                <p className="text-red-700 text-sm">{a.message}</p>
              </div>
            </div>
          ))}
          {errors.map((a, i) => (
            <div key={`e-${i}`} className="bg-red-50 border border-red-200 rounded-xl p-4 flex items-start gap-3">
              <AlertIcon level="error" />
              <div>
                <p className="font-semibold text-red-700 text-sm">{a.component}</p>
                <p className="text-red-600 text-sm">{a.message}</p>
              </div>
            </div>
          ))}
          {warnings.map((a, i) => (
            <div key={`w-${i}`} className="bg-yellow-50 border border-yellow-200 rounded-xl p-4 flex items-start gap-3">
              <AlertIcon level="warning" />
              <div>
                <p className="font-semibold text-yellow-800 text-sm">{a.component}</p>
                <p className="text-yellow-700 text-sm">{a.message}</p>
              </div>
            </div>
          ))}
        </div>
      )}

      {alerts.length === 0 && (
        <div className="bg-green-50 border border-green-200 rounded-xl p-4 flex items-center gap-3">
          <span className="text-green-600 text-xl">&#10003;</span>
          <p className="text-green-800 font-medium">Tutto operativo — nessun problema rilevato</p>
        </div>
      )}

      {/* Connectors */}
      <div className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
        <div className="px-6 py-4 border-b border-slate-200 bg-slate-50">
          <h3 className="font-bold text-slate-800">Connettori</h3>
        </div>
        <div className="divide-y divide-slate-100">
          {Object.entries(connectors).map(([name, conn]) => (
            <div key={name} className="px-6 py-4 flex items-center justify-between">
              <div className="flex items-center gap-4">
                <div className="w-10 h-10 rounded-lg bg-slate-100 flex items-center justify-center text-lg font-bold text-slate-600">
                  {name === 'fatturapro' ? 'FP' : name === 'fattura24' ? 'F24' : name === 'shopify' ? 'SH' : 'TW'}
                </div>
                <div>
                  <p className="font-semibold text-slate-800 capitalize">{name}</p>
                  {conn.api_version && (
                    <p className="text-xs text-slate-500">API v{conn.api_version}</p>
                  )}
                  {conn.last_result && conn.last_result.error && (
                    <p className="text-xs text-red-500 mt-0.5 max-w-md truncate">{conn.last_result.error}</p>
                  )}
                  {conn.error && (
                    <p className="text-xs text-red-500 mt-0.5 max-w-lg truncate">{conn.error}</p>
                  )}
                </div>
              </div>
              <StatusBadge status={conn.status} />
            </div>
          ))}
        </div>
      </div>

      {/* Database & Data */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Database */}
        <div className="bg-white rounded-xl border border-slate-200 shadow-sm">
          <div className="px-6 py-4 border-b border-slate-200 bg-slate-50">
            <h3 className="font-bold text-slate-800">Database</h3>
          </div>
          <div className="p-6 space-y-4">
            <div className="flex justify-between items-center">
              <span className="text-sm text-slate-600">Connessione</span>
              <StatusBadge status={database.connected ? 'ok' : 'error'} />
            </div>
            <div className="flex justify-between items-center">
              <span className="text-sm text-slate-600">Latenza</span>
              <span className="text-sm font-mono text-slate-800">{database.latency_ms}ms</span>
            </div>
            <hr className="border-slate-100" />
            <div className="space-y-2">
              <div className="flex justify-between">
                <span className="text-sm text-slate-600">Clienti totali</span>
                <span className="text-sm font-semibold">{database.tables.customers.total}</span>
              </div>
              <div className="flex justify-between pl-4">
                <span className="text-xs text-slate-500">da Shopify</span>
                <span className="text-xs text-slate-600">{database.tables.customers.shopify}</span>
              </div>
              <div className="flex justify-between pl-4">
                <span className="text-xs text-slate-500">auto-creati da fatture</span>
                <span className="text-xs text-slate-600">{database.tables.customers.auto_created}</span>
              </div>
            </div>
            <div className="space-y-2">
              <div className="flex justify-between">
                <span className="text-sm text-slate-600">Fatture totali</span>
                <span className="text-sm font-semibold">{database.tables.invoices.total}</span>
              </div>
              <div className="flex justify-between pl-4">
                <span className="text-xs text-slate-500">aperte</span>
                <span className="text-xs text-slate-600">{database.tables.invoices.open}</span>
              </div>
              <div className="flex justify-between pl-4">
                <span className="text-xs text-slate-500">pagate</span>
                <span className="text-xs text-green-600">{database.tables.invoices.paid}</span>
              </div>
              <div className="flex justify-between pl-4">
                <span className="text-xs text-slate-500">associate a cliente</span>
                <span className="text-xs text-slate-600">{database.tables.invoices.matched}</span>
              </div>
              <div className="flex justify-between pl-4">
                <span className="text-xs text-slate-500">senza cliente</span>
                <span className="text-xs text-yellow-600">{database.tables.invoices.unmatched}</span>
              </div>
              <div className="flex justify-between pl-4">
                <span className="text-xs text-slate-500">FatturaPro</span>
                <span className="text-xs text-slate-600">{database.tables.invoices.fatturapro}</span>
              </div>
              <div className="flex justify-between pl-4">
                <span className="text-xs text-slate-500">Fattura24</span>
                <span className="text-xs text-slate-600">{database.tables.invoices.fattura24}</span>
              </div>
            </div>
            <div className="flex justify-between">
              <span className="text-sm text-slate-600">Messaggi</span>
              <span className="text-sm font-semibold">{database.tables.messages.total}</span>
            </div>
            <hr className="border-slate-100" />
            <div className="flex justify-between items-center">
              <span className="text-sm font-semibold text-slate-700">Crediti aperti</span>
              <span className="text-lg font-bold text-blue-700">
                EUR {database.totals.crediti_aperti.toLocaleString('it-IT', { minimumFractionDigits: 2 })}
              </span>
            </div>
          </div>
        </div>

        {/* Sync Pipeline */}
        <div className="bg-white rounded-xl border border-slate-200 shadow-sm">
          <div className="px-6 py-4 border-b border-slate-200 bg-slate-50">
            <h3 className="font-bold text-slate-800">Pipeline Sync</h3>
          </div>
          <div className="p-6 space-y-4">
            {['invoices', 'customers', 'matching', 'escalations'].map(key => {
              const s = sync[key]
              const labels = {
                invoices: 'Fatture',
                customers: 'Clienti',
                matching: 'Associazione',
                escalations: 'Escalation',
              }
              return (
                <div key={key} className="border border-slate-100 rounded-lg p-3">
                  <div className="flex justify-between items-center">
                    <span className="text-sm font-semibold text-slate-700">{labels[key]}</span>
                    <span className="text-xs text-slate-500">{timeAgo(s.last_sync)}</span>
                  </div>
                  <p className="text-xs text-slate-600 mt-1">{s.result_summary}</p>
                  {s.stale && (
                    <p className="text-xs text-yellow-600 mt-1 font-medium">
                      Dati non aggiornati da più di 24h
                    </p>
                  )}
                </div>
              )
            })}
            <hr className="border-slate-100" />
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <span className="text-sm text-slate-600">Scheduler</span>
                <StatusBadge status={scheduler.running ? 'ok' : 'error'} />
              </div>
              <div className="flex justify-between">
                <span className="text-xs text-slate-500">Sync giornaliero</span>
                <span className="text-xs text-slate-600">
                  {String(scheduler.scheduler_hour).padStart(2, '0')}:{String(scheduler.scheduler_minute).padStart(2, '0')} ({scheduler.timezone})
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Data Integrity */}
      <div className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
        <div className="px-6 py-4 border-b border-slate-200 bg-slate-50">
          <h3 className="font-bold text-slate-800">Integrità Dati</h3>
        </div>
        <div className="divide-y divide-slate-100">
          {Object.entries(integrity).map(([key, check]) => (
            <div key={key} className="px-6 py-3 flex items-center justify-between">
              <div>
                <p className="text-sm text-slate-700">{check.description}</p>
              </div>
              <div className="flex items-center gap-3">
                <span className="text-sm font-mono font-semibold text-slate-800">{check.count}</span>
                <StatusBadge status={check.status} />
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
