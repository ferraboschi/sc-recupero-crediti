import React, { useEffect, useState } from 'react'
import client from '../api/client'

// Stato delle email automatiche (promemoria pre-scadenza e 2° avviso) sotto il
// nome del cliente. Testi e regole arrivano dal server
// (backend/engine/email_status.py: solo invii reali, solo fatture ancora da
// saldare); qui solo colore e disposizione.
//
// Un colore = un significato, lo stesso in ogni pagina: verde = inviata;
// grigio vuoto = in programma; ambra = serve un operatore, e da Sistema può
// farlo; rosso = c'è un dato da sistemare, o l'esito è incerto; viola = ADESSO
// il giro la salterebbe («In Attesa», promessa…); grigio pieno = non partita,
// un fatto passato che non chiede nulla.
const DOT = {
  sent: 'bg-accent-green',
  planned: 'border border-txt-muted',
  stuck: 'bg-accent-amber',
  missing: 'bg-accent-red',
  paused: 'bg-accent-purple',
  closed: 'bg-txt-muted',
}

const TEXT = {
  sent: 'text-txt-secondary',
  planned: 'text-txt-muted',
  stuck: 'text-accent-amber font-semibold',
  missing: 'text-accent-red font-semibold',
  paused: 'text-accent-purple',
  closed: 'text-txt-muted',
}

const LEGEND = [
  ['sent', 'inviata'],
  ['planned', 'in programma'],
  ['stuck', 'da gestire'],
  ['missing', 'da sistemare o incerta'],
  ['paused', 'sospesa'],
  ['closed', 'non partita'],
]

export function EmailItem({ tone, title, children }) {
  return (
    <span
      className={`inline-flex items-start gap-1.5 text-xs leading-4 ${TEXT[tone] || TEXT.planned}`}
      title={title || undefined}
      data-testid="email-status-item"
      data-state={tone}
    >
      <span className={`mt-1 inline-block h-2 w-2 flex-shrink-0 rounded-full ${DOT[tone] || DOT.planned}`} />
      {/* Può andare a capo ovunque: non deve mai allargare la colonna del nome. */}
      <span className="min-w-0 [overflow-wrap:anywhere]">{children}</span>
    </span>
  )
}

// Una sola lettura per pagina: {id cliente: [voci]}. La pagina non la
// aspetta. Finché la risposta non c'è, o se non arriva, la legenda lo DICE:
// l'assenza della riga sotto un nome non deve mai sembrare «nessuna email».
const STATUS_TIMEOUT_MS = 30000
// Lo stato cambia col giro del mattino: una scheda lasciata aperta lo rilegge
// da sola (ogni 5 minuti, e quando torna in primo piano) e dice a che ora.
const STATUS_REFRESH_MS = 5 * 60 * 1000

export function useEmailStatus() {
  const [status, setStatus] = useState({ byCustomer: {}, loading: true, error: false, followupReadable: true, readAt: null })
  useEffect(() => {
    let alive = true
    let latest = 0
    const read = () => {
      // Vale solo la risposta dell'ULTIMA lettura partita: una vecchia che
      // arriva tardi (il client riprova da solo per decine di secondi) non
      // deve coprire una più nuova.
      const mine = ++latest
      client.get('/reminders/email/customer-status', { timeout: STATUS_TIMEOUT_MS })
        .then(res => {
          if (!alive || mine !== latest) return
          const customers = res.data?.customers
          if (!customers || typeof customers !== 'object' || Array.isArray(customers)) {
            // Risposta che non è lo stato (es. pagina d'errore con 200): è un errore.
            setStatus({ byCustomer: {}, loading: false, error: true, followupReadable: true, readAt: null })
            return
          }
          setStatus({
            byCustomer: customers,
            loading: false,
            error: false,
            followupReadable: res.data.followup_readable !== false,
            readAt: res.data.read_at || null,
          })
        })
        .catch(() => {
          // Meglio nessuna riga, e dirlo, che righe che potrebbero non valere più.
          if (alive && mine === latest) setStatus({ byCustomer: {}, loading: false, error: true, followupReadable: true, readAt: null })
        })
    }
    read()
    const timer = setInterval(read, STATUS_REFRESH_MS)
    const onVisible = () => { if (document.visibilityState === 'visible') read() }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      alive = false
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [])
  return status
}

export function EmailStatusLine({ status, customerId, className = 'mt-1' }) {
  const items = status?.byCustomer?.[customerId]
  if (!Array.isArray(items) || items.length === 0) return null
  return (
    <div className={`flex flex-wrap items-center gap-x-3 gap-y-0.5 ${className}`} data-testid="email-status-line">
      {items.map((item, i) => (
        <EmailItem key={`${i}-${item.kind}-${item.state}`} tone={item.state} title={item.title}>
          {item.label}
        </EmailItem>
      ))}
    </div>
  )
}

// `flush`: senza rientro laterale, per le tabelle che non ne hanno (Attività).
export function EmailLegend({ status, flush = false, className = '' }) {
  return (
    <div
      className={`flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-dark-border py-2 ${flush ? '' : 'px-4'} ${className}`}
      data-testid="email-status-legend"
    >
      <span className="text-xs font-semibold uppercase tracking-wider text-txt-label">Email automatiche sotto il nome</span>
      {LEGEND.map(([tone, label]) => <EmailItem key={tone} tone={tone}>{label}</EmailItem>)}
      {status?.loading && (
        <span className="text-xs text-txt-muted" role="status" data-testid="email-status-loading">
          Lettura dello stato delle email in corso…
        </span>
      )}
      {status?.error && (
        <span className="text-xs font-semibold text-accent-amber" role="status">
          Stato delle email non disponibile: ricarica la pagina.
        </span>
      )}
      {!status?.loading && !status?.error && status?.followupReadable === false && (
        <span className="text-xs font-semibold text-accent-amber" role="status">
          Secondi avvisi non leggibili: qui sotto solo i promemoria.
        </span>
      )}
      {!status?.loading && !status?.error && status?.readAt && (
        <span className="text-xs text-txt-muted" data-testid="email-status-read-at">letto alle {status.readAt}</span>
      )}
    </div>
  )
}

// Dove la legenda non c'è (elenchi nei riquadri): almeno dire se lo stato
// delle email non è ancora arrivato o non è disponibile.
export function EmailStatusNote({ status, className = '' }) {
  const unreadable = !status?.loading && !status?.error && status?.followupReadable === false
  if (!status?.loading && !status?.error && !unreadable) return null
  return (
    <p className={`text-xs ${status.loading ? 'text-txt-muted' : 'font-semibold text-accent-amber'} ${className}`} role="status">
      {status.error
        ? 'Stato delle email non disponibile: ricarica la pagina.'
        : unreadable
          ? 'Secondi avvisi non leggibili: sotto i nomi solo i promemoria.'
          : 'Lettura dello stato delle email in corso…'}
    </p>
  )
}
