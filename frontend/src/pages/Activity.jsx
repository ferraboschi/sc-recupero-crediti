import React, { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import client from '../api/client'

export default function Activity() {
  const navigate = useNavigate()
  const [report, setReport] = useState(null)
  const [activities, setActivities] = useState([])
  const [calendar, setCalendar] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [activeTab, setActiveTab] = useState('attivi')
  const [calendarMonth, setCalendarMonth] = useState(() => {
    const now = new Date()
    return { year: now.getFullYear(), month: now.getMonth() }
  })

  useEffect(() => {
    const fetchData = async () => {
      try {
        setLoading(true)
        const [reportRes, activityRes, calendarRes] = await Promise.all([
          client.get('/recovery/report'),
          client.get('/dashboard'),
          client.get('/recovery/calendar'),
        ])
        setReport(reportRes.data)
        setActivities(activityRes.data.recent_activity || [])
        setCalendar(calendarRes.data)
      } catch (err) {
        setError('Errore nel caricamento dei dati')
        console.error(err)
      } finally {
        setLoading(false)
      }
    }
    fetchData()
  }, [])

  const tabs = [
    { id: 'attivi', label: 'Recuperi Attivi', icon: '🔥' },
    { id: 'scadenze', label: 'Calendario', icon: '📅' },
    { id: 'recuperati', label: 'Recuperati', icon: '💰' },
    { id: 'saldato', label: 'Saldato', icon: '✅' },
    { id: 'attesa', label: 'In Attesa', icon: '⏳' },
    { id: 'avvocato', label: 'Avvocato', icon: '⚖️' },
    { id: 'log', label: 'Log', icon: '📜' },
  ]

  const getActionLabel = (action) => {
    const labels = {
      first_contact: 'I Contatto',
      second_contact: 'II Contatto',
      lawyer: 'Avvocato',
      archive: 'Archivia',
      wait: 'Attendi',
      note: 'Nota',
      sync: 'Sincronizzazione',
      match: 'Abbinamento',
      escalation: 'Escalation',
      status_change: 'Cambio Stato',
      customer_excluded: 'Cliente Escluso',
      customer_included: 'Cliente Incluso',
      phone_updated: 'Telefono Aggiornato',
      csv_import: 'Import CSV',
      recovery_first_contact: 'I Contatto',
      recovery_second_contact: 'II Contatto',
      recovery_lawyer: 'Avvocato',
      recovery_completed: 'Completata',
    }
    return labels[action] || action
  }

  const getStatusBadge = (status) => {
    const styles = {
      first_contact: 'bg-yellow-100 text-yellow-800',
      second_contact: 'bg-orange-100 text-orange-800',
      lawyer: 'bg-red-100 text-red-800',
      waiting: 'bg-blue-100 text-blue-800',
      idle: 'bg-slate-100 text-slate-600',
      archived: 'bg-slate-200 text-slate-500',
    }
    const labels = {
      first_contact: 'I Contatto',
      second_contact: 'II Contatto',
      lawyer: 'Avvocato',
      waiting: 'In Attesa',
      idle: 'Da Avviare',
      archived: 'Archiviato',
    }
    return (
      <span className={`px-2 py-1 rounded-full text-xs font-medium ${styles[status] || 'bg-slate-100 text-slate-600'}`}>
        {labels[status] || status}
      </span>
    )
  }

  const formatCurrency = (val) => {
    return new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' }).format(val || 0)
  }

  const formatDate = (dateStr) => {
    if (!dateStr) return '-'
    const d = dateStr.length === 10 ? new Date(dateStr + 'T00:00:00') : new Date(dateStr)
    return d.toLocaleDateString('it-IT')
  }

  // Calendar helpers
  const getDaysInMonth = (year, month) => new Date(year, month + 1, 0).getDate()
  const getFirstDayOfMonth = (year, month) => {
    const day = new Date(year, month, 1).getDay()
    return day === 0 ? 6 : day - 1 // Monday = 0
  }

  const getCalendarDays = () => {
    const { year, month } = calendarMonth
    const daysInMonth = getDaysInMonth(year, month)
    const firstDay = getFirstDayOfMonth(year, month)
    const days = []

    // Empty cells before first day
    for (let i = 0; i < firstDay; i++) days.push(null)
    // Days of the month
    for (let i = 1; i <= daysInMonth; i++) days.push(i)
    return days
  }

  const getEventsForDay = (day) => {
    if (!calendar?.items || !day) return []
    const { year, month } = calendarMonth
    const dateStr = `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`
    return calendar.items.filter(item => item.scheduled_date === dateStr)
  }

  const isToday = (day) => {
    if (!day) return false
    const { year, month } = calendarMonth
    const now = new Date()
    return year === now.getFullYear() && month === now.getMonth() && day === now.getDate()
  }

  const isPast = (day) => {
    if (!day) return false
    const { year, month } = calendarMonth
    const d = new Date(year, month, day)
    const today = new Date()
    today.setHours(0, 0, 0, 0)
    return d < today
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-96">
        <svg className="animate-spin w-8 h-8 text-blue-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
        </svg>
      </div>
    )
  }

  if (error) {
    return (
      <div className="bg-red-50 border border-red-200 rounded-lg p-4 text-red-600">
        {error}
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Summary Cards */}
      {report?.summary && (
        <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-4">
          <div className="bg-white rounded-lg p-4 border border-slate-200 cursor-pointer hover:shadow-md transition-shadow" onClick={() => setActiveTab('attivi')}>
            <div className="text-2xl font-bold text-orange-600">{report.summary.active_count}</div>
            <div className="text-xs text-slate-500 mt-1">Recuperi Attivi</div>
            <div className="text-sm font-medium text-slate-700 mt-1">{formatCurrency(report.summary.active_total_due)}</div>
          </div>
          <div className="bg-white rounded-lg p-4 border border-slate-200 cursor-pointer hover:shadow-md transition-shadow" onClick={() => setActiveTab('scadenze')}>
            <div className="text-2xl font-bold text-purple-600">{report.summary.upcoming_actions_count}</div>
            <div className="text-xs text-slate-500 mt-1">Prossimi Solleciti</div>
          </div>
          <div className="bg-white rounded-lg p-4 border-2 border-green-300 bg-green-50 cursor-pointer hover:shadow-md transition-shadow" onClick={() => setActiveTab('recuperati')}>
            <div className="text-2xl font-bold text-green-700">{report.summary.recovered_count || 0}</div>
            <div className="text-xs text-green-600 mt-1 font-semibold">Recuperati</div>
            <div className="text-sm font-bold text-green-700 mt-1">{formatCurrency(report.summary.recovered_total || 0)}</div>
          </div>
          <div className="bg-white rounded-lg p-4 border border-slate-200 cursor-pointer hover:shadow-md transition-shadow" onClick={() => setActiveTab('saldato')}>
            <div className="text-2xl font-bold text-green-600">{report.summary.paid_count}</div>
            <div className="text-xs text-slate-500 mt-1">Saldato</div>
            <div className="text-sm font-medium text-slate-700 mt-1">{formatCurrency(report.summary.paid_total)}</div>
          </div>
          <div className="bg-white rounded-lg p-4 border border-slate-200 cursor-pointer hover:shadow-md transition-shadow" onClick={() => setActiveTab('attesa')}>
            <div className="text-2xl font-bold text-blue-600">{report.summary.waiting_count}</div>
            <div className="text-xs text-slate-500 mt-1">In Attesa</div>
          </div>
          <div className="bg-white rounded-lg p-4 border border-slate-200 cursor-pointer hover:shadow-md transition-shadow" onClick={() => setActiveTab('avvocato')}>
            <div className="text-2xl font-bold text-red-600">{report.summary.lawyer_count}</div>
            <div className="text-xs text-slate-500 mt-1">Avvocato</div>
          </div>
          <div className="bg-white rounded-lg p-4 border border-slate-200 cursor-pointer hover:shadow-md transition-shadow" onClick={() => setActiveTab('log')}>
            <div className="text-2xl font-bold text-slate-600">{activities.length}</div>
            <div className="text-xs text-slate-500 mt-1">Log Recenti</div>
          </div>
        </div>
      )}

      {/* Tabs */}
      <div className="bg-white rounded-lg border border-slate-200">
        <div className="flex border-b border-slate-200 overflow-x-auto">
          {tabs.map(tab => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-2 px-4 py-3 text-sm font-medium whitespace-nowrap border-b-2 transition-colors ${
                activeTab === tab.id
                  ? 'border-blue-600 text-blue-600'
                  : 'border-transparent text-slate-500 hover:text-slate-700 hover:border-slate-300'
              }`}
            >
              <span>{tab.icon}</span>
              {tab.label}
            </button>
          ))}
        </div>

        <div className="p-6">
          {/* Recuperi Attivi */}
          {activeTab === 'attivi' && (
            <div>
              <h3 className="text-lg font-semibold text-slate-800 mb-4">Recuperi Attivi ({report?.recuperi_attivi?.length || 0})</h3>
              {report?.recuperi_attivi?.length === 0 ? (
                <div className="bg-blue-50 border border-blue-200 rounded-lg p-6 text-center">
                  <div className="text-3xl mb-3">🚀</div>
                  <h4 className="font-semibold text-blue-800 mb-2">Nessun recupero attivo</h4>
                  <p className="text-blue-700 text-sm mb-3">Per avviare il flusso di recupero, vai nella pagina <strong>Clienti</strong> e seleziona un cliente con fatture scadute.</p>
                  <button
                    onClick={() => navigate('/customers')}
                    className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700"
                  >
                    Vai ai Clienti
                  </button>
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-slate-50 text-left">
                        <th className="px-4 py-3 font-medium text-slate-600">Cliente</th>
                        <th className="px-4 py-3 font-medium text-slate-600">P.IVA</th>
                        <th className="px-4 py-3 font-medium text-slate-600">Stato</th>
                        <th className="px-4 py-3 font-medium text-slate-600 text-right">Dovuto</th>
                        <th className="px-4 py-3 font-medium text-slate-600 text-center">Fatture</th>
                        <th className="px-4 py-3 font-medium text-slate-600">Prossimo Sollecito</th>
                        <th className="px-4 py-3 font-medium text-slate-600">Prossima Azione</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {report.recuperi_attivi.map(c => {
                        const nextDate = c.next_action_date ? new Date(c.next_action_date + 'T00:00:00') : null
                        const today = new Date()
                        today.setHours(0, 0, 0, 0)
                        const isOverdue = nextDate && nextDate <= today
                        return (
                          <tr
                            key={c.id}
                            className={`hover:bg-slate-50 cursor-pointer ${isOverdue ? 'bg-red-50/50' : ''}`}
                            onClick={() => navigate(`/customers/${c.id}`)}
                          >
                            <td className="px-4 py-3 font-medium text-slate-900">{c.ragione_sociale}</td>
                            <td className="px-4 py-3 text-slate-600">{c.partita_iva || '-'}</td>
                            <td className="px-4 py-3">{getStatusBadge(c.recovery_status)}</td>
                            <td className="px-4 py-3 text-right font-medium text-red-600">{formatCurrency(c.total_due)}</td>
                            <td className="px-4 py-3 text-center">{c.invoice_count}</td>
                            <td className="px-4 py-3">
                              {nextDate ? (
                                <span className={`font-medium ${isOverdue ? 'text-red-600' : 'text-slate-700'}`}>
                                  {isOverdue && '⚠ '}{nextDate.toLocaleDateString('it-IT')}
                                </span>
                              ) : '-'}
                            </td>
                            <td className="px-4 py-3 text-slate-500 text-xs">
                              {c.next_action_type ? getActionLabel(c.next_action_type) : '-'}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {/* Calendario */}
          {activeTab === 'scadenze' && (
            <div>
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-lg font-semibold text-slate-800">Calendario Solleciti</h3>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => setCalendarMonth(prev => {
                      let m = prev.month - 1, y = prev.year
                      if (m < 0) { m = 11; y-- }
                      return { year: y, month: m }
                    })}
                    className="p-2 hover:bg-slate-100 rounded-lg transition-colors"
                  >
                    ←
                  </button>
                  <span className="text-sm font-medium text-slate-700 min-w-[140px] text-center">
                    {new Date(calendarMonth.year, calendarMonth.month).toLocaleDateString('it-IT', { month: 'long', year: 'numeric' })}
                  </span>
                  <button
                    onClick={() => setCalendarMonth(prev => {
                      let m = prev.month + 1, y = prev.year
                      if (m > 11) { m = 0; y++ }
                      return { year: y, month: m }
                    })}
                    className="p-2 hover:bg-slate-100 rounded-lg transition-colors"
                  >
                    →
                  </button>
                </div>
              </div>

              {/* Calendar Grid */}
              <div className="grid grid-cols-7 gap-px bg-slate-200 rounded-lg overflow-hidden border border-slate-200">
                {['Lun', 'Mar', 'Mer', 'Gio', 'Ven', 'Sab', 'Dom'].map(d => (
                  <div key={d} className="bg-slate-50 px-2 py-2 text-xs font-medium text-slate-500 text-center">{d}</div>
                ))}
                {getCalendarDays().map((day, idx) => {
                  const events = getEventsForDay(day)
                  const today = isToday(day)
                  const past = isPast(day)
                  return (
                    <div
                      key={idx}
                      className={`bg-white min-h-[80px] p-1.5 ${!day ? 'bg-slate-50' : ''} ${today ? 'ring-2 ring-blue-500 ring-inset' : ''} ${past && day ? 'bg-slate-50/50' : ''}`}
                    >
                      {day && (
                        <>
                          <div className={`text-xs font-medium mb-1 ${today ? 'text-blue-600 font-bold' : past ? 'text-slate-400' : 'text-slate-600'}`}>
                            {day}
                          </div>
                          {events.slice(0, 3).map((ev, i) => (
                            <div
                              key={i}
                              onClick={() => navigate(`/customers/${ev.customer_id}`)}
                              className={`text-xs px-1 py-0.5 rounded mb-0.5 cursor-pointer truncate ${
                                ev.action_type === 'lawyer' ? 'bg-red-100 text-red-700' :
                                ev.action_type === 'second_contact' ? 'bg-orange-100 text-orange-700' :
                                ev.action_type === 'first_contact' ? 'bg-yellow-100 text-yellow-700' :
                                'bg-blue-100 text-blue-700'
                              } ${past ? 'opacity-60' : 'hover:opacity-80'}`}
                              title={`${ev.customer_name} — ${getActionLabel(ev.action_type)}`}
                            >
                              {ev.customer_name?.split(' ')[0]}
                            </div>
                          ))}
                          {events.length > 3 && (
                            <div className="text-xs text-slate-400 px-1">+{events.length - 3}</div>
                          )}
                        </>
                      )}
                    </div>
                  )
                })}
              </div>

              {/* Upcoming list below calendar */}
              <div className="mt-6">
                <h4 className="text-sm font-semibold text-slate-700 mb-3">Prossimi Solleciti</h4>
                {calendar?.items?.length === 0 ? (
                  <p className="text-slate-500 text-sm">Nessun sollecito programmato</p>
                ) : (
                  <div className="space-y-2">
                    {calendar?.items?.slice(0, 20).map((item, idx) => {
                      const itemDate = new Date(item.scheduled_date + 'T00:00:00')
                      const today = new Date()
                      today.setHours(0, 0, 0, 0)
                      const isOverdue = itemDate < today
                      const isToday = itemDate.getTime() === today.getTime()
                      return (
                        <div
                          key={item.id || idx}
                          className={`flex items-center justify-between p-3 rounded-lg border cursor-pointer hover:shadow-sm transition-shadow ${
                            isOverdue ? 'bg-red-50 border-red-200' : isToday ? 'bg-blue-50 border-blue-200' : 'bg-white border-slate-200'
                          }`}
                          onClick={() => navigate(`/customers/${item.customer_id}`)}
                        >
                          <div className="flex items-center gap-3">
                            <div className={`w-2 h-2 rounded-full ${
                              item.action_type === 'lawyer' ? 'bg-red-500' :
                              item.action_type === 'second_contact' ? 'bg-orange-500' :
                              'bg-yellow-500'
                            }`} />
                            <div>
                              <span className="text-sm font-medium text-slate-800">{item.customer_name}</span>
                              <span className="text-xs text-slate-500 ml-2">{getActionLabel(item.action_type)}</span>
                            </div>
                          </div>
                          <div className="flex items-center gap-3">
                            {item.total_due > 0 && (
                              <span className="text-xs font-medium text-red-600">{formatCurrency(item.total_due)}</span>
                            )}
                            <span className={`text-xs font-medium ${isOverdue ? 'text-red-600' : isToday ? 'text-blue-600' : 'text-slate-600'}`}>
                              {isOverdue ? '⚠ ' : isToday ? '📌 ' : ''}
                              {itemDate.toLocaleDateString('it-IT', { weekday: 'short', day: 'numeric', month: 'short' })}
                            </span>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Recuperati */}
          {activeTab === 'recuperati' && (
            <div>
              <div className="bg-green-50 border border-green-200 rounded-lg p-6 mb-6">
                <div className="flex items-center justify-between">
                  <div>
                    <h3 className="text-lg font-bold text-green-800">Crediti Recuperati</h3>
                    <p className="text-sm text-green-700 mt-1">
                      Fatture saldate dopo aver avviato il processo di recupero crediti
                    </p>
                  </div>
                  <div className="text-right">
                    <div className="text-3xl font-bold text-green-700">{formatCurrency(report?.summary?.recovered_total || 0)}</div>
                    <div className="text-sm text-green-600">{report?.summary?.recovered_count || 0} fatture recuperate</div>
                  </div>
                </div>
              </div>

              {report?.recuperati?.length === 0 ? (
                <div className="text-center py-8">
                  <div className="text-4xl mb-3">🎯</div>
                  <p className="text-slate-500 mb-2">Nessun credito recuperato ancora</p>
                  <p className="text-sm text-slate-400">
                    Quando un cliente con solleciti attivi salda le fatture, apparira qui.
                    Il circolo si chiude: sollecito → risposta → pagamento → recuperato!
                  </p>
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-green-50 text-left">
                        <th className="px-4 py-3 font-medium text-green-700">N. Fattura</th>
                        <th className="px-4 py-3 font-medium text-green-700">Cliente</th>
                        <th className="px-4 py-3 font-medium text-green-700 text-right">Importo Recuperato</th>
                        <th className="px-4 py-3 font-medium text-green-700">Piattaforma</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-green-100">
                      {report.recuperati.map(inv => (
                        <tr key={inv.id} className="hover:bg-green-50/50 cursor-pointer" onClick={() => inv.customer_id && navigate(`/customers/${inv.customer_id}`)}>
                          <td className="px-4 py-3 font-medium text-slate-900">{inv.invoice_number}</td>
                          <td className="px-4 py-3 text-slate-600">{inv.customer_name || '-'}</td>
                          <td className="px-4 py-3 text-right font-bold text-green-600">{formatCurrency(inv.amount)}</td>
                          <td className="px-4 py-3">
                            <span className={`px-2 py-1 rounded-full text-xs font-medium ${inv.source_platform === 'fatturapro' ? 'bg-blue-100 text-blue-700' : 'bg-purple-100 text-purple-700'}`}>
                              {inv.source_platform === 'fatturapro' ? 'FatturaPro' : 'Fattura24'}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                    <tfoot>
                      <tr className="bg-green-100 font-bold">
                        <td className="px-4 py-3" colSpan={2}>TOTALE RECUPERATO</td>
                        <td className="px-4 py-3 text-right text-green-700">{formatCurrency(report?.summary?.recovered_total || 0)}</td>
                        <td></td>
                      </tr>
                    </tfoot>
                  </table>
                </div>
              )}
            </div>
          )}

          {/* Saldato */}
          {activeTab === 'saldato' && (
            <div>
              <h3 className="text-lg font-semibold text-slate-800 mb-4">Fatture Saldate ({report?.saldato?.length || 0})</h3>
              {report?.saldato?.length === 0 ? (
                <p className="text-slate-500">Nessuna fattura saldata</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-slate-50 text-left">
                        <th className="px-4 py-3 font-medium text-slate-600">N. Fattura</th>
                        <th className="px-4 py-3 font-medium text-slate-600">Cliente</th>
                        <th className="px-4 py-3 font-medium text-slate-600 text-right">Importo</th>
                        <th className="px-4 py-3 font-medium text-slate-600">Piattaforma</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {report.saldato.map(inv => (
                        <tr key={inv.id} className="hover:bg-slate-50 cursor-pointer" onClick={() => inv.customer_id && navigate(`/customers/${inv.customer_id}`)}>
                          <td className="px-4 py-3 font-medium text-slate-900">{inv.invoice_number}</td>
                          <td className="px-4 py-3 text-slate-600">{inv.customer_name || '-'}</td>
                          <td className="px-4 py-3 text-right font-medium text-green-600">{formatCurrency(inv.amount)}</td>
                          <td className="px-4 py-3">
                            <span className={`px-2 py-1 rounded-full text-xs font-medium ${inv.source_platform === 'fatturapro' ? 'bg-blue-100 text-blue-700' : 'bg-purple-100 text-purple-700'}`}>
                              {inv.source_platform === 'fatturapro' ? 'FatturaPro' : 'Fattura24'}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {/* In Attesa */}
          {activeTab === 'attesa' && (
            <div>
              <h3 className="text-lg font-semibold text-slate-800 mb-4">In Attesa ({report?.in_attesa?.length || 0})</h3>
              {report?.in_attesa?.length === 0 ? (
                <p className="text-slate-500">Nessun cliente in attesa</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-slate-50 text-left">
                        <th className="px-4 py-3 font-medium text-slate-600">Cliente</th>
                        <th className="px-4 py-3 font-medium text-slate-600">P.IVA</th>
                        <th className="px-4 py-3 font-medium text-slate-600 text-right">Dovuto</th>
                        <th className="px-4 py-3 font-medium text-slate-600 text-center">Fatture</th>
                        <th className="px-4 py-3 font-medium text-slate-600">Riprendi Il</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {report.in_attesa.map(c => (
                        <tr key={c.id} className="hover:bg-slate-50 cursor-pointer" onClick={() => navigate(`/customers/${c.id}`)}>
                          <td className="px-4 py-3 font-medium text-slate-900">{c.ragione_sociale}</td>
                          <td className="px-4 py-3 text-slate-600">{c.partita_iva || '-'}</td>
                          <td className="px-4 py-3 text-right font-medium text-orange-600">{formatCurrency(c.total_due)}</td>
                          <td className="px-4 py-3 text-center">{c.invoice_count}</td>
                          <td className="px-4 py-3 text-slate-600">{c.next_action_date ? new Date(c.next_action_date + 'T00:00:00').toLocaleDateString('it-IT') : '-'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {/* Avvocato */}
          {activeTab === 'avvocato' && (
            <div>
              <h3 className="text-lg font-semibold text-slate-800 mb-4">Passati ad Avvocato ({report?.avvocato?.length || 0})</h3>
              {report?.avvocato?.length === 0 ? (
                <p className="text-slate-500">Nessun caso in mano all'avvocato</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-slate-50 text-left">
                        <th className="px-4 py-3 font-medium text-slate-600">Cliente</th>
                        <th className="px-4 py-3 font-medium text-slate-600">P.IVA</th>
                        <th className="px-4 py-3 font-medium text-slate-600 text-right">Dovuto</th>
                        <th className="px-4 py-3 font-medium text-slate-600 text-center">Fatture</th>
                        <th className="px-4 py-3 font-medium text-slate-600">Follow-up</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {report.avvocato.map(c => (
                        <tr key={c.id} className="hover:bg-slate-50 cursor-pointer" onClick={() => navigate(`/customers/${c.id}`)}>
                          <td className="px-4 py-3 font-medium text-slate-900">{c.ragione_sociale}</td>
                          <td className="px-4 py-3 text-slate-600">{c.partita_iva || '-'}</td>
                          <td className="px-4 py-3 text-right font-medium text-red-600">{formatCurrency(c.total_due)}</td>
                          <td className="px-4 py-3 text-center">{c.invoice_count}</td>
                          <td className="px-4 py-3 text-slate-600">{c.next_action_date ? new Date(c.next_action_date + 'T00:00:00').toLocaleDateString('it-IT') : '-'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {/* Log Attività */}
          {activeTab === 'log' && (
            <div>
              <h3 className="text-lg font-semibold text-slate-800 mb-4">Log Attività Recenti</h3>
              {activities.length === 0 ? (
                <p className="text-slate-500">Nessuna attività recente</p>
              ) : (
                <div className="space-y-3">
                  {activities.map((activity, index) => (
                    <div key={activity.id || index} className="flex items-start gap-3 bg-slate-50 rounded-lg p-3 border border-slate-200">
                      <div className="text-lg">
                        {{
                          sync: '🔄', match: '🔗', escalation: '📈', status_change: '✏️',
                          message_sent: '📤', customer_excluded: '🚫', customer_included: '✅',
                          phone_updated: '📱', csv_import: '📥',
                          recovery_first_contact: '📞', recovery_second_contact: '📞',
                          recovery_lawyer: '⚖️', recovery_completed: '✔️',
                        }[activity.action] || '📋'}
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-medium text-sm text-slate-800">{getActionLabel(activity.action)}</span>
                          <span className="text-xs text-slate-500 whitespace-nowrap">
                            {new Date(activity.timestamp).toLocaleString('it-IT')}
                          </span>
                        </div>
                        {activity.details && (
                          <div className="mt-1">
                            {activity.details.ragione_sociale && (
                              <span className="text-xs text-slate-600">{activity.details.ragione_sociale}</span>
                            )}
                            {activity.details.next_action_date && (
                              <span className="text-xs text-blue-600 ml-2">→ Prossimo: {formatDate(activity.details.next_action_date)}</span>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
