import React, { useState, useEffect } from 'react'
import { PieChart, Pie, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer, Cell } from 'recharts'
import client from '../api/client'
import StatsWidget from '../components/StatsWidget'

export default function Dashboard() {
  const [loading, setLoading] = useState(true)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    const fetchData = async () => {
      try {
        setLoading(true)
        const response = await client.get('/dashboard')
        setData(response.data)
      } catch (err) {
        setError('Errore nel caricamento dei dati del dashboard')
        console.error(err)
      } finally {
        setLoading(false)
      }
    }
    fetchData()
  }, [])

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
      <div className="bg-red-50 border border-red-200 rounded-lg p-4 text-red-800">
        {error}
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
          value={data.recent_activity?.length || 0}
          color="orange"
        />
        <StatsWidget
          label="Ultimi Aggiornamenti"
          value={data.recent_activity?.length || 0}
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
