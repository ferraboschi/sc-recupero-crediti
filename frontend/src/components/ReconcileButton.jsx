import React, { useState, useRef, useEffect } from 'react'
import { API_BASE, getAuthHeaders } from '../utils/api'

/**
 * "Aggiorna incassi adesso" — azione quotidiana principale, nell'header.
 *
 * Chiama il reconcile SINCRONO (due passaggi di rilevamento pagamenti): gli
 * incassi già registrati in FatturaPro si vedono subito, senza aspettare il
 * sync automatico orario. Sta nell'header (non nella pagina Sistema, che è per
 * la diagnostica) così è raggiungibile da qualsiasi pagina.
 *
 * Concorrenza: il backend serializza reconcile e full-sync sullo STESSO
 * _sync_lock (acquire non-bloccante). Se un sync è già in corso, il reconcile
 * degrada con grazia ("già in corso, riprova") — l'integrità NON dipende dal
 * pulsante disabilitato, quindi qui basta il disable locale anti doppio-click.
 *
 * L'esito appare ACCANTO al pulsante (niente layout shift) e si auto-cancella
 * dopo 45s: l'header non si smonta al cambio pagina, e un risultato vecchio
 * pinnato su una pagina non correlata confonderebbe.
 */
export default function ReconcileButton({ label = 'Aggiorna incassi', className = '' }) {
  const [reconciling, setReconciling] = useState(false)
  const [msg, setMsg] = useState(null)
  const dismissTimer = useRef(null)

  useEffect(() => () => {
    if (dismissTimer.current) clearTimeout(dismissTimer.current)
  }, [])

  const run = async () => {
    if (dismissTimer.current) {
      clearTimeout(dismissTimer.current)
      dismissTimer.current = null
    }
    setReconciling(true)
    setMsg(null)
    try {
      const res = await fetch(`${API_BASE}/sync/reconcile-incassi`, {
        method: 'POST',
        headers: getAuthHeaders(),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const d = await res.json()
      // Verde SOLO per incassi davvero registrati (confermati); ambra per
      // lista incompleta; muto quando non c'è nulla di nuovo.
      const tone = d.partial ? 'warning' : (d.marked_paid > 0 ? 'success' : 'muted')
      setMsg({ text: d.message, tone })
    } catch (e) {
      setMsg({ text: `Errore: ${e.message}`, tone: 'warning' })
    } finally {
      setReconciling(false)
      dismissTimer.current = setTimeout(() => setMsg(null), 45000)
    }
  }

  return (
    <div className="flex items-center gap-2 min-w-0">
      {msg && (
        <span
          className={`text-xs font-medium truncate max-w-[220px] hidden lg:inline ${
            msg.tone === 'success' ? 'text-accent-green'
              : msg.tone === 'warning' ? 'text-accent-amber'
              : 'text-txt-muted'
          }`}
          title={msg.text}
        >
          {msg.text}
        </span>
      )}
      <button
        onClick={run}
        disabled={reconciling}
        title="Aggiorna incassi adesso: rileva i pagamenti già segnati in FatturaPro (due passaggi)."
        className={`sc-btn-secondary shrink-0 whitespace-nowrap ${reconciling ? 'opacity-50 cursor-not-allowed' : ''} ${className}`}
      >
        {reconciling ? 'Aggiornamento…' : label}
      </button>
    </div>
  )
}
