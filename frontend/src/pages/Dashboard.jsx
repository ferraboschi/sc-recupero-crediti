import React, { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import client from '../api/client'
import StatsWidget from '../components/StatsWidget'

const ACTION_LABELS = {
  first_contact: 'I Contatto',
  second_contact: 'II Contatto',
  lawyer: 'Avvocato',
  archive: 'Archivia',
  wait: 'Attendi',
  idle: 'Da Gestire',
  waiting: 'In Attesa',
  note: 'Nota',
}

const ACTION_BADGE_COLORS = {
  first_contact: 'bg-blue-100 text-blue-700 border-blue-200',
  second_contact: 'bg-amber-100 text-amber-700 border-amber-200',
  lawyer: 'bg-red-100 text-red-700 border-red-200',
  archive: 'bg-slate-100 text-slate-600 border-slate-200',
  wait: 'bg-purple-100 text-purple-700 border-purple-200',
  idle: 'bg-slate-100 text-slate-600 border-slate-200',
  waiting: 'bg-purple-100 text-purple-700 border-purple-200',
}

const OUTCOME_LABELS = {
  contacted: 'Contattato',
  promised: 'Promessa Pagamento',
  partial_payment: 'Pagamento Parziale',
  paid: 'Pagato',
  unreachable: 'Irraggiungibile',
  disputed: 'Contestazione',
  no_answer: 'Non Risponde',
}

const OUTCOME_COLORS = {
  contacted: 'bg-blue-100 text-blue-700',
  promised: 'bg-amber-100 text-amber-700',
  partial_payment: 'bg-teal-100 text-teal-700',
  paid: 'bg-green-100 text-green-700',
  unreachable: 'bg-slate-100 text-slate-600',
  disputed: 'bg-red-100 text-red-700',
  no_answer: 'bg-orange-100 text-orange-700',
}

const PRIORITY_CONFIG = {
  overdue: { label: 'In Ritardo', bg: 'bg-red-50', border: 'border-red-200', badge: 'bg-red-600 text-white' },
  today: { label: 'Oggi', bg: 'bg-blue-50', border: 'border-blue-200', badge: 'bg-blue-600 text-white' },
  new: { label: 'Da Contattare', bg: 'bg-amber-50', border: 'border-amber-200', badge: 'bg-amber-600 text-white' },
  upcoming: { label: 'Prossimamente', bg: 'bg-slate-50', border: 'border-slate-200', badge: 'bg-slate-500 text-white' },
}

const WEEKDAYS = ['Lun', 'Mar', 'Mer', 'Gio', 'Ven', 'Sab', 'Dom']
const MONTH_NAMES = ['Gennaio', 'Febbraio', 'Marzo', 'Aprile', 'Maggio', 'Giugno',
  'Luglio', 'Agosto', 'Settembre', 'Ottobre', 'Novembre', 'Dicembre']

export default function Dashboard() {
  const navigate = useNavigate()
  const [loading, setLoading] = useState(true)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [syncing, setSyncing] = useState(false)
  const [syncMessage, setSyncMessage] = useState('')
  const [lastSync, setLastSync] = useState(null)
  const [todos, setTodos] = useState([])
  const [todoCounts, setTodoCounts] = useState({})
  const [todoLoading, setTodoLoading] = useState(true)
  const [retryCount, setRetryCount] = useState(0)

  // Search state
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState(null)
  const [searchLoading, setSearchLoading] = useState(false)
  const searchTimeout = useRef(null)

  // Calendar state
  const now = new Date()
  const [calYear, setCalYear] = useState(now.getFullYear())
  const [calMonth, setCalMonth] = useState(now.getMonth() + 1)
  const [calData, setCalData] = useState(null)
  const [calLoading, setCalLoading] = useState(true)
  const [selectedDay, setSelectedDay] = useState(null)

  const fetchData = async (retry = 0) => {
    try {
      setLoading(true)
      const response = await client.get('/dashboard')
      setData(response.data)
      setRetryCount(0)
      const savedLastSync = localStorage.getItem('lastSyncTime')
      if (savedLastSync) setLastSync(new Date(savedLastSync))
    } catch (err) {
      console.error(err)
      if (retry < 2) {
        setRetryCount(retry + 1)
        setTimeout(() => fetchData(retry + 1), 10000)
      } else {
        setError('Errore nel caricamento dei dati del dashboard')
      }
    } finally {
      setLoading(false)
    }
  }

  const fetchTodos = async () => {
    try {
      setTodoLoading(true)
      const response = await client.get('/dashboard/todos')
      setTodos(response.data.todos || [])
      setTodoCounts(response.data.counts || {})
    } catch (err) {
      console.error('Error fetching todos:', err)
    } finally {
      setTodoLoading(false)
    }
  }

  const fetchCalendar = async (y, m) => {
    try {
      setCalLoading(true)
      const response = await client.get('/dashboard/calendar', { params: { year: y, month: m } })
      setCalData(response.data)
    } catch (err) {
      console.error('Error fetching calendar:', err)
    } finally {
      setCalLoading(false)
    }
  }

  const handleSearch = async (q) => {
    if (!q || q.trim().length < 2) {
      setSearchResults(null)
      return
    }
    try {
      setSearchLoading(true)
      const response = await client.get('/dashboard/search', { params: { q } })
      setSearchResults(response.data.results || [])
    } catch (err) {
      console.error('Error searching:', err)
    } finally {
      setSearchLoading(false)
    }
  }

  const onSearchInput = (val) => {
    setSearchQuery(val)
    if (searchTimeout.current) clearTimeout(searchTimeout.current)
    searchTimeout.current = setTimeout(() => handleSearch(val), 300)
  }

  useEffect(() => {
    fetchData()
    fetchTodos()
  }, [])

  useEffect(() => {
    fetchCalendar(calYear, calMonth)
  }, [calYear, calMonth])

  const handleSync = async () => {
    setSyncing(true)
    setSyncMessage('')
    try {
      await client.post('/sync/full')
      setSyncMessage('Sincronizzazione completata con successo')
      const syncNow = new Date()
      setLastSync(syncNow)
      localStorage.setItem('lastSyncTime', syncNow.toISOString())
      await fetchData()
      await fetchTodos()
      await fetchCalendar(calYear, calMonth)
      setTimeout(() => setSyncMessage(''), 3000)
    } catch (err) {
      setSyncMessage('Errore nella sincronizzazione')
      console.error(err)
    } finally {
      setSyncing(false)
    }
  }

  const formatCurrency = (value) =>
    new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR', minimumFractionDigits: 0, maximumFractionDigits: 0 }).format(value)

  const formatDate = (dateStr) => {
    if (!dateStr) return '-'
    return new Date(dateStr).toLocaleDateString('it-IT', { weekday: 'short', day: 'numeric', month: 'short' })
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
        isToday: iso === new Date().toISOString().split('T')[0],
        actions: calData.days[iso] || [],
      })
      d.setDate(d.getDate() + 1)
    }
    return days
  }

  // Get today's actions from calendar data
  const todayStr = new Date().toISOString().split('T')[0]
  const todayActions = calData?.days?.[todayStr] || []

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-center">
          <svg className="animate-spin-slow w-12 h-12 text-blue-600 mx-auto mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          <p className="text-slate-600">Caricamento dati...</p>
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="space-y-6">
        <div className="bg-amber-50 border border-amber-200 rounded-lg p-6">
          <div className="flex items-center gap-3 mb-2">
            <svg className="animate-spin w-5 h-5 text-amber-600" fill="none" viewBox="0 0 24 24">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
            </svg>
            <h3 className="font-semibold text-amber-800 text-lg">Server in avvio...</h3>
          </div>
          <p className="text-amber-700 mb-3">Il server si sta risvegliando (fino a 60 secondi).</p>
          <button onClick={() => { setError(null); fetchData(); fetchTodos(); }}
            className="px-4 py-2 bg-amber-600 text-white rounded-lg text-sm font-medium hover:bg-amber-700">
            Riprova Ora
          </button>
        </div>
      </div>
    )
  }

  if (!data) return null

  // Group todos by priority
  const todosByPriority = {}
  todos.forEach(todo => {
    if (!todosByPriority[todo.priority]) todosByPriority[todo.priority] = []
    todosByPriority[todo.priority].push(todo)
  })

  const renderTodoItem = (todo) => (
    <div
      key={todo.id}
      onClick={() => navigate(`/customers/${todo.customer_id}`)}
      className="flex items-center justify-between bg-white rounded-lg px-3 py-2 cursor-pointer hover:shadow-sm transition-shadow border border-slate-100"
    >
      <div className="flex items-center gap-3 min-w-0">
        <span className={`${ACTION_BADGE_COLORS[todo.action_type] || ACTION_BADGE_COLORS.idle} px-2 py-0.5 rounded text-xs font-medium border shrink-0`}>
          {ACTION_LABELS[todo.action_type] || todo.action_type}
        </span>
        <div className="min-w-0">
          <span className="text-sm font-medium text-slate-900 truncate block max-w-[200px]">
            {todo.customer_name}
          </span>
          {todo.partita_iva && (
            <span className="text-xs text-slate-400 font-mono">{todo.partita_iva}</span>
          )}
        </div>
      </div>
      <div className="flex items-center gap-3 text-xs text-slate-500 shrink-0">
        {todo.overdue_count > 0 && (
          <span className="bg-red-50 text-red-600 px-1.5 py-0.5 rounded">
            {todo.overdue_count} scad.
          </span>
        )}
        {todo.total_overdue > 0 && (
          <span className="font-medium text-red-700">{formatCurrency(todo.total_overdue)}</span>
        )}
        {todo.phone && (
          <a
            href={`https://wa.me/${todo.phone.replace(/[^+\d]/g, '')}`}
            target="_blank" rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            className="w-5 h-5 bg-green-500 text-white rounded-full text-xs flex items-center justify-center hover:bg-green-600"
            title="WhatsApp"
          >W</a>
        )}
        <span>{formatDate(todo.scheduled_date)}</span>
      </div>
    </div>
  )

  const renderTodoSection = (priority) => {
    const items = todosByPriority[priority]
    if (!items || items.length === 0) return null
    const config = PRIORITY_CONFIG[priority]
    return (
      <div key={priority} className={`${config.bg} rounded-lg p-4 border ${config.border}`}>
        <div className="flex items-center gap-2 mb-3">
          <span className={`${config.badge} px-2 py-0.5 rounded text-xs font-bold`}>
            {config.label}
          </span>
          <span className="text-sm text-slate-500">({items.length})</span>
        </div>
        <div className="space-y-2">
          {items.map(renderTodoItem)}
        </div>
      </div>
    )
  }

  const totalTodoOverdue = todos.reduce((sum, t) => sum + (t.total_overdue || 0), 0)
  const calendarDays = buildCalendarDays()
  const selectedDayActions = selectedDay && calData?.days?.[selectedDay] ? calData.days[selectedDay] : null

  return (
    <div className="space-y-6">
      {/* Search Bar + Sync Bar */}
      <div className="bg-white rounded-lg p-4 border border-slate-200">
        <div className="flex items-center gap-4">
          {/* Search */}
          <div className="relative flex-1 max-w-md">
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => onSearchInput(e.target.value)}
              placeholder="Cerca per ragione sociale o P.IVA..."
              className="w-full px-4 py-2 pl-10 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-300 focus:border-blue-400"
            />
            <svg className="absolute left-3 top-2.5 w-4 h-4 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
            {searchLoading && (
              <div className="absolute right-3 top-2.5">
                <svg className="animate-spin w-4 h-4 text-blue-500" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
                </svg>
              </div>
            )}
            {/* Search dropdown */}
            {searchResults !== null && (
              <div className="absolute top-full left-0 right-0 mt-1 bg-white rounded-lg shadow-lg border border-slate-200 z-50 max-h-80 overflow-y-auto">
                {searchResults.length === 0 ? (
                  <p className="p-3 text-sm text-slate-500">Nessun risultato</p>
                ) : (
                  searchResults.map(r => (
                    <button
                      key={r.id}
                      onClick={() => { navigate(`/customers/${r.id}`); setSearchResults(null); setSearchQuery(''); }}
                      className="w-full flex items-center justify-between px-4 py-3 hover:bg-blue-50 transition-colors text-left border-b border-slate-100 last:border-0"
                    >
                      <div>
                        <p className="text-sm font-medium text-slate-900">{r.ragione_sociale}</p>
                        {r.partita_iva && <p className="text-xs text-slate-400 font-mono">{r.partita_iva}</p>}
                      </div>
                      <div className="text-right">
                        {r.total_overdue > 0 && (
                          <p className="text-sm font-bold text-red-600">{formatCurrency(r.total_overdue)}</p>
                        )}
                        {r.overdue_count > 0 && (
                          <p className="text-xs text-slate-500">{r.overdue_count} scadute</p>
                        )}
                      </div>
                    </button>
                  ))
                )}
              </div>
            )}
          </div>

          <div className="flex items-center gap-4 ml-auto">
            <p className="text-sm text-slate-500 hidden md:block">
              {lastSync ? `Agg: ${lastSync.toLocaleString('it-IT')}` : ''}
            </p>
            {syncMessage && (
              <p className={`text-sm font-medium ${syncMessage.includes('Errore') ? 'text-red-600' : 'text-green-600'}`}>
                {syncMessage}
              </p>
            )}
            <button onClick={handleSync} disabled={syncing}
              className={`px-4 py-2 rounded-lg font-medium text-sm transition-colors flex items-center gap-2 ${
                syncing ? 'bg-slate-300 text-slate-500 cursor-not-allowed' : 'bg-blue-600 text-white hover:bg-blue-700'
              }`}>
              {syncing ? 'Sync...' : 'Sincronizza'}
            </button>
          </div>
        </div>
      </div>

      {/* Top Stats */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatsWidget label="Totale Scaduto" value={formatCurrency(data.total_scaduto || 0)} color="red" />
        <StatsWidget label="Fatture Scadute" value={data.total_fatture_scadute || 0} color="orange" />
        <StatsWidget label="Clienti con Scaduto" value={data.total_clienti_scaduti || 0} color="purple" />
        <StatsWidget label="Da Gestire" value={todos.length || 0} color="blue" />
      </div>

      {/* OGGI - Today's scheduled actions (prominent section) */}
      {(todayActions.length > 0 || (todoCounts.overdue || 0) > 0) && (
        <div className="bg-blue-50 border-2 border-blue-300 rounded-lg p-5">
          <div className="flex items-center gap-3 mb-3">
            <span className="bg-blue-600 text-white px-3 py-1 rounded-full text-sm font-bold">
              Oggi
            </span>
            <span className="text-sm text-blue-700">
              {todayActions.filter(a => !a.completed_at).length} azioni pianificate
              {(todoCounts.overdue || 0) > 0 && ` + ${todoCounts.overdue} in ritardo`}
            </span>
          </div>
          <div className="space-y-2">
            {/* Overdue actions first */}
            {(todosByPriority['overdue'] || []).slice(0, 5).map(todo => (
              <div key={todo.id}
                onClick={() => navigate(`/customers/${todo.customer_id}`)}
                className="flex items-center justify-between bg-red-50 rounded-lg px-3 py-2 cursor-pointer hover:bg-red-100 transition-colors border border-red-200"
              >
                <div className="flex items-center gap-2 min-w-0">
                  <span className="bg-red-600 text-white px-2 py-0.5 rounded text-xs font-bold shrink-0">SCADUTA</span>
                  <span className="text-sm font-medium text-slate-900 truncate">{todo.customer_name}</span>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <span className="text-sm font-bold text-red-700">{formatCurrency(todo.total_overdue)}</span>
                  {todo.phone && (
                    <a href={`https://wa.me/${todo.phone.replace(/[^+\d]/g, '')}`}
                      target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}
                      className="w-5 h-5 bg-green-500 text-white rounded-full text-xs flex items-center justify-center hover:bg-green-600">W</a>
                  )}
                </div>
              </div>
            ))}
            {/* Today's actions */}
            {todayActions.filter(a => !a.completed_at).map(a => (
              <div key={a.id}
                onClick={() => navigate(`/customers/${a.customer_id}`)}
                className="flex items-center justify-between bg-white rounded-lg px-3 py-2 cursor-pointer hover:bg-blue-50 transition-colors border border-blue-200"
              >
                <div className="flex items-center gap-2 min-w-0">
                  <span className={`${ACTION_BADGE_COLORS[a.action_type] || ACTION_BADGE_COLORS.idle} px-2 py-0.5 rounded text-xs font-medium border shrink-0`}>
                    {ACTION_LABELS[a.action_type] || a.action_type}
                  </span>
                  <span className="text-sm font-medium text-slate-900 truncate">{a.customer_name}</span>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  {a.total_overdue > 0 && <span className="text-sm font-bold text-red-700">{formatCurrency(a.total_overdue)}</span>}
                  {a.phone && (
                    <a href={`https://wa.me/${a.phone.replace(/[^+\d]/g, '')}`}
                      target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}
                      className="w-5 h-5 bg-green-500 text-white rounded-full text-xs flex items-center justify-center hover:bg-green-600">W</a>
                  )}
                </div>
              </div>
            ))}
            {/* Completed today */}
            {todayActions.filter(a => a.completed_at).map(a => (
              <div key={`done-${a.id}`}
                className="flex items-center justify-between bg-green-50 rounded-lg px-3 py-2 border border-green-200 opacity-70"
              >
                <div className="flex items-center gap-2 min-w-0">
                  <span className="bg-green-100 text-green-700 px-2 py-0.5 rounded text-xs font-medium border border-green-200 shrink-0">
                    {a.outcome ? (OUTCOME_LABELS[a.outcome] || a.outcome) : 'Completata'}
                  </span>
                  <span className="text-sm text-slate-600 line-through truncate">{a.customer_name}</span>
                </div>
                {a.total_overdue > 0 && <span className="text-sm text-green-700">{formatCurrency(a.total_overdue)}</span>}
              </div>
            ))}
          </div>
          {(todosByPriority['overdue'] || []).length > 5 && (
            <p className="text-xs text-blue-600 mt-2">+ {(todosByPriority['overdue'] || []).length - 5} altre azioni in ritardo</p>
          )}
        </div>
      )}

      {/* Main content: 2-column layout */}
      <div className="grid grid-cols-1 lg:grid-cols-5 gap-6">

        {/* LEFT: TODO Section (3 cols) */}
        <div className="lg:col-span-3 bg-white rounded-lg p-6 border border-slate-200">
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-3">
              <h2 className="text-lg font-bold text-slate-900">Da Fare</h2>
              {todos.length > 0 && (
                <span className="bg-red-100 text-red-700 px-2.5 py-0.5 rounded-full text-sm font-bold">
                  {todos.length}
                </span>
              )}
            </div>
            {totalTodoOverdue > 0 && (
              <div className="text-right">
                <p className="text-xs text-slate-500">Totale in gestione</p>
                <p className="text-lg font-bold text-red-600">{formatCurrency(totalTodoOverdue)}</p>
              </div>
            )}
          </div>

          {todoLoading ? (
            <p className="text-slate-500 text-center py-4">Caricamento...</p>
          ) : todos.length === 0 ? (
            <div className="text-center py-8">
              <p className="text-slate-500 mb-2">Nessuna azione da fare</p>
              <p className="text-sm text-slate-400">Sincronizza le fatture e vai nella scheda Clienti per iniziare.</p>
            </div>
          ) : (
            <div className="space-y-4 max-h-[600px] overflow-y-auto">
              {['overdue', 'today', 'new', 'upcoming'].map(p => renderTodoSection(p))}
            </div>
          )}
        </div>

        {/* RIGHT: Calendar (2 cols) */}
        <div className="lg:col-span-2 bg-white rounded-lg p-6 border border-slate-200">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-bold text-slate-900">Calendario</h2>
            <div className="flex items-center gap-2">
              <button onClick={prevMonth} className="p-1.5 hover:bg-slate-100 rounded text-slate-600 text-sm font-bold">&lt;</button>
              <span className="text-sm font-medium text-slate-700 min-w-[130px] text-center">
                {MONTH_NAMES[calMonth - 1]} {calYear}
              </span>
              <button onClick={nextMonth} className="p-1.5 hover:bg-slate-100 rounded text-slate-600 text-sm font-bold">&gt;</button>
            </div>
          </div>

          {calLoading ? (
            <p className="text-slate-500 text-center py-4">Caricamento...</p>
          ) : (
            <>
              {/* Weekday headers */}
              <div className="grid grid-cols-7 mb-1">
                {WEEKDAYS.map(d => (
                  <div key={d} className="text-center text-xs font-medium text-slate-400 py-1">{d}</div>
                ))}
              </div>

              {/* Calendar grid */}
              <div className="grid grid-cols-7 gap-0.5">
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
                      className={`relative p-1.5 text-center text-sm rounded transition-colors min-h-[40px]
                        ${!day.isCurrentMonth ? 'text-slate-300' : 'text-slate-700'}
                        ${day.isToday ? 'ring-2 ring-blue-400 font-bold' : ''}
                        ${isSelected ? 'bg-blue-100 ring-2 ring-blue-500' : ''}
                        ${allDone && !isSelected ? 'bg-green-50' : ''}
                        ${hasActions && !allDone && !isSelected ? (isPast ? 'bg-red-50 hover:bg-red-100' : 'bg-blue-50 hover:bg-blue-100') : ''}
                        ${hasActions ? 'cursor-pointer' : 'cursor-default'}
                      `}
                    >
                      {day.day}
                      {hasActions && (
                        <div className="flex justify-center mt-0.5 gap-0.5">
                          {allDone ? (
                            <span className="text-[10px] text-green-600 font-bold">&#10003;</span>
                          ) : pendingActions.length <= 3 ? (
                            pendingActions.map((_, i) => (
                              <div key={i} className={`w-1.5 h-1.5 rounded-full ${isPast ? 'bg-red-400' : 'bg-blue-400'}`} />
                            ))
                          ) : (
                            <span className={`text-[10px] font-bold ${isPast ? 'text-red-500' : 'text-blue-500'}`}>
                              {pendingActions.length}
                            </span>
                          )}
                        </div>
                      )}
                    </button>
                  )
                })}
              </div>

              {/* Selected day detail */}
              {selectedDayActions && (
                <div className="mt-4 border-t border-slate-200 pt-4">
                  <p className="text-sm font-medium text-slate-700 mb-2">
                    {new Date(selectedDay + 'T00:00:00').toLocaleDateString('it-IT', { weekday: 'long', day: 'numeric', month: 'long' })}
                    <span className="text-slate-400 ml-2">({selectedDayActions.length} azioni)</span>
                  </p>
                  <div className="space-y-2 max-h-[250px] overflow-y-auto">
                    {selectedDayActions.map((a) => (
                      <div
                        key={a.id}
                        onClick={() => navigate(`/customers/${a.customer_id}`)}
                        className={`flex items-center justify-between rounded-lg px-3 py-2 cursor-pointer transition-colors border ${
                          a.completed_at
                            ? 'bg-green-50 border-green-200 opacity-75'
                            : 'bg-slate-50 border-slate-100 hover:bg-blue-50'
                        }`}
                      >
                        <div className="flex items-center gap-2 min-w-0">
                          {a.completed_at ? (
                            <span className={`${a.outcome ? (OUTCOME_COLORS[a.outcome] || 'bg-green-100 text-green-700') : 'bg-green-100 text-green-700'} px-2 py-0.5 rounded text-xs font-medium shrink-0`}>
                              {a.outcome ? (OUTCOME_LABELS[a.outcome] || a.outcome) : 'Fatto'}
                            </span>
                          ) : (
                            <span className={`${ACTION_BADGE_COLORS[a.action_type] || ACTION_BADGE_COLORS.idle} px-2 py-0.5 rounded text-xs font-medium border shrink-0`}>
                              {ACTION_LABELS[a.action_type] || a.action_type}
                            </span>
                          )}
                          <span className={`text-sm font-medium truncate ${a.completed_at ? 'text-slate-500 line-through' : 'text-slate-900'}`}>
                            {a.customer_name}
                          </span>
                        </div>
                        <div className="flex items-center gap-2 shrink-0">
                          {a.total_overdue > 0 && !a.completed_at && (
                            <span className="text-xs font-medium text-red-600">{formatCurrency(a.total_overdue)}</span>
                          )}
                          {a.phone && !a.completed_at && (
                            <a href={`https://wa.me/${a.phone.replace(/[^+\d]/g, '')}`}
                              target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}
                              className="w-5 h-5 bg-green-500 text-white rounded-full text-xs flex items-center justify-center hover:bg-green-600"
                              title="WhatsApp">W</a>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Legend */}
              <div className="mt-3 flex items-center gap-4 text-xs text-slate-400 flex-wrap">
                <div className="flex items-center gap-1">
                  <div className="w-2 h-2 rounded-full bg-red-400" /> Scadute
                </div>
                <div className="flex items-center gap-1">
                  <div className="w-2 h-2 rounded-full bg-blue-400" /> Pianificate
                </div>
                <div className="flex items-center gap-1">
                  <span className="text-green-600 font-bold text-[10px]">&#10003;</span> Completate
                </div>
                <div className="flex items-center gap-1">
                  <div className="w-2 h-2 ring-2 ring-blue-400 rounded-full" /> Oggi
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
