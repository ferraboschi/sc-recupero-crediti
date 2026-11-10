import React, { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import client from '../api/client'
import AgentBadges from './AgentBadges'

// Promemoria email il giorno PRIMA della scadenza (backend/engine/reminder_email.py).
// Pannello di controllo (pagina Sistema): modalità effettiva, cosa manca per
// l'invio reale, anteprima del prossimo giro col testo, esecuzione manuale e
// registro delle email composte/inviate/saltate. Niente invii impliciti: la
// modalità dipende SOLO dalle variabili d'ambiente su Render e ogni
// esecuzione chiede conferma citando la modalità, che il server ricontrolla
// (409 se nel frattempo è cambiata).

const fmtEur = (v) => new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' }).format(v || 0)
const fmtDate = (s) => {
  if (!s) return '—'
  const d = s.length === 10 ? new Date(s + 'T00:00:00') : new Date(s)
  return d.toLocaleDateString('it-IT')
}
// I timestamp del server sono UTC senza suffisso: va dichiarato, altrimenti
// il browser li legge come ora locale (−1/−2 ore).
const asUtc = (s) => (s.endsWith('Z') || /[+-]\d\d:\d\d$/.test(s) ? s : s + 'Z')
const fmtDateTime = (s) => (s ? new Date(asUtc(s)).toLocaleString('it-IT', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—')

const MODE_STYLE = {
  prova: 'bg-[rgba(148,163,184,0.15)] text-txt-secondary',
  test: 'bg-accent-amber/15 text-accent-amber',
  reale: 'bg-accent-green/15 text-accent-green',
}
const RUN_LABEL = {
  prova: 'Prepara ora (senza inviare)',
  test: 'Invia ora (indirizzo di prova)',
  reale: 'Invia ora ai clienti',
}
const STATUS_LABEL = {
  sent: ['Inviata', 'bg-accent-green/15 text-accent-green'],
  prepared: ['Preparata (prova)', 'bg-[rgba(148,163,184,0.15)] text-txt-secondary'],
  sending: ['Interrotta', 'bg-accent-red/15 text-accent-red'],
  failed: ['Errore', 'bg-accent-red/15 text-accent-red'],
  skipped: ['Saltata', 'bg-accent-amber/15 text-accent-amber'],
}
const PLANNED_STYLE = 'bg-accent-blue/15 text-accent-blue'

export function ReminderStatusBadge({ row }) {
  const [label, cls] = STATUS_LABEL[row.status] || [row.status, STATUS_LABEL.prepared[1]]
  const suffix = row.status === 'skipped' && row.skip_label ? `: ${row.skip_label}` : ''
  return (
    <span className={`sc-badge whitespace-nowrap ${cls}`} title={row.error || row.skip_label || ''}>
      {label}{suffix}
    </span>
  )
}

async function errorDetail(e) {
  // Con responseType blob la risposta d'errore è un Blob: va letta.
  let detail = e.message
  try {
    const data = e.response?.data
    if (data && typeof data.text === 'function') detail = JSON.parse(await data.text())?.detail || detail
    else if (data?.detail) detail = data.detail
  } catch { /* resta e.message */ }
  return detail
}

function Attachments({ row }) {
  const [busy, setBusy] = useState(null)
  const download = async (index, name) => {
    if (busy !== null) return
    setBusy(index)
    try {
      const res = await client.get(`/reminders/email/${row.id}/attachments/${index}`, { responseType: 'blob' })
      const url = URL.createObjectURL(res.data)
      const a = document.createElement('a')
      a.href = url
      a.download = name
      a.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (e) {
      window.alert(`Allegato non disponibile (viene rigenerato dai dati attuali e può non esistere più): ${await errorDetail(e)}`)
    } finally {
      setBusy(null)
    }
  }
  if (!row.attachments || row.attachments.length === 0) return <span className="text-txt-muted">—</span>
  return (
    <span className="flex flex-wrap gap-1">
      {row.attachments.map((name, i) => (
        <button key={name} type="button" onClick={() => download(i, name)} disabled={busy !== null}
          className="text-xs text-accent-teal underline hover:text-accent-cyan disabled:opacity-50"
          title="Scarica il PDF (rigenerato dai dati attuali)">
          {busy === i ? '…' : i === 0 ? 'Copia di cortesia' : 'Riepilogo scadute'}
        </button>
      ))}
    </span>
  )
}

function CustomerName({ id, name, navigate }) {
  if (!id) return <span className="text-txt-primary">{name || '—'}</span>
  return (
    <button type="button" onClick={() => navigate(`/customers/${id}`)} className="text-accent-teal hover:text-accent-cyan text-left">
      {name || '—'}
    </button>
  )
}

export default function ReminderEmailPanel() {
  const navigate = useNavigate()
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [running, setRunning] = useState(false)
  const [runResult, setRunResult] = useState(null)
  const [retryingId, setRetryingId] = useState(null)
  const [testingId, setTestingId] = useState(null)
  const [openText, setOpenText] = useState(null)   // invoice_id dell'anteprima aperta
  const [detail, setDetail] = useState(null)       // riga del registro aperta

  const load = useCallback(async () => {
    try {
      const res = await client.get('/reminders/email')
      setData(res.data)
      setError(null)
      return res.data
    } catch (e) {
      setError(e.response?.data?.detail || e.message)
      return null
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const runNow = async () => {
    // Si ricarica lo stato PRIMA di chiedere conferma: la modalità mostrata
    // deve essere quella attuale (le env possono essere cambiate).
    const fresh = await load()
    if (!fresh) return
    const toSend = (fresh.preview || []).filter(p => p.will_process)
    const n = toSend.length
    const dates = (fresh.target_due_dates || []).map(fmtDate).join(', ')
    const msg = fresh.mode === 'reale'
      ? `Invio ADESSO le email AI CLIENTI per ${n} fattur${n === 1 ? 'a' : 'e'} in scadenza il ${dates}, con copia a fatture@ e al commerciale. Procedere?`
      : fresh.mode === 'test'
        ? `Invio ${n} email di prova a ${fresh.test_to} (i clienti non ricevono nulla). Procedere?`
        : `Modalità prova: preparo e registro ${n} email senza inviarle. Procedere?`
    if (!window.confirm(msg)) return
    setRunning(true)
    setRunResult(null)
    try {
      const res = await client.post('/reminders/email/run', { expected_mode: fresh.mode }, { noRetry: true, timeout: 600000 })
      setRunResult(res.data)
      await load()
    } catch (e) {
      setRunResult({ error: e.response?.data?.detail || e.message })
      await load()
    } finally {
      setRunning(false)
    }
  }

  const retry = async (row) => {
    const fresh = await load()
    if (!fresh) return
    const msg = fresh.mode === 'reale'
      ? `Ritento ADESSO l'invio AL CLIENTE (${row.to_email}) della fattura ${row.invoice_number}. Se l'invio precedente fosse in realtà partito, Resend non lo duplica. Procedere?`
      : fresh.mode === 'test'
        ? `Ritento l'invio di prova a ${fresh.test_to} per la fattura ${row.invoice_number}. Procedere?`
        : `Modalità prova: preparo di nuovo il promemoria della fattura ${row.invoice_number} senza inviarlo. Procedere?`
    if (!window.confirm(msg)) return
    setRetryingId(row.id)
    setRunResult(null)
    try {
      const res = await client.post(`/reminders/email/${row.id}/retry`, { expected_mode: fresh.mode }, { noRetry: true, timeout: 120000 })
      setRunResult(res.data)
      await load()
    } catch (e) {
      setRunResult({ error: e.response?.data?.detail || e.message })
      await load()
    } finally {
      setRetryingId(null)
    }
  }

  const sendTest = async (p) => {
    const to = window.prompt(`Invio di PROVA dell'email per ${p.ragione_sociale} (fattura ${p.invoice_number}).\nA quale indirizzo? (nessuna copia, il registro non cambia)`, data?.test_to || '')
    if (!to) return
    setTestingId(p.invoice_id)
    setRunResult(null)
    try {
      const res = await client.post('/reminders/email/test', { invoice_id: p.invoice_id, to }, { noRetry: true, timeout: 120000 })
      setRunResult({ info: `Prova inviata a ${res.data.sent_to} (${res.data.attachments.length} allegati). In reale andrebbe in copia a: ${(res.data.would_cc || []).join(', ') || 'nessuno'}.` })
    } catch (e) {
      setRunResult({ error: e.response?.data?.detail || e.message })
    } finally {
      setTestingId(null)
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
      {data && (
        <span className="text-xs text-txt-muted">
          Ogni giorno alle {data.schedule} · {data.weekend
            ? 'weekend: nessun invio (il venerdì copre sabato, domenica e lunedì)'
            : `scadenz${(data.target_due_dates || []).length === 1 ? 'a' : 'e'}: ${(data.target_due_dates || []).map(fmtDate).join(', ')}`}
        </span>
      )}
    </div>
  )

  if (loading) return <div className="sc-card overflow-hidden">{header}<p className="px-6 py-4 text-sm text-txt-muted">Caricamento…</p></div>
  if (error) return <div className="sc-card overflow-hidden">{header}<p className="px-6 py-4 text-sm text-accent-red">Errore: {error}</p></div>

  const preview = data.preview || []
  const toSend = preview.filter(p => p.will_process)
  const recent = data.recent || []
  const company = data.company || {}

  return (
    <div className="sc-card overflow-hidden">
      {header}

      {/* Configurazione */}
      <div className="px-6 py-4 border-b border-dark-border text-sm space-y-2">
        <p className="text-txt-secondary">
          Per ogni fattura non pagata in scadenza domani il cliente riceve una email con la copia di cortesia della fattura e, se ha scadute non saldate, il riepilogo con il totale. Una email per fattura.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-1 text-xs">
          <p><span className="text-txt-muted">Mittente:</span> <span className="text-txt-primary">{data.sender || '—'}</span></p>
          <p><span className="text-txt-muted">Risposte a:</span> <span className="text-txt-primary">{data.reply_to || '—'}</span></p>
          <p><span className="text-txt-muted">In copia (invio reale):</span> <span className="text-txt-primary">{data.cc || '—'}{Object.keys(data.agent_emails || {}).length > 0 ? ' + commerciale del cliente' : ''}</span></p>
          <p><span className="text-txt-muted">Commerciali:</span> <span className="text-txt-primary">{Object.entries(data.agent_emails || {}).map(([k, v]) => `${k} → ${v}`).join(' · ') || '—'}</span></p>
          <p><span className="text-txt-muted">Copia nascosta:</span> <span className="text-txt-primary">{data.bcc || '—'}</span></p>
          <p><span className="text-txt-muted">Indirizzo di prova:</span> <span className="text-txt-primary">{data.test_to || '—'}</span></p>
          <p><span className="text-txt-muted">Intestazione PDF:</span> <span className="text-txt-primary">{company.name}{company.vat ? ` · P.IVA ${company.vat}` : ''}{company.address ? ` · ${company.address}` : ''}</span></p>
          <p><span className="text-txt-muted">IBAN sui PDF:</span> <span className="text-txt-primary">{company.iban_tail ? `…${company.iban_tail}` : '—'}</span></p>
        </div>
        {data.missing_config?.length > 0 && (
          <p className="text-xs text-accent-amber">
            Invio reale spento. Mancano su Render: <span className="font-mono">{data.missing_config.join(', ')}</span>.
            Con <span className="font-mono">REMINDER_EMAIL_TEST_TO</span> impostato, tutte le email vanno a quell'indirizzo (prova sicura).
          </p>
        )}
        {!company.vat && (
          <p className="text-xs text-accent-amber">
            Sui PDF manca la P.IVA aziendale: impostare <span className="font-mono">COMPANY_VAT</span> (e <span className="font-mono">COMPANY_ADDRESS</span>) su Render prima dell'invio ai clienti.
          </p>
        )}
      </div>

      {/* Anteprima del prossimo giro */}
      <div className="px-6 py-4 border-b border-dark-border">
        <div className="flex items-center justify-between gap-3 flex-wrap mb-3">
          <div className="flex items-center gap-3 flex-wrap min-h-[2rem]">
            <p className="text-sm font-semibold text-txt-primary">
              {data.weekend ? 'Weekend: nessun promemoria oggi' : `Prossimo giro: ${preview.length === 0 ? 'nessuna fattura' : `${preview.length} fattur${preview.length === 1 ? 'a' : 'e'}`}`}
              {preview.length > 0 && <span className="text-txt-muted font-normal"> · {toSend.length} da lavorare</span>}
            </p>
            {runResult && (
              <span className={`text-xs ${runResult.error ? 'text-accent-red' : 'text-txt-secondary'}`} role="status">
                {runResult.error
                  ? `Errore: ${runResult.error}`
                  : runResult.info
                    ? runResult.info
                    : `Esito: ${runResult.sent} inviate · ${runResult.prepared} preparate · ${runResult.skipped} saltate · ${runResult.failed} errori · ${runResult.already} non lavorate`}
              </span>
            )}
          </div>
          <button type="button" onClick={runNow} disabled={running || toSend.length === 0} className="sc-btn-primary text-xs disabled:opacity-50">
            {running ? 'In corso…' : RUN_LABEL[data.mode] || 'Esegui ora'}
          </button>
        </div>
        {preview.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs uppercase tracking-wider text-txt-label border-b border-dark-border">
                  <th className="text-left py-2 pr-3">Cliente</th>
                  <th className="text-left py-2 pr-3">Fattura</th>
                  <th className="text-left py-2 pr-3">Scade</th>
                  <th className="text-right py-2 pr-3">Importo</th>
                  <th className="text-right py-2 pr-3">Scadute</th>
                  <th className="text-left py-2 pr-3">Esito previsto</th>
                  <th className="text-left py-2">Azioni</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-dark-border">
                {preview.map(p => (
                  <React.Fragment key={p.invoice_id}>
                    <tr>
                      <td className="py-2 pr-3">
                        <CustomerName id={p.customer_id} name={p.ragione_sociale} navigate={navigate} />
                        <span className="block text-xs text-txt-muted font-mono">{p.to_email || 'senza email'}</span>
                        {p.cc && p.cc.length > 0 && <span className="block text-xs text-txt-muted">cc: {p.cc.join(', ')}</span>}
                      </td>
                      <td className="py-2 pr-3 font-mono text-xs text-txt-secondary">{p.invoice_number}</td>
                      <td className="py-2 pr-3 text-xs text-txt-secondary whitespace-nowrap">{p.due_label || fmtDate(p.due_date)}</td>
                      <td className="py-2 pr-3 text-right text-txt-primary">{fmtEur(p.amount_due)}</td>
                      <td className="py-2 pr-3 text-right text-txt-secondary">{p.overdue_count > 0 ? `${p.overdue_count} · ${fmtEur(p.overdue_total)}` : '—'}</td>
                      <td className="py-2 pr-3">
                        {p.skip_reason
                          ? <span className="sc-badge whitespace-nowrap bg-accent-amber/15 text-accent-amber">Saltata: {p.skip_label}</span>
                          : p.will_process
                            ? <span className={`sc-badge whitespace-nowrap ${PLANNED_STYLE}`}>{data.mode === 'prova' ? 'Da preparare' : 'Da inviare'}{p.already ? ` (${p.why})` : ''}</span>
                            : <ReminderStatusBadge row={{ status: p.already, skip_label: p.why }} />}
                      </td>
                      <td className="py-2">
                        <span className="flex items-center gap-2 whitespace-nowrap">
                          {p.text && (
                            <button type="button" aria-expanded={openText === p.invoice_id} onClick={() => setOpenText(openText === p.invoice_id ? null : p.invoice_id)} className="text-xs text-accent-teal underline hover:text-accent-cyan">
                              {openText === p.invoice_id ? 'Chiudi' : 'Testo'}
                            </button>
                          )}
                          {!p.skip_reason && (
                            <button type="button" onClick={() => sendTest(p)} disabled={testingId !== null} className="sc-btn-secondary text-xs disabled:opacity-50" title="Invia questa email a un indirizzo a tua scelta (prefisso [PROVA], nessuna copia, registro intoccato)">
                              {testingId === p.invoice_id ? '…' : 'Invia prova a…'}
                            </button>
                          )}
                        </span>
                      </td>
                    </tr>
                    {openText === p.invoice_id && (
                      <tr>
                        <td colSpan={7} className="py-3">
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
                  <th className="text-left py-2">Azioni</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-dark-border">
                {recent.map(row => (
                  <React.Fragment key={row.id}>
                    <tr>
                      <td className="py-2 pr-3 text-xs text-txt-secondary whitespace-nowrap">{fmtDateTime(row.sent_at || row.attempted_at || row.created_at)}</td>
                      <td className="py-2 pr-3">
                        <CustomerName id={row.customer_id} name={row.ragione_sociale} navigate={navigate} />
                        <AgentBadges agents={row.agents} className="ml-2" />
                      </td>
                      <td className="py-2 pr-3 font-mono text-xs text-txt-secondary whitespace-nowrap">{row.invoice_number} <span className="text-txt-muted">· scad. {fmtDate(row.due_date)}</span></td>
                      <td className="py-2 pr-3 text-right text-txt-primary">{fmtEur(row.amount_due)}</td>
                      <td className="py-2 pr-3"><ReminderStatusBadge row={row} />{row.mode === 'test' && <span className="ml-1 text-xs text-txt-muted">(test)</span>}</td>
                      <td className="py-2 pr-3 text-xs font-mono text-txt-secondary">
                        {row.to_email || '—'}
                        {row.original_to && row.original_to !== row.to_email && <span className="block text-txt-muted">vero: {row.original_to}</span>}
                        {row.cc && row.cc.length > 0 && <span className="block text-txt-muted">cc: {row.cc.join(', ')}</span>}
                      </td>
                      <td className="py-2 pr-3"><Attachments row={row} /></td>
                      <td className="py-2">
                        <span className="flex items-center gap-2 whitespace-nowrap">
                          {row.subject && (
                            <button type="button" aria-expanded={detail?.id === row.id} onClick={() => openDetail(row)} className="text-xs text-accent-teal underline hover:text-accent-cyan">
                              {detail?.id === row.id ? 'Chiudi' : 'Testo'}
                            </button>
                          )}
                          {(row.status === 'failed' || row.status === 'sending') && (
                            <button type="button" onClick={() => retry(row)} disabled={retryingId !== null || running} className="sc-btn-secondary text-xs disabled:opacity-50" title={row.needs_manual_retry ? 'Ferma finché non la ritenti tu' : 'Verrà ritentata anche al prossimo giro'}>
                              {retryingId === row.id ? '…' : 'Riprova'}
                            </button>
                          )}
                        </span>
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
      <div className="sc-card-header flex items-center justify-between gap-3 flex-wrap">
        <h2 className="text-base font-bold text-txt-primary">Promemoria email</h2>
        <span className="text-xs text-txt-muted">Registro dei promemoria automatici (il giorno prima della scadenza): ultimi {items.length}</span>
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
                <td className="px-6 py-2 text-xs text-txt-secondary whitespace-nowrap">{fmtDateTime(row.sent_at || row.attempted_at || row.created_at)}</td>
                <td className="px-3 py-2 font-mono text-xs text-txt-secondary whitespace-nowrap">{row.invoice_number} <span className="text-txt-muted">· scad. {fmtDate(row.due_date)}</span></td>
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
