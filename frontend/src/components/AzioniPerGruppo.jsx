import React, { useState } from 'react'

// Azioni di recupero PER GRUPPO (Fase 4): il soggetto è la fattura; le fatture
// allo stesso stadio formano un gruppo che "ha seguito questo flusso e si trova
// qui". Ogni blocco: fatture del gruppo (con caselle), la sua storia (ogni
// azione con nota modificabile sulla riga), i pulsanti giusti per lo stadio.
// Tutte le azioni sulle fatture vivono QUI, in un posto solo.

const STAGE_STYLE = {
  none: 'bg-[rgba(148,163,184,0.15)] text-txt-secondary',
  first: 'bg-accent-teal/15 text-accent-teal',
  second: 'bg-accent-amber/15 text-accent-amber',
  lawyer: 'bg-accent-purple/15 text-accent-purple',
  in_incasso: 'bg-accent-teal/15 text-accent-teal',
  insoluto: 'bg-accent-red text-dark-bg',
  sospetto: 'bg-accent-amber/25 text-accent-amber',
}

const CHANNEL_LABELS = { whatsapp_copy: 'WhatsApp', whatsapp_link: 'WhatsApp', phone: 'Telefono', email: 'Email' }

export function StageBadge({ stage, label, className = '' }) {
  if (!stage) return <span className="text-txt-muted">—</span>
  return (
    <span className={`sc-badge ${STAGE_STYLE[stage] || STAGE_STYLE.none} ${className}`}>{label || stage}</span>
  )
}

function NoteInline({ action, onSave }) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(action.notes || '')
  const [busy, setBusy] = useState(false)
  if (editing) {
    return (
      <div className="mt-1 flex items-start gap-2">
        <textarea
          value={value}
          onChange={e => setValue(e.target.value)}
          rows={2}
          autoFocus
          className="flex-1 text-sm px-2 py-1 rounded bg-dark-bg border border-dark-border text-txt-primary"
          placeholder="Nota su questa azione…"
        />
        <button
          onClick={async () => { setBusy(true); try { await onSave(action.id, value); setEditing(false) } finally { setBusy(false) } }}
          disabled={busy}
          className="sc-btn-primary text-xs"
        >Salva</button>
        <button onClick={() => { setEditing(false); setValue(action.notes || '') }} className="sc-btn-secondary text-xs">Annulla</button>
      </div>
    )
  }
  return (
    <p
      className={`mt-0.5 text-sm cursor-pointer hover:underline ${action.notes ? 'text-txt-secondary' : 'text-txt-muted italic'}`}
      onClick={() => setEditing(true)}
      title="Clicca per scrivere o modificare la nota"
    >
      {action.notes || 'aggiungi una nota…'}
    </p>
  )
}

