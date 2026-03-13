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
  first_contact: 'bg-blue-100 text-blue-700 border-blue-300',
  second_contact: 'bg-amber-100 text-amber-700 border-amber-300',
  lawyer: 'bg-red-100 text-red-700 border-red-300',
  archived: 'bg-slate-100 text-slate-500 border-slate-300',
  waiting: 'bg-purple-100 text-purple-700 border-purple-300',
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

// Pipeline stage config
const PIPELINE_STAGES = [
  { key: 'idle', label: 'Da Gestire', color: 'bg-slate-400', textColor: 'text-slate-700', lightBg: 'bg-slate-50' },
  { key: 'first_contact', label: 'I Contatto', color: 'bg-blue-500', textColor: 'text-blue-700', lightBg: 'bg-blue-50' },
  { key: 'second_contact', label: 'II Contatto', color: 'bg-amber-500', textColor: 'text-amber-700', lightBg: 'bg-amber-50' },
  { key: 'lawyer', label: 'Avvocato', color: 'bg-red-500', textColor: 'text-red-700', lightBg: 'bg-red-50' },
  { key: 'waiting', label: 'In Attesa', color: 'bg-purple-500', textColor: 'text-purple-700', lightBg: 'bg-purple-50' },
  { key: 'resolved', label: 'Incassato', color: 'bg-green-500', textColor: 'text-green-700', lightBg: 'bg-green-50' },
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

  // Overlay modal state: null or 'contacted' | 'incassati' | 'recovered' | 'overdue'
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
          <svg className="animate-spin w-12 h-12 text-blue-600 mx-auto mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          <p className="text-slate-600">Caricamento attività...</p>
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
      <div className="bg-white rounded-lg p-6 border border-slate-200">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h2 className="text-lg font-bold text-slate-900">Pipeline Recupero</h2>
            <p className="text-xs text-slate-500 mt-0.5">Stato di avanzamento di tutti i clienti con debiti scaduti</p>
          </div>
          {pipeline?.total_with_overdue > 0 && (
            <span className="text-sm text-slate-500">
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
                <div className="flex-1 bg-slate-100 rounded-full h-7 relative overflow-hidden">
                  <div
                    className={`${stage.color} h-full rounded-full transition-all duration-700 flex items-center justify-end pr-2`}
                    style={{ width: `${pct}%`, minWidth: data.count > 0 ? '40px' : '0' }}
                  >
                    {data.count > 0 && (
                      <span className="text-xs text-white font-bold">{data.count}</span>
                    )}
                  </div>
                </div>
                <span className="text-xs text-slate-500 w-24">
                  {data.amount > 0 ? formatCurrency(data.amount) : '-'}
                </span>
              </div>
            )
          })}
        </div>
      </div>

      {/* ── SUMMARY CARDS (clickable) ── */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <button onClick={() => setOverlayType('contacted')} className="bg-white rounded-lg p-4 border border-slate-200 text-left hover:border-blue-400 hover:shadow-md transition-all group">
          <p className="text-xs text-slate-500 uppercase tracking-wide">Account Contattati</p>
          <p className="text-2xl font-bold text-blue-700 mt-1">{summary.total_contacted || 0}</p>
          <p className="text-xs text-slate-400 mt-1 group-hover:text-blue-500">clienti in lavorazione — clicca per dettagli</p>
        </button>
        <button onClick={() => setOverlayType('incassati')} className="rounded-lg p-4 border border-green-200 bg-green-50/30 text-left hover:border-green-400 hover:shadow-md transition-all group">
          <p className="text-xs text-green-600 uppercase tracking-wide font-medium">Incassati</p>
          <p className="text-2xl font-bold text-green-700 mt-1">{summary.fully_resolved || 0}</p>
          <p className="text-xs text-green-500 mt-1 group-hover:text-green-700">debiti risolti — clicca per dettagli</p>
        </button>
        <button onClick={() => setOverlayType('recovered')} className="rounded-lg p-4 border border-green-200 bg-green-50/30 text-left hover:border-green-400 hover:shadow-md transition-all group">
          <p className="text-xs text-green-600 uppercase tracking-wide font-medium">Totale Recuperato</p>
          <p className="text-2xl font-bold text-green-700 mt-1">{formatCurrency(summary.total_recovered || 0)}</p>
          <p className="text-xs text-green-500 mt-1 group-hover:text-green-700">importo incassato — clicca per dettagli</p>
        </button>
        <button onClick={() => setOverlayType('overdue')} className="bg-white rounded-lg p-4 border border-slate-200 text-left hover:border-red-400 hover:shadow-md transition-all group">
          <p className="text-xs text-slate-500 uppercase tracking-wide">Azioni Scadute</p>
          <p className="text-2xl font-bold text-red-600 mt-1">{calData?.overdue_count || 0}</p>
          <p className="text-xs text-red-400 mt-1 group-hover:text-red-600">da completare — clicca per dettagli</p>
        </button>
      </div>

      {/* ── OVERLAY MODAL ── */}
      {overlayType && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4" onClick={() => setOverlayType(null)}>
          <div className="bg-white rounded-xl shadow-2xl max-w-lg w-full max-h-[80vh] overflow-hidden" onClick={(e) => e.stopPropagation()}>
            {/* Header */}
            <div className={`px-6 py-4 flex items-center justify-between border-b ${
              overlayType === 'contacted' ? 'bg-blue-50 border-blue-200' :
              overlayType === 'overdue' ? 'bg-red-50 border-red-200' :
              'bg-green-50 border-green-200'
            }`}>
              <h3 className="text-lg font-bold text-slate-900">
                {overlayType === 'contacted' && 'Account Contattati'}
                {overlayType === 'incassati' && 'Incassati — Dettaglio'}
                {overlayType === 'recovered' && 'Totale Recuperato — Dettaglio'}
                {overlayType === 'overdue' && 'Azioni Scadute — Dettaglio'}
              </h3>
              <button onClick={() => setOverlayType(null)} className="text-slate-400 hover:text-slate-700 text-xl font-bold leading-none">&times;</button>
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
                    <p className="text-sm text-slate-500">{contacted.length} clienti in lavorazione attiva</p>
                    {statusOrder.filter(s => byStatus[s]).map(s => (
                      <div key={s} className="border border-slate-200 rounded-lg overflow-hidden">
                        <div className={`px-4 py-2 flex items-center justify-between ${
                          s === 'first_contact' ? 'bg-blue-50' :
                          s === 'second_contact' ? 'bg-amber-50' :
                          s === 'lawyer' ? 'bg-red-50' : 'bg-purple-50'
                        }`}>
                          <span className={`text-sm font-bold ${
                            s === 'first_contact' ? 'text-blue-700' :
                            s === 'second_contact' ? 'text-amber-700' :
                            s === 'lawyer' ? 'text-red-700' : 'text-purple-700'
                          }`}>{ACTION_LABELS[s] || s}</span>
                          <span className="text-sm text-slate-600">{byStatus[s].count} clienti — {formatCurrency(byStatus[s].amount)} scaduto</span>
                        </div>
                        <div className="divide-y divide-slate-100">
                          {byStatus[s].customers.map(c => (
                            <div key={c.id} onClick={() => { setOverlayType(null); navigate(`/customers/${c.id}`) }}
                              className="px-4 py-2 flex items-center justify-between hover:bg-slate-50 cursor-pointer">
                              <div>
                                <p className="text-sm font-medium text-slate-800">{c.ragione_sociale}</p>
                                <p className="text-xs text-slate-400">Ultimo: {formatDate(c.last_contact_date)} {c.last_outcome ? `— ${OUTCOME_LABELS[c.last_outcome] || c.last_outcome}` : ''}</p>
                              </div>
                              <p className="text-sm font-bold text-red-600">{formatCurrency(c.total_overdue)}</p>
                            </div>
                          ))}
                        </div>
                      </div>
                    ))}
                    {contacted.length === 0 && <p className="text-sm text-slate-400 text-center py-4">Nessun account contattato.</p>}
                  </div>
                )
              })()}

              {/* INCASSATI: customer list with resolution status */}
              {overlayType === 'incassati' && (
                <div className="space-y-3">
                  <p className="text-sm text-slate-500">
                    {incassati.filter(i => i.fully_resolved).length} completamente risolti su {incassati.length} con pagamenti
                  </p>
                  {incassati.length === 0 && <p className="text-sm text-slate-400 text-center py-4">Nessun incasso da azioni di recupero.</p>}
                  {incassati.map(inc => (
                    <div key={inc.id} onClick={() => { setOverlayType(null); navigate(`/customers/${inc.id}`) }}
                      className={`rounded-lg px-4 py-3 cursor-pointer transition-colors border ${
                        inc.fully_resolved ? 'bg-green-50 border-green-300 hover:bg-green-100' : 'bg-amber-50 border-amber-200 hover:bg-amber-100'
                      }`}>
                      <div className="flex items-center justify-between">
                        <div>
                          <p className="text-sm font-medium text-slate-900">{inc.ragione_sociale}</p>
                          <p className="text-xs text-slate-400 mt-0.5">
                            {inc.paid_count} fatture pagate — ultimo: {formatDate(inc.last_payment)}
                          </p>
                        </div>
                        <div className="text-right">
                          <p className="text-sm font-bold text-green-700">{formatCurrency(inc.total_paid)}</p>
                          <span className={`text-xs font-bold px-2 py-0.5 rounded ${
                            inc.fully_resolved ? 'bg-green-200 text-green-800' : 'bg-amber-200 text-amber-800'
                          }`}>{inc.fully_resolved ? 'RISOLTO' : 'PARZIALE'}</span>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {/* TOTALE RECUPERATO: breakdown per customer with amounts */}
              {overlayType === 'recovered' && (
                <div className="space-y-3">
                  <div className="flex items-center justify-between mb-2">
                    <p className="text-sm text-slate-500">{incassati.length} clienti con pagamenti da recupero</p>
                    <p className="text-lg font-bold text-green-700">{formatCurrency(summary.total_recovered || 0)}</p>
                  </div>
                  {incassati.length === 0 && <p className="text-sm text-slate-400 text-center py-4">Nessun importo recuperato.</p>}
                  {/* Bar chart */}
                  {incassati.map(inc => {
                    const pct = summary.total_recovered > 0 ? Math.max((inc.total_paid / summary.total_recovered) * 100, 2) : 0
                    return (
                      <div key={inc.id} onClick={() => { setOverlayType(null); navigate(`/customers/${inc.id}`) }}
                        className="cursor-pointer hover:bg-slate-50 rounded-lg p-3 border border-slate-200 transition-colors">
                        <div className="flex items-center justify-between mb-1.5">
                          <p className="text-sm font-medium text-slate-800">{inc.ragione_sociale}</p>
                          <p className="text-sm font-bold text-green-700">{formatCurrency(inc.total_paid)}</p>
                        </div>
                        <div className="w-full bg-slate-100 rounded-full h-2.5">
                          <div className="bg-green-500 h-2.5 rounded-full transition-all" style={{ width: `${pct}%` }} />
                        </div>
                        <div className="flex items-center justify-between mt-1">
                          <p className="text-xs text-slate-400">{inc.paid_count} fatture — ultimo {formatDate(inc.last_payment)}</p>
                          <p className="text-xs text-slate-400">{pct.toFixed(1)}%</p>
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}

              {/* AZIONI SCADUTE: list from calendar */}
              {overlayType === 'overdue' && (() => {
                // Collect overdue actions from calData.days
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
                    <p className="text-sm text-slate-500">{overdueActions.length} azioni scadute da completare o ripianificare</p>
                    {overdueActions.length === 0 && <p className="text-sm text-green-600 text-center py-4">Nessuna azione scaduta! Tutto in ordine.</p>}
                    {overdueActions.map((a, idx) => (
                      <div key={a.id || idx} onClick={() => { setOverlayType(null); navigate(`/customers/${a.customer_id}`) }}
                        className="flex items-center justify-between rounded-lg px-4 py-3 cursor-pointer bg-red-50 border border-red-200 hover:bg-red-100 transition-colors">
                        <div className="flex items-center gap-3 min-w-0">
                          <span className={`${STATUS_BADGE[a.action_type] || 'bg-slate-100 text-slate-600 border-slate-300'} px-2 py-1 rounded text-xs font-bold border shrink-0`}>
                            {ACTION_LABELS[a.action_type] || a.action_type}
                          </span>
                          <div className="min-w-0">
                            <p className="text-sm font-medium text-slate-900 truncate">{a.customer_name}</p>
                            <p className="text-xs text-red-500">Scaduta il {formatDate(a.date)}</p>
                          </div>
                        </div>
                        {a.total_overdue > 0 && (
                          <p className="text-sm font-bold text-red-600 shrink-0">{formatCurrency(a.total_overdue)}</p>
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
      <div className="bg-white rounded-lg p-6 border border-slate-200">
        <div className="flex items-center justify-between mb-1">
          <div>
            <h2 className="text-lg font-bold text-slate-900">Calendario Attività</h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Clicca su un giorno con azioni per vederne i dettagli. Le azioni scadute sono in rosso.
            </p>
          </div>
          <div className="flex items-center gap-3">
            <button onClick={prevMonth} className="px-3 py-1.5 hover:bg-slate-100 rounded text-slate-600 font-bold">&lt;</button>
            <span className="text-base font-semibold text-slate-700 min-w-[160px] text-center">
              {MONTH_NAMES[calMonth - 1]} {calYear}
            </span>
            <button onClick={nextMonth} className="px-3 py-1.5 hover:bg-slate-100 rounded text-slate-600 font-bold">&gt;</button>
          </div>
        </div>

        {calLoading ? (
          <p className="text-slate-500 text-center py-8">Caricamento calendario...</p>
        ) : (
          <>
            {/* Weekday headers */}
            <div className="grid grid-cols-7 mb-2 mt-4">
              {WEEKDAYS.map(d => (
                <div key={d} className="text-center text-sm font-semibold text-slate-500 py-2">{d}</div>
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
                      ${!day.isCurrentMonth ? 'text-slate-300 bg-slate-50/50' : 'text-slate-700'}
                      ${day.isToday ? 'ring-2 ring-blue-500 font-bold bg-blue-50' : ''}
                      ${isSelected ? 'bg-blue-100 ring-2 ring-blue-600' : ''}
                      ${allDone && !isSelected && !day.isToday ? 'bg-green-50' : ''}
                      ${hasActions && !allDone && !isSelected && !day.isToday ? (isPast ? 'bg-red-50 hover:bg-red-100' : 'bg-amber-50 hover:bg-amber-100') : ''}
                      ${hasActions ? 'cursor-pointer' : 'cursor-default'}
                      ${!hasActions && day.isCurrentMonth && !day.isToday ? 'hover:bg-slate-50' : ''}
                    `}
                  >
                    <span className="text-sm">{day.day}</span>
                    {hasActions && (
                      <div className="flex flex-wrap justify-center mt-1 gap-0.5">
                        {allDone ? (
                          <span className="text-xs text-green-600 font-bold">&#10003; {completedActions.length}</span>
                        ) : pendingActions.length <= 3 ? (
                          pendingActions.map((_, i) => (
                            <div key={i} className={`w-2 h-2 rounded-full ${isPast ? 'bg-red-400' : 'bg-blue-400'}`} />
                          ))
                        ) : (
                          <span className={`text-xs font-bold ${isPast ? 'text-red-500' : 'text-blue-600'}`}>
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
            <div className="mt-4 flex items-center gap-6 text-xs text-slate-500">
              <div className="flex items-center gap-1.5">
                <div className="w-2.5 h-2.5 rounded-full bg-red-400" /> Scadute (da completare)
              </div>
              <div className="flex items-center gap-1.5">
                <div className="w-2.5 h-2.5 rounded-full bg-blue-400" /> Pianificate
              </div>
              <div className="flex items-center gap-1.5">
                <span className="text-green-600 font-bold">&#10003;</span> Completate
              </div>
              <div className="flex items-center gap-1.5">
                <div className="w-2.5 h-2.5 ring-2 ring-blue-500 rounded-full" /> Oggi
              </div>
            </div>

            {/* Selected day detail */}
            {selectedDayActions && (
              <div className="mt-4 border-t border-slate-200 pt-4">
                <div className="flex items-center justify-between mb-3">
                  <p className="text-sm font-semibold text-slate-700">
                    {new Date(selectedDay + 'T00:00:00').toLocaleDateString('it-IT', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })}
                    <span className="text-slate-400 ml-2 font-normal">({selectedDayActions.length} azioni)</span>
                  </p>
                  {selectedDay < todayStr && selectedDayActions.some(a => !a.completed_at) && (
                    <span className="text-xs bg-red-100 text-red-700 px-2 py-1 rounded font-medium">
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
                          ? 'bg-green-50 border-green-200 opacity-80'
                          : 'bg-white border-slate-200 hover:bg-blue-50 hover:border-blue-300'
                      }`}
                    >
                      <div className="flex items-center gap-3 min-w-0">
                        <span className={`${STATUS_BADGE[a.action_type] || STATUS_BADGE.first_contact} px-2.5 py-1 rounded text-xs font-bold border shrink-0`}>
                          {ACTION_LABELS[a.action_type] || a.action_type}
                        </span>
                        <span className={`text-sm font-medium truncate ${a.completed_at ? 'text-slate-500 line-through' : 'text-slate-900'}`}>
                          {a.customer_name}
                        </span>
                        {a.outcome && (
                          <span className="text-xs text-slate-400">— {OUTCOME_LABELS[a.outcome] || a.outcome}</span>
                        )}
                      </div>
                      <div className="flex items-center gap-3 shrink-0">
                        {a.total_overdue > 0 && !a.completed_at && (
                          <span className="text-sm font-bold text-red-600">{formatCurrency(a.total_overdue)}</span>
                        )}
                        {a.phone && !a.completed_at && (
                          <a href={`https://wa.me/${a.phone.replace(/[^+\d]/g, '')}`}
                            target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}
                            className="w-6 h-6 bg-green-500 text-white rounded-full text-xs flex items-center justify-center hover:bg-green-600 font-bold"
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
      <div className="bg-white rounded-lg border border-slate-200">
        {/* Tab headers */}
        <div className="flex border-b border-slate-200">
          <button
            onClick={() => setActiveTab('contacted')}
            className={`flex-1 py-3 px-4 text-sm font-semibold transition-colors ${
              activeTab === 'contacted'
                ? 'text-blue-700 border-b-2 border-blue-600 bg-blue-50/50'
                : 'text-slate-500 hover:text-slate-700 hover:bg-slate-50'
            }`}
          >
            Account Contattati ({contacted.length})
          </button>
          <button
            onClick={() => setActiveTab('incassati')}
            className={`flex-1 py-3 px-4 text-sm font-semibold transition-colors ${
              activeTab === 'incassati'
                ? 'text-green-700 border-b-2 border-green-600 bg-green-50/50'
                : 'text-slate-500 hover:text-slate-700 hover:bg-slate-50'
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
                  <p className="text-slate-400 mb-2">Nessun account contattato.</p>
                  <p className="text-sm text-slate-400">Vai nella scheda cliente e registra la prima azione per iniziare il recupero.</p>
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full">
                    <thead>
                      <tr className="text-left text-xs text-slate-500 uppercase tracking-wider border-b border-slate-200">
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
                        const statusColor = STATUS_BADGE[c.recovery_status] || 'bg-slate-100 text-slate-600 border-slate-300'
                        const isOverdue = c.next_action_date && c.next_action_date < todayStr
                        return (
                          <tr
                            key={c.id}
                            onClick={() => navigate(`/customers/${c.id}`)}
                            className="border-b border-slate-100 hover:bg-blue-50 cursor-pointer transition-colors"
                          >
                            <td className="py-3 pr-4">
                              <p className="text-sm font-medium text-slate-900">{c.ragione_sociale}</p>
                              {c.partita_iva && <p className="text-xs text-slate-400 font-mono">{c.partita_iva}</p>}
                            </td>
                            <td className="py-3 pr-4">
                              <span className={`${statusColor} px-2.5 py-1 rounded text-xs font-bold border inline-block`}>
                                {ACTION_LABELS[c.recovery_status] || c.recovery_status}
                              </span>
                              {c.last_outcome && (
                                <p className="text-xs text-slate-400 mt-1">{OUTCOME_LABELS[c.last_outcome] || c.last_outcome}</p>
                              )}
                            </td>
                            <td className="py-3 pr-4">
                              <p className="text-sm text-slate-700">{formatDate(c.last_contact_date)}</p>
                              {c.last_action_type && (
                                <p className="text-xs text-slate-400">{ACTION_LABELS[c.last_action_type] || c.last_action_type}</p>
                              )}
                            </td>
                            <td className="py-3 pr-4">
                              <p className={`text-sm font-medium ${isOverdue ? 'text-red-600' : 'text-slate-700'}`}>
                                {formatDate(c.next_action_date)}
                              </p>
                              {/* System-suggested next step */}
                              <p className="text-xs text-blue-500 font-medium mt-0.5">
                                {suggestNextAction(c)}
                              </p>
                              {isOverdue && (
                                <span className="text-[10px] text-red-500 font-bold">SCADUTA</span>
                              )}
                            </td>
                            <td className="py-3 pr-4 text-right">
                              {c.total_overdue > 0 ? (
                                <>
                                  <p className="text-sm font-bold text-red-600">{formatCurrency(c.total_overdue)}</p>
                                  <p className="text-xs text-slate-400">{c.overdue_count} fatt.</p>
                                </>
                              ) : (
                                <span className="text-sm text-slate-400">-</span>
                              )}
                            </td>
                            <td className="py-3">
                              {c.phone && (
                                <a href={`https://wa.me/${c.phone.replace(/[^+\d]/g, '')}`}
                                  target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}
                                  className="w-7 h-7 bg-green-500 text-white rounded-full text-xs flex items-center justify-center hover:bg-green-600 font-bold"
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
                  <p className="text-slate-400 mb-2">Nessun incasso registrato.</p>
                  <p className="text-sm text-slate-400">Quando le fatture vengono pagate e sincronizzate, appariranno qui.</p>
                </div>
              ) : (
                <div className="space-y-2">
                  {incassati.map(inc => (
                    <div
                      key={inc.id}
                      onClick={() => navigate(`/customers/${inc.id}`)}
                      className={`flex items-center justify-between rounded-lg px-4 py-3 cursor-pointer transition-colors border ${
                        inc.fully_resolved
                          ? 'bg-green-50 border-green-300 hover:bg-green-100'
                          : 'bg-amber-50 border-amber-200 hover:bg-amber-100'
                      }`}
                    >
                      <div className="flex items-center gap-3 min-w-0">
                        <span className={`px-2.5 py-1 rounded text-xs font-bold border shrink-0 ${
                          inc.fully_resolved
                            ? 'bg-green-200 text-green-800 border-green-400'
                            : 'bg-amber-200 text-amber-800 border-amber-400'
                        }`}>
                          {inc.fully_resolved ? 'INCASSATO' : 'PARZIALE'}
                        </span>
                        <div className="min-w-0">
                          <p className="text-sm font-medium text-slate-900 truncate">{inc.ragione_sociale}</p>
                          {inc.partita_iva && <p className="text-xs text-slate-400 font-mono">{inc.partita_iva}</p>}
                        </div>
                      </div>
                      <div className="flex items-center gap-4 shrink-0">
                        <div className="text-right">
                          <p className="text-sm font-bold text-green-700">{formatCurrency(inc.total_paid)}</p>
                          <p className="text-xs text-slate-400">{inc.paid_count} fatture</p>
                        </div>
                        <div className="text-right">
                          <p className="text-xs text-slate-500">Ultimo pagamento</p>
                          <p className="text-sm text-slate-700">{formatDate(inc.last_payment)}</p>
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
