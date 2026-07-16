/**
 * Attesa del completamento di un sync avviato in background.
 *
 * POST /sync/full risponde SUBITO 'sync_started' (BackgroundTasks): l'esito
 * non è nella risposta e i dati letti immediatamente dopo sono ancora quelli
 * PRE-sync. Il completamento si osserva da GET /sync/status: 'cases' è
 * l'ULTIMO step della pipeline del full sync (invoices → customers →
 * matching → auto-create → order matching → cases) e persiste il proprio
 * stato anche in caso di errore, quindi il suo last_sync che cambia segnala
 * la fine dell'intera pipeline.
 */

import client from '../api/client'

const POLL_INTERVAL_MS = 5000
const MAX_ATTEMPTS = 36 // ~3 minuti, stesso limite del polling di System.jsx

/** Marker di completamento: timestamp dell'ultimo step della pipeline. */
export async function getSyncMarker() {
  const res = await client.get('/sync/status')
  return res.data?.last_sync?.cases?.last_sync || ''
}

/**
 * Polla /sync/status finché il marker cambia rispetto a markerBefore.
 * Ritorna true se il sync è completato, false al timeout (sync ancora
 * in corso in background: il chiamante lo dica onestamente all'utente).
 */
export async function waitForSyncCompletion(markerBefore) {
  for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
    await new Promise(resolve => setTimeout(resolve, POLL_INTERVAL_MS))
    try {
      const marker = await getSyncMarker()
      if (marker && marker !== markerBefore) return true
    } catch {
      // errore transitorio di polling: si riprova al giro successivo
    }
  }
  return false
}
