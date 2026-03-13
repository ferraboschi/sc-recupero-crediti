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
  overdue: { label: 'In Ritardo', bg: 'bg-red-50', border: 'border-red-200', badge: 'bg-red-600 text-white' },
  today: { label: 'Oggi', bg: 'bg-blue-50', border: 'border-blue-200', badge: 'bg-blue-600 text-white' },
  new: { label: 'Da Contattare', bg: 'bg-amber-50', border: 'border-amber-200', badge: 'bg-amber-600 text-white' },
  upcoming: { label: 'Prossimamente', bg: 'bg-slate-50', border: 'border-slate-200', badge: 'bg-slate-500 text-white' },
}

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

      {/* Clienti Da Fare - full width list with grouping */}
      <div className="bg-white rounded-lg p-6 border border-slate-200">
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
          <div className="space-y-4 max-h-[700px] overflow-y-auto">
            {['overdue', 'today', 'new', 'upcoming'].map(p => renderTodoSection(p))}
          </div>
        )}
      </div>
    </div>
  )
}
