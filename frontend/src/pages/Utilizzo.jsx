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

// "2026-08-19" -> "Martedi 19 agosto 2026" (giorno di calendario italiano,
// gia' calcolato dal backend: qui NON si tocca il fuso, si formatta e basta).
function formatGiorno(dataStr) {
  if (!dataStr) return '-'
  const d = new Date(dataStr + 'T00:00:00')
  const s = d.toLocaleDateString('it-IT', {
    weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
  })
  return s.charAt(0).toUpperCase() + s.slice(1)
}

// Pluralizzazione minima ("azione"/"azioni"; "account" resta invariato).
const azioniLabel = (n) => `${n} ${n === 1 ? 'azione' : 'azioni'}`
const accountLabel = (n) => `${n} account`

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

  // Intestazione riusabile (titolo + riga che DICHIARA cosa conta come azione).
  const Intestazione = () => (
    <div>
      <h2 className="text-base font-bold text-txt-primary">Utilizzo</h2>
      <p className="text-xs text-txt-muted mt-0.5">
        Registro giorno-per-giorno del lavoro di recupero crediti, dall'inizio dei dati
      </p>
      <p className="text-xs text-txt-secondary mt-1.5">
        <span className="text-accent-teal font-semibold">Azioni</span>
        {' = solleciti realmente inviati (registrati dal sistema), non intenzioni dichiarate.'}
      </p>
    </div>
  )

  // -- Loading -------------------------------------------------------
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

  // -- Errore --------------------------------------------------------
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

  // Mappa data -> conteggi (il per-giorno e' la fonte autorevole dei totali).
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

  // KPI complessivi (dall'inizio dei dati):
  // - azioni totali = numero di solleciti,
  // - account lavorati = clienti DISTINTI su tutto lo storico,
  // - giorni con attivita' = giorni in cui c'e' stato almeno un sollecito.
  const totAzioni = eventi.length
  const totAccount = new Set(eventi.map(e => e.customer_id)).size
  const totGiorni = perGiorno.length

  // -- Stato vuoto ---------------------------------------------------
  if (eventi.length === 0) {
    return (
      <div className="space-y-6">
        <Intestazione />
        <div className="sc-card p-10 text-center">
          <p className="text-txt-secondary mb-1">Nessuna azione registrata</p>
          <p className="text-sm text-txt-muted">
            Quando invii un sollecito (Copia Messaggio / WhatsApp) dalla scheda cliente, comparira' qui.
          </p>
        </div>
      </div>
    )
  }

  // -- Vista principale ----------------------------------------------
  return (
    <div className="space-y-6">
      {/* Intestazione + KPI d'audit */}
      <div className="flex items-end justify-between gap-4 flex-wrap">
        <Intestazione />
        <div className="flex gap-3 shrink-0">
          <div className="sc-kpi py-3 px-5">
            <div className="sc-kpi-value">{totAzioni}</div>
            <div className="sc-kpi-label">Azioni totali</div>
          </div>
          <div className="sc-kpi py-3 px-5">
            <div className="sc-kpi-value">{totAccount}</div>
            <div className="sc-kpi-label">Account lavorati</div>
          </div>
          <div className="sc-kpi py-3 px-5">
            <div className="sc-kpi-value">{totGiorni}</div>
            <div className="sc-kpi-label">Giorni con attivita'</div>
          </div>
        </div>
      </div>

      {/* Registro raggruppato per giorno (le RIGHE sono i GIORNI) */}
      <div className="sc-card overflow-hidden">
        <div className="overflow-x-auto">
          {gruppi.map(g => {
            const c = countsByDay[g.data] || { azioni: g.righe.length, account: 0 }
            return (
              <div key={g.data} className="border-b border-dark-border last:border-0">
                {/* Intestazione del giorno (colpo d'occhio: giorni pieni vs vuoti) */}
                <div className="flex items-baseline justify-between gap-3 px-5 py-3 bg-dark-surface border-b border-dark-border">
                  <span className="text-sm font-semibold text-txt-primary">{formatGiorno(g.data)}</span>
                  <span className="text-xs text-txt-secondary whitespace-nowrap">
                    <span className="text-accent-teal font-semibold">{azioniLabel(c.azioni)}</span>
                    {' · '}
                    <span className="font-semibold">{accountLabel(c.account)}</span>
                  </span>
                </div>
                {/* Dettaglio del giorno: gli account lavorati (Cliente + tipo/canale) */}
                <div>
                  {g.righe.map((ev, i) => (
                    <div
                      key={`${g.data}-${i}`}
                      className="flex items-baseline justify-between gap-4 px-5 py-2 border-b border-dark-border/60 last:border-0"
                    >
                      <span className="text-sm text-txt-primary truncate">{ev.cliente || '—'}</span>
                      <span className="text-xs text-txt-muted shrink-0">
                        {ACTION_LABELS[ev.action_type] || ev.action_type}
                        {ev.channel && <> · {CHANNEL_LABELS[ev.channel] || ev.channel}</>}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