export default function AzioniPerGruppo({
  groups = [], clientActions = [], pendingActions = [],
  selectedInvoices, toggleInvoiceSelection, setSelectedInvoices,
  hasPhone, formatCurrency, formatDate,
  onCopy, onWhatsApp, onHandover, onAssegno, onInsoluto, onCancelAssegno, onAddNote, onEditNote,
}) {
  const [collapsed, setCollapsed] = useState(() => new Set(groups.filter(g => !g.tone).map(g => g.stage)))
  const [assegnoForm, setAssegnoForm] = useState(null)  // { stage, expected, note }
  const [noteForm, setNoteForm] = useState(null)        // { stage, text }
  const [copiedStage, setCopiedStage] = useState(null)
  const [busy, setBusy] = useState(null)

  const selIn = (g) => g.invoices.filter(i => selectedInvoices.has(i.id))
  const selIds = (g) => selIn(g).map(i => i.id)
  const selTotal = (g) => selIn(g).reduce((s, i) => s + (i.amount_due || 0), 0)
  const allSelected = (g) => g.invoices.length > 0 && g.invoices.every(i => selectedInvoices.has(i.id))
  const toggleAll = (g) => setSelectedInvoices(prev => {
    const n = new Set(prev)
    if (allSelected(g)) g.invoice_ids.forEach(id => n.delete(id)); else g.invoice_ids.forEach(id => n.add(id))
    return n
  })
  const toggleCollapse = (stage) => setCollapsed(prev => { const n = new Set(prev); if (n.has(stage)) n.delete(stage); else n.add(stage); return n })

  const run = async (key, fn) => { setBusy(key); try { await fn() } finally { setBusy(null) } }

  if (!groups.length && !pendingActions.length) {
    return <p className="text-sm text-txt-muted">Nessuna fattura scaduta: niente da recuperare.</p>
  }

  return (
    <div className="space-y-4">
      {pendingActions.length > 0 && (
        <div className="text-xs text-txt-muted">
          Prossime azioni pianificate:{' '}
          {pendingActions.map(p => `${p.label}${p.scheduled_date ? ' il ' + formatDate(p.scheduled_date) : ''}`).join(' · ')}
          <span className="ml-1">(le gestisci nella cronologia in fondo)</span>
        </div>
      )}

      {groups.map(g => {
        const isCollapsed = collapsed.has(g.stage)
        const n = selIds(g).length
        return (
          <div key={g.stage} className={`rounded-xl border ${g.stage === 'insoluto' ? 'border-accent-red/60' : 'border-dark-border'} bg-dark-surface/40`}>
            {/* Intestazione del gruppo */}
            <div className="px-4 py-3 flex items-center justify-between gap-3 flex-wrap">
              <div className="flex items-center gap-3 flex-wrap">
                <button onClick={() => toggleCollapse(g.stage)} className="text-txt-muted hover:text-txt-primary text-sm" title={isCollapsed ? 'Apri' : 'Chiudi'}>
                  {isCollapsed ? '▸' : '▾'}
                </button>
                <StageBadge stage={g.stage} label={g.label} />
                <span className="text-sm text-txt-secondary">
                  {g.invoices.length} fattur{g.invoices.length === 1 ? 'a' : 'e'} · <span className="font-semibold text-txt-primary">{formatCurrency(g.total)}</span>
                </span>
              </div>
              <label className="text-xs text-txt-muted flex items-center gap-1 cursor-pointer">
                <input type="checkbox" checked={allSelected(g)} onChange={() => toggleAll(g)} className="rounded border-dark-border bg-dark-bg" />
                tutte
              </label>
            </div>

            {!isCollapsed && (
              <div className="px-4 pb-4 space-y-3">
                {/* Fatture del gruppo */}
                <div className="flex flex-wrap gap-2">
                  {g.invoices.map(i => (
                    <label key={i.id} className={`inline-flex items-center gap-1.5 px-2 py-1 rounded border text-xs cursor-pointer ${selectedInvoices.has(i.id) ? 'border-accent-teal/50 bg-accent-teal/10 text-txt-primary' : 'border-dark-border text-txt-secondary'}`}>
                      <input type="checkbox" checked={selectedInvoices.has(i.id)} onChange={() => toggleInvoiceSelection(i.id)} className="rounded border-dark-border bg-dark-bg" />
                      <span className="font-medium">{i.invoice_number}</span>
                      <span className="text-txt-muted">{formatCurrency(i.amount_due)} · +{i.days_overdue}gg</span>
                    </label>
                  ))}
                </div>

                {/* Storia del gruppo */}
                {g.actions.length === 0 ? (
                  <p className="text-xs text-txt-muted italic">Nessuna azione ancora su queste fatture.</p>
                ) : (
                  <div className="border-l-2 border-dark-border pl-4 space-y-2">
                    {g.actions.map(a => (
                      <div key={a.id} className="relative">
                        <div className={`absolute -left-[21px] top-1.5 w-3 h-3 rounded-full border-2 border-dark-card ${a.completed_at ? 'bg-accent-green' : 'bg-txt-muted'}`}></div>
                        <div className="flex items-center gap-2 flex-wrap text-sm">
                          <span className="font-medium text-txt-primary">{a.label}</span>
                          <span className="text-xs text-txt-muted">
                            {a.completed_at ? formatDate(a.completed_at) : a.created_at ? formatDate(a.created_at) : ''}
                          </span>
                          {a.channel && (
                            <span className="text-xs px-1.5 py-0.5 rounded bg-accent-green/15 text-accent-green">{CHANNEL_LABELS[a.channel] || a.channel}</span>
                          )}
                          {a.outcome && a.outcome !== 'contacted' && (
                            <span className="text-xs px-1.5 py-0.5 rounded bg-dark-surface text-txt-secondary">{a.outcome}</span>
                          )}
                          {a.legacy_all ? (
                            <span className="text-xs text-txt-muted" title="Azione storica senza fatture citate: copriva tutte le scadute all'epoca">tutte le scadute all'epoca</span>
                          ) : a.cited_total != null && a.cited_in_group != null && a.cited_in_group < a.cited_total ? (
                            <span className="text-xs text-txt-muted" title="Questa azione citava anche fatture oggi in un altro stadio">{a.cited_in_group} di {a.cited_total} fatture citate</span>
                          ) : null}
                        </div>
                        <NoteInline action={a} onSave={onEditNote} />
                      </div>
                    ))}
                  </div>
                )}

                {/* Form assegno (gruppo) */}
                {assegnoForm?.stage === g.stage && (
                  <div className="flex items-end gap-3 flex-wrap p-3 rounded-lg bg-dark-bg/60 border border-dark-border">
                    <div className="text-xs text-txt-secondary">
                      Pagate con <strong className="text-txt-primary">assegno</strong> da incassare — {n} fattur{n === 1 ? 'a' : 'e'} selezionat{n === 1 ? 'a' : 'e'} · {formatCurrency(selTotal(g))}
                    </div>
                    <label className="text-xs text-txt-muted">Incasso previsto
                      <input type="date" value={assegnoForm.expected} onChange={e => setAssegnoForm({ ...assegnoForm, expected: e.target.value })}
                        className="ml-2 px-2 py-1 rounded bg-dark-bg border border-dark-border text-sm text-txt-primary" />
                    </label>
                    <label className="text-xs text-txt-muted flex-1 min-w-[16rem]">Nota
                      <input type="text" value={assegnoForm.note} placeholder="es. assegno n. 123, verrà incassato il …"
                        onChange={e => setAssegnoForm({ ...assegnoForm, note: e.target.value })}
                        className="ml-2 w-full max-w-md px-2 py-1 rounded bg-dark-bg border border-dark-border text-sm text-txt-primary" />
                    </label>
                    <button disabled={n === 0 || busy === 'assegno'} onClick={() => run('assegno', async () => { await onAssegno(selIds(g), assegnoForm.expected || null, assegnoForm.note || null); setAssegnoForm(null) })} className="sc-btn-primary text-xs">Registra assegno</button>
                    <button onClick={() => setAssegnoForm(null)} className="sc-btn-secondary text-xs">Chiudi</button>
                  </div>
                )}

                {/* Form nota di gruppo */}
                {noteForm?.stage === g.stage && (
                  <div className="flex items-start gap-2 p-3 rounded-lg bg-dark-bg/60 border border-dark-border">
                    <textarea value={noteForm.text} onChange={e => setNoteForm({ ...noteForm, text: e.target.value })} rows={2} autoFocus
                      placeholder={`Nota su ${n} fattur${n === 1 ? 'a' : 'e'} selezionat${n === 1 ? 'a' : 'e'} (viaggia con le fatture fino al dossier)`}
                      className="flex-1 text-sm px-2 py-1 rounded bg-dark-bg border border-dark-border text-txt-primary" />
                    <button disabled={!noteForm.text.trim() || n === 0 || busy === 'note'} onClick={() => run('note', async () => { await onAddNote(selIds(g), noteForm.text.trim()); setNoteForm(null) })} className="sc-btn-primary text-xs">Salva nota</button>
                    <button onClick={() => setNoteForm(null)} className="sc-btn-secondary text-xs">Annulla</button>
                  </div>
                )}

                {/* Pulsanti per stadio — agiscono sulle fatture SELEZIONATE del gruppo */}
                <div className="flex items-center gap-2 flex-wrap pt-1">
                  <span className="text-xs text-txt-muted mr-1">{n} selezionat{n === 1 ? 'a' : 'e'} · {formatCurrency(selTotal(g))}</span>
                  {g.tone && (
                    <>
                      <button
                        disabled={n === 0}
                        onClick={() => run('copy', async () => { if (await onCopy(g, selIn(g))) { setCopiedStage(g.stage); setTimeout(() => setCopiedStage(null), 2000) } })}
                        className={`sc-btn-secondary text-xs font-bold ${copiedStage === g.stage ? 'border-accent-green text-accent-green' : ''}`}
                        title={g.tone === 'first' ? 'Messaggio cordiale (1° sollecito) per le fatture selezionate' : 'Messaggio perentorio (sollecito successivo) per le fatture selezionate'}
                      >
                        {copiedStage === g.stage ? 'Copiato!' : `Copia messaggio ${g.tone === 'first' ? '1°' : '2°'}`}
                      </button>
                      {hasPhone && (
                        <button disabled={n === 0} onClick={() => run('wa', async () => onWhatsApp(g, selIn(g)))} className="px-3 py-1.5 bg-accent-green text-dark-bg rounded-lg text-xs font-bold hover:brightness-110 disabled:opacity-50">WhatsApp</button>
                      )}
                      <button disabled={n === 0} onClick={() => run('ho', async () => onHandover(selIds(g)))} className="px-3 py-1.5 bg-accent-red/15 text-accent-red rounded-lg text-xs font-bold hover:bg-accent-red/25 disabled:opacity-50" title="Consegna le fatture selezionate all'avvocato">Consegna all'avvocato</button>
                    </>
                  )}
                  {(g.tone || g.stage === 'lawyer') && g.stage !== 'in_incasso' && (
                    <button disabled={n === 0} onClick={() => setAssegnoForm(assegnoForm?.stage === g.stage ? null : { stage: g.stage, expected: '', note: '' })} className="px-3 py-1.5 bg-accent-teal/15 text-accent-teal rounded-lg text-xs font-medium hover:bg-accent-teal/25 disabled:opacity-50" title="Pagate con assegno da incassare">
                      {(g.stage === 'insoluto' || g.stage === 'sospetto') ? 'Nuovo assegno' : 'Assegno'}
                    </button>
                  )}
                  {(g.stage === 'in_incasso' || g.stage === 'sospetto') && (
                    <button disabled={n === 0} onClick={() => run('ins', async () => { for (const i of selIn(g)) await onInsoluto(i) })} className="px-3 py-1.5 bg-accent-red/15 text-accent-red rounded-lg text-xs font-bold hover:bg-accent-red/25 disabled:opacity-50" title="L'assegno è tornato indietro: la fattura torna scaduta SUBITO">Insoluto</button>
                  )}
                  {g.stage === 'in_incasso' && (
                    <button disabled={n === 0} onClick={() => run('canc', async () => { for (const i of selIn(g)) await onCancelAssegno(i) })} className="px-3 py-1.5 bg-dark-surface text-txt-muted rounded-lg text-xs hover:text-txt-primary disabled:opacity-50" title="Annulla la registrazione (solo se fatta per errore)">Annulla assegno</button>
                  )}
                  {g.stage === 'sospetto' && (
                    <button disabled={n === 0} onClick={() => run('canc', async () => { for (const i of selIn(g)) await onCancelAssegno(i) })} className="px-3 py-1.5 bg-dark-surface text-txt-muted rounded-lg text-xs hover:text-txt-primary disabled:opacity-50" title="La riapertura su FatturaPro non è un insoluto (es. nota di credito)">Non è insoluto</button>
                  )}
                  <button disabled={n === 0} onClick={() => setNoteForm(noteForm?.stage === g.stage ? null : { stage: g.stage, text: '' })} className="sc-btn-secondary text-xs">+ Nota</button>
                </div>
              </div>
            )}
          </div>
        )
      })}

      {clientActions.length > 0 && (
        <div className="text-xs text-txt-muted">
          Azioni sul cliente (non legate a fatture): {clientActions.map(a => `${a.label}${a.completed_at ? ' ' + formatDate(a.completed_at) : ''}`).join(' · ')}
        </div>
      )}
    </div>
  )
}
