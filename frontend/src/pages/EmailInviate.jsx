import React, { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import client from '../api/client'
import AgentBadges from '../components/AgentBadges'
import { EmailItem } from '../components/EmailStatus'

// «Email inviate»: promemoria pre-scadenza e 2° avviso in un solo elenco,
// giorno per giorno (richiesta owner 26/11/2026: «a Luigi serve sapere quali
// mail sono partite e quando»). Sola lettura: righe e regole arrivano da
// backend/engine/email_status.py (solo invii reali; prove e test restano in
// Sistema). Le azioni sulle email ferme restano in Sistema.

const STEP_DAYS = 14
// L'elenco cambia col giro del mattino: una scheda lasciata aperta lo rilegge
// da sola (ogni 5 minuti, e quando torna in primo piano).
const REFRESH_MS = 5 * 60 * 1000

const FILTERS = [
  ['all', 'Tutte'],
  ['promemoria', 'Promemoria'],
  ['followup', '2° avviso'],
  ['stuck', 'Da gestire'],
]

// Il tipo di email è scritto nel badge: niente colore (blu e ambra sono già
// I e II Contatto). Il 2° avviso è pieno, il promemoria a solo bordo.
const KIND_STYLE = {
  promemoria: 'border border-txt-secondary/60 text-txt-secondary',
  followup: 'bg-[rgba(148,163,184,0.18)] text-txt-primary',
}

const formatCurrency = (value) =>
  new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' }).format(value || 0)

const formatDay = (iso) => {
  if (!iso) return ''
  const [y, m, d] = iso.split('-')
  return `${d}/${m}/${y}`
}

function InvoiceNumbers({ invoices }) {
  const numbers = (invoices || []).map(i => i.number).filter(Boolean)
  if (numbers.length === 0) return <span>—</span>
  return numbers.map((number, i) => <span key={`${i}-${number}`} className="block whitespace-nowrap">{number}</span>)
}

function matches(entry, q) {
  if (!q) return true
  const hay = [entry.ragione_sociale, entry.to_email, ...(entry.invoices || []).map(i => i.number)]
    .filter(Boolean).join(' ').toLowerCase()
  return hay.includes(q)
}

function daySummary(items) {
  const count = (state) => items.filter(e => e.state === state).length
  const sent = count('sent')
  const parts = sent ? [`${sent} ${sent === 1 ? 'email inviata' : 'email inviate'}`] : []
  const stuck = count('stuck')
  if (stuck) parts.push(`${stuck} ${stuck === 1 ? 'ferma' : 'ferme'}`)
  const missing = count('missing')
  if (missing) parts.push(`${missing} da sistemare o ${missing === 1 ? 'incerta' : 'incerte'}`)
  return parts.length ? parts.join(' · ') : 'nessuna email inviata'
}

function EntriesTable({ items, withDay = false }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full table-fixed min-w-[980px]">
        <colgroup>
          {/* Fisse le colonne dal contenuto corto (ora, tipo, numero fattura, importo,
              esito); il destinatario cresce con lo schermo; il cliente prende il resto
              (a 1280 con la barra laterale aperta: circa 150 px). */}
          {[withDay ? '104px' : '80px', '128px', undefined, '164px', '120px', '19%', '168px'].map((w, i) => (
            <col key={i} style={w ? { width: w } : undefined} />
          ))}
        </colgroup>
        <thead className="bg-dark-surface border-b border-dark-border">
          <tr>
            {[withDay ? 'Quando' : 'Ora', 'Email', 'Cliente', 'Fatture', 'Importo', 'Destinatario', 'Esito'].map((h, i) => (
              <th
                key={h}
                className={`px-4 py-3 text-xs font-semibold text-txt-label uppercase tracking-wider ${i === 4 ? 'text-right' : 'text-left'}`}
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {items.map((entry, i) => (
            <tr key={`${i}-${entry.key}`} className="sc-table-row align-top" data-testid="email-sent-row" data-state={entry.state}>
              <td className="px-4 py-3 text-sm text-txt-muted font-mono">
                {withDay && <span className="block text-xs">{formatDay(entry.day)}</span>}
                {entry.time || '—'}
              </td>
              <td className="px-4 py-3">
                <span className={`${KIND_STYLE[entry.kind] || KIND_STYLE.promemoria} sc-badge whitespace-nowrap`}>
                  {entry.kind_label}
                </span>
              </td>
              <td className="px-4 py-3 text-sm font-medium break-words">
                {entry.customer_id ? (
                  <Link to={`/customers/${entry.customer_id}`} className="text-accent-teal hover:text-accent-cyan">
                    {entry.ragione_sociale || `Cliente #${entry.customer_id}`}
                  </Link>
                ) : (
                  <span className="text-txt-secondary">{entry.ragione_sociale || '—'}</span>
                )}
                <AgentBadges agents={entry.agents} className="ml-2" />
              </td>
              <td className="px-4 py-3 text-sm text-txt-secondary"><InvoiceNumbers invoices={entry.invoices} /></td>
              <td className="px-4 py-3 text-sm text-right font-bold text-txt-primary whitespace-nowrap">
                {formatCurrency(entry.amount)}
              </td>
              <td className="px-4 py-3 text-xs text-txt-muted [overflow-wrap:anywhere]">
                {entry.to_email || '—'}
                {Array.isArray(entry.cc) && entry.cc.length > 0 && (
                  <span className="block mt-1" data-testid="email-sent-cc">
                    in copia:
                    {entry.cc.map(address => <span key={address} className="block">{address}</span>)}
                  </span>
                )}
              </td>
              <td className="px-4 py-3">
                <EmailItem tone={entry.state} title={entry.detail}>{entry.state_label}</EmailItem>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function EmailInviate() {
  const [days, setDays] = useState(STEP_DAYS)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [attempt, setAttempt] = useState(0)  // una nuova lettura anche a parità di giorni (dopo un errore)
  const [refreshFailed, setRefreshFailed] = useState(false)  // l'ultima rilettura automatica non è riuscita
  const [filter, setFilter] = useState('all')
  const [search, setSearch] = useState('')

  useEffect(() => {
    let alive = true
    let latest = 0
    let busy = false   // una lettura (la prima o una rilettura) è in corso
    const read = (quiet) => {
      // Una rilettura automatica non parte mai mentre un'altra lettura è in
      // corso: se fallisse subito ne butterebbe via la risposta buona.
      if (quiet && busy) return
      busy = true
      const mine = ++latest   // vale solo la risposta dell'ultima lettura partita
      if (!quiet) setLoading(true)
      client.get('/reminders/email/sent', { params: { days }, noRetry: quiet })
        .then(res => {
          if (!alive || mine !== latest) return
          // Una risposta che non è l'elenco (es. una pagina d'errore con 200) è un errore.
          if (!Array.isArray(res.data?.days) || !Array.isArray(res.data?.stuck)) throw new Error('risposta non valida')
          setData(res.data)
          setError(null)
          setRefreshFailed(false)
        })
        .catch(() => {
          if (!alive || mine !== latest) return
          // Una rilettura automatica fallita non cancella ciò che c'è, ma lo dice.
          if (quiet) setRefreshFailed(true)
          else setError('Elenco delle email non disponibile: ricarica la pagina.')
        })
        .finally(() => {
          busy = false
          if (!quiet && alive) setLoading(false)
        })
    }
    read(false)
    const timer = setInterval(() => read(true), REFRESH_MS)
    const onVisible = () => { if (document.visibilityState === 'visible') read(true) }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      alive = false
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [days, attempt])

  const q = search.trim().toLowerCase()
  const visibleDays = useMemo(() => {
    if (!data) return []
    return data.days
      .map(day => ({
        ...day,
        items: day.items.filter(e => (filter === 'all' || e.kind === filter) && matches(e, q)),
      }))
      .filter(day => day.items.length > 0)
  }, [data, filter, q])
  const stuck = useMemo(() => (data ? data.stuck.filter(e => matches(e, q)) : []), [data, q])

  const totals = data?.totals || {}
  const maxDays = data?.range?.max_days || 90
  // Senza dati non si mostra uno zero: «0 ferme» sarebbe un'affermazione.
  const shown = (value) => (data ? (value ?? 0) : '—')
  const cards = [
    ['Oggi', shown(totals.today_sent), 'email inviate', 'text-txt-primary'],
    ['Ultimi 7 giorni', shown(totals.week_sent), 'email inviate', 'text-txt-primary'],
    ['Ferme', shown(totals.stuck), 'da gestire', (totals.stuck || 0) > 0 ? 'text-accent-amber' : 'text-txt-primary'],
    ['Da sistemare o incerte', shown(totals.week_missing), 'negli ultimi 7 giorni', (totals.week_missing || 0) > 0 ? 'text-accent-red' : 'text-txt-primary'],
  ]

  return (
    <div className="space-y-6" data-testid="email-sent-page">
      <div className="sc-card">
        <div className="sc-card-body flex flex-wrap items-end gap-4">
          <div className="flex-1 min-w-[240px]">
            <label htmlFor="email-sent-search" className="sc-section-title block mb-2">Cerca</label>
            <input
              id="email-sent-search"
              type="text"
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Ragione sociale, numero fattura, indirizzo…"
              className="w-full bg-dark-surface border border-dark-border rounded-lg px-3 py-2 text-sm text-txt-primary placeholder-txt-muted focus:outline-none focus:border-accent-teal"
            />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {FILTERS.map(([key, label]) => (
              <button
                key={key}
                type="button"
                onClick={() => setFilter(key)}
                aria-pressed={filter === key}
                className={`px-3 py-2 rounded-lg text-sm border transition-colors ${filter === key
                  ? 'bg-accent-teal/15 border-accent-teal text-accent-teal'
                  : 'bg-dark-surface border-dark-border text-txt-secondary hover:text-txt-primary'}`}
              >
                {label}{key === 'stuck' && (totals.stuck || 0) > 0 ? ` (${totals.stuck})` : ''}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-2 xl:grid-cols-4 gap-4">
        {cards.map(([title, value, sub, color]) => (
          <div key={title} className="sc-card px-5 py-4">
            <p className="sc-section-title">{title}</p>
            <p className={`text-2xl font-bold mt-1 ${color}`}>{value}</p>
            <p className="text-xs text-txt-muted">{sub}</p>
          </div>
        ))}
      </div>

      {error && <div className="sc-card p-5 text-accent-red" role="alert">{error}</div>}
      {data && refreshFailed && (
        <div className="sc-card p-4 text-sm text-accent-amber" role="status" data-testid="email-sent-stale">
          L'aggiornamento automatico non è riuscito: i dati sono quelli letti alle {data.read_at}
          {data.today ? ` del ${formatDay(data.today)}` : ''}. Ricarica la pagina.
        </div>
      )}
      {data && data.followup_readable === false && (
        <div className="sc-card p-4 text-sm text-accent-amber" role="status">
          Secondi avvisi non leggibili in questo momento: l'elenco mostra solo i promemoria.
        </div>
      )}

      {loading && !data && <div className="sc-card p-8 text-center text-txt-muted">Caricamento…</div>}

      {data && filter === 'stuck' && (
        <div className="sc-card overflow-hidden" data-testid="email-sent-stuck">
          <div className="sc-card-header">
            <h3 className="text-base font-bold text-txt-primary">Da gestire</h3>
            <Link to="/system" className="text-xs font-semibold text-accent-teal hover:text-accent-cyan">
              Gestisci in Sistema →
            </Link>
          </div>
          {stuck.length === 0 ? (
            <p className="p-6 text-center text-sm text-txt-muted">
              {q ? 'Nessuna email ferma per questa ricerca.' : 'Nessuna email ferma.'}
            </p>
          ) : (
            <EntriesTable items={stuck} withDay />
          )}
          <p className="px-5 py-3 text-xs text-txt-muted border-t border-dark-border">
            Solo ciò che si può riprendere da Sistema: secondi avvisi fermi di fatture ancora da saldare (di qualunque
            data) e promemoria fermi la cui scadenza non è ancora passata. Nessun giro in programma li riprenderà.
            {data.read_at ? ` Letto alle ${data.read_at}.` : ''}
          </p>
        </div>
      )}

      {data && filter !== 'stuck' && (
        <>
          {visibleDays.length === 0 && (
            <div className="sc-card p-8 text-center text-txt-muted">
              {q || filter !== 'all' ? 'Nessuna email per questi filtri nel periodo.' : 'Nessuna email nel periodo.'}
            </div>
          )}
          {visibleDays.map(day => (
            <div key={day.date} className="sc-card overflow-hidden" data-testid="email-sent-day">
              <div className="sc-card-header">
                <h3 className="text-base font-bold text-txt-primary">{day.label}</h3>
                <span className="text-xs text-txt-muted">{daySummary(day.items)}</span>
              </div>
              <EntriesTable items={day.items} />
            </div>
          ))}
          <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-txt-muted">
            <span>
              Dal {formatDay(data.range.from)} al {formatDay(data.range.to)} · letto alle {data.read_at} · solo invii reali ai clienti (prove e test restano in Sistema)
              {data.followup_mode === 'off' ? ' · secondo avviso spento: partono solo i promemoria' : ''}
            </span>
            {data.range.days < maxDays && (
              <button
                type="button"
                className="sc-btn-secondary"
                disabled={loading}
                onClick={() => { setDays(Math.min(data.range.days + STEP_DAYS, maxDays)); setAttempt(n => n + 1) }}
              >
                {loading ? 'Caricamento…' : `Mostra altri ${Math.min(STEP_DAYS, maxDays - data.range.days)} giorni`}
              </button>
            )}
          </div>
        </>
      )}
    </div>
  )
}
