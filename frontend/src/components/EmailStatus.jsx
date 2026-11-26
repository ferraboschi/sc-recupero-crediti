import React, { useEffect, useState } from 'react'
import client from '../api/client'

// Stato delle email automatiche (promemoria pre-scadenza e 2° avviso) sotto il
// nome del cliente. Testi e regole arrivano dal server
// (backend/engine/email_status.py: solo invii reali, solo fatture ancora da
// saldare); qui solo colore e disposizione.
//
// Un colore = un significato, lo stesso in ogni pagina: verde = inviata,
// grigio = in programma, ambra = ferma (un operatore può riprenderla da
// Sistema), rosso = non partita o dall'esito incerto (il cliente potrebbe non
// averla), viola = non partita per una scelta che vale ancora («In Attesa»).
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
  ['stuck', 'ferma, da gestire'],
  ['missing', 'non partita o incerta'],
  ['paused', 'sospesa'],
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

export function useEmailStatus() {
  const [status, setStatus] = useState({ byCustomer: {}, loading: true, error: false, followupReadable: true })
  useEffect(() => {
    let alive = true
    client.get('/reminders/email/customer-status', { timeout: STATUS_TIMEOUT_MS })
      .then(res => {
        if (!alive) return
        const customers = res.data?.customers
        if (!customers || typeof customers !== 'object' || Array.isArray(customers)) {
          // Risposta che non è lo stato (es. pagina d'errore con 200): è un errore.
          setStatus({ byCustomer: {}, loading: false, error: true, followupReadable: true })
          return
        }
        setStatus({
          byCustomer: customers,
          loading: false,
          error: false,
          followupReadable: res.data.followup_readable !== false,
        })
      })
      .catch(() => {
        if (alive) setStatus({ byCustomer: {}, loading: false, error: true, followupReadable: true })
      })
    return () => { alive = false }
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
    </div>
  )
}
