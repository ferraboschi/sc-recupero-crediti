import React, { useState, useEffect, useCallback } from 'react'
import client from '../api/client'

// Sezione Avvocato: candidati alla pratica legale (debito > soglia + entrambi
// i solleciti fatti), con i giorni dall'ultimo sollecito. Il SOGGETTO è la
// FATTURA: per ogni candidato l'operatore SCEGLIE quali fatture consegnare
// (decisione owner) — le vecchie al legale, le nuove restano in sollecito; il
// cliente resta in lista finché ha scadute non consegnate. Consegna = download
// del pacchetto (Dossier PDF + PDF fatture) delle sole fatture scelte.
// Handover manuale, per-fattura. Desktop only.

const formatCurrency = (v) =>
  new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' }).format(v || 0)

const formatDate = (s) => {
  if (!s) return '—'
  const d = new Date(s)
  return isNaN(d) ? '—' : d.toLocaleDateString('it-IT')
}

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

// Selezione di default: SOLO le fatture non consegnate con almeno 2 solleciti
// propri (mature). Niente fallback: al legale non si propone una fattura mai
// sollecitata — l'operatore può comunque sceglierla a mano. Stessa regola del
// server per "Prepara tutti".
function defaultSelection(invoices) {
  const mature = (invoices || []).filter(i => !i.delivered && (i.sollecito_count || 0) >= 2)
  return new Set(mature.map(i => i.id))
}

