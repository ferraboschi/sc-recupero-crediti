import React, { useState, useEffect, useCallback } from 'react'
import client from '../api/client'

// Sezione Avvocato: candidati alla pratica legale (debito > soglia + entrambi
// i solleciti fatti), con i giorni dall'ultimo sollecito così l'operatore non
// passa al legale chi è stato appena sollecitato. Consegna = download del
// pacchetto (Dossier PDF + PDF fatture). Handover manuale. Desktop only.

const formatCurrency = (v) =>
  new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' }).format(v || 0)

function downloadBlob(data, filename) {
  const url = window.URL.createObjectURL(new Blob([data]))
  const link = document.createElement('a')
  link.href = url
  link.setAttribute('download', filename)
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.URL.revokeObjectURL(url)
}

const safeName = (s) => (s || 'cliente').replace(/[^A-Za-z0-9._-]+/g, '_')

export default function Avvocato() {
  const [items, setItems] = useState([])
  const [meta, setMeta] = useState({ min_debt: 1500, grace_days: 14 })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [busyKey, setBusyKey] = useState(null)  // `${id}:dl` | `${id}:ho`
  const [busyAll, setBusyAll] = useState(false)

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const res = await client.get('/avvocato/candidates')
      setItems(res.data.items || [])
      setMeta({ min_debt: res.data.min_debt, grace_days: res.data.grace_days })
    } catch (err) {
      console.error(err)
      setError('Errore nel caricamento dei candidati')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const downloadDossier = async (item) => {
    try {
      setBusyKey(`${item.id}:dl`)
      const res = await client.get(`/avvocato/customers/${item.id}/dossier-zip`, { responseType: 'blob' })
      downloadBlob(res.data, `dossier_${safeName(item.ragione_sociale)}.zip`)
    } catch (err) {
      console.error(err)
      setError('Errore nel download del dossier')
    } finally {
      setBusyKey(null)
    }
  }

  const downloadAll = async () => {
    try {
      setBusyAll(true)
      const res = await client.get('/avvocato/dossier-zip-all', { responseType: 'blob' })
      const stamp = new Date().toISOString().slice(0, 10).replace(/-/g, '')
      downloadBlob(res.data, `dossier_avvocato_${stamp}.zip`)
    } catch (err) {
      console.error(err)
      setError('Errore nel download del pacchetto completo')
    } finally {
      setBusyAll(false)
    }
  }

  const handover = async (item) => {
    if (!window.confirm(
      `Segnare "${item.ragione_sociale}" come consegnato all'avvocato?\n\n`
      + 'Registra la data di passaggio e lo toglie dalla lista candidati. '
      + 'Assicurati di aver già scaricato il dossier da consegnare.'
    )) return
    try {
      setBusyKey(`${item.id}:ho`)
      await client.post(`/avvocato/customers/${item.id}/handover`)
      await load()
    } catch (err) {
      console.error(err)
      setError('Errore nel passaggio all\'avvocato')
    } finally {
      setBusyKey(null)
    }
  }

  const totale = items.reduce((s, i) => s + (i.total_overdue || 0), 0)

  return (
    <div className="space-y-6">
      {/* Intestazione */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-lg font-semibold text-txt-primary">Avvocato</h2>
          <p className="text-sm text-txt-secondary mt-1 max-w-2xl">
            Clienti con debito oltre {formatCurrency(meta.min_debt)} e <strong>entrambi
            i solleciti</strong> già fatti, pronti per la pratica legale. La colonna
            &quot;ultimo sollecito&quot; ti dice chi è stato contattato di recente:
            verde = pronto, ambra = sollecitato da meno di {meta.grace_days} giorni.
          </p>
        </div>
        {items.length > 0 && (
          <button
            onClick={downloadAll}
            disabled={busyAll}
            className={`sc-btn-primary shrink-0 ${busyAll ? 'opacity-50 cursor-not-allowed' : ''}`}
          >
            {busyAll ? 'Preparo…' : 'Prepara tutti (ZIP)'}
          </button>
        )}
      </div>

      {/* KPI */}
      {!loading && !error && items.length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
          <div className="sc-card p-4">
            <div className="sc-kpi-value">{items.length}</div>
            <div className="sc-kpi-label">Candidati</div>
          </div>
          <div className="sc-card p-4">
            <div className="sc-kpi-value text-accent-red">{formatCurrency(totale)}</div>
            <div className="sc-kpi-label">Scaduto totale</div>
          </div>
          <div className="sc-card p-4">
            <div className="sc-kpi-value text-accent-green">{items.filter(i => i.ready).length}</div>
            <div className="sc-kpi-label">Pronti (≥{meta.grace_days}gg)</div>
          </div>
        </div>
      )}

      {error && <div className="sc-card p-4 text-accent-red">{error}</div>}

      {loading ? (
        <div className="sc-card flex items-center justify-center h-64">
          <svg className="animate-spin-slow w-8 h-8 text-accent-teal" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
        </div>
      ) : items.length === 0 ? (
        <div className="sc-card p-10 text-center">
          <p className="text-txt-primary font-medium">Nessun candidato per l&apos;avvocato</p>
          <p className="text-sm text-txt-muted mt-1">
            Nessun cliente con debito oltre {formatCurrency(meta.min_debt)} ed entrambi i solleciti fatti.
          </p>
        </div>
      ) : (
        <div className="sc-card overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="bg-dark-surface border-b border-dark-border">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-txt-label uppercase tracking-wider">Azienda</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-txt-label uppercase tracking-wider">P.IVA</th>
                  <th className="px-4 py-3 text-right text-xs font-semibold text-txt-label uppercase tracking-wider">Scaduto</th>
                  <th className="px-4 py-3 text-center text-xs font-semibold text-txt-label uppercase tracking-wider">Fatture</th>
                  <th className="px-4 py-3 text-center text-xs font-semibold text-txt-label uppercase tracking-wider">Solleciti</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-txt-label uppercase tracking-wider">Ultimo sollecito</th>
                  <th className="px-4 py-3 text-right text-xs font-semibold text-txt-label uppercase tracking-wider">Azioni</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-dark-border">
                {items.map(item => (
                  <tr key={item.id} className="sc-table-row">
                    <td className="px-4 py-3 text-sm font-medium text-txt-primary">{item.ragione_sociale}</td>
                    <td className="px-4 py-3 text-sm font-mono text-txt-secondary">{item.partita_iva || '—'}</td>
                    <td className="px-4 py-3 text-sm text-right font-bold text-accent-red">{formatCurrency(item.total_overdue)}</td>
                    <td className="px-4 py-3 text-sm text-center text-txt-secondary">{item.overdue_count}</td>
                    <td className="px-4 py-3 text-sm text-center text-txt-secondary">{item.contact_count}</td>
                    <td className="px-4 py-3">
                      {item.days_since_last_sollecito == null ? (
                        <span className="text-txt-muted text-sm">—</span>
                      ) : (
                        <span className={`sc-badge ${item.ready ? 'bg-accent-green/15 text-accent-green' : 'bg-accent-amber/15 text-accent-amber'}`}>
                          {item.days_since_last_sollecito} gg fa · {item.ready ? 'pronto' : 'di recente'}
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center justify-end gap-2">
                        <button
                          onClick={() => downloadDossier(item)}
                          disabled={busyKey === `${item.id}:dl`}
                          className="sc-btn-secondary text-xs whitespace-nowrap disabled:opacity-40"
                        >
                          Scarica dossier
                        </button>
                        <button
                          onClick={() => handover(item)}
                          disabled={busyKey === `${item.id}:ho`}
                          className="sc-btn-primary text-xs whitespace-nowrap disabled:opacity-40"
                          title="Registra il passaggio all'avvocato e togli dalla lista"
                        >
                          Consegnato
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
