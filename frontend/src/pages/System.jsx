import React, { useState, useEffect, useCallback } from 'react'

const API = import.meta.env.VITE_API_URL || 'https://sc-recupero-api.onrender.com/api'

function StatusBadge({ status }) {
  const colors = {
    ok: 'bg-accent-green/15 text-accent-green',
    healthy: 'bg-accent-green/15 text-accent-green',
    configured: 'bg-accent-green/15 text-accent-green',
    imported: 'bg-accent-green/15 text-accent-green',
    error: 'bg-accent-red/15 text-accent-red',
    degraded: 'bg-accent-amber/15 text-accent-amber',
    warning: 'bg-accent-amber/15 text-accent-amber',
    not_configured: 'bg-[rgba(148,163,184,0.15)] text-txt-muted',
    unknown: 'bg-[rgba(148,163,184,0.15)] text-txt-muted',
    info: 'bg-accent-blue/15 text-accent-blue',
  }
  const labels = {
    ok: 'Attivo',
    healthy: 'Operativo',
    configured: 'Configurato',
    imported: 'Fatture Scaricate',
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
  if (level === 'critical') return <span className="text-accent-red text-lg">&#9888;</span>
  if (level === 'error') return <span className="text-accent-red text-lg">&#9888;</span>
  if (level === 'warning') return <span className="text-accent-amber text-lg">&#9888;</span>
  return <span className="text-accent-blue text-lg">&#9432;</span>
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
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-accent-teal"></div>
        <span className="ml-3 text-txt-muted">Caricamento diagnostica...</span>
      </div>
    )
  }

  if (error && !data) {
    return (
      <div className="bg-accent-red/10 border border-accent-red/20 rounded-xl p-6 text-accent-red">
        <p className="font-bold">Errore di connessione</p>
        <p className="text-sm mt-1">{error}</p>
        <button onClick={fetchData} className="mt-3 px-4 py-2 bg-accent-red text-dark-bg rounded-lg text-sm hover:brightness-110 font-medium">
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
          <p className="text-sm text-txt-muted mt-1">
            Diagnostica e stato di allineamento — aggiornato {timeAgo(data.timestamp)}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <StatusBadge status={data.status} />
          <button
            onClick={triggerSync}
            disabled={syncing}
            className={`sc-btn-primary ${syncing ? 'opacity-50 cursor-not-allowed' : ''}`}
          >
            {syncing ? 'Sync in corso...' : 'Forza Sync Completo'}
          </button>
          <button
            onClick={fetchData}
            className="sc-btn-secondary"
          >
            Aggiorna
          </button>
        </div>
      </div>

      {/* Alerts */}
      {alerts.length > 0 && (
        <div className="space-y-2">
          {criticals.map((a, i) => (
            <div key={`c-${i}`} className="bg-accent-red/10 border border-accent-red/20 rounded-xl p-4 flex items-start gap-3">
              <AlertIcon level="critical" />
              <div>
                <p className="font-semibold text-accent-red text-sm">{a.component}</p>
                <p className="text-accent-red/80 text-sm">{a.message}</p>
              </div>
            </div>
          ))}
          {errors.map((a, i) => (
            <div key={`e-${i}`} className="bg-accent-red/10 border border-accent-red/20 rounded-xl p-4 flex items-start gap-3">
              <AlertIcon level="error" />
              <div>
                <p className="font-semibold text-accent-red text-sm">{a.component}</p>
                <p className="text-accent-red/80 text-sm">{a.message}</p>
              </div>
            </div>
          ))}
          {warnings.map((a, i) => (
            <div key={`w-${i}`} className="bg-accent-amber/10 border border-accent-amber/20 rounded-xl p-4 flex items-start gap-3">
              <AlertIcon level="warning" />
              <div>
                <p className="font-semibold text-accent-amber text-sm">{a.component}</p>
                <p className="text-accent-amber/80 text-sm">{a.message}</p>
              </div>
            </div>
          ))}
        </div>
      )}

      {alerts.length === 0 && (
        <div className="bg-accent-green/10 border border-accent-green/20 rounded-xl p-4 flex items-center gap-3">
          <span className="text-accent-green text-xl">&#10003;</span>
          <p className="text-accent-green font-medium">Tutto operativo — nessun problema rilevato</p>
        </div>
      )}

      {/* Connectors */}
      <div className="sc-card overflow-hidden">
        <div className="sc-card-header bg-dark-surface">
          <h3 className="sc-section-title">Connettori</h3>
        </div>
        <div className="divide-y divide-dark-border">
          {Object.entries(connectors).map(([name, conn]) => (
            <div key={name} className="px-6 py-4 flex items-center justify-between">
              <div className="flex items-center gap-4">
                <div className="w-10 h-10 rounded-lg bg-dark-surface flex items-center justify-center text-lg font-bold text-txt-secondary">
                  {name === 'fatturapro' ? 'FP' : name === 'fattura24' ? 'F24' : name === 'shopify' ? 'SH' : 'TW'}
                </div>
                <div>
                  <p className="font-semibold text-txt-primary capitalize">{name}</p>
                  {conn.api_version && (
                    <p className="text-xs text-txt-muted">API v{conn.api_version}</p>
                  )}
                  {conn.status === 'imported' && conn.last_result?.imported_count && (
                    <p className="text-xs text-accent-green mt-0.5">{conn.last_result.imported_count} fatture importate via CSV</p>
                  )}
                  {conn.last_result && conn.last_result.error && conn.status !== 'imported' && (
                    <p className="text-xs text-accent-red mt-0.5 max-w-md truncate">{conn.last_result.error}</p>
                  )}
                  {conn.error && conn.status !== 'imported' && (
                    <p className="text-xs text-accent-red mt-0.5 max-w-lg truncate">{conn.error}</p>
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
        <div className="sc-card">
          <div className="sc-card-header bg-dark-surface">
            <h3 className="sc-section-title">Database</h3>
          </div>
          <div className="p-6 space-y-4">
            <div className="flex justify-between items-center">
              <span className="text-sm text-txt-secondary">Connessione</span>
              <StatusBadge status={database.connected ? 'ok' : 'error'} />
            </div>
            <div className="flex justify-between items-center">
              <span className="text-sm text-txt-secondary">Latenza</span>
              <span className="text-sm font-mono text-txt-primary">{database.latency_ms}ms</span>
            </div>
            <hr className="border-dark-border" />
            <div className="space-y-2">
              <div className="flex justify-between">
                <span className="text-sm text-txt-secondary">Clienti totali</span>
                <span className="text-sm font-semibold text-txt-primary">{database.tables.customers.total}</span>
              </div>
              <div className="flex justify-between pl-4">
                <span className="text-xs text-txt-muted">da Shopify</span>
                <span className="text-xs text-txt-secondary">{database.tables.customers.shopify}</span>
              </div>
              <div className="flex justify-between pl-4">
                <span className="text-xs text-txt-muted">auto-creati da fatture</span>
                <span className="text-xs text-txt-secondary">{database.tables.customers.auto_created}</span>
              </div>
            </div>
            <div className="space-y-2">
              <div className="flex justify-between">
                <span className="text-sm text-txt-secondary">Fatture totali</span>
                <span className="text-sm font-semibold text-txt-primary">{database.tables.invoices.total}</span>
              </div>
              <div className="flex justify-between pl-4">
                <span className="text-xs text-txt-muted">aperte</span>
                <span className="text-xs text-txt-secondary">{database.tables.invoices.open}</span>
              </div>
              <div className="flex justify-between pl-4">
                <span className="text-xs text-txt-muted">pagate</span>
                <span className="text-xs text-accent-green">{database.tables.invoices.paid}</span>
              </div>
              <div className="flex justify-between pl-4">
                <span className="text-xs text-txt-muted">associate a cliente</span>
                <span className="text-xs text-txt-secondary">{database.tables.invoices.matched}</span>
              </div>
              <div className="flex justify-between pl-4">
                <span className="text-xs text-txt-muted">senza cliente</span>
                <span className="text-xs text-accent-amber">{database.tables.invoices.unmatched}</span>
              </div>
              <div className="flex justify-between pl-4">
                <span className="text-xs text-txt-muted">FatturaPro</span>
                <span className="text-xs text-txt-secondary">{database.tables.invoices.fatturapro}</span>
              </div>
              <div className="flex justify-between pl-4">
                <span className="text-xs text-txt-muted">Fattura24</span>
                <span className="text-xs text-txt-secondary">{database.tables.invoices.fattura24}</span>
              </div>
            </div>
            <div className="flex justify-between">
              <span className="text-sm text-txt-secondary">Messaggi</span>
              <span className="text-sm font-semibold text-txt-primary">{database.tables.messages.total}</span>
            </div>
            <hr className="border-dark-border" />
            <div className="flex justify-between items-center">
              <span className="text-sm font-semibold text-txt-primary">Crediti aperti</span>
              <span className="text-lg font-bold text-accent-teal">
                EUR {database.totals.crediti_aperti.toLocaleString('it-IT', { minimumFractionDigits: 2 })}
              </span>
            </div>
          </div>
        </div>

        {/* Sync Pipeline */}
        <div className="sc-card">
          <div className="sc-card-header bg-dark-surface">
            <h3 className="sc-section-title">Pipeline Sync</h3>
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
                <div key={key} className="border border-dark-border rounded-lg p-3">
                  <div className="flex justify-between items-center">
                    <span className="text-sm font-semibold text-txt-primary">{labels[key]}</span>
                    <span className="text-xs text-txt-muted">{timeAgo(s.last_sync)}</span>
                  </div>
                  <p className="text-xs text-txt-secondary mt-1">{s.result_summary}</p>
                  {s.stale && (
                    <p className="text-xs text-accent-amber mt-1 font-medium">
                      Dati non aggiornati da più di 24h
                    </p>
                  )}
                </div>
              )
            })}
            <hr className="border-dark-border" />
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <span className="text-sm text-txt-secondary">Scheduler</span>
                <StatusBadge status={scheduler.running ? 'ok' : 'error'} />
              </div>
              <div className="flex justify-between">
                <span className="text-xs text-txt-muted">Sync giornaliero</span>
                <span className="text-xs text-txt-secondary">
                  {String(scheduler.scheduler_hour).padStart(2, '0')}:{String(scheduler.scheduler_minute).padStart(2, '0')} ({scheduler.timezone})
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Data Integrity */}
      <div className="sc-card overflow-hidden">
        <div className="sc-card-header bg-dark-surface">
          <h3 className="sc-section-title">Integrità Dati</h3>
        </div>
        <div className="divide-y divide-dark-border">
          {Object.entries(integrity).map(([key, check]) => (
            <div key={key} className="px-6 py-3 flex items-center justify-between">
              <div>
                <p className="text-sm text-txt-secondary">{check.description}</p>
              </div>
              <div className="flex items-center gap-3">
                <span className="text-sm font-mono font-semibold text-txt-primary">{check.count}</span>
                <StatusBadge status={check.status} />
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
