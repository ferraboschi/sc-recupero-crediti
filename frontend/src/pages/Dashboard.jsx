import React, { useState, useEffect } from 'react'
import { PieChart, Pie, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer, Cell } from 'recharts'
import client from '../api/client'
import StatsWidget from '../components/StatsWidget'

export default function Dashboard() {
  const [loading, setLoading] = useState(true)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [syncing, setSyncing] = useState(false)
  const [syncMessage, setSyncMessage] = useState('')
  const [lastSync, setLastSync] = useState(null)

  const fetchData = async () => {
    try {
      setLoading(true)
      const response = await client.get('/dashboard')
      setData(response.data)
      // Load last sync time from localStorage
      const savedLastSync = localStorage.getItem('lastSyncTime')
      if (savedLastSync) {
        setLastSync(new Date(savedLastSync))
      }
    } catch (err) {
      setError('Errore nel caricamento dei dati del dashboard')
      console.error(err)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchData()
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
      // Refresh dashboard data
      await fetchData()
      setTimeout(() => setSyncMessage(''), 3000)
    } catch (err) {
      setSyncMessage('Errore nella sincronizzazione')
      console.error(err)
    } finally {
      setSyncing(false)
    }
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
          <div className="flex items-start gap-4">
            <svg className="w-6 h-6 text-amber-600 mt-0.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z" />
            </svg>
            <div>
              <h3 className="font-semibold text-amber-800 text-lg mb-2">Backend non raggiungibile</h3>
              <p className="text-amber-700 mb-3">
                Il server API non è ancora configurato. Il frontend è attivo su GitHub Pages, ma il backend deve essere deployato separatamente su un VPS.
              </p>
              <div className="bg-amber-100 rounded-md p-4 text-sm text-amber-800">
                <p className="font-medium mb-2">Per completare il setup:</p>
                <ol className="list-decimal list-inside space-y-1">
                  <li>Deploy del backend su un VPS con Docker</li>
                  <li>Configurare il file .env con le API key</li>
                  <li>Impostare VITE_API_BASE_URL e ri-deployare il frontend</li>
                </ol>
              </div>
            </div>
          </div>
        </div>

        {/* Preview delle funzionalità */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
          <div className="bg-white rounded-lg p-6 border border-slate-200">
            <p className="text-sm text-slate-500 mb-1">Totale Crediti</p>
            <p className="text-2xl font-bold text-slate-300">--</p>
          </div>
          <div className="bg-white rounded-lg p-6 border border-slate-200">
            <p className="text-sm text-slate-500 mb-1">Posizioni Aperte</p>
            <p className="text-2xl font-bold text-slate-300">--</p>
          </div>
          <div className="bg-white rounded-lg p-6 border border-slate-200">
            <p className="text-sm text-slate-500 mb-1">Messaggi in Coda</p>
            <p className="text-2xl font-bold text-slate-300">--</p>
          </div>
          <div className="bg-white rounded-lg p-6 border border-slate-200">
            <p className="text-sm text-slate-500 mb-1">Clienti Attivi</p>
            <p className="text-2xl font-bold text-slate-300">--</p>
          </div>
        </div>
      </div>
    )
  }

  if (!data) return null

  // Prepare status breakdown data for pie chart
  const statusData = Object.entries(data.positions_by_status || {}).map(([status, info]) => ({
    name: getStatusLabel(status),
    value: info.count,
    amount: info.amount,
  }))

  // Prepare escalation data for bar chart
  const escalationData = Object.entries(data.positions_by_escalation_level || {}).map(([level, info]) => ({
    name: `Livello ${level}`,
    count: info.count,
    amount: info.amount,
  }))

  // Format currency
  const formatCurrency = (value) => {
    return new Intl.NumberFormat('it-IT', {
      style: 'currency',
      currency: 'EUR',
      minimumFractionDigits: 0,
      maximumFractionDigits: 0,
    }).format(value)
  }

  // Get status label
  function getStatusLabel(status) {
    const labels = {
      open: 'Aperto',
      contacted: 'Contattato',
      promised: 'Promesso',
      paid: 'Pagato',
      disputed: 'Contestato',
      escalated: 'Escalato',
    }
    return labels[status] || status
  }

  const COLORS = ['#3b82f6', '#f59e0b', '#a855f7', '#10b981', '#ef4444', '#f97316']

  return (
    <div className="space-y-8">
      {/* Last Sync and Sync Button */}
      <div className="bg-white rounded-lg p-6 border border-slate-200 flex items-center justify-between">
        <div>
          <h3 className="font-medium text-slate-900 mb-2">Stato Sincronizzazione</h3>
          <p className="text-sm text-slate-600">
            {lastSync
              ? `Ultimo aggiornamento: ${lastSync.toLocaleString('it-IT')}`
              : 'Nessuna sincronizzazione eseguita'
            }
          </p>
        </div>
        <div className="flex items-center gap-4">
          {syncMessage && (
            <p className={`text-sm font-medium ${syncMessage.includes('Errore') ? 'text-red-600' : 'text-green-600'}`}>
              {syncMessage}
            </p>
          )}
          <button
            onClick={handleSync}
            disabled={syncing}
            className={`px-4 py-2 rounded-lg font-medium text-sm transition-colors flex items-center gap-2 ${
              syncing
                ? 'bg-slate-300 text-slate-500 cursor-not-allowed'
                : 'bg-blue-600 text-white hover:bg-blue-700'
            }`}
          >
            {syncing ? (
              <>
                <svg className="animate-spin w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
                Sincronizzazione...
              </>
            ) : (
              <>
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
                Sincronizza Ora
              </>
            )}
          </button>
        </div>
      </div>

      {/* Top Stats */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
        <StatsWidget
          label="Totale Crediti"
          value={formatCurrency(data.total_crediti)}
          color="blue"
        />
        <StatsWidget
          label="Posizioni Aperte"
          value={data.total_positions}
          color="purple"
        />
        <StatsWidget
          label="Messaggi in Coda"
          value={data.draft_messages || 0}
          color="orange"
        />
        <StatsWidget
          label="Clienti Totali"
          value={data.total_customers || 0}
          color="green"
        />
      </div>

      {/* Charts */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Status Breakdown */}
        <div className="bg-white rounded-lg p-6 border border-slate-200">
          <h2 className="text-lg font-bold text-slate-900 mb-4">Distribuzione per Stato</h2>
          {statusData.length > 0 ? (
            <ResponsiveContainer width="100%" height={300}>
              <PieChart>
                <Pie
                  data={statusData}
                  cx="50%"
                  cy="50%"
                  labelLine={false}
                  label={({ name, value }) => `${name}: ${value}`}
                  outerRadius={80}
                  fill="#8884d8"
                  dataKey="value"
                >
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

        {/* Escalation Levels */}
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
                    <span className="text-xs bg-slate-100 text-slate-600 px-2 py-1 rounded">
                      {activity.entity_type}
                    </span>
                  </div>
                  <p className="text-sm text-slate-600 mt-1">
                    {new Date(activity.timestamp).toLocaleString('it-IT')}
                  </p>
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
