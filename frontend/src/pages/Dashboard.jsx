import React, { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { PieChart, Pie, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer, Cell } from 'recharts'
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

  const fetchData = async (retry = 0) => {
    try {
      setLoading(true)
      const response = await client.get('/dashboard')
      setData(response.data)
      setRetryCount(0)
      const savedLastSync = localStorage.getItem('lastSyncTime')
      if (savedLastSync) {
        setLastSync(new Date(savedLastSync))
      }
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
      const now = new Date()
      setLastSync(now)
      localStorage.setItem('lastSyncTime', now.toISOString())
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
    return new Date(dateStr).toLocaleDateString('it-IT', { weekday: 'short', day: 'numeric', month: 'short' })
  }

  function getStatusLabel(status) {
    const labels = {
      open: 'Aperto', contacted: 'Contattato', promised: 'Promesso',
      paid: 'Pagato', disputed: 'Contestato', escalated: 'Escalato',
    }
    return labels[status] || status
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
          <p className="text-amber-700 mb-3">Il server si sta risvegliando (può richiedere fino a 60 secondi). Ricaricamento automatico in corso.</p>
          <button
            onClick={() => { setError(null); fetchData(); fetchTodos(); }}
            className="px-4 py-2 bg-amber-600 text-white rounded-lg text-sm font-medium hover:bg-amber-700"
          >
            Riprova Ora
          </button>
        </div>
      </div>
    )
  }

  if (!data) return null

  const statusData = Object.entries(data.positions_by_status || {}).map(([status, info]) => ({
    name: getStatusLabel(status), value: info.count, amount: info.amount,
  }))
  const escalationData = Object.entries(data.positions_by_escalation_level || {}).map(([level, info]) => ({
    name: `Livello ${level}`, count: info.count, amount: info.amount,
  }))

  const COLORS = ['#3b82f6', '#f59e0b', '#a855f7', '#10b981', '#ef4444', '#f97316']

  // Group todos by priority
  const todosByPriority = {}
  todos.forEach(todo => {
    if (!todosByPriority[todo.priority]) todosByPriority[todo.priority] = []
    todosByPriority[todo.priority].push(todo)
  })

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
          {items.map((todo) => (
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
                    target="_blank"
                    rel="noopener noreferrer"
                    onClick={(e) => e.stopPropagation()}
                    className="w-5 h-5 bg-green-500 text-white rounded-full text-xs flex items-center justify-center hover:bg-green-600"
                    title="WhatsApp"
                  >
                    W
                  </a>
                )}
                <span>{formatDate(todo.scheduled_date)}</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    )
  }

  const totalTodoOverdue = todos.reduce((sum, t) => sum + (t.total_overdue || 0), 0)

  return (
    <div className="space-y-8">
      {/* Sync Bar */}
      <div className="bg-white rounded-lg p-6 border border-slate-200 flex items-center justify-between">
        <div>
          <h3 className="font-medium text-slate-900 mb-2">Stato Sincronizzazione</h3>
          <p className="text-sm text-slate-600">
            {lastSync ? `Ultimo aggiornamento: ${lastSync.toLocaleString('it-IT')}` : 'Nessuna sincronizzazione eseguita'}
          </p>
        </div>
        <div className="flex items-center gap-4">
          {syncMessage && (
            <p className={`text-sm font-medium ${syncMessage.includes('Errore') ? 'text-red-600' : 'text-green-600'}`}>
              {syncMessage}
            </p>
          )}
          <button onClick={handleSync} disabled={syncing}
            className={`px-4 py-2 rounded-lg font-medium text-sm transition-colors flex items-center gap-2 ${
              syncing ? 'bg-slate-300 text-slate-500 cursor-not-allowed' : 'bg-blue-600 text-white hover:bg-blue-700'
            }`}>
            {syncing ? 'Sincronizzazione...' : 'Sincronizza Ora'}
          </button>
        </div>
      </div>

      {/* Top Stats */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
        <StatsWidget label="Totale Crediti" value={formatCurrency(data.total_crediti)} color="blue" />
        <StatsWidget label="Posizioni Aperte" value={data.total_positions} color="purple" />
        <StatsWidget label="Da Gestire" value={todos.length || 0} color="orange" />
        <StatsWidget label="Clienti Totali" value={data.total_customers || 0} color="green" />
      </div>

      {/* TODO Section — primary focus */}
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
              <p className="text-sm text-slate-500">Totale scaduto in gestione</p>
              <p className="text-lg font-bold text-red-600">{formatCurrency(totalTodoOverdue)}</p>
            </div>
          )}
        </div>

        {todoLoading ? (
          <p className="text-slate-500 text-center py-4">Caricamento...</p>
        ) : todos.length === 0 ? (
          <div className="text-center py-8">
            <p className="text-slate-500 mb-2">Nessuna azione da fare</p>
            <p className="text-sm text-slate-400">Sincronizza le fatture e vai nella scheda Clienti per iniziare il flusso di recupero.</p>
          </div>
        ) : (
          <div className="space-y-4">
            {['overdue', 'today', 'new', 'upcoming'].map(p => renderTodoSection(p))}
          </div>
        )}
      </div>

      {/* Charts */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-white rounded-lg p-6 border border-slate-200">
          <h2 className="text-lg font-bold text-slate-900 mb-4">Distribuzione per Stato</h2>
          {statusData.length > 0 ? (
            <ResponsiveContainer width="100%" height={300}>
              <PieChart>
                <Pie data={statusData} cx="50%" cy="50%" labelLine={false}
                  label={({ name, value }) => `${name}: ${value}`}
                  outerRadius={80} fill="#8884d8" dataKey="value">
                  {statusData.map((entry, index) => (
                    <Cell key={`cell-${index}`} fill={COLORS[index % COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip formatter={(value) => value} />
              </PieChart>
            </ResponsiveContainer>
          ) : (
            <p className="text-slate-500 text-center py-8">Nessun dato disponibile</p>
          )}
        </div>

        <div className="bg-white rounded-lg p-6 border border-slate-200">
          <h2 className="text-lg font-bold text-slate-900 mb-4">Distribuzione per Livello di Escalation</h2>
          {escalationData.length > 0 ? (
            <ResponsiveContainer width="100%" height={300}>
              <BarChart data={escalationData}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="name" />
                <YAxis />
                <Tooltip />
                <Legend />
                <Bar dataKey="count" fill="#3b82f6" name="Conteggio" />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <p className="text-slate-500 text-center py-8">Nessun dato disponibile</p>
          )}
        </div>
      </div>

      {/* Recent Activity */}
      <div className="bg-white rounded-lg p-6 border border-slate-200">
        <h2 className="text-lg font-bold text-slate-900 mb-4">Attività Recente</h2>
        {data.recent_activity && data.recent_activity.length > 0 ? (
          <div className="space-y-4">
            {data.recent_activity.map((activity) => (
              <div key={activity.id} className="flex items-start gap-4 pb-4 border-b border-slate-100 last:border-b-0">
                <div className="mt-1">
                  <div className="w-2 h-2 bg-blue-600 rounded-full"></div>
                </div>
                <div className="flex-1">
                  <div className="flex items-center gap-2">
                    <p className="font-medium text-slate-900">{activity.action}</p>
                    <span className="text-xs bg-slate-100 text-slate-600 px-2 py-1 rounded">{activity.entity_type}</span>
                  </div>
                  <p className="text-sm text-slate-600 mt-1">{new Date(activity.timestamp).toLocaleString('it-IT')}</p>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-slate-500 text-center py-8">Nessuna attività recente</p>
        )}
      </div>
    </div>
  )
}