export default function Avvocato() {
  const [items, setItems] = useState([])
  const [meta, setMeta] = useState({ min_debt: 1500, grace_days: 14 })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [busyKey, setBusyKey] = useState(null)  // `${id}:dl` | `${id}:ho`
  const [busyAll, setBusyAll] = useState(false)
  const [selected, setSelected] = useState({})     // { [customerId]: Set(invoiceId) }
  const [expanded, setExpanded] = useState(new Set())

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const res = await client.get('/avvocato/candidates')
      const list = res.data.items || []
      setItems(list)
      setMeta({ min_debt: res.data.min_debt, grace_days: res.data.grace_days })
      // Conserva le selezioni manuali dei clienti ancora in lista (solo le
      // fatture ancora da consegnare); default solo per i nuovi arrivati.
      setSelected(prev => {
        const sel = {}
        list.forEach(it => {
          const open = new Set((it.invoices || []).filter(i => !i.delivered).map(i => i.id))
          const kept = prev[it.id] ? new Set([...prev[it.id]].filter(id => open.has(id))) : null
          sel[it.id] = kept && kept.size ? kept : defaultSelection(it.invoices)
        })
        return sel
      })
    } catch (err) {
      console.error(err)
      setError('Errore nel caricamento dei candidati')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const toggleExpand = (id) => setExpanded(prev => {
    const n = new Set(prev)
    if (n.has(id)) n.delete(id); else n.add(id)
    return n
  })
  const toggleInvoice = (customerId, invoiceId) => setSelected(prev => {
    const cur = new Set(prev[customerId] || [])
    if (cur.has(invoiceId)) cur.delete(invoiceId); else cur.add(invoiceId)
    return { ...prev, [customerId]: cur }
  })
  const selSet = (item) => selected[item.id] || new Set()
  const selIds = (item) => Array.from(selSet(item))
  const openInvoices = (item) => (item.invoices || []).filter(i => !i.delivered)
  const selTotal = (item) =>
    (item.invoices || []).filter(i => selSet(item).has(i.id)).reduce((s, i) => s + (i.amount_due || 0), 0)

  const downloadDossier = async (item) => {
    const ids = selIds(item)
    if (ids.length === 0) { setError('Seleziona almeno una fattura da consegnare'); return }
    try {
      setBusyKey(`${item.id}:dl`)
      const res = await client.get(`/avvocato/customers/${item.id}/dossier-zip`, {
        responseType: 'blob', params: { invoice_ids: ids.join(',') },
      })
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
    const ids = selIds(item)
    if (ids.length === 0) { setError('Seleziona almeno una fattura da consegnare'); return }
    const open = openInvoices(item).length
    const partial = ids.length < open
    const plural = ids.length !== 1
    const immature = (item.invoices || []).filter(i => ids.includes(i.id) && (i.sollecito_count || 0) < 2)
    if (!window.confirm(
      `Segnare ${ids.length} fattur${plural ? 'e' : 'a'} di "${item.ragione_sociale}" come consegnat${plural ? 'e' : 'a'} all'avvocato?\n\n`
      + (immature.length
        ? `ATTENZIONE: ${immature.map(i => i.invoice_number).join(', ')} ${immature.length === 1 ? 'ha' : 'hanno'} meno di 2 solleciti.\n`
        : '')
      + (partial
        ? `Le altre ${open - ids.length} restano in sollecito: il cliente rimane in lista per quelle.\n`
        : 'È tutto il ciclo: il cliente passa allo stato legale ed esce dalla lista.\n')
      + 'Assicurati di aver già scaricato il dossier da consegnare.'
    )) return
    try {
      setBusyKey(`${item.id}:ho`)
      await client.post(`/avvocato/customers/${item.id}/handover`, { invoice_ids: ids })
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
            i solleciti</strong> già fatti. Per ognuno <strong>scegli quali fatture</strong> consegnare
            al legale: le altre restano in sollecito e il cliente resta in lista per quelle.
            &quot;Ultimo sollecito&quot;: verde = pronto, ambra = sollecitato da meno di {meta.grace_days} giorni.
          </p>
        </div>
        {items.length > 0 && (
          <button
            onClick={downloadAll}
            disabled={busyAll}
            className={`sc-btn-primary shrink-0 ${busyAll ? 'opacity-50 cursor-not-allowed' : ''}`}
            title="Un ZIP con una cartella per candidato: le fatture non consegnate con almeno 2 solleciti"
          >
            {busyAll ? 'Preparo…' : 'Prepara tutti (ZIP)'}
          </button>
        )}
      </div>

      {/* KPI */}
      {!loading && !error && items.length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <div className="sc-card p-4">
            <div className="sc-kpi-value">{items.length}</div>
            <div className="sc-kpi-label">Candidati</div>
          </div>
          <div className="sc-card p-4">
            <div className="sc-kpi-value text-accent-red">{formatCurrency(totale)}</div>
            <div className="sc-kpi-label">Scaduto totale</div>
          </div>
          <div className="sc-card p-4">
            <div className="sc-kpi-value text-accent-amber">{formatCurrency(items.reduce((s, i) => s + (i.undelivered_total || 0), 0))}</div>
            <div className="sc-kpi-label">Da consegnare</div>
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
                  <th className="px-4 py-3 text-center text-xs font-semibold text-txt-label uppercase tracking-wider" title="Fatture scelte / da consegnare">Fatture</th>
                  <th className="px-4 py-3 text-center text-xs font-semibold text-txt-label uppercase tracking-wider" title="Contatti del ciclo aperto (pratica)">Solleciti (pratica)</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-txt-label uppercase tracking-wider">Ultimo sollecito</th>
                  <th className="px-4 py-3 text-right text-xs font-semibold text-txt-label uppercase tracking-wider">Azioni</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-dark-border">
                {items.map(item => (
                  <React.Fragment key={item.id}>
                  <tr className="sc-table-row">
                    <td className="px-4 py-3 text-sm font-medium text-txt-primary">{item.ragione_sociale}</td>
                    <td className="px-4 py-3 text-sm font-mono text-txt-secondary">{item.partita_iva || '—'}</td>
                    <td className="px-4 py-3 text-sm text-right">
                      <div className="font-bold text-accent-red">{formatCurrency(item.total_overdue)}</div>
                      {item.undelivered_total != null && item.undelivered_total < item.total_overdue && (
                        <div className="text-[11px] text-txt-muted">da consegnare {formatCurrency(item.undelivered_total)}</div>
                      )}
                    </td>
                    <td className="px-4 py-3 text-sm text-center">
                      <button
                        onClick={() => toggleExpand(item.id)}
                        className="sc-btn-secondary text-xs whitespace-nowrap"
                        title="Scegli quali fatture consegnare all'avvocato"
                      >
                        {selIds(item).length}/{openInvoices(item).length} scelte {expanded.has(item.id) ? '▴' : '▾'}
                      </button>
                    </td>
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
                          disabled={busyKey === `${item.id}:dl` || selIds(item).length === 0}
                          className="sc-btn-secondary text-xs whitespace-nowrap disabled:opacity-40"
                          title="Dossier PDF + PDF delle sole fatture scelte"
                        >
                          Scarica dossier
                        </button>
                        <button
                          onClick={() => handover(item)}
                          disabled={busyKey === `${item.id}:ho` || selIds(item).length === 0}
                          className="sc-btn-primary text-xs whitespace-nowrap disabled:opacity-40"
                          title="Registra la consegna delle fatture scelte"
                        >
                          Consegna {selIds(item).length}
                        </button>
                      </div>
                    </td>
                  </tr>
                  {expanded.has(item.id) && (
                    <tr key={`${item.id}-inv`} className="bg-dark-surface/40">
                      <td colSpan={7} className="px-4 pb-3">
                        <div className="text-xs text-txt-muted mb-2">
                          Scegli le fatture da consegnare. Selezionate: <strong className="text-txt-secondary">{selIds(item).length}</strong> · {formatCurrency(selTotal(item))}
                        </div>
                        <table className="w-full text-sm">
                          <thead>
                            <tr className="text-[11px] uppercase tracking-wider text-txt-label">
                              <th className="px-2 py-1 w-8"></th>
                              <th className="px-2 py-1 text-left">Fattura</th>
                              <th className="px-2 py-1 text-right">Dovuto</th>
                              <th className="px-2 py-1 text-left">Scadenza</th>
                              <th className="px-2 py-1 text-right">GG</th>
                              <th className="px-2 py-1 text-center" title="Solleciti ricevuti da questa fattura">Solleciti (fattura)</th>
                              <th className="px-2 py-1 text-left">Consegna</th>
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-dark-border">
                            {(item.invoices || []).map(inv => (
                              <tr key={inv.id} className={inv.delivered ? 'opacity-60' : ''}>
                                <td className="px-2 py-1.5 text-center">
                                  <input
                                    type="checkbox"
                                    disabled={inv.delivered}
                                    checked={!inv.delivered && selSet(item).has(inv.id)}
                                    onChange={() => toggleInvoice(item.id, inv.id)}
                                    className="rounded border-dark-border bg-dark-bg"
                                  />
                                </td>
                                <td className="px-2 py-1.5 font-medium text-txt-primary">{inv.invoice_number}</td>
                                <td className="px-2 py-1.5 text-right text-txt-primary">{formatCurrency(inv.amount_due)}</td>
                                <td className="px-2 py-1.5 text-txt-secondary">{formatDate(inv.due_date)}</td>
                                <td className="px-2 py-1.5 text-right">
                                  <span className={inv.days_overdue > 30 ? 'text-accent-red font-medium' : 'text-accent-amber'}>+{inv.days_overdue}gg</span>
                                </td>
                                <td className="px-2 py-1.5 text-center">
                                  {(inv.sollecito_count || 0) === 0 ? (
                                    <span className="text-txt-muted text-xs">0</span>
                                  ) : (
                                    <span className={`sc-badge ${inv.sollecito_count >= 2 ? 'bg-accent-amber/15 text-accent-amber' : 'bg-accent-teal/15 text-accent-teal'}`}>
                                      {inv.sollecito_count} sollecit{inv.sollecito_count === 1 ? 'o' : 'i'}
                                    </span>
                                  )}
                                </td>
                                <td className="px-2 py-1.5">
                                  {inv.delivered ? (
                                    <span className="sc-badge bg-[rgba(148,163,184,0.15)] text-txt-muted">consegnata</span>
                                  ) : (
                                    <span className="text-txt-muted text-xs">da consegnare</span>
                                  )}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </td>
                    </tr>
                  )}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
