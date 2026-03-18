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
  first_contact: 'badge-open',
  second_contact: 'badge-contacted',
  lawyer: 'badge-disputed',
  archive: 'bg-[rgba(148,163,184,0.15)] text-txt-muted',
  wait: 'badge-promised',
  idle: 'bg-[rgba(148,163,184,0.15)] text-txt-muted',
  waiting: 'badge-promised',
}

const PRIORITY_CONFIG = {
  overdue: { label: 'In Ritardo', bg: 'bg-accent-red/5', border: 'border-accent-red/20', badge: 'bg-accent-red text-dark-bg', dot: 'bg-accent-red' },
  today: { label: 'Oggi', bg: 'bg-accent-blue/5', border: 'border-accent-blue/20', badge: 'bg-accent-blue text-dark-bg', dot: 'bg-accent-blue' },
  new: { label: 'Da Contattare', bg: 'bg-accent-amber/5', border: 'border-accent-amber/20', badge: 'bg-accent-amber text-dark-bg', dot: 'bg-accent-amber' },
  upcoming: { label: 'Prossimamente', bg: 'bg-dark-surface', border: 'border-dark-border', badge: 'bg-txt-muted text-dark-bg', dot: 'bg-txt-muted' },
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
          <svg className="animate-spin-slow w-12 h-12 text-accent-teal mx-auto mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          <p className="text-txt-muted">Caricamento dati...</p>
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="space-y-6">
        <div className="bg-accent-amber/10 border border-accent-amber/30 rounded-xl p-6">
          <div className="flex items-center gap-3 mb-2">
            <svg className="animate-spin w-5 h-5 text-accent-amber" fill="none" viewBox="0 0 24 24">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
            </svg>
            <h3 className="font-semibold text-accent-amber text-lg">Server in avvio...</h3>
          </div>
          <p className="text-txt-secondary mb-3">Il server si sta risvegliando (fino a 60 secondi).</p>
          <button onClick={() => { setError(null); fetchData(); fetchTodos(); }}
            className="sc-btn-primary">
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

  const renderTodoHeader = () => (
    <div className="flex items-center px-4 py-2 text-xs text-txt-muted uppercase tracking-wider font-semibold border-b border-dark-border mb-1">
      <div className="flex items-center gap-3 min-w-0 flex-1">
        <span className="w-[80px] shrink-0">Azione</span>
        <span>Cliente</span>
      </div>
      <div className="flex items-center shrink-0">
        <span className="w-[50px] text-center">GG</span>
        <span className="w-[55px] text-center">Fatt.</span>
        <span className="w-[85px] text-right">Importo</span>
        <span className="w-[28px]"></span>
      </div>
    </div>
  )

  const renderTodoItem = (todo) => (
    <div
      key={todo.id}
      onClick={() => navigate(`/customers/${todo.customer_id}`)}
      className="flex items-center bg-dark-card rounded-lg px-4 py-3 cursor-pointer hover:bg-dark-cardHover transition-all border border-dark-border group"
    >
      <div className="flex items-center gap-3 min-w-0 flex-1">
        <span className={`${ACTION_BADGE_COLORS[todo.action_type] || ACTION_BADGE_COLORS.idle} sc-badge shrink-0 w-[80px] text-center`}>
          {ACTION_LABELS[todo.action_type] || todo.action_type}
        </span>
        <div className="min-w-0">
          <span className="text-sm font-medium text-txt-primary truncate block max-w-[220px]">
            {todo.customer_name}
          </span>
          {todo.partita_iva && (
            <span className="text-xs text-txt-muted font-mono">{todo.partita_iva}</span>
          )}
        </div>
      </div>
      <div className="flex items-center shrink-0 text-xs">
        <span className={`w-[50px] text-center font-medium ${
          todo.max_days_overdue > 60 ? 'text-accent-red' :
          todo.max_days_overdue > 30 ? 'text-accent-amber' :
          'text-txt-muted'
        }`}>
          {todo.max_days_overdue > 0 ? `${todo.max_days_overdue}` : '-'}
        </span>
        <span className="w-[55px] text-center text-txt-secondary">
          {todo.overdue_count > 0 ? todo.overdue_count : '-'}
        </span>
        <span className="w-[85px] text-right font-semibold text-accent-red">
          {todo.total_overdue > 0 ? formatCurrency(todo.total_overdue) : '-'}
        </span>
        <span className="w-[28px] flex justify-center">
          {todo.phone && (
            <a
              href={`https://wa.me/${todo.phone.replace(/[^+\d]/g, '')}`}
              target="_blank" rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="w-6 h-6 bg-accent-green text-dark-bg rounded-full text-xs flex items-center justify-center hover:brightness-110 opacity-0 group-hover:opacity-100 transition-opacity font-bold"
              title="WhatsApp"
            >W</a>
          )}
        </span>
      </div>
    </div>
  )

  const renderTodoSection = (priority, showHeader = false) => {
    const items = todosByPriority[priority]
    if (!items || items.length === 0) return null
    const config = PRIORITY_CONFIG[priority]
    const sectionTotal = items.reduce((sum, t) => sum + (t.total_overdue || 0), 0)
    return (
      <div key={priority} className={`${config.bg} rounded-xl p-4 border ${config.border}`}>
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <div className={`w-2.5 h-2.5 rounded-full ${config.dot}`} />
            <span className={`${config.badge} px-2.5 py-0.5 rounded text-xs font-bold`}>
              {config.label}
            </span>
            <span className="text-sm text-txt-muted">({items.length})</span>
          </div>
          {sectionTotal > 0 && (
            <span className="text-sm font-semibold text-txt-secondary">{formatCurrency(sectionTotal)}</span>
          )}
        </div>
        {showHeader && renderTodoHeader()}
        <div className="space-y-2">
          {items.map(renderTodoItem)}
        </div>
      </div>
    )
  }

  const totalTodoOverdue = todos.reduce((sum, t) => sum + (t.total_overdue || 0), 0)

  return (
    <div className="space-y-6">
      {/* Search Bar */}
      <div className="sc-card p-4">
        <div className="flex items-center gap-4">
          {/* Search */}
          <div className="relative flex-1 max-w-md">
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => onSearchInput(e.target.value)}
              placeholder="Cerca per ragione sociale o P.IVA..."
              className="sc-input w-full pl-10"
            />
            <svg className="absolute left-3 top-2.5 w-4 h-4 text-txt-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
            {searchLoading && (
              <div className="absolute right-3 top-2.5">
                <svg className="animate-spin w-4 h-4 text-accent-teal" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
                </svg>
              </div>
            )}
            {/* Search dropdown */}
            {searchResults !== null && (
              <div className="absolute top-full left-0 right-0 mt-1 bg-dark-card rounded-lg shadow-lg border border-dark-border z-50 max-h-80 overflow-y-auto">
                {searchResults.length === 0 ? (
                  <p className="p-3 text-sm text-txt-muted">Nessun risultato</p>
                ) : (
                  searchResults.map(r => (
                    <button
                      key={r.id}
                      onClick={() => { navigate(`/customers/${r.id}`); setSearchResults(null); setSearchQuery(''); }}
                      className="w-full flex items-center justify-between px-4 py-3 hover:bg-dark-cardHover transition-colors text-left border-b border-dark-border last:border-0"
                    >
                      <div>
                        <p className="text-sm font-medium text-txt-primary">{r.ragione_sociale}</p>
                        {r.partita_iva && <p className="text-xs text-txt-muted font-mono">{r.partita_iva}</p>}
                      </div>
                      <div className="text-right">
                        {r.total_overdue > 0 && (
                          <p className="text-sm font-bold text-accent-red">{formatCurrency(r.total_overdue)}</p>
                        )}
                        {r.overdue_count > 0 && (
                          <p className="text-xs text-txt-muted">{r.overdue_count} scadute</p>
                        )}
                      </div>
                    </button>
                  ))
                )}
              </div>
            )}
          </div>

          <div className="flex items-center gap-4 ml-auto">
            <p className="text-sm text-txt-muted hidden md:block">
              {lastSync ? `Agg: ${lastSync.toLocaleString('it-IT')}` : ''}
            </p>
            {syncMessage && (
              <p className={`text-sm font-medium ${syncMessage.includes('Errore') ? 'text-accent-red' : 'text-accent-green'}`}>
                {syncMessage}
              </p>
            )}
            <button onClick={handleSync} disabled={syncing}
              className={`sc-btn-primary flex items-center gap-2 ${
                syncing ? 'opacity-50 cursor-not-allowed' : ''
              }`}>
              {syncing ? 'Sync...' : 'Sincronizza'}
            </button>
          </div>
        </div>
      </div>

      {/* Top Stats */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatsWidget label="Totale Scaduto" value={formatCurrency(data.total_scaduto || 0)} color="red" />
        <StatsWidget label="Fatture Scadute" value={data.total_fatture_scadute || 0} subtitle="numero fatture" color="orange" />
        <StatsWidget label="Clienti con Scaduto" value={data.total_clienti_scaduti || 0} subtitle="numero aziende" color="purple" />
        <StatsWidget label="Da Gestire" value={todos.length || 0} color="blue" />
      </div>

      {/* Clienti Da Fare - with filters and sorting */}
      <div className="sc-card">
        {/* Header with title and total */}
        <div className="sc-card-header">
          <div className="flex items-center gap-3">
            <h2 className="text-base font-bold text-txt-primary">Da Fare</h2>
            {todos.length > 0 && (
              <span className="bg-accent-red/20 text-accent-red px-2.5 py-0.5 rounded-full text-sm font-bold">
                {todos.length}
              </span>
            )}
          </div>
          {totalTodoOverdue > 0 && (
            <div className="text-right">
              <p className="text-xs text-txt-muted">Totale in gestione</p>
              <p className="text-lg font-bold text-accent-red">{formatCurrency(totalTodoOverdue)}</p>
            </div>
          )}
        </div>

        {/* Filter chips + Sort selector */}
        {todos.length > 0 && (
          <div className="px-5 py-3 border-b border-dark-border bg-dark-surface/50 flex flex-wrap items-center justify-between gap-3">
            {/* Priority filter chips */}
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xs text-txt-muted font-medium mr-1">Filtra:</span>
              <button
                onClick={() => setFilterPriority('all')}
                className={`px-3 py-1 rounded-full text-xs font-medium transition-colors ${
                  filterPriority === 'all'
                    ? 'bg-accent-teal text-dark-bg'
                    : 'bg-dark-card text-txt-secondary border border-dark-border hover:bg-dark-cardHover'
                }`}
              >
                Tutti ({todos.length})
              </button>
              {todoCounts.today > 0 && (
                <button
                  onClick={() => setFilterPriority('today')}
                  className={`px-3 py-1 rounded-full text-xs font-medium transition-colors ${
                    filterPriority === 'today'
                      ? 'bg-accent-blue text-dark-bg'
                      : 'bg-dark-card text-accent-blue border border-accent-blue/30 hover:bg-accent-blue/10'
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
                      ? 'bg-accent-amber text-dark-bg'
                      : 'bg-dark-card text-accent-amber border border-accent-amber/30 hover:bg-accent-amber/10'
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
                      ? 'bg-txt-muted text-dark-bg'
                      : 'bg-dark-card text-txt-secondary border border-dark-border hover:bg-dark-cardHover'
                  }`}
                >
                  Prossimi ({todoCounts.upcoming})
                </button>
              )}
            </div>

            {/* Sort selector */}
            <div className="flex items-center gap-2">
              <span className="text-xs text-txt-muted font-medium">Ordina:</span>
              <select
                value={sortBy}
                onChange={(e) => setSortBy(e.target.value)}
                className="sc-input text-xs px-2 py-1.5"
              >
                {SORT_OPTIONS.map(opt => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
            </div>
          </div>
        )}

        {/* Todo list content */}
        <div className="sc-card-body">
          {todoLoading ? (
            <p className="text-txt-muted text-center py-4">Caricamento...</p>
          ) : todos.length === 0 ? (
            <div className="text-center py-8">
              <p className="text-txt-muted mb-2">Nessuna azione da fare</p>
              <p className="text-sm text-txt-muted">Sincronizza le fatture e vai nella scheda Clienti per iniziare.</p>
            </div>
          ) : sortBy === 'priority' && filterPriority === 'all' ? (
            /* Grouped by priority */
            <div className="space-y-4 max-h-[700px] overflow-y-auto">
              {['overdue', 'today', 'new', 'upcoming'].map((p, idx) => renderTodoSection(p, idx === 0))}
            </div>
          ) : (
            /* Flat sorted list */
            <div className="space-y-2 max-h-[700px] overflow-y-auto">
              {sortedTodos.length === 0 ? (
                <p className="text-txt-muted text-center py-4">Nessun risultato per questo filtro</p>
              ) : (
                <>
                  {renderTodoHeader()}
                  {sortedTodos.map((todo, idx) => (
                    <div key={todo.id} className="flex items-center gap-2">
                      <span className="text-xs text-txt-muted w-6 text-right shrink-0">{idx + 1}.</span>
                      <div className="flex-1">{renderTodoItem(todo)}</div>
                    </div>
                  ))}
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
