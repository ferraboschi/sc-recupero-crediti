import React, { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import client from '../api/client'

// Vista di REVISIONE della bonifica P.IVA in blocco. Vive su una pagina
// dedicata (raggiunta da un banner nella lista Clienti) invece di affollare
// la tabella clienti: il flusso multi-selezione + conferma ha bisogno di
// spazio suo, e il banner compare solo quando c'è davvero qualcosa da fare
// — stesso spirito del chip "Da sanificare". Desktop only.

const formatCurrency = (value) =>
  new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' }).format(value || 0)

// Certezza = somiglianza nome (stesso scorer del pannello per-riga). Un colore
// = un significato: teal = certezza alta (NON verde: queste righe sono
// candidati da approvare, non ancora verificati — il verde resta "confermato"),
// ambra = da guardare, rosso = poco simile (rischioso).
function confidenceStyle(conf) {
  if (conf >= 100) return 'bg-accent-teal/15 text-accent-teal'
  if (conf >= 75) return 'bg-accent-amber/15 text-accent-amber'
  return 'bg-accent-red/15 text-accent-red'
}

export default function BonificaAnagrafica() {
  const navigate = useNavigate()
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(() => new Set())
  const [applying, setApplying] = useState(false)
  const [lastResult, setLastResult] = useState(null)

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const res = await client.get('/customers/bonifica-suggestions')
      const list = res.data.items || []
      setItems(list)
      // I 100% (ragione sociale identica) partono PRE-SELEZIONATI: sono i
      // quasi-certi, il grosso del lavoro passa in un clic.
      setSelected(new Set(list.filter(i => i.confidence >= 100).map(i => i.customer_id)))
    } catch (err) {
      console.error(err)
      setError('Errore nel caricamento della lista di bonifica')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const toggleOne = (id) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const allSelected = items.length > 0 && selected.size === items.length
  const toggleAll = () => {
    setSelected(allSelected ? new Set() : new Set(items.map(i => i.customer_id)))
  }

  const handleApprove = async () => {
    const ids = Array.from(selected)
    if (ids.length === 0) return
    if (!window.confirm(
      `Assegno la P.IVA a ${ids.length} cliente${ids.length === 1 ? '' : 'i'}.\n\n`
      + 'Assegni l\'identità al cliente: da ora QUALSIASI fattura, anche FUTURA, '
      + 'con quella P.IVA si aggancia da sola a questo cliente e risulta verificata.\n\n'
      + 'La certezza mostrata è la somiglianza del NOME, non una verifica della '
      + 'P.IVA: se la P.IVA fosse errata (un refuso), il verde comparirebbe lo stesso.\n\n'
      + 'Reversibile per singolo cliente (Rimuovi P.IVA sulla scheda).'
    )) return
    try {
      setApplying(true)
      const res = await client.post('/customers/bonifica-piva/bulk', { customer_ids: ids })
      setLastResult(res.data)
      // Ricarico: le righe bonificate spariscono dalla lista (esce chi ha
      // ora una P.IVA valida).
      await load()
    } catch (err) {
      console.error(err)
      setError('Errore durante l\'assegnazione in blocco')
    } finally {
      setApplying(false)
    }
  }

  return (
    <div className="space-y-6">
      {/* Intestazione + ritorno */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <button
            onClick={() => navigate('/customers')}
            className="text-sm text-txt-secondary hover:text-accent-teal transition-colors mb-2 flex items-center gap-1"
          >
            <span aria-hidden="true">←</span> Torna ai clienti
          </button>
          <h2 className="text-lg font-semibold text-txt-primary">Bonifica P.IVA in blocco</h2>
          <p className="text-sm text-txt-secondary mt-1 max-w-2xl">
            Clienti senza P.IVA sul profilo le cui fatture ne portano una sola,
            valida e concorde (da FatturaPro). Assegnala per completare
            l&apos;anagrafica: le fatture, presenti e future, diventano verificate.
          </p>
        </div>
      </div>

      {/* Esito dell'ultimo apply */}
      {lastResult && (
        <div className="rounded-xl border border-accent-green/30 bg-accent-green/5 p-4">
          <p className="text-sm text-accent-green font-semibold">
            {lastResult.applied} cliente{lastResult.applied === 1 ? '' : 'i'} bonificat{lastResult.applied === 1 ? 'o' : 'i'}.
          </p>
          {(() => {
            const skipped = (lastResult.results || []).filter(r => r.result !== 'applied')
            if (skipped.length === 0) return null
            const conflict = skipped.filter(r => r.result === 'skipped_conflict').length
            const hasPiva = skipped.filter(r => r.result === 'skipped_has_piva').length
            const other = skipped.length - conflict - hasPiva
            const parts = []
            if (conflict) parts.push(`${conflict} con P.IVA diverse (forse due clienti)`)
            if (hasPiva) parts.push(`${hasPiva} già con P.IVA`)
            if (other) parts.push(`${other} senza P.IVA da assegnare`)
            return (
              <p className="text-xs text-txt-secondary mt-1">
                Saltati: {parts.join(' · ')}.
              </p>
            )
          })()}
        </div>
      )}

      {loading ? (
        <div className="sc-card flex items-center justify-center h-64">
          <svg className="animate-spin-slow w-8 h-8 text-accent-teal" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
        </div>
      ) : error ? (
        <div className="sc-card p-6 text-accent-red">{error}</div>
      ) : items.length === 0 ? (
        <div className="sc-card p-10 text-center">
          <p className="text-txt-primary font-medium">Nessun cliente da bonificare</p>
          <p className="text-sm text-txt-muted mt-1">
            Tutti i clienti con una P.IVA univoca sulle fatture hanno già l&apos;anagrafica completa.
          </p>
        </div>
      ) : (
        <div className="sc-card overflow-hidden">
          {/* Barra azioni */}
          <div className="px-5 py-3 border-b border-dark-border flex items-center justify-between gap-4">
            <p className="text-sm text-txt-secondary">
              <span className="font-semibold text-txt-primary">{items.length}</span> bonificabili ·{' '}
              <span className="font-semibold text-accent-teal">{selected.size}</span> selezionati
            </p>
            <button
              onClick={handleApprove}
              disabled={selected.size === 0 || applying}
              className="sc-btn-primary text-sm font-bold disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {applying ? 'Assegno…' : `Approva selezionati (${selected.size})`}
            </button>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="bg-dark-surface border-b border-dark-border">
                <tr>
                  <th className="px-4 py-3 w-10 text-center">
                    <input
                      type="checkbox"
                      checked={allSelected}
                      onChange={toggleAll}
                      aria-label="Seleziona tutti"
                      className="h-4 w-4 accent-accent-teal cursor-pointer"
                    />
                  </th>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-txt-label uppercase tracking-wider">Azienda</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-txt-label uppercase tracking-wider">P.IVA suggerita</th>
                  <th className="px-4 py-3 text-center text-xs font-semibold text-txt-label uppercase tracking-wider">Certezza</th>
                  <th className="px-4 py-3 text-center text-xs font-semibold text-txt-label uppercase tracking-wider">Fatture</th>
                  <th className="px-4 py-3 text-right text-xs font-semibold text-txt-label uppercase tracking-wider">Scaduto</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-dark-border">
                {items.map(item => {
                  const isSel = selected.has(item.customer_id)
                  return (
                    <tr
                      key={item.customer_id}
                      className={`sc-table-row cursor-pointer ${isSel ? 'bg-accent-teal/5' : ''}`}
                      onClick={() => toggleOne(item.customer_id)}
                    >
                      <td className="px-4 py-3 text-center" onClick={(e) => e.stopPropagation()}>
                        <input
                          type="checkbox"
                          checked={isSel}
                          onChange={() => toggleOne(item.customer_id)}
                          aria-label={`Seleziona ${item.ragione_sociale}`}
                          className="h-4 w-4 accent-accent-teal cursor-pointer"
                        />
                      </td>
                      <td className="px-4 py-3">
                        <button
                          onClick={(e) => { e.stopPropagation(); navigate(`/customers/${item.customer_id}`) }}
                          className="text-sm font-medium text-accent-teal hover:text-accent-cyan text-left"
                          title="Apri la scheda cliente"
                        >
                          {item.ragione_sociale || `Cliente #${item.customer_id}`}
                        </button>
                      </td>
                      <td className="px-4 py-3 text-sm font-mono text-txt-primary">
                        {item.piva_suggerita}
                      </td>
                      <td className="px-4 py-3 text-center">
                        <span
                          className={`sc-badge ${confidenceStyle(item.confidence)}`}
                          title="Somiglianza del NOME fra la ragione sociale e l'intestazione delle fatture (minimo del gruppo). NON è una verifica della P.IVA."
                        >
                          {item.confidence}%
                        </span>
                        {item.confidence >= 100 && (
                          <span className="block text-[10px] text-txt-muted mt-0.5">nome identico</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-sm text-center text-txt-secondary">
                        {item.invoice_count}
                      </td>
                      <td className="px-4 py-3 text-sm text-right font-bold">
                        {item.total_overdue > 0 ? (
                          <span className="text-accent-red">{formatCurrency(item.total_overdue)}</span>
                        ) : (
                          <span className="text-txt-muted">-</span>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
