import React, { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import client from '../api/client'

const ACTION_LABELS = {
  first_contact: 'I Contatto',
  second_contact: 'II Contatto',
  lawyer: 'Avvocato',
  archive: 'Archiviato',
  wait: 'In Attesa',
  idle: 'Da Gestire',
  waiting: 'In Attesa',
}

const STATUS_BADGE = {
  first_contact: 'badge-open',
  second_contact: 'badge-contacted',
  lawyer: 'badge-disputed',
  archived: 'bg-[rgba(148,163,184,0.15)] text-txt-muted',
  waiting: 'badge-promised',
}

const OUTCOME_LABELS = {
  contacted: 'Contattato',
  promised: 'Promessa Pagamento',
  partial_payment: 'Pag. Parziale',
  paid: 'Pagato',
  unreachable: 'Irraggiungibile',
  disputed: 'Contestazione',
  no_answer: 'Non Risponde',
}

const WEEKDAYS = ['Lun', 'Mar', 'Mer', 'Gio', 'Ven', 'Sab', 'Dom']
const MONTH_NAMES = ['Gennaio', 'Febbraio', 'Marzo', 'Aprile', 'Maggio', 'Giugno',
  'Luglio', 'Agosto', 'Settembre', 'Ottobre', 'Novembre', 'Dicembre']

// Pipeline stage config – dark theme
const PIPELINE_STAGES = [
  { key: 'idle', label: 'Da Gestire', color: 'bg-slate-500', textColor: 'text-txt-muted' },
  { key: 'first_contact', label: 'I Contatto', color: 'bg-accent-blue', textColor: 'text-accent-blue' },
  { key: 'second_contact', label: 'II Contatto', color: 'bg-accent-amber', textColor: 'text-accent-amber' },
  { key: 'lawyer', label: 'Avvocato', color: 'bg-accent-red', textColor: 'text-accent-red' },
  { key: 'waiting', label: 'In Attesa', color: 'bg-accent-purple', textColor: 'text-accent-purple' },
  { key: 'resolved', label: 'Incassato', color: 'bg-accent-green', textColor: 'text-accent-green' },
]

export default function Attivita() {
  const navigate = useNavigate()
  const now = new Date()

  // Calendar state
  const [calYear, setCalYear] = useState(now.getFullYear())
  const [calMonth, setCalMonth] = useState(now.getMonth() + 1)
  const [calData, setCalData] = useState(null)
  const [calLoading, setCalLoading] = useState(true)
  const [selectedDay, setSelectedDay] = useState(null)

  // Attivita data
  const [attivita, setAttivita] = useState(null)
  const [attivitaLoading, setAttivitaLoading] = useState(true)

  // Pipeline data
  const [pipeline, setPipeline] = useState(null)

  // Tab state for bottom section
  const [activeTab, setActiveTab] = useState('contacted')

  // Overlay modal state
  const [overlayType, setOverlayType] = useState(null)

  const fetchCalendar = useCallback(async (y, m) => {
    try {
      setCalLoading(true)
      const response = await client.get('/dashboard/calendar', { params: { year: y, month: m } })
      setCalData(response.data)
    } catch (err) {
      console.error('Error fetching calendar:', err)
    } finally {
      setCalLoading(false)
    }
  }, [])

  const fetchAttivita = useCallback(async () => {
    try {
      setAttivitaLoading(true)
      const response = await client.get('/dashboard/attivita')
      setAttivita(response.data)
    } catch (err) {
      console.error('Error fetching attivita:', err)
    } finally {
      setAttivitaLoading(false)
    }
  }, [])

  const fetchPipeline = useCallback(async () => {
    try {
      const response = await client.get('/dashboard/pipeline')
      setPipeline(response.data)
    } catch (err) {
      console.error('Error fetching pipeline:', err)
    }
  }, [])

  useEffect(() => {
    fetchAttivita()
    fetchPipeline()
  }, [fetchAttivita, fetchPipeline])

  useEffect(() => {
    fetchCalendar(calYear, calMonth)
  }, [calYear, calMonth, fetchCalendar])

  const formatCurrency = (value) =>
    new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR', minimumFractionDigits: 0, maximumFractionDigits: 0 }).format(value)

  const formatDate = (dateStr) => {
    if (!dateStr) return '-'
    const d = dateStr.length === 10 ? new Date(dateStr + 'T00:00:00') : new Date(dateStr)
    return d.toLocaleDateString('it-IT')
  }

  const prevMonth = () => {
    if (calMonth === 1) { setCalMonth(12); setCalYear(calYear - 1) }
    else setCalMonth(calMonth - 1)
    setSelectedDay(null)
  }
  const nextMonth = () => {
    if (calMonth === 12) { setCalMonth(1); setCalYear(calYear + 1) }
    else setCalMonth(calMonth + 1)
    setSelectedDay(null)
  }

  const todayStr = now.toISOString().split('T')[0]

  // Build calendar grid
  const buildCalendarDays = () => {
    if (!calData) return []
    const start = new Date(calData.start)
    const end = new Date(calData.end)
    const days = []
    const d = new Date(start)
    while (d <= end) {
      const iso = d.toISOString().split('T')[0]
      days.push({
        date: iso,
        day: d.getDate(),
        isCurrentMonth: d.getMonth() + 1 === calMonth,
        isToday: iso === todayStr,
        actions: calData.days[iso] || [],
      })
      d.setDate(d.getDate() + 1)
    }
    return days
  }

  const calendarDays = buildCalendarDays()
  const selectedDayActions = selectedDay && calData?.days?.[selectedDay] ? calData.days[selectedDay] : null

  if (attivitaLoading && calLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-center">
          <svg className="animate-spin w-12 h-12 text-accent-teal mx-auto mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          <p className="text-txt-muted">Caricamento attività...</p>
        </div>
      </div>
    )
  }

  const contacted = attivita?.contacted || []
  const incassati = attivita?.incassati || []
  const summary = attivita?.summary || {}
  const stages = pipeline?.stages || {}

  // Calculate pipeline total for progress bar
  const pipelineTotal = PIPELINE_STAGES.reduce((sum, s) => sum + (stages[s.key]?.count || 0), 0)

  // Suggest next action based on contacted accounts
  const suggestNextAction = (c) => {
    if (c.recovery_status === 'first_contact') return 'Invia II Contatto'
    if (c.recovery_status === 'second_contact') return 'Valuta Avvocato'
    if (c.recovery_status === 'lawyer') return 'Verifica risposta legale'
    return 'Contatta'
  }

  return (
    <div className="space-y-6">

      {/* ── PIPELINE FUNNEL ── */}
      <div className="sc-card p-6">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h2 className="text-base font-bold text-txt-primary">Pipeline Recupero</h2>
            <p className="text-xs text-txt-muted mt-0.5">Stato di avanzamento di tutti i clienti con debiti scaduti</p>
          </div>
          {pipeline?.total_with_overdue > 0 && (
            <span className="text-sm text-txt-muted">
              {pipeline.total_with_overdue} clienti con scaduto
            </span>
          )}
        </div>

        {/* Funnel bars */}
        <div className="space-y-2">
          {PIPELINE_STAGES.map((stage) => {
            const data = stages[stage.key] || { count: 0, amount: 0 }
            const pct = pipelineTotal > 0 ? Math.max((data.count / pipelineTotal) * 100, data.count > 0 ? 4 : 0) : 0
            return (
              <div key={stage.key} className="flex items-center gap-3">
                <span className={`text-xs font-medium w-24 text-right ${stage.textColor}`}>{stage.label}</span>
                <div className="flex-1 bg-dark-surface rounded-full h-7 relative overflow-hidden">
                  <div
                    className={`${stage.color} h-full rounded-full transition-all duration-700 flex items-center justify-end pr-2`}
                    style={{ width: `${pct}%`, minWidth: data.count > 0 ? '40px' : '0' }}
                  >
                    {data.count > 0 && (
                      <span className="text-xs text-dark-bg font-bold">{data.count}</span>
                    )}
                  </div>
                </div>
                <span className="text-xs text-txt-muted w-24">
                  {data.amount > 0 ? formatCurrency(data.amount) : '-'}
                </span>
              </div>
            )
          })}
        </div>
      </div>

      {/* ── SUMMARY CARDS (clickable) ── */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <button onClick={() => setOverlayType('contacted')} className="sc-card p-4 text-left hover:border-accent-blue/50 transition-all group">
          <p className="sc-kpi-label">Account Contattati</p>
          <p className="text-2xl font-bold text-accent-blue mt-1">{summary.total_contacted || 0}</p>
          <p className="text-xs text-txt-muted mt-1 group-hover:text-accent-blue">clienti in lavorazione</p>
        </button>
        <button onClick={() => setOverlayType('incassati')} className="sc-card p-4 text-left border-accent-green/20 hover:border-accent-green/50 transition-all group">
          <p className="sc-kpi-label text-accent-green">Incassati</p>
          <p className="text-2xl font-bold text-accent-green mt-1">{summary.fully_resolved || 0}</p>
          <p className="text-xs text-txt-muted mt-1 group-hover:text-accent-green">debiti risolti</p>
        </button>
        <button onClick={() => setOverlayType('recovered')} className="sc-card p-4 text-left border-accent-green/20 hover:border-accent-green/50 transition-all group">
          <p className="sc-kpi-label text-accent-green">Totale Recuperato</p>
          <p className="text-2xl font-bold text-accent-green mt-1">{formatCurrency(summary.total_recovered || 0)}</p>
          <p className="text-xs text-txt-muted mt-1 group-hover:text-accent-green">importo incassato</p>
        </button>
        <button onClick={() => setOverlayType('overdue')} className="sc-card p-4 text-left hover:border-accent-red/50 transition-all group">
          <p className="sc-kpi-label">Azioni Scadute</p>
          <p className="text-2xl font-bold text-accent-red mt-1">{calData?.overdue_count || 0}</p>
          <p className="text-xs text-txt-muted mt-1 group-hover:text-accent-red">da completare</p>
        </button>
      </div>

      {/* ── OVERLAY MODAL ── */}
      {overlayType && (
        <div className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4" onClick={() => setOverlayType(null)}>
          <div className="bg-dark-card rounded-xl shadow-2xl border border-dark-border max-w-lg w-full max-h-[80vh] overflow-hidden" onClick={(e) => e.stopPropagation()}>
            {/* Header */}
            <div className={`px-6 py-4 flex items-center justify-between border-b border-dark-border ${
              overlayType === 'contacted' ? 'bg-accent-blue/5' :
              overlayType === 'overdue' ? 'bg-accent-red/5' :
              'bg-accent-green/5'
            }`}>
              <h3 className="text-base font-bold text-txt-primary">
                {overlayType === 'contacted' && 'Account Contattati'}
                {overlayType === 'incassati' && 'Incassati — Dettaglio'}
                {overlayType === 'recovered' && 'Totale Recuperato — Dettaglio'}
                {overlayType === 'overdue' && 'Azioni Scadute — Dettaglio'}
              </h3>
              <button onClick={() => setOverlayType(null)} className="text-txt-muted hover:text-txt-primary text-xl font-bold leading-none">&times;</button>
            </div>

            {/* Body */}
            <div className="px-6 py-4 overflow-y-auto max-h-[65vh]">

              {/* CONTATTATI: breakdown by status */}
              {overlayType === 'contacted' && (() => {
                const byStatus = {}
                contacted.forEach(c => {
                  const s = c.recovery_status || 'unknown'
                  if (!byStatus[s]) byStatus[s] = { count: 0, amount: 0, customers: [] }
                  byStatus[s].count++
                  byStatus[s].amount += c.total_overdue || 0
                  byStatus[s].customers.push(c)
                })
                const statusOrder = ['first_contact', 'second_contact', 'lawyer', 'waiting']
                return (
                  <div className="space-y-4">
                    <p className="text-sm text-txt-muted">{contacted.length} clienti in lavorazione attiva</p>
                    {statusOrder.filter(s => byStatus[s]).map(s => (
                      <div key={s} className="border border-dark-border rounded-lg overflow-hidden">
                        <div className={`px-4 py-2 flex items-center justify-between ${
                          s === 'first_contact' ? 'bg-accent-blue/5' :
                          s === 'second_contact' ? 'bg-accent-amber/5' :
                          s === 'lawyer' ? 'bg-accent-red/5' : 'bg-accent-purple/5'
                        }`}>
                          <span className={`text-sm font-bold ${
                            s === 'first_contact' ? 'text-accent-blue' :
                            s === 'second_contact' ? 'text-accent-amber' :
                            s === 'lawyer' ? 'text-accent-red' : 'text-accent-purple'
                          }`}>{ACTION_LABELS[s] || s}</span>
                          <span className="text-sm text-txt-secondary">{byStatus[s].count} clienti — {formatCurrency(byStatus[s].amount)} scaduto</span>
                        </div>
                        <div className="divide-y divide-dark-border">
                          {byStatus[s].customers.map(c => (
                            <div key={c.id} onClick={() => { setOverlayType(null); navigate(`/customers/${c.id}`) }}
                              className="px-4 py-2 flex items-center justify-between hover:bg-dark-cardHover cursor-pointer transition-colors">
                              <div>
                                <p className="text-sm font-medium text-txt-primary">{c.ragione_sociale}</p>
                                <p className="text-xs text-txt-muted">Ultimo: {formatDate(c.last_contact_date)} {c.last_outcome ? `— ${OUTCOME_LABELS[c.last_outcome] || c.last_outcome}` : ''}</p>
                              </div>
                              <p className="text-sm font-bold text-accent-red">{formatCurrency(c.total_overdue)}</p>
                            </div>
                          ))}
                        </div>
                      </div>
                    ))}
                    {contacted.length === 0 && <p className="text-sm text-txt-muted text-center py-4">Nessun account contattato.</p>}
                  </div>
                )
              })()}

              {/* INCASSATI */}
              {overlayType === 'incassati' && (
                <div className="space-y-3">
                  <p className="text-sm text-txt-muted">
                    {incassati.filter(i => i.fully_resolved).length} completamente risolti su {incassati.length} con pagamenti
                  </p>
                  {incassati.length === 0 && <p className="text-sm text-txt-muted text-center py-4">Nessun incasso da azioni di recupero.</p>}
                  {incassati.map(inc => (
                    <div key={inc.id} onClick={() => { setOverlayType(null); navigate(`/customers/${inc.id}`) }}
                      className={`rounded-lg px-4 py-3 cursor-pointer transition-colors border ${
                        inc.fully_resolved ? 'bg-accent-green/5 border-accent-green/30 hover:bg-accent-green/10' : 'bg-accent-amber/5 border-accent-amber/30 hover:bg-accent-amber/10'
                      }`}>
                      <div className="flex items-center justify-between">
                        <div>
                          <p className="text-sm font-medium text-txt-primary">{inc.ragione_sociale}</p>
                          <p className="text-xs text-txt-muted mt-0.5">
                            {inc.paid_count} fatture pagate — ultimo: {formatDate(inc.last_payment)}
                          </p>
                        </div>
                        <div className="text-right">
                          <p className="text-sm font-bold text-accent-green">{formatCurrency(inc.total_paid)}</p>
                          <span className={`text-xs font-bold px-2 py-0.5 rounded ${
                            inc.fully_resolved ? 'bg-accent-green/20 text-accent-green' : 'bg-accent-amber/20 text-accent-amber'
                          }`}>{inc.fully_resolved ? 'RISOLTO' : 'PARZIALE'}</span>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {/* TOTALE RECUPERATO */}
              {overlayType === 'recovered' && (
                <div className="space-y-3">
                  <div className="flex items-center justify-between mb-2">
                    <p className="text-sm text-txt-muted">{incassati.length} clienti con pagamenti da recupero</p>
                    <p className="text-lg font-bold text-accent-green">{formatCurrency(summary.total_recovered || 0)}</p>
                  </div>
                  {incassati.length === 0 && <p className="text-sm text-txt-muted text-center py-4">Nessun importo recuperato.</p>}
                  {incassati.map(inc => {
                    const pct = summary.total_recovered > 0 ? Math.max((inc.total_paid / summary.total_recovered) * 100, 2) : 0
                    return (
                      <div key={inc.id} onClick={() => { setOverlayType(null); navigate(`/customers/${inc.id}`) }}
                        className="cursor-pointer hover:bg-dark-cardHover rounded-lg p-3 border border-dark-border transition-colors">
                        <div className="flex items-center justify-between mb-1.5">
                          <p className="text-sm font-medium text-txt-primary">{inc.ragione_sociale}</p>
                          <p className="text-sm font-bold text-accent-green">{formatCurrency(inc.total_paid)}</p>
                        </div>
                        <div className="w-full bg-dark-surface rounded-full h-2.5">
                          <div className="bg-accent-green h-2.5 rounded-full transition-all" style={{ width: `${pct}%` }} />
                        </div>
                        <div className="flex items-center justify-between mt-1">
                          <p className="text-xs text-txt-muted">{inc.paid_count} fatture — ultimo {formatDate(inc.last_payment)}</p>
                          <p className="text-xs text-txt-muted">{pct.toFixed(1)}%</p>
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}

              {/* AZIONI SCADUTE */}
              {overlayType === 'overdue' && (() => {
                const overdueActions = []
                const todayStr2 = now.toISOString().split('T')[0]
                if (calData?.days) {
                  Object.entries(calData.days).forEach(([date, actions]) => {
                    if (date < todayStr2) {
                      actions.filter(a => !a.completed_at).forEach(a => overdueActions.push({ ...a, date }))
                    }
                  })
                }
                overdueActions.sort((a, b) => a.date.localeCompare(b.date))
                return (
                  <div className="space-y-2">
                    <p className="text-sm text-txt-muted">{overdueActions.length} azioni scadute da completare o ripianificare</p>
                    {overdueActions.length === 0 && <p className="text-sm text-accent-green text-center py-4">Nessuna azione scaduta! Tutto in ordine.</p>}
                    {overdueActions.map((a, idx) => (
                      <div key={a.id || idx} onClick={() => { setOverlayType(null); navigate(`/customers/${a.customer_id}`) }}
                        className="flex items-center justify-between rounded-lg px-4 py-3 cursor-pointer bg-accent-red/5 border border-accent-red/20 hover:bg-accent-red/10 transition-colors">
                        <div className="flex items-center gap-3 min-w-0">
                          <span className={`${STATUS_BADGE[a.action_type] || 'bg-[rgba(148,163,184,0.15)] text-txt-muted'} sc-badge shrink-0`}>
                            {ACTION_LABELS[a.action_type] || a.action_type}
                          </span>
                          <div className="min-w-0">
                            <p className="text-sm font-medium text-txt-primary truncate">{a.customer_name}</p>
                            <p className="text-xs text-accent-red">Scaduta il {formatDate(a.date)}</p>
                          </div>
                        </div>
                        {a.total_overdue > 0 && (
                          <p className="text-sm font-bold text-accent-red shrink-0">{formatCurrency(a.total_overdue)}</p>
                        )}
                      </div>
                    ))}
                  </div>
                )
              })()}

            </div>
          </div>
        </div>
      )}

      {/* ── CALENDAR ── */}
      <div className="sc-card p-6">
        <div className="flex items-center justify-between mb-1">
          <div>
            <h2 className="text-base font-bold text-txt-primary">Calendario Attività</h2>
            <p className="text-xs text-txt-muted mt-0.5">
              Clicca su un giorno con azioni per vederne i dettagli. Le azioni scadute sono in rosso.
            </p>
          </div>
          <div className="flex items-center gap-3">
            <button onClick={prevMonth} className="px-3 py-1.5 hover:bg-dark-cardHover rounded text-txt-secondary font-bold transition-colors">&lt;</button>
            <span className="text-base font-semibold text-txt-primary min-w-[160px] text-center">
              {MONTH_NAMES[calMonth - 1]} {calYear}
            </span>
            <button onClick={nextMonth} className="px-3 py-1.5 hover:bg-dark-cardHover rounded text-txt-secondary font-bold transition-colors">&gt;</button>
          </div>
        </div>

        {calLoading ? (
          <p className="text-txt-muted text-center py-8">Caricamento calendario...</p>
        ) : (
          <>
            {/* Weekday headers */}
            <div className="grid grid-cols-7 mb-2 mt-4">
              {WEEKDAYS.map(d => (
                <div key={d} className="text-center text-sm font-semibold text-txt-muted py-2">{d}</div>
              ))}
            </div>

            {/* Calendar grid */}
            <div className="grid grid-cols-7 gap-1">
              {calendarDays.map((day) => {
                const allActions = day.actions
                const pendingActions = allActions.filter(a => !a.completed_at)
                const completedActions = allActions.filter(a => a.completed_at)
                const hasActions = allActions.length > 0
                const isSelected = selectedDay === day.date
                const isPast = day.date < todayStr
                const allDone = hasActions && pendingActions.length === 0
                return (
                  <button
                    key={day.date}
                    onClick={() => hasActions ? setSelectedDay(isSelected ? null : day.date) : null}
                    className={`relative p-2 text-center rounded-lg transition-colors min-h-[70px] flex flex-col items-center justify-start
                      ${!day.isCurrentMonth ? 'text-txt-muted/40 bg-dark-surface/30' : 'text-txt-secondary'}
                      ${day.isToday ? 'ring-2 ring-accent-teal font-bold bg-accent-teal/10' : ''}
                      ${isSelected ? 'bg-accent-blue/15 ring-2 ring-accent-blue' : ''}
                      ${allDone && !isSelected && !day.isToday ? 'bg-accent-green/5' : ''}
                      ${hasActions && !allDone && !isSelected && !day.isToday ? (isPast ? 'bg-accent-red/5 hover:bg-accent-red/10' : 'bg-accent-amber/5 hover:bg-accent-amber/10') : ''}
                      ${hasActions ? 'cursor-pointer' : 'cursor-default'}
                      ${!hasActions && day.isCurrentMonth && !day.isToday ? 'hover:bg-dark-cardHover' : ''}
                    `}
                  >
                    <span className="text-sm">{day.day}</span>
                    {hasActions && (
                      <div className="flex flex-wrap justify-center mt-1 gap-0.5">
                        {allDone ? (
                          <span className="text-xs text-accent-green font-bold">&#10003; {completedActions.length}</span>
                        ) : pendingActions.length <= 3 ? (
                          pendingActions.map((_, i) => (
                            <div key={i} className={`w-2 h-2 rounded-full ${isPast ? 'bg-accent-red' : 'bg-accent-blue'}`} />
                          ))
                        ) : (
                          <span className={`text-xs font-bold ${isPast ? 'text-accent-red' : 'text-accent-blue'}`}>
                            {pendingActions.length}
                          </span>
                        )}
                      </div>
                    )}
                  </button>
                )
              })}
            </div>

            {/* Legend */}
            <div className="mt-4 flex items-center gap-6 text-xs text-txt-muted">
              <div className="flex items-center gap-1.5">
                <div className="w-2.5 h-2.5 rounded-full bg-accent-red" /> Scadute
              </div>
              <div className="flex items-center gap-1.5">
                <div className="w-2.5 h-2.5 rounded-full bg-accent-blue" /> Pianificate
              </div>
              <div className="flex items-center gap-1.5">
                <span className="text-accent-green font-bold">&#10003;</span> Completate
              </div>
              <div className="flex items-center gap-1.5">
                <div className="w-2.5 h-2.5 ring-2 ring-accent-teal rounded-full" /> Oggi
              </div>
            </div>

            {/* Selected day detail */}
            {selectedDayActions && (
              <div className="mt-4 border-t border-dark-border pt-4">
                <div className="flex items-center justify-between mb-3">
                  <p className="text-sm font-semibold text-txt-primary">
                    {new Date(selectedDay + 'T00:00:00').toLocaleDateString('it-IT', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })}
                    <span className="text-txt-muted ml-2 font-normal">({selectedDayActions.length} azioni)</span>
                  </p>
                  {selectedDay < todayStr && selectedDayActions.some(a => !a.completed_at) && (
                    <span className="text-xs bg-accent-red/15 text-accent-red px-2 py-1 rounded font-medium">
                      Azioni scadute — vanno completate o ripianificate
                    </span>
                  )}
                </div>
                <div className="space-y-2 max-h-[300px] overflow-y-auto">
                  {selectedDayActions.map((a) => (
                    <div
                      key={a.id}
                      onClick={() => navigate(`/customers/${a.customer_id}`)}
                      className={`flex items-center justify-between rounded-lg px-4 py-3 cursor-pointer transition-colors border ${
                        a.completed_at
                          ? 'bg-accent-green/5 border-accent-green/20 opacity-80'
                          : 'bg-dark-card border-dark-border hover:bg-dark-cardHover hover:border-accent-teal/30'
                      }`}
                    >
                      <div className="flex items-center gap-3 min-w-0">
                        <span className={`${STATUS_BADGE[a.action_type] || STATUS_BADGE.first_contact} sc-badge shrink-0`}>
                          {ACTION_LABELS[a.action_type] || a.action_type}
                        </span>
                        <span className={`text-sm font-medium truncate ${a.completed_at ? 'text-txt-muted line-through' : 'text-txt-primary'}`}>
                          {a.customer_name}
                        </span>
                        {a.outcome && (
                          <span className="text-xs text-txt-muted">— {OUTCOME_LABELS[a.outcome] || a.outcome}</span>
                        )}
                      </div>
                      <div className="flex items-center gap-3 shrink-0">
                        {a.total_overdue > 0 && !a.completed_at && (
                          <span className="text-sm font-bold text-accent-red">{formatCurrency(a.total_overdue)}</span>
                        )}
                        {a.phone && !a.completed_at && (
                          <a href={`https://wa.me/${a.phone.replace(/[^+\d]/g, '')}`}
                            target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}
                            className="w-6 h-6 bg-accent-green text-dark-bg rounded-full text-xs flex items-center justify-center hover:brightness-110 font-bold"
                            title="WhatsApp">W</a>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {/* ── TABS: CONTATTATI / INCASSATI ── */}
      <div className="sc-card">
        {/* Tab headers */}
        <div className="flex border-b border-dark-border">
          <button
            onClick={() => setActiveTab('contacted')}
            className={`flex-1 py-3 px-4 text-sm font-semibold transition-colors ${
              activeTab === 'contacted'
                ? 'text-accent-blue border-b-2 border-accent-blue bg-accent-blue/5'
                : 'text-txt-muted hover:text-txt-secondary hover:bg-dark-cardHover'
            }`}
          >
            Account Contattati ({contacted.length})
          </button>
          <button
            onClick={() => setActiveTab('incassati')}
            className={`flex-1 py-3 px-4 text-sm font-semibold transition-colors ${
              activeTab === 'incassati'
                ? 'text-accent-green border-b-2 border-accent-green bg-accent-green/5'
                : 'text-txt-muted hover:text-txt-secondary hover:bg-dark-cardHover'
            }`}
          >
            Incassati ({incassati.filter(i => i.fully_resolved).length})
          </button>
        </div>

        {/* Tab content */}
        <div className="p-4">
          {activeTab === 'contacted' && (
            <>
              {contacted.length === 0 ? (
                <div className="text-center py-8">
                  <p className="text-txt-muted mb-2">Nessun account contattato.</p>
                  <p className="text-sm text-txt-muted">Vai nella scheda cliente e registra la prima azione per iniziare il recupero.</p>
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full">
                    <thead>
                      <tr className="text-left text-xs text-txt-muted uppercase tracking-wider border-b border-dark-border">
                        <th className="pb-3 pr-4">Cliente</th>
                        <th className="pb-3 pr-4">Stato</th>
                        <th className="pb-3 pr-4">Ultimo Contatto</th>
                        <th className="pb-3 pr-4">Prossima Azione</th>
                        <th className="pb-3 pr-4 text-right">Scaduto</th>
                        <th className="pb-3"></th>
                      </tr>
                    </thead>
                    <tbody>
                      {contacted.map(c => {
                        const statusColor = STATUS_BADGE[c.recovery_status] || 'bg-[rgba(148,163,184,0.15)] text-txt-muted'
                        const isOverdue = c.next_action_date && c.next_action_date < todayStr
                        return (
                          <tr
                            key={c.id}
                            onClick={() => navigate(`/customers/${c.id}`)}
                            className="sc-table-row cursor-pointer"
                          >
                            <td className="py-3 pr-4">
                              <p className="text-sm font-medium text-txt-primary">{c.ragione_sociale}</p>
                              {c.partita_iva && <p className="text-xs text-txt-muted font-mono">{c.partita_iva}</p>}
                            </td>
                            <td className="py-3 pr-4">
                              <span className={`${statusColor} sc-badge inline-block`}>
                                {ACTION_LABELS[c.recovery_status] || c.recovery_status}
                              </span>
                              {c.last_outcome && (
                                <p className="text-xs text-txt-muted mt-1">{OUTCOME_LABELS[c.last_outcome] || c.last_outcome}</p>
                              )}
                            </td>
                            <td className="py-3 pr-4">
                              <p className="text-sm text-txt-secondary">{formatDate(c.last_contact_date)}</p>
                              {c.last_action_type && (
                                <p className="text-xs text-txt-muted">{ACTION_LABELS[c.last_action_type] || c.last_action_type}</p>
                              )}
                            </td>
                            <td className="py-3 pr-4">
                              <p className={`text-sm font-medium ${isOverdue ? 'text-accent-red' : 'text-txt-secondary'}`}>
                                {formatDate(c.next_action_date)}
                              </p>
                              <p className="text-xs text-accent-teal font-medium mt-0.5">
                                {suggestNextAction(c)}
                              </p>
                              {isOverdue && (
                                <span className="text-[10px] text-accent-red font-bold">SCADUTA</span>
                              )}
                            </td>
                            <td className="py-3 pr-4 text-right">
                              {c.total_overdue > 0 ? (
                                <>
                                  <p className="text-sm font-bold text-accent-red">{formatCurrency(c.total_overdue)}</p>
                                  <p className="text-xs text-txt-muted">{c.overdue_count} fatt.</p>
                                </>
                              ) : (
                                <span className="text-sm text-txt-muted">-</span>
                              )}
                            </td>
                            <td className="py-3">
                              {c.phone && (
                                <a href={`https://wa.me/${c.phone.replace(/[^+\d]/g, '')}`}
                                  target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}
                                  className="w-7 h-7 bg-accent-green text-dark-bg rounded-full text-xs flex items-center justify-center hover:brightness-110 font-bold"
                                  title="WhatsApp">W</a>
                              )}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}

          {activeTab === 'incassati' && (
            <>
              {incassati.length === 0 ? (
                <div className="text-center py-8">
                  <p className="text-txt-muted mb-2">Nessun incasso registrato.</p>
                  <p className="text-sm text-txt-muted">Quando le fatture vengono pagate e sincronizzate, appariranno qui.</p>
                </div>
              ) : (
                <div className="space-y-2">
                  {incassati.map(inc => (
                    <div
                      key={inc.id}
                      onClick={() => navigate(`/customers/${inc.id}`)}
                      className={`flex items-center justify-between rounded-lg px-4 py-3 cursor-pointer transition-colors border ${
                        inc.fully_resolved
                          ? 'bg-accent-green/5 border-accent-green/30 hover:bg-accent-green/10'
                          : 'bg-accent-amber/5 border-accent-amber/30 hover:bg-accent-amber/10'
                      }`}
                    >
                      <div className="flex items-center gap-3 min-w-0">
                        <span className={`sc-badge shrink-0 ${
                          inc.fully_resolved
                            ? 'bg-accent-green/20 text-accent-green'
                            : 'bg-accent-amber/20 text-accent-amber'
                        }`}>
                          {inc.fully_resolved ? 'INCASSATO' : 'PARZIALE'}
                        </span>
                        <div className="min-w-0">
                          <p className="text-sm font-medium text-txt-primary truncate">{inc.ragione_sociale}</p>
                          {inc.partita_iva && <p className="text-xs text-txt-muted font-mono">{inc.partita_iva}</p>}
                        </div>
                      </div>
                      <div className="flex items-center gap-4 shrink-0">
                        <div className="text-right">
                          <p className="text-sm font-bold text-accent-green">{formatCurrency(inc.total_paid)}</p>
                          <p className="text-xs text-txt-muted">{inc.paid_count} fatture</p>
                        </div>
                        <div className="text-right">
                          <p className="text-xs text-txt-muted">Ultimo pagamento</p>
                          <p className="text-sm text-txt-secondary">{formatDate(inc.last_payment)}</p>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
