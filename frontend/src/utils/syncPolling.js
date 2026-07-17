/**
 * Attesa del completamento di un sync avviato in background.
 *
 * POST /sync/full risponde SUBITO 'sync_started' (BackgroundTasks): l'esito
 * non è nella risposta e i dati letti immediatamente dopo sono ancora quelli
 * PRE-sync. Il completamento si osserva da GET /sync/status: 'cases' è
 * l'ULTIMO step della pipeline del full sync (invoices → customers →
 * matching → auto-create → order matching → cases) e persiste il proprio
 * stato anche in caso di errore, quindi il suo last_sync che cambia segnala
 * la fine dell'intera pipeline — NON il suo successo: gli errori per-step
 * vanno letti dai result (collectSyncErrors).
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
 * Step della pipeline falliti nell'ultimo sync, letti dai result persistiti.
 * Il marker che cambia dice solo che la pipeline è ARRIVATA in fondo: ogni
 * step ha il proprio try/except e può essere fallito senza fermare gli altri.
 */
export function collectSyncErrors(lastSync) {
  const failed = []
  for (const [step, info] of Object.entries(lastSync || {})) {
    // order_matching è enrichment in background (parte DOPO il marker che
    // attendiamo): un suo errore non significa "dati core non aggiornati".
    if (step === 'order_matching') continue
    const r = info?.result
    if (!r) continue
    const fp = r.fatturapro
    if (r.error || r.shopify_error || (fp && (fp.error || fp.success === false))) {
      failed.push(step)
    }
  }
  return failed
}

/**
 * Polla /sync/status finché il marker cambia rispetto a markerBefore.
 * Ritorna { completed, errors }: completed=false al timeout (sync ancora
 * in corso in background: il chiamante lo dica onestamente all'utente);
 * errors = step falliti quando completed=true.
 */
export async function waitForSyncCompletion(markerBefore) {
  let baseline = markerBefore
  for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
    await new Promise(resolve => setTimeout(resolve, POLL_INTERVAL_MS))
    try {
      const res = await client.get('/sync/status')
      const lastSync = res.data?.last_sync
      const marker = lastSync?.cases?.last_sync || ''
      if (!baseline) {
        // Baseline non letta prima del POST (errore transitorio): il primo
        // marker osservato NON distingue il sync precedente da quello in
        // corso — lo si adotta come riferimento e si attende che cambi.
        baseline = marker
        continue
      }
      if (marker && marker !== baseline) {
        return { completed: true, errors: collectSyncErrors(lastSync) }
      }
    } catch {
      // errore transitorio di polling: si riprova al giro successivo
    }
  }
  return { completed: false, errors: [] }
}
