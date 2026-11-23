import React, { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import client from '../api/client'
import AgentBadges from './AgentBadges'
import {
  Attachments, CustomerName, MODE_STYLE, PLANNED_STYLE, RUN_LABEL, ReminderStatusBadge,
  fmtDate, fmtDateTime, fmtEur, rowPath,
} from './ReminderEmailPanel'

// Secondo avviso email (backend/engine/followup_email.py): a N giorni dalla
// scadenza, se la fattura del promemoria risulta ancora non pagata, il
// cliente riceve una seconda email, più diretta. Pannello di controllo
// (pagina Sistema), gemello di quello dei promemoria: acceso/spento,
// affidabilità del dato «non pagata» (ultimo sync delle fatture), anteprima
// del prossimo giro col testo, esecuzione manuale e registro. SPENTO finché
// REMINDER_FOLLOWUP_ENABLED non è impostata su Render: da spento si vede solo
// cosa partirebbe e si può inviare una prova a un indirizzo interno.

const OFF_STYLE = MODE_STYLE.prova

export default function FollowupEmailPanel() {
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
      const res = await client.get('/reminders/followup')
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

  const copies = (d) => `${(d.cc_effective || []).length ? `, con copia a ${d.cc_effective.join(', ')}` : ''}${Object.keys(d.agent_emails || {}).length ? ' e al commerciale del cliente' : ''}`

  const runNow = async () => {
    // Stato ricaricato PRIMA della conferma: modalità, sync e anteprima
    // devono essere quelli attuali.
    const fresh = await load()
    if (!fresh || !fresh.enabled) return
    const n = (fresh.preview || []).filter(p => p.will_process).length
    const fatture = `${n} fattur${n === 1 ? 'a' : 'e'} scadut${n === 1 ? 'a' : 'e'} da almeno ${fresh.days_after_due} giorni`
    const msg = fresh.mode === 'reale'
      ? `Invio ADESSO il secondo avviso AI CLIENTI per ${fatture}${copies(fresh)}. Procedere?`
      : fresh.mode === 'test'
        ? `Invio ${n} email di prova a ${fresh.test_to} (i clienti non ricevono nulla). Procedere?`
        : `Modalità prova: preparo e registro ${n} avvisi senza inviarli. Procedere?`
    if (!window.confirm(msg)) return
    setRunning(true)
    setRunResult(null)
    try {
      const res = await client.post('/reminders/followup/run', { expected_mode: fresh.mode }, { noRetry: true, timeout: 600000 })
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
      ? `Ritento ADESSO l'invio AL CLIENTE (${row.to_email}) del secondo avviso per la fattura ${row.invoice_number}. Se l'invio precedente fosse in realtà partito, Resend non lo duplica. Procedere?`
      : fresh.mode === 'test'
        ? `Ritento l'invio di prova a ${fresh.test_to} per la fattura ${row.invoice_number}. Procedere?`
        : `Ritento il secondo avviso della fattura ${row.invoice_number} (modalità: ${fresh.mode_label}). Procedere?`
    if (!window.confirm(msg)) return
    setRetryingId(row.id)
    setRunResult(null)
    try {
      const res = await client.post(`${rowPath(row)}/retry`, { expected_mode: fresh.mode }, { noRetry: true, timeout: 120000 })
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
    const to = window.prompt(`Invio di PROVA del secondo avviso per ${p.ragione_sociale} (fattura ${p.invoice_number}).\nA quale indirizzo? (nessuna copia, il registro non cambia)`, data?.test_to || '')
    if (!to) return
    setTestingId(p.invoice_id)
    setRunResult(null)
    try {
      const res = await client.post('/reminders/followup/test', { invoice_id: p.invoice_id, to }, { noRetry: true, timeout: 120000 })
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
      const res = await client.get(rowPath(row))
      setDetail(res.data)
    } catch (e) {
      window.alert(e.response?.data?.detail || e.message)
    }
  }

  const header = (
    <div className="sc-card-header bg-dark-surface flex items-center justify-between gap-3 flex-wrap">
      <div className="flex items-center gap-3 flex-wrap">
        <h3 className="sc-section-title">Secondo avviso email (fattura scaduta{data ? ` da ${data.days_after_due} giorni` : ''})</h3>
        {data && (
          <span className={`sc-badge ${data.enabled ? (MODE_STYLE[data.mode] || MODE_STYLE.prova) : OFF_STYLE}`} data-testid="followup-mode">
            {data.mode_label}
          </span>
        )}
      </div>
      {data && (
        <span className="text-xs text-txt-muted">
          Ogni giorno alle {data.schedule}, dopo i promemoria · {data.weekend
            ? 'weekend: nessun invio (le scadenze di sabato e domenica partono il lunedì)'
            : `scadenze dal ${fmtDate(data.window[0])} al ${fmtDate(data.window[1])}`}
        </span>
      )}
    </div>
  )

  if (loading) return <div className="sc-card overflow-hidden">{header}<p className="px-6 py-4 text-sm text-txt-muted">Caricamento…</p></div>
  if (error) return <div className="sc-card overflow-hidden">{header}<p className="px-6 py-4 text-sm text-accent-red">Errore: {error}</p></div>

  const preview = data.preview || []
  const toSend = preview.filter(p => p.will_process)
  const recent = data.recent || []
  const sync = data.sync || {}
  const canRun = data.enabled && !data.weekend && sync.ok && toSend.length > 0
  const plannedLabel = !data.enabled ? 'Partirebbe (ora spento)' : data.mode === 'prova' ? 'Da preparare' : 'Da inviare'

  return (
    <div className="sc-card overflow-hidden" data-testid="followup-panel">
      {header}

      {/* Regole e stato */}
      <div className="px-6 py-4 border-b border-dark-border text-sm space-y-2">
        <p className="text-txt-secondary">
          Se a {data.days_after_due} giorni dalla scadenza la fattura a cui è stato inviato il promemoria risulta ancora non pagata, il cliente riceve una seconda email, più diretta: la fattura è scaduta da {data.days_after_due} giorni, chiediamo il saldo. In allegato la copia della fattura e, se ha altre scadute, il riepilogo con il totale. Una email per fattura, una volta sola; mai nel weekend.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-1 text-xs">
          <p><span className="text-txt-muted">Mittente:</span> <span className="text-txt-primary">{data.sender || '—'}</span></p>
          <p><span className="text-txt-muted">In copia (invio reale):</span> <span className="text-txt-primary">{(data.cc_effective || []).join(', ') || '—'}{Object.keys(data.agent_emails || {}).length > 0 ? ' + commerciale del cliente' : ''}</span></p>
          <p><span className="text-txt-muted">Quando:</span> <span className="text-txt-primary">primo giorno lavorativo dal {data.days_after_due}° giorno di ritardo (recupero fino al {data.days_after_due + data.lookback_days}°)</span></p>
          <p><span className="text-txt-muted">Recupero manuale:</span> <span className="text-txt-primary">non cambia (non conta tra i solleciti della pratica)</span></p>
        </div>
        <p className={`text-xs ${sync.ok ? 'text-txt-secondary' : 'text-accent-red'}`} data-testid="followup-sync">
          {sync.ok
            ? `Controllo dei pagamenti: ultimo sync delle fatture riuscito ${sync.age_minutes} minuti fa.`
            : `Invio sospeso: ${sync.detail || 'sync delle fatture non verificabile'}. Parte solo con un sync delle fatture riuscito nelle ultime ${sync.max_age_hours} ore; le fatture restano in lista per i giorni di recupero.`}
        </p>
        {!data.enabled && (
          <p className="text-xs text-accent-amber">
            Spento: nessuna email parte. Qui sotto vedi cosa partirebbe al prossimo giro e con «Invia prova a…» ricevi l'email vera a un tuo indirizzo. Per accenderlo: <span className="font-mono">REMINDER_FOLLOWUP_ENABLED=true</span> su Render{data.missing_config.length > 1 ? <> (mancano anche: <span className="font-mono">{data.missing_config.filter(m => m !== 'REMINDER_FOLLOWUP_ENABLED').join(', ')}</span>)</> : null}.
          </p>
        )}
        {data.enabled && data.missing_config?.length > 0 && (
          <p className="text-xs text-accent-amber">
            Invio reale spento. Mancano su Render: <span className="font-mono">{data.missing_config.join(', ')}</span>.
          </p>
        )}
      </div>

      {/* Anteprima del prossimo giro */}
      <div className="px-6 py-4 border-b border-dark-border">
        <div className="flex items-center justify-between gap-3 flex-wrap mb-3">
          <div className="flex items-center gap-3 flex-wrap min-h-[2rem]">
            <p className="text-sm font-semibold text-txt-primary">
              {data.weekend ? 'Weekend: nessun avviso oggi' : `Prossimo giro: ${preview.length === 0 ? 'nessuna fattura' : `${preview.length} fattur${preview.length === 1 ? 'a' : 'e'}`}`}
              {preview.length > 0 && <span className="text-txt-muted font-normal"> · {toSend.length} da {data.enabled ? 'lavorare' : 'inviare se acceso'}</span>}
            </p>
            {runResult && (
              <span className={`text-xs ${runResult.error || runResult.blocked ? 'text-accent-red' : 'text-txt-secondary'}`} role="status">
                {runResult.error
                  ? `Errore: ${runResult.error}`
                  : runResult.info
                    ? runResult.info
                    : runResult.blocked
                      ? `Non eseguito: ${runResult.blocked_label}`
                      : `Esito: ${runResult.sent} inviate · ${runResult.prepared} preparate · ${runResult.skipped} saltate · ${runResult.failed} errori · ${runResult.already} non lavorate`}
              </span>
            )}
          </div>
          <button type="button" onClick={runNow} disabled={running || !canRun} className="sc-btn-primary text-xs disabled:opacity-50"
            title={!data.enabled ? 'Secondo avviso spento' : !sync.ok ? 'Sync delle fatture non aggiornato' : ''}>
            {running ? 'In corso…' : !data.enabled ? 'Spento' : RUN_LABEL[data.mode] || 'Esegui ora'}
          </button>
        </div>
        {preview.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs uppercase tracking-wider text-txt-label border-b border-dark-border">
                  <th className="text-left py-2 pr-3">Cliente</th>
                  <th className="text-left py-2 pr-3">Fattura</th>
                  <th className="text-left py-2 pr-3">Scaduta il</th>
                  <th className="text-right py-2 pr-3">Importo</th>
                  <th className="text-right py-2 pr-3">Altre scadute</th>
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
                        {(!p.cc || p.cc.length === 0) && p.would_cc && p.would_cc.length > 0 && <span className="block text-xs text-txt-muted">in reale cc: {p.would_cc.join(', ')}</span>}
                      </td>
                      <td className="py-2 pr-3 font-mono text-xs text-txt-secondary">{p.invoice_number}</td>
                      <td className="py-2 pr-3 text-xs text-txt-secondary whitespace-nowrap">
                        {fmtDate(p.due_date)} <span className="text-txt-muted">· {p.days_overdue} giorni</span>
                        {p.reminder_sent_at && <span className="block text-txt-muted">promemoria: {fmtDateTime(p.reminder_sent_at)}</span>}
                      </td>
                      <td className="py-2 pr-3 text-right text-txt-primary">{fmtEur(p.amount_due)}</td>
                      <td className="py-2 pr-3 text-right text-txt-secondary">{p.overdue_count > 0 ? `${p.overdue_count} · ${fmtEur(p.overdue_total)}` : '—'}</td>
                      <td className="py-2 pr-3">
                        {p.skip_reason
                          ? (
                            // Motivo sotto il badge (va a capo): la tabella resta nella larghezza della scheda.
                            <>
                              <span className="sc-badge whitespace-nowrap bg-accent-amber/15 text-accent-amber">Saltata</span>
                              <span className="block text-xs text-txt-muted mt-1 max-w-[15rem]">{p.skip_label}</span>
                            </>
                          )
                          : p.will_process
                            ? <span className={`sc-badge whitespace-nowrap ${PLANNED_STYLE}`}>{plannedLabel}{p.already ? ` (${p.why})` : ''}</span>
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
        <p className="text-sm font-semibold text-txt-primary mb-3">Ultimi secondi avvisi registrati</p>
        {recent.length === 0 ? (
          <p className="text-sm text-txt-muted">Nessun secondo avviso ancora registrato.</p>
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
