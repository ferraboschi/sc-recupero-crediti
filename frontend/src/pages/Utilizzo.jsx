import React, { useState, useEffect, useCallback } from 'react'
import client from '../api/client'

// Etichette leggibili per tipo azione e canale del sollecito.
const ACTION_LABELS = {
  first_contact: 'I Contatto',
  second_contact: 'II Contatto',
  lawyer: 'Avvocato',
}

const CHANNEL_LABELS = {
  whatsapp_copy: 'Copia Messaggio',
  whatsapp_link: 'Link WhatsApp',
}

// "2026-01-15" → "Giovedì 15 gennaio 2026" (giorno di calendario italiano,
// già calcolato dal backend: qui NON si tocca il fuso, si formatta e basta).
function formatGiorno(dataStr) {
  if (!dataStr) return '-'
  const d = new Date(dataStr + 'T00:00:00')
  const s = d.toLocaleDateString('it-IT', {
    weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
  })
  return s.charAt(0).toUpperCase() + s.slice(1)
}

export default function Utilizzo() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)

  const fetchUtilizzo = useCallback(async () => {
    try {
      setLoading(true)
      setError(false)
      const resp = await client.get('/recovery/utilizzo')
      setData(resp.data)
    } catch (err) {
      console.error('Error fetching utilizzo:', err)
      setError(true)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchUtilizzo() }, [fetchUtilizzo])

  // ── Loading ──────────────────────────────────────────────────────
  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-center">
          <svg className="animate-spin w-12 h-12 text-accent-teal mx-auto mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          <p className="text-txt-muted">Caricamento utilizzo...</p>
        </div>
      </div>
    )
  }

  // ── Errore ───────────────────────────────────────────────────────
  if (error) {
    return (
      <div className="sc-card p-8 text-center">
        <p className="text-txt-secondary mb-3">Impossibile caricare i dati di utilizzo.</p>
        <button onClick={fetchUtilizzo} className="sc-btn-secondary">Riprova</button>
      </div>
    )
  }

  const eventi = data?.eventi || []
  const perGiorno = data?.per_giorno || []

  // Mappa data → conteggi (il per-giorno è la fonte autorevole dei totali).
  const countsByDay = {}
  for (const g of perGiorno) countsByDay[g.data] = g

  // Raggruppa gli eventi per giorno preservando l'ordine (data desc, cliente asc).
  const gruppi = []
  const indexByDay = {}
  for (const ev of eventi) {
    let g = indexByDay[ev.data]
    if (!g) {
      g = { data: ev.data, righe: [] }
      indexByDay[ev.data] = g
      gruppi.push(g)
    }
    g.righe.push(ev)
  }

  // Totali complessivi (dall'inizio dei dati).
  const totEventi = eventi.length
  const totClientiGiorni = perGiorno.reduce((s, g) => s + g.clienti_sollecitati, 0)

  // ── Stato vuoto ──────────────────────────────────────────────────
  if (eventi.length === 0) {
    return (
      <div className="space-y-6">
        <div>
          <h2 className="text-base font-bold text-txt-primary">Utilizzo</h2>
          <p className="text-xs text-txt-muted mt-0.5">Clienti sollecitati ogni giorno, dall'inizio dei dati</p>
        </div>
        <div className="sc-card p-10 text-center">
          <p className="text-txt-secondary mb-1">Nessun sollecito registrato</p>
          <p className="text-sm text-txt-muted">Quando invii un sollecito (Copia Messaggio / WhatsApp) dalla scheda cliente, comparirà qui.</p>
        </div>
      </div>
    )
  }

  // ── Vista principale ─────────────────────────────────────────────
  return (
    <div className="space-y-6">
      {/* Intestazione + riepilogo */}
      <div className="flex items-end justify-between gap-4">
        <div>
          <h2 className="text-base font-bold text-txt-primary">Utilizzo</h2>
          <p className="text-xs text-txt-muted mt-0.5">Clienti sollecitati ogni giorno, dall'inizio dei dati</p>
        </div>
        <div className="flex gap-3 shrink-0">
          <div className="sc-kpi py-3 px-5">
            <div className="sc-kpi-value">{totEventi}</div>
            <div className="sc-kpi-label">Solleciti totali</div>
          </div>
          <div className="sc-kpi py-3 px-5">
            <div className="sc-kpi-value">{gruppi.length}</div>
            <div className="sc-kpi-label">Giorni di attività</div>
          </div>
          <div className="sc-kpi py-3 px-5">
            <div className="sc-kpi-value">{totClientiGiorni}</div>
            <div className="sc-kpi-label">Clienti · giorno</div>
          </div>
        </div>
      </div>

      {/* Tabella raggruppata per giorno */}
      <div className="sc-card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="text-left text-xs text-txt-muted uppercase tracking-wider border-b border-dark-border">
                <th className="px-5 py-3 w-48">Data</th>
                <th className="px-5 py-3">Cliente</th>
                <th className="px-5 py-3 w-56">Tipo / Canale</th>
              </tr>
            </thead>
            <tbody>
              {gruppi.map(g => {
                const c = countsByDay[g.data] || { clienti_sollecitati: 0, eventi_totali: g.righe.length }
                return (
                  <React.Fragment key={g.data}>
                    {/* Intestazione del giorno */}
                    <tr className="bg-dark-surface border-b border-dark-border">
                      <td colSpan={3} className="px-5 py-2.5">
                        <div className="flex items-baseline justify-between gap-3">
                          <span className="text-sm font-semibold text-txt-primary">{formatGiorno(g.data)}</span>
                          <span className="text-xs text-txt-secondary">
                            <span className="text-accent-teal font-semibold">{c.clienti_sollecitati}</span>
                            {' '}client{c.clienti_sollecitati === 1 ? 'e' : 'i'} sollecitat{c.clienti_sollecitati === 1 ? 'o' : 'i'}
                            {' · '}
                            <span className="font-semibold">{c.eventi_totali}</span> event{c.eventi_totali === 1 ? 'o' : 'i'}
                          </span>
                        </div>
                      </td>
                    </tr>
                    {/* Righe eventi del giorno */}
                    {g.righe.map((ev, i) => (
                      <tr key={`${g.data}-${i}`} className="border-b border-dark-border last:border-0">
                        <td className="px-5 py-2.5 text-xs text-txt-muted font-mono align-top">{ev.data}</td>
                        <td className="px-5 py-2.5 text-sm text-txt-primary">{ev.cliente}</td>
                        <td className="px-5 py-2.5 align-top">
                          <span className="text-sm text-txt-secondary">{ACTION_LABELS[ev.action_type] || ev.action_type}</span>
                          {ev.channel && (
                            <span className="text-xs text-txt-muted ml-2">· {CHANNEL_LABELS[ev.channel] || ev.channel}</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </React.Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
