import React, { useState, useEffect, useCallback } from 'react'
import { Link } from 'react-router-dom'

import { API_BASE as API } from '../utils/api'

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

function fmtEur(n) {
  return (n ?? 0).toLocaleString('it-IT', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

// ── Audit abbinamenti: legenda + confronto affiancato ────────────────

function AuditLegend() {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
      <div className="border border-accent-red/20 bg-accent-red/5 rounded-lg p-3">
        <p className="text-sm font-semibold text-accent-red mb-1">Critico</p>
        <p className="text-xs text-txt-secondary leading-relaxed">
          Quasi certamente la fattura è finita sul cliente sbagliato: la P.IVA
          della fattura è <strong>diversa</strong> da quella del cliente, oppure i
          nomi sono <strong>del tutto diversi</strong>, oppure la P.IVA coincide
          ma i nomi no (probabile P.IVA errata su uno dei due). Da correggere:
          scollega o riassegna.
        </p>
      </div>
      <div className="border border-accent-amber/20 bg-accent-amber/5 rounded-lg p-3">
        <p className="text-sm font-semibold text-accent-amber mb-1">Dubbio</p>
        <p className="text-xs text-txt-secondary leading-relaxed">
          Non è garantito che sia lo stesso soggetto: <strong>manca la P.IVA</strong> da
          un lato (es. la fattura ce l&apos;ha e il cliente no), oppure i nomi
          sono <strong>simili ma non identici</strong>, oppure la fattura non
          riporta un nome. Serve un controllo manuale.
        </p>
      </div>
    </div>
  )
}

// Stato del confronto (spunta verde / croce rossa / trattino neutro).
function pivaStatus(v) {
  if (!v) return 'muted'
  if (v.piva_match) return 'ok'
  if (v.piva_conflict) return 'bad'
  return 'muted'
}

function nameStatus(v) {
  if (!v) return 'muted'
  if (v.name_equivalent) return 'ok'
  if (v.name_score !== null && v.name_score !== undefined) return 'bad'
  return 'muted'
}

function CompareLine({ label, value, status }) {
  return (
    <div className="flex items-center gap-1.5">
      {status === 'ok' && <span className="text-accent-green">&#10003;</span>}
      {status === 'bad' && <span className="text-accent-red">&#10007;</span>}
      {(!status || status === 'muted') && <span className="text-txt-muted">&#8211;</span>}
      <span className="text-txt-muted">{label}:</span>
      <span className="text-txt-secondary font-mono truncate">{value || '—'}</span>
    </div>
  )
}

export default function System() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [syncing, setSyncing] = useState(false)
  const [reconciling, setReconciling] = useState(false)
  const [reconcileMsg, setReconcileMsg] = useState(null)
  const [audit, setAudit] = useState(null)
  const [auditLoading, setAuditLoading] = useState(false)
  const [auditError, setAuditError] = useState(null)
  const [unlinkingId, setUnlinkingId] = useState(null)
  const [actioningId, setActioningId] = useState(null)
  const [includeReviewed, setIncludeReviewed] = useState(false)
  const [collapsedGroups, setCollapsedGroups] = useState({})

  const authHeaders = () => {
    const token = localStorage.getItem('sc_token')
    return token ? { Authorization: `Bearer ${token}` } : {}
  }

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch(`${API}/system`, { headers: authHeaders() })
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

  // opts.includeReviewed permette al toggle di rieseguire l'audit col valore
  // nuovo senza aspettare il re-render (setState è asincrono).
  const runAudit = async (opts = {}) => {
    const incRev = opts.includeReviewed ?? includeReviewed
    setAuditLoading(true)
    setAuditError(null)
    try {
      const res = await fetch(
        `${API}/system/match-audit?only_problems=true&limit=100&include_reviewed=${incRev}`,
        { headers: authHeaders() }
      )
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setAudit(await res.json())
    } catch (e) {
      setAuditError(e.message)
    } finally {
      setAuditLoading(false)
    }
  }

  const toggleReviewed = () => {
    const next = !includeReviewed
    setIncludeReviewed(next)
    runAudit({ includeReviewed: next })
  }

  const toggleGroup = (customerId) => {
    setCollapsedGroups(prev => ({ ...prev, [customerId]: !prev[customerId] }))
  }

  const unlinkInvoice = async (item) => {
    const ok = window.confirm(
      `Scollegare la fattura ${item.invoice_number} dal cliente "${item.customer_name}"?`
    )
    if (!ok) return
    setUnlinkingId(item.invoice_id)
    try {
      const res = await fetch(`${API}/positions/${item.invoice_id}/unlink`, { method: 'POST', headers: authHeaders() })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await runAudit()
      fetchData()
    } catch (e) {
      setAuditError(e.message)
    } finally {
      setUnlinkingId(null)
    }
  }

  const assignPiva = async (item) => {
    const piva = item.verification?.invoice_piva || item.customer_piva_raw
    const ok = window.confirm(
      `Assegnare la P.IVA ${piva} (presa dalla fattura ${item.invoice_number}) ` +
      `al cliente "${item.customer_name}"?\n\n` +
      `Serve quando la fattura ha una P.IVA valida ma il cliente in anagrafica ` +
      `non ne ha una: così i prossimi abbinamenti saranno garantiti per P.IVA.`
    )
    if (!ok) return
    setActioningId(item.invoice_id)
    try {
      const res = await fetch(`${API}/positions/${item.invoice_id}/assign-piva-to-customer`, { method: 'POST', headers: authHeaders() })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || `HTTP ${res.status}`)
      }
      await runAudit()
      fetchData()
    } catch (e) {
      setAuditError(e.message)
    } finally {
      setActioningId(null)
    }
  }

  const markReviewed = async (item) => {
    setActioningId(item.invoice_id)
    try {
      const res = await fetch(`${API}/positions/${item.invoice_id}/mark-reviewed`, { method: 'POST', headers: authHeaders() })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await runAudit()
      fetchData()
    } catch (e) {
      setAuditError(e.message)
    } finally {
      setActioningId(null)
    }
  }

  const unmarkReviewed = async (item) => {
    setActioningId(item.invoice_id)
    try {
      const res = await fetch(`${API}/positions/${item.invoice_id}/unmark-reviewed`, { method: 'POST', headers: authHeaders() })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await runAudit()
      fetchData()
    } catch (e) {
      setAuditError(e.message)
    } finally {
      setActioningId(null)
    }
  }

  const triggerSync = async () => {
    setSyncing(true)
    const beforeSync = data?.sync?.invoices?.last_sync || ''
    try {
      await fetch(`${API}/sync/full`, { method: 'POST', headers: authHeaders() })
      let attempts = 0
      const poll = setInterval(async () => {
        attempts++
        try {
          const res = await fetch(`${API}/system`, { headers: authHeaders() })
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

  // "Aggiorna incassi adesso": chiama il reconcile SINCRONO (due passaggi di
  // rilevamento pagamenti), mostra l'esito e ricarica. Disabilitato durante
  // l'esecuzione per evitare il doppio invio.
  const triggerReconcile = async () => {
    setReconciling(true)
    setReconcileMsg(null)
    try {
      const res = await fetch(`${API}/sync/reconcile-incassi`, { method: 'POST', headers: authHeaders() })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const d = await res.json()
      // Verde SOLO per incassi davvero registrati (confermati); ambra per
      // lista incompleta; muto quando non c'è nulla di nuovo.
      const tone = d.partial ? 'warning' : (d.marked_paid > 0 ? 'success' : 'muted')
      setReconcileMsg({ text: d.message, tone })
      await fetchData()
    } catch (e) {
      setReconcileMsg({ text: `Errore: ${e.message}`, tone: 'warning' })
    } finally {
      setReconciling(false)
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
          {/* Esito reconcile ACCANTO ai pulsanti (non sopra): niente
              layout shift, i pulsanti non si spostano. */}
          {reconcileMsg && (
            <span className={`text-sm font-medium ${
              reconcileMsg.tone === 'success' ? 'text-accent-green'
                : reconcileMsg.tone === 'warning' ? 'text-accent-amber'
                : 'text-txt-muted'
            }`}>
              {reconcileMsg.text}
            </span>
          )}
          <StatusBadge status={data.status} />
          <button
            onClick={triggerSync}
            disabled={syncing || reconciling}
            className={`sc-btn-primary ${(syncing || reconciling) ? 'opacity-50 cursor-not-allowed' : ''}`}
          >
            {syncing ? 'Sync in corso...' : 'Forza Sync Completo'}
          </button>
          <button
            onClick={triggerReconcile}
            disabled={reconciling || syncing}
            className={`sc-btn-secondary ${(reconciling || syncing) ? 'opacity-50 cursor-not-allowed' : ''}`}
            title="Esegue due passaggi di rilevamento pagamenti: gli incassi già registrati in FatturaPro si vedono subito."
          >
            {reconciling ? 'Aggiornamento incassi…' : 'Aggiorna incassi adesso'}
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
                  {name === 'fatturapro' ? 'FP' : name === 'fattura24' ? 'F24' : name === 'shopify' ? 'SH' : name.slice(0, 2).toUpperCase()}
                </div>
                <div>
                  <p className="font-semibold text-txt-primary capitalize">
                    {name}
                  </p>
                  {conn.api_version && (
                    <p className="text-xs text-txt-muted">API v{conn.api_version}</p>
                  )}
                  {conn.status === 'imported' && conn.last_result?.imported_count && (
                    <p className="text-xs text-accent-green mt-0.5">{conn.last_result.imported_count} fatture importate via CSV</p>
                  )}
                  {conn.last_result?.error && conn.status !== 'imported' && (
                    <p className="text-xs text-accent-red mt-0.5 max-w-md truncate">{conn.last_result.error}</p>
                  )}
                  {conn.error && conn.status !== 'imported' && (
                    <p className="text-xs text-accent-red mt-0.5 max-w-lg truncate">{conn.error}</p>
                  )}
                  {conn.note && (
                    <p className="text-xs text-txt-muted mt-0.5 max-w-lg truncate">{conn.note}</p>
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
              <span className="text-sm text-txt-secondary">Pratiche aperte</span>
              <span className="text-sm font-semibold text-txt-primary">{database.tables.cases?.open ?? 0}</span>
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
            {['invoices', 'customers', 'matching', 'cases'].map(key => {
              const s = sync[key]
              if (!s) return null
              const labels = {
                invoices: 'Fatture',
                customers: 'Clienti',
                matching: 'Associazione',
                cases: 'Pratiche',
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
              <div className="flex justify-between">
                <span className="text-xs text-txt-muted">Prossimo sync orario</span>
                <span className="text-xs text-txt-secondary">
                  {scheduler.next_run_times?.hourly_sync_job
                    ? new Date(scheduler.next_run_times.hourly_sync_job).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' })
                    : '—'}
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

      {/* Match Audit — strumento d'indagine raggruppato per cliente */}
      <div className="sc-card overflow-hidden">
        <div className="sc-card-header bg-dark-surface flex items-center justify-between">
          <h3 className="sc-section-title">Audit abbinamenti</h3>
          <button
            onClick={() => runAudit()}
            disabled={auditLoading}
            className={`sc-btn-secondary ${auditLoading ? 'opacity-50 cursor-not-allowed' : ''}`}
          >
            {auditLoading ? 'Audit in corso...' : (audit ? 'Ripeti audit' : 'Esegui audit')}
          </button>
        </div>

        {auditError && (
          <div className="px-6 py-3 text-sm text-accent-red border-b border-dark-border">
            Errore audit: {auditError}
          </div>
        )}

        {!audit && !auditLoading && !auditError && (
          <div className="p-6 text-sm text-txt-muted">
            Controlla che ogni fattura sia sul cliente giusto. L&apos;audit
            raggruppa i problemi per cliente e spiega il perché di ognuno.
            Premi &quot;Esegui audit&quot; per avviare il controllo.
          </div>
        )}

        {audit && (
          <div className="p-6 space-y-5">
            {/* Legenda sempre visibile: cosa vuol dire Critico / Dubbio */}
            <AuditLegend />

            {/* Conteggi per fattura */}
            <div className="grid grid-cols-3 gap-4">
              <div className="text-center">
                <p className="text-2xl font-bold text-accent-green">{audit.counts?.ok ?? 0}</p>
                <p className="text-xs text-txt-muted mt-1">OK</p>
              </div>
              <div className="text-center">
                <p className="text-2xl font-bold text-accent-amber">{audit.counts?.warn ?? 0}</p>
                <p className="text-xs text-txt-muted mt-1">Dubbi</p>
              </div>
              <div className="text-center">
                <p className="text-2xl font-bold text-accent-red">{audit.counts?.bad ?? 0}</p>
                <p className="text-xs text-txt-muted mt-1">Critici</p>
              </div>
            </div>
            <p className="text-xs text-txt-muted text-center">
              Analizzate {audit.total_audited} fatture · {audit.total_problems} problematiche
              {' · '}{audit.reviewed_count ?? 0} già verificate
            </p>

            <label className="flex items-center justify-center gap-2 text-xs text-txt-secondary cursor-pointer">
              <input
                type="checkbox"
                checked={includeReviewed}
                onChange={toggleReviewed}
                className="accent-accent-teal"
              />
              Mostra anche le fatture già verificate
            </label>

            {/* Gruppi per cliente */}
            {(audit.groups?.length ?? 0) === 0 ? (
              <div className="bg-accent-green/10 border border-accent-green/20 rounded-xl p-4 text-center">
                <p className="text-accent-green font-medium">
                  {includeReviewed
                    ? 'Nessun abbinamento da controllare ✓'
                    : 'Nessun problema da verificare ✓'}
                </p>
              </div>
            ) : (
              <>
                <p className="text-xs text-txt-muted">
                  {audit.groups.length} {audit.groups.length === 1 ? 'cliente' : 'clienti'} con abbinamenti da controllare
                </p>
                <div className="space-y-3">
                  {audit.groups.map(group => {
                    const expanded = !collapsedGroups[group.customer_id]
                    return (
                      <div key={group.customer_id} className="border border-dark-border rounded-lg overflow-hidden">
                        {/* Header cliente: nome, verdetto peggiore, N su M */}
                        <div
                          className="px-4 py-3 bg-dark-surface flex items-center justify-between gap-3 cursor-pointer hover:brightness-110"
                          onClick={() => toggleGroup(group.customer_id)}
                        >
                          <div className="flex items-center gap-3 min-w-0">
                            <span className="text-txt-muted text-xs w-3">{expanded ? '▾' : '▸'}</span>
                            <div className="min-w-0">
                              <div className="flex items-center gap-2 flex-wrap">
                                <Link
                                  to={`/customers/${group.customer_id}`}
                                  onClick={e => e.stopPropagation()}
                                  className="font-semibold text-accent-teal hover:text-accent-cyan truncate"
                                >
                                  {group.customer_name}
                                </Link>
                                <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${
                                  group.worst_verdict === 'bad'
                                    ? 'bg-accent-red/15 text-accent-red'
                                    : 'bg-accent-amber/15 text-accent-amber'
                                }`}>
                                  {group.worst_verdict === 'bad' ? 'Critico' : 'Dubbio'}
                                </span>
                              </div>
                              {group.customer_piva && (
                                <p className="text-xs text-txt-muted mt-0.5 font-mono">P.IVA cliente: {group.customer_piva}</p>
                              )}
                            </div>
                          </div>
                          <div className="text-right shrink-0">
                            <p className="text-sm font-semibold text-txt-primary whitespace-nowrap">
                              {group.problem_count} {group.problem_count === 1 ? 'fattura' : 'fatture'} su {group.total_invoices}
                            </p>
                            <p className="text-xs text-txt-muted whitespace-nowrap">EUR {fmtEur(group.problems_amount_due)}</p>
                          </div>
                        </div>

                        {expanded && (
                          <div className="divide-y divide-dark-border">
                            {group.items.map(item => {
                              const busy = actioningId === item.invoice_id || unlinkingId === item.invoice_id
                              return (
                                <div key={item.invoice_id} className="px-4 py-3 space-y-2.5">
                                  <div className="flex items-center justify-between gap-3">
                                    <span className="text-sm font-mono text-txt-primary">{item.invoice_number}</span>
                                    <span className="text-sm font-mono text-txt-secondary whitespace-nowrap">EUR {fmtEur(item.amount_due)}</span>
                                  </div>

                                  {/* Il perché (messaggio del motore di verifica) */}
                                  <p className="text-xs text-txt-secondary leading-relaxed">{item.verification?.message}</p>

                                  {/* Confronto affiancato: P.IVA e ragione sociale */}
                                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs bg-dark-surface rounded-lg p-3">
                                    <div className="space-y-1">
                                      <p className="text-txt-label uppercase tracking-wide">P.IVA</p>
                                      <CompareLine label="Fattura" value={item.verification?.invoice_piva} />
                                      <CompareLine label="Cliente" value={item.verification?.customer_piva} status={pivaStatus(item.verification)} />
                                    </div>
                                    <div className="space-y-1">
                                      <p className="text-txt-label uppercase tracking-wide">Ragione sociale</p>
                                      <CompareLine label="Fattura" value={item.verification?.invoice_name} />
                                      <CompareLine label="Cliente" value={item.verification?.customer_name} status={nameStatus(item.verification)} />
                                    </div>
                                  </div>

                                  {/* Azioni */}
                                  <div className="flex flex-wrap items-center gap-2 pt-0.5">
                                    <Link
                                      to={`/customers/${item.customer_id}`}
                                      className="px-3 py-1.5 rounded-lg text-xs font-semibold bg-dark-surface text-txt-secondary hover:text-accent-teal transition-colors"
                                    >
                                      Apri scheda cliente
                                    </Link>
                                    <button
                                      onClick={() => unlinkInvoice(item)}
                                      disabled={busy}
                                      className={`px-3 py-1.5 rounded-lg text-xs font-semibold bg-accent-red/15 text-accent-red hover:bg-accent-red/25 transition-colors ${busy ? 'opacity-50 cursor-not-allowed' : ''}`}
                                    >
                                      {unlinkingId === item.invoice_id ? 'Scollego...' : 'Scollega'}
                                    </button>
                                    {item.can_assign_piva && (
                                      <button
                                        onClick={() => assignPiva(item)}
                                        disabled={busy}
                                        className={`px-3 py-1.5 rounded-lg text-xs font-semibold bg-accent-teal/15 text-accent-teal hover:bg-accent-teal/25 transition-colors ${busy ? 'opacity-50 cursor-not-allowed' : ''}`}
                                      >
                                        Assegna P.IVA al cliente
                                      </button>
                                    )}
                                    {item.reviewed ? (
                                      <button
                                        onClick={() => unmarkReviewed(item)}
                                        disabled={busy}
                                        className={`px-3 py-1.5 rounded-lg text-xs font-semibold bg-dark-surface text-txt-muted hover:text-txt-secondary transition-colors ${busy ? 'opacity-50 cursor-not-allowed' : ''}`}
                                      >
                                        Annulla verifica
                                      </button>
                                    ) : (
                                      <button
                                        onClick={() => markReviewed(item)}
                                        disabled={busy}
                                        className={`px-3 py-1.5 rounded-lg text-xs font-semibold bg-accent-green/15 text-accent-green hover:bg-accent-green/25 transition-colors ${busy ? 'opacity-50 cursor-not-allowed' : ''}`}
                                      >
                                        Segna verificato
                                      </button>
                                    )}
                                  </div>
                                </div>
                              )
                            })}
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
