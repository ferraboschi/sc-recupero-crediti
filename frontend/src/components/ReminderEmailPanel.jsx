import React, { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import client from '../api/client'
import AgentBadges from './AgentBadges'

// Promemoria email il giorno PRIMA della scadenza (backend/engine/reminder_email.py).
// Pannello di controllo (pagina Sistema): modalità effettiva, cosa manca per
// l'invio reale, anteprima del prossimo giro col testo, esecuzione manuale e
// registro delle email composte/inviate/saltate. Niente invii impliciti: la
// modalità dipende SOLO dalle variabili d'ambiente su Render.

const fmtEur = (v) => new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' }).format(v || 0)
const fmtDate = (s) => {
  if (!s) return '—'
  const d = s.length === 10 ? new Date(s + 'T00:00:00') : new Date(s)
  return d.toLocaleDateString('it-IT')
}
const fmtDateTime = (s) => (s ? new Date(s).toLocaleString('it-IT', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—')

const MODE_STYLE = {
  prova: 'bg-[rgba(148,163,184,0.15)] text-txt-secondary',
  test: 'bg-accent-amber/15 text-accent-amber',
  reale: 'bg-accent-green/15 text-accent-green',
}
const STATUS_LABEL = {
  sent: ['Inviata', 'bg-accent-green/15 text-accent-green'],
  prepared: ['Preparata (prova)', 'bg-[rgba(148,163,184,0.15)] text-txt-secondary'],
  failed: ['Errore', 'bg-accent-red/15 text-accent-red'],
  skipped: ['Saltata', 'bg-accent-amber/15 text-accent-amber'],
}

export function ReminderStatusBadge({ row }) {
  const [label, cls] = STATUS_LABEL[row.status] || [row.status, STATUS_LABEL.prepared[1]]
  return (
    <span className={`sc-badge ${cls}`} title={row.error || row.skip_label || ''}>
      {label}{row.status === 'skipped' && row.skip_label ? `: ${row.skip_label}` : ''}
    </span>
  )
}

function Attachments({ row }) {
  const download = async (index, name) => {
    try {
      const res = await client.get(`/reminders/email/${row.id}/attachments/${index}`, { responseType: 'blob' })
      const url = URL.createObjectURL(res.data)
      const a = document.createElement('a')
      a.href = url
      a.download = name
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      window.alert('Allegato non disponibile: ' + (e.response?.data?.detail || e.message))
    }
  }
  if (!row.attachments || row.attachments.length === 0) return <span className="text-txt-muted">—</span>
  return (
    <span className="flex flex-wrap gap-1">
      {row.attachments.map((name, i) => (
        <button key={name} type="button" onClick={() => download(i, name)} className="text-xs text-accent-teal underline hover:text-accent-cyan" title="Scarica il PDF (rigenerato dai dati attuali)">
          {i === 0 ? 'Fattura' : 'Riepilogo scadute'}
        </button>
      ))}
    </span>
  )
}

export default function ReminderEmailPanel() {
  const navigate = useNavigate()
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [running, setRunning] = useState(false)
  const [runResult, setRunResult] = useState(null)
  const [openText, setOpenText] = useState(null)   // invoice_id dell'anteprima aperta
  const [detail, setDetail] = useState(null)       // riga del registro aperta

  const load = useCallback(async () => {
    try {
      const res = await client.get('/reminders/email')
      setData(res.data)
      setError(null)
    } catch (e) {
      setError(e.response?.data?.detail || e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const runNow = async () => {
    if (!data) return
    const n = (data.preview || []).filter(p => !p.skip_reason && !p.already).length
    const msg = data.mode === 'reale'
      ? `Invio ADESSO le email AI CLIENTI per le ${n} fatture in scadenza il ${fmtDate(data.target_due_date)}. Procedere?`
      : data.mode === 'test'
        ? `Invio le ${n} email di prova a ${data.test_to} (i clienti non ricevono nulla). Procedere?`
        : `Modalità prova: preparo e registro le ${n} email senza inviarle. Procedere?`
    if (!window.confirm(msg)) return
    setRunning(true)
    setRunResult(null)
    try {
      const res = await client.post('/reminders/email/run')
      setRunResult(res.data)
      await load()
    } catch (e) {
      setRunResult({ error: e.response?.data?.detail || e.message })
    } finally {
      setRunning(false)
    }
  }

  const openDetail = async (row) => {
    if (detail?.id === row.id) { setDetail(null); return }
    try {
      const res = await client.get(`/reminders/email/${row.id}`)
      setDetail(res.data)
    } catch (e) {
      window.alert(e.response?.data?.detail || e.message)
    }
  }

  const header = (
    <div className="sc-card-header bg-dark-surface flex items-center justify-between gap-3 flex-wrap">
      <div className="flex items-center gap-3 flex-wrap">
        <h3 className="sc-section-title">Promemoria email pre-scadenza</h3>
        {data && <span className={`sc-badge ${MODE_STYLE[data.mode] || MODE_STYLE.prova}`}>{data.mode_label}</span>}
      </div>
      {data && <span className="text-xs text-txt-muted">Ogni giorno alle {data.schedule} · scadenza di domani: {fmtDate(data.target_due_date)}</span>}
    </div>
  )

  if (loading) return <div className="sc-card overflow-hidden">{header}<p className="px-6 py-4 text-sm text-txt-muted">Caricamento…</p></div>
  if (error) return <div className="sc-card overflow-hidden">{header}<p className="px-6 py-4 text-sm text-accent-red">Errore: {error}</p></div>

  const preview = data.preview || []
  const toSend = preview.filter(p => !p.skip_reason && !p.already)
  const recent = data.recent || []

  return (
    <div className="sc-card overflow-hidden">
      {header}

      {/* Configurazione */}
      <div className="px-6 py-4 border-b border-dark-border text-sm space-y-2">
        <p className="text-txt-secondary">
          Per ogni fattura non pagata in scadenza domani il cliente riceve una email con il PDF della fattura e, se ha scadute non saldate, il riepilogo con il totale. Una email per fattura.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-1 text-xs">
          <p><span className="text-txt-muted">Mittente:</span> <span className="text-txt-primary">{data.sender || '—'}</span></p>
          <p><span className="text-txt-muted">Risposte a:</span> <span className="text-txt-primary">{data.reply_to || '—'}</span></p>
          <p><span className="text-txt-muted">Copia nascosta:</span> <span className="text-txt-primary">{data.bcc || '—'}</span></p>
          <p><span className="text-txt-muted">Indirizzo di prova:</span> <span className="text-txt-primary">{data.test_to || '—'}</span></p>
        </div>
        {data.missing_config?.length > 0 && (
          <p className="text-xs text-accent-amber">
            Invio reale spento. Mancano su Render: <span className="font-mono">{data.missing_config.join(', ')}</span>.
            Con <span className="font-mono">REMINDER_EMAIL_TEST_TO</span> impostato, tutte le email vanno a quell'indirizzo (prova sicura).
          </p>
        )}
      </div>

      {/* Anteprima del prossimo giro */}
      <div className="px-6 py-4 border-b border-dark-border">
        <div className="flex items-center justify-between gap-3 flex-wrap mb-3">
          <p className="text-sm font-semibold text-txt-primary">
            Scadenza {fmtDate(data.target_due_date)}: {preview.length === 0 ? 'nessuna fattura' : `${preview.length} fattur${preview.length === 1 ? 'a' : 'e'}`}
            {preview.length > 0 && <span className="text-txt-muted font-normal"> · {toSend.length} da inviare</span>}
          </p>
          <button type="button" onClick={runNow} disabled={running || preview.length === 0} className="sc-btn-primary text-xs disabled:opacity-50">
            {running ? 'In corso…' : data.mode === 'prova' ? 'Prepara ora (senza inviare)' : 'Esegui ora'}
          </button>
        </div>
        {runResult && (
          <p className={`text-xs mb-3 ${runResult.error ? 'text-accent-red' : 'text-txt-secondary'}`}>
            {runResult.error
              ? `Errore: ${runResult.error}`
              : `Esito: ${runResult.sent} inviate · ${runResult.prepared} preparate · ${runResult.skipped} saltate · ${runResult.failed} errori · ${runResult.already} già fatte`}
          </p>
        )}
        {preview.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs uppercase tracking-wider text-txt-label border-b border-dark-border">
                  <th className="text-left py-2 pr-3">Cliente</th>
                  <th className="text-left py-2 pr-3">Fattura</th>
                  <th className="text-right py-2 pr-3">Importo</th>
                  <th className="text-right py-2 pr-3">Scadute</th>
                  <th className="text-left py-2 pr-3">Esito previsto</th>
                  <th className="text-left py-2">Testo</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-dark-border">
                {preview.map(p => (
                  <React.Fragment key={p.invoice_id}>
                    <tr>
                      <td className="py-2 pr-3">
                        <button type="button" onClick={() => p.customer_id && navigate(`/customers/${p.customer_id}`)} className="text-accent-teal hover:text-accent-cyan text-left">{p.ragione_sociale || '—'}</button>
                        <span className="block text-xs text-txt-muted font-mono">{p.to_email || 'senza email'}</span>
                      </td>
                      <td className="py-2 pr-3 font-mono text-xs text-txt-secondary">{p.invoice_number}</td>
                      <td className="py-2 pr-3 text-right text-txt-primary">{fmtEur(p.amount_due)}</td>
                      <td className="py-2 pr-3 text-right text-txt-secondary">{p.overdue_count > 0 ? `${p.overdue_count} · ${fmtEur(p.overdue_total)}` : '—'}</td>
                      <td className="py-2 pr-3">
                        {p.already
                          ? <ReminderStatusBadge row={{ status: p.already }} />
                          : p.skip_reason
                            ? <span className="sc-badge bg-accent-amber/15 text-accent-amber">Saltata: {p.skip_label}</span>
                            : <span className="sc-badge bg-accent-green/15 text-accent-green">{data.mode === 'prova' ? 'Da preparare' : 'Da inviare'}</span>}
                      </td>
                      <td className="py-2">
                        {p.text && (
                          <button type="button" onClick={() => setOpenText(openText === p.invoice_id ? null : p.invoice_id)} className="text-xs text-accent-teal underline hover:text-accent-cyan">
                            {openText === p.invoice_id ? 'Chiudi' : 'Mostra'}
                          </button>
                        )}
                      </td>
                    </tr>
                    {openText === p.invoice_id && (
                      <tr>
                        <td colSpan={6} className="py-3">
                          <p className="text-xs text-txt-muted mb-1">Oggetto: <span className="text-txt-primary">{p.subject}</span></p>
                          <pre className="whitespace-pre-wrap text-xs text-txt-secondary bg-dark-bg/60 border border-dark-border rounded-lg p-3 font-sans">{p.text}</pre>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Registro */}
      <div className="px-6 py-4">
        <p className="text-sm font-semibold text-txt-primary mb-3">Ultime email registrate</p>
        {recent.length === 0 ? (
          <p className="text-sm text-txt-muted">Nessuna email ancora registrata.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs uppercase tracking-wider text-txt-label border-b border-dark-border">
                  <th className="text-left py-2 pr-3">Quando</th>
                  <th className="text-left py-2 pr-3">Cliente</th>
                  <th className="text-left py-2 pr-3">Fattura</th>
                  <th className="text-right py-2 pr-3">Importo</th>
                  <th className="text-left py-2 pr-3">Stato</th>
                  <th className="text-left py-2 pr-3">Destinatario</th>
                  <th className="text-left py-2 pr-3">Allegati</th>
                  <th className="text-left py-2">Testo</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-dark-border">
                {recent.map(row => (
                  <React.Fragment key={row.id}>
                    <tr>
                      <td className="py-2 pr-3 text-xs text-txt-secondary whitespace-nowrap">{fmtDateTime(row.sent_at || row.created_at)}</td>
                      <td className="py-2 pr-3">
                        <button type="button" onClick={() => row.customer_id && navigate(`/customers/${row.customer_id}`)} className="text-accent-teal hover:text-accent-cyan text-left">{row.ragione_sociale || '—'}</button>
                        <AgentBadges agents={row.agents} className="ml-2" />
                      </td>
                      <td className="py-2 pr-3 font-mono text-xs text-txt-secondary">{row.invoice_number} <span className="text-txt-muted">· scad. {fmtDate(row.due_date)}</span></td>
                      <td className="py-2 pr-3 text-right text-txt-primary">{fmtEur(row.amount_due)}</td>
                      <td className="py-2 pr-3"><ReminderStatusBadge row={row} />{row.mode === 'test' && <span className="ml-1 text-xs text-txt-muted">(test)</span>}</td>
                      <td className="py-2 pr-3 text-xs font-mono text-txt-secondary">
                        {row.to_email || '—'}
                        {row.original_to && row.original_to !== row.to_email && <span className="block text-txt-muted">vero: {row.original_to}</span>}
                      </td>
                      <td className="py-2 pr-3"><Attachments row={row} /></td>
                      <td className="py-2">
                        {row.subject && (
                          <button type="button" onClick={() => openDetail(row)} className="text-xs text-accent-teal underline hover:text-accent-cyan">
                            {detail?.id === row.id ? 'Chiudi' : 'Mostra'}
                          </button>
                        )}
                      </td>
                    </tr>
                    {detail?.id === row.id && (
                      <tr>
                        <td colSpan={8} className="py-3">
                          <p className="text-xs text-txt-muted mb-1">Oggetto: <span className="text-txt-primary">{detail.subject}</span></p>
                          <pre className="whitespace-pre-wrap text-xs text-txt-secondary bg-dark-bg/60 border border-dark-border rounded-lg p-3 font-sans">{detail.body_text}</pre>
                          {detail.error && <p className="text-xs text-accent-red mt-2">Errore: {detail.error}</p>}
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

// Scheda cliente: le email di promemoria che riguardano QUESTO cliente.
export function ReminderEmailHistory({ customerId }) {
  const [items, setItems] = useState(null)
  useEffect(() => {
    let alive = true
    client.get('/reminders/email/log', { params: { customer_id: customerId, limit: 20 } })
      .then(res => { if (alive) setItems(res.data.items || []) })
      .catch(() => { if (alive) setItems([]) })
    return () => { alive = false }
  }, [customerId])
  if (!items || items.length === 0) return null
  return (
    <div className="sc-card overflow-hidden">
      <div className="sc-card-header">
        <h2 className="text-base font-bold text-txt-primary">Promemoria email ({items.length})</h2>
        <span className="text-xs text-txt-muted">Inviati automaticamente il giorno prima della scadenza</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs uppercase tracking-wider text-txt-label border-b border-dark-border">
              <th className="text-left px-6 py-2">Quando</th>
              <th className="text-left px-3 py-2">Fattura</th>
              <th className="text-right px-3 py-2">Importo</th>
              <th className="text-left px-3 py-2">Stato</th>
              <th className="text-left px-3 py-2">Destinatario</th>
              <th className="text-left px-3 py-2">Allegati</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-dark-border">
            {items.map(row => (
              <tr key={row.id}>
                <td className="px-6 py-2 text-xs text-txt-secondary whitespace-nowrap">{fmtDateTime(row.sent_at || row.created_at)}</td>
                <td className="px-3 py-2 font-mono text-xs text-txt-secondary">{row.invoice_number} <span className="text-txt-muted">· scad. {fmtDate(row.due_date)}</span></td>
                <td className="px-3 py-2 text-right text-txt-primary">{fmtEur(row.amount_due)}</td>
                <td className="px-3 py-2"><ReminderStatusBadge row={row} />{row.mode === 'test' && <span className="ml-1 text-xs text-txt-muted">(test)</span>}</td>
                <td className="px-3 py-2 text-xs font-mono text-txt-secondary">{row.to_email || '—'}</td>
                <td className="px-3 py-2"><Attachments row={row} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
