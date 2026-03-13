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

const PRIORITY_CONFIG = {
  overdue: { label: 'In Ritardo', bg: 'bg-red-50', border: 'border-red-200', badge: 'bg-red-600 text-white', icon: '🔴' },
  today: { label: 'Oggi', bg: 'bg-blue-50', border: 'border-blue-200', badge: 'bg-blue-600 text-white', icon: '🔵' },
  new: { label: 'Da Contattare', bg: 'bg-amber-50', border: 'border-amber-200', badge: 'bg-amber-600 text-white', icon: '🟡' },
  upcoming: { label: 'Prossimamente', bg: 'bg-slate-50', border: 'border-slate-200', badge: 'bg-slate-500 text-white', icon: '⏳' },
}

const SORT_OPTIONS = [
  { value: 'priority', label: 'Priorità' },
  { value: 'amount_desc', label: 'Più esposto (importo)' },
  { value: 'oldest_debt', label: 'Debito più vecchio' },
  { value: 'days_overdue', label: 'GG scaduto' },
  { value: 'name', label: 'Nome A-Z' },
]

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

  // Filter & Sort state
  const [filterPriority, setFilterPriority] = useState('all')
  const [sortBy, setSortBy] = useState('priority')

  // Search state
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState(null)
  const [searchLoading, setSearchLoading] = useState(false)
  const searchTimeout = useRef(null)

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
      if (retry < 5) {
        setRetryCount(retry + 1)
        setTimeout(() => fetchData(retry + 1), 3000)
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
    const d = dateStr.length === 10 ? new Date(dateStr + 'T00:00:00') : new Date(dateStr)
    return d.toLocaleDateString('it-IT', { weekday: 'short', day: 'numeric', month: 'short' })
  }

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

  // Filter todos by priority
  const filteredTodos = filterPriority === 'all'
    ? todos
    : todos.filter(t => t.priority === filterPriority)

  // Sort todos
  const sortedTodos = [...filteredTodos].sort((a, b) => {
    if (sortBy === 'amount_desc') {
      return (b.total_overdue || 0) - (a.total_overdue || 0)
    }
    if (sortBy === 'oldest_debt') {
      const dateA = a.oldest_due_date || '9999-12-31'
      const dateB = b.oldest_due_date || '9999-12-31'
      return dateA.localeCompare(dateB)
    }
    if (sortBy === 'days_overdue') {
      return (b.max_days_overdue || 0) - (a.max_days_overdue || 0)
    }
    if (sortBy === 'name') {
      return (a.customer_name || '').localeCompare(b.customer_name || '')
    }
    // Default: priority
    const priorityOrder = { overdue: 0, today: 1, new: 2, upcoming: 3 }
    const pA = priorityOrder[a.priority] ?? 9
    const pB = priorityOrder[b.priority] ?? 9
    if (pA !== pB) return pA - pB
    return (a.scheduled_date || '').localeCompare(b.scheduled_date || '')
  })

  // Group by priority (only when sorted by priority)
  const todosByPriority = {}
  if (sortBy === 'priority') {
    sortedTodos.forEach(todo => {
      if (!todosByPriority[todo.priority]) todosByPriority[todo.priority] = []
      todosByPriority[todo.priority].push(todo)
    })
  }

  const renderTodoItem = (todo) => (
    <div
      key={todo.id}
      onClick={() => navigate(`/customers/${todo.customer_id}`)}
      className="flex items-center justify-between bg-white rounded-lg px-4 py-3 cursor-pointer hover:shadow-md transition-all border border-slate-100 group"
    >
      <div className="flex items-center gap-3 min-w-0">
        <span className={`${ACTION_BADGE_COLORS[todo.action_type] || ACTION_BADGE_COLORS.idle} px-2 py-0.5 rounded text-xs font-medium border shrink-0`}>
          {ACTION_LABELS[todo.action_type] || todo.action_type}
        </span>
        <div className="min-w-0">
          <span className="text-sm font-medium text-slate-900 truncate block max-w-[220px]">
            {todo.customer_name}
          </span>
          {todo.partita_iva && (
            <span className="text-xs text-slate-400 font-mono">{todo.partita_iva}</span>
          )}
        </div>
      </div>
      <div className="flex items-center gap-3 text-xs text-slate-500 shrink-0">
        {/* Days overdue badge */}
        {todo.max_days_overdue > 0 && (
          <span className={`px-1.5 py-0.5 rounded font-medium ${
            todo.max_days_overdue > 60 ? 'bg-red-100 text-red-700' :
            todo.max_days_overdue > 30 ? 'bg-amber-100 text-amber-700' :
            'bg-slate-100 text-slate-600'
          }`}>
            {todo.max_days_overdue}gg
          </span>
        )}
        {todo.overdue_count > 0 && (
          <span className="bg-red-50 text-red-600 px-1.5 py-0.5 rounded">
            {todo.overdue_count} fatt.
          </span>
        )}
        {todo.total_overdue > 0 && (
          <span className="font-semibold text-red-700 min-w-[70px] text-right">{formatCurrency(todo.total_overdue)}</span>
        )}
        {todo.phone && (
          <a
            href={`https://wa.me/${todo.phone.replace(/[^+\d]/g, '')}`}
            target="_blank" rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            className="w-6 h-6 bg-green-500 text-white rounded-full text-xs flex items-center justify-center hover:bg-green-600 opacity-0 group-hover:opacity-100 transition-opacity"
            title="WhatsApp"
          >W</a>
        )}
        <span className="text-slate-400 min-w-[80px] text-right">{formatDate(todo.scheduled_date)}</span>
      </div>
    </div>
  )

  const renderTodoSection = (priority) => {
    const items = todosByPriority[priority]
    if (!items || items.length === 0) return null
    const config = PRIORITY_CONFIG[priority]
    const sectionTotal = items.reduce((sum, t) => sum + (t.total_overdue || 0), 0)
    return (
      <div key={priority} className={`${config.bg} rounded-lg p-4 border ${config.border}`}>
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <span className="text-sm">{config.icon}</span>
            <span className={`${config.badge} px-2.5 py-0.5 rounded text-xs font-bold`}>
              {config.label}
            </span>
            <span className="text-sm text-slate-500">({items.length})</span>
          </div>
          {sectionTotal > 0 && (
            <span className="text-sm font-semibold text-slate-700">{formatCurrency(sectionTotal)}</span>
          )}
        </div>
        <div className="space-y-2">
          {items.map(renderTodoItem)}
        </div>
      </div>
    )
  }

  const totalTodoOverdue = todos.reduce((sum, t) => sum + (t.total_overdue || 0), 0)

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

      {/* Clienti Da Fare - with filters and sorting */}
      <div className="bg-white rounded-lg border border-slate-200">
        {/* Header with title and total */}
        <div className="px-6 py-4 border-b border-slate-200">
          <div className="flex items-center justify-between">
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
        </div>

        {/* Filter chips + Sort selector */}
        {todos.length > 0 && (
          <div className="px-6 py-3 border-b border-slate-100 bg-slate-50/50 flex flex-wrap items-center justify-between gap-3">
            {/* Priority filter chips */}
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xs text-slate-500 font-medium mr-1">Filtra:</span>
              <button
                onClick={() => setFilterPriority('all')}
                className={`px-3 py-1 rounded-full text-xs font-medium transition-colors ${
                  filterPriority === 'all'
                    ? 'bg-slate-800 text-white'
                    : 'bg-white text-slate-600 border border-slate-300 hover:bg-slate-100'
                }`}
              >
                Tutti ({todos.length})
              </button>
              {todoCounts.overdue > 0 && (
                <button
                  onClick={() => setFilterPriority('overdue')}
                  className={`px-3 py-1 rounded-full text-xs font-medium transition-colors ${
                    filterPriority === 'overdue'
                      ? 'bg-red-600 text-white'
                      : 'bg-white text-red-600 border border-red-300 hover:bg-red-50'
                  }`}
                >
                  In Ritardo ({todoCounts.overdue})
                </button>
              )}
              {todoCounts.today > 0 && (
                <button
                  onClick={() => setFilterPriority('today')}
                  className={`px-3 py-1 rounded-full text-xs font-medium transition-colors ${
                    filterPriority === 'today'
                      ? 'bg-blue-600 text-white'
                      : 'bg-white text-blue-600 border border-blue-300 hover:bg-blue-50'
                  }`}
                >
                  Oggi ({todoCounts.today})
                </button>
              )}
              {todoCounts.new > 0 && (
                <button
                  onClick={() => setFilterPriority('new')}
                  className={`px-3 py-1 rounded-full text-xs font-medium transition-colors ${
                    filterPriority === 'new'
                      ? 'bg-amber-600 text-white'
                      : 'bg-white text-amber-600 border border-amber-300 hover:bg-amber-50'
                  }`}
                >
                  Da Contattare ({todoCounts.new})
                </button>
              )}
              {todoCounts.upcoming > 0 && (
                <button
                  onClick={() => setFilterPriority('upcoming')}
                  className={`px-3 py-1 rounded-full text-xs font-medium transition-colors ${
                    filterPriority === 'upcoming'
                      ? 'bg-slate-600 text-white'
                      : 'bg-white text-slate-600 border border-slate-300 hover:bg-slate-50'
                  }`}
                >
                  Prossimi ({todoCounts.upcoming})
                </button>
              )}
            </div>

            {/* Sort selector */}
            <div className="flex items-center gap-2">
              <span className="text-xs text-slate-500 font-medium">Ordina:</span>
              <select
                value={sortBy}
                onChange={(e) => setSortBy(e.target.value)}
                className="text-xs border border-slate-300 rounded-lg px-2 py-1.5 bg-white text-slate-700 focus:outline-none focus:ring-2 focus:ring-blue-300"
              >
                {SORT_OPTIONS.map(opt => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
            </div>
          </div>
        )}

        {/* Todo list content */}
        <div className="p-6">
          {todoLoading ? (
            <p className="text-slate-500 text-center py-4">Caricamento...</p>
          ) : todos.length === 0 ? (
            <div className="text-center py-8">
              <p className="text-slate-500 mb-2">Nessuna azione da fare</p>
              <p className="text-sm text-slate-400">Sincronizza le fatture e vai nella scheda Clienti per iniziare.</p>
            </div>
          ) : sortBy === 'priority' && filterPriority === 'all' ? (
            /* Grouped by priority */
            <div className="space-y-4 max-h-[700px] overflow-y-auto">
              {['overdue', 'today', 'new', 'upcoming'].map(p => renderTodoSection(p))}
            </div>
          ) : (
            /* Flat sorted list */
            <div className="space-y-2 max-h-[700px] overflow-y-auto">
              {sortedTodos.length === 0 ? (
                <p className="text-slate-400 text-center py-4">Nessun risultato per questo filtro</p>
              ) : (
                sortedTodos.map((todo, idx) => (
                  <div key={todo.id} className="flex items-center gap-2">
                    <span className="text-xs text-slate-300 w-6 text-right shrink-0">{idx + 1}.</span>
                    <div className="flex-1">{renderTodoItem(todo)}</div>
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
